"""
DB読み書きヘルパー。

research_shelve のデータ取得・更新をWebアプリ用にラップする。
排他制御は research_shelve._flock() を共用し、Web側とバッチ側で
同じロックファイルを取ることでプロセス間の安全な共存を保証する。
"""

import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from db_shelve import STOCKS_SHELVE, ShelveDB
from html_sanitizer import sanitize_html
from ks_util import get_price_day, log_warning
from research_shelve import (
    get_research_record,
    upsert_research_record,
    create_research_record,
    create_snapshot,
    list_research_records,
    sort_shikiho_comments_desc,
    validate_code_s,
    normalize_code_s,
    validate_rating,
    _flock,
    normalize_kessan_post_price_changes,
    VALID_RATINGS,
    VALID_EXPECTATIONS,
    MAX_KESSAN_COMMENTS,
    KESSAN_REACTION_PERIODS,
)


def get_research_detail(code_s: str) -> Optional[Dict[str, Any]]:
    """1銘柄の調査レコードを取得する。

    表示用に shikiho_comments を period 降順（新しい順）に並べ替える。
    period 空 / "-" は最古扱いで末尾に寄せ、同値同士は元リスト順を保つ。
    過去の決算コメントで post_price_changes に欠損期間があれば
    price_log から補完計算して in-memory で埋める（永続化はしない）。
    """
    validate_code_s(code_s)
    record = get_research_record(code_s)
    if record is not None:
        record["shikiho_comments"] = sort_shikiho_comments_desc(
            record.get("shikiho_comments") or []
        )
        _backfill_post_price_changes_for_entries(
            code_s,
            record.get("kessan_comments") or [],
        )
    return record


def _backfill_post_price_changes_for_entries(
    code_s: str,
    entries: List[Dict[str, Any]],
) -> None:
    """過去エントリの post_price_changes に欠損期間があれば price_log から補完する。

    永続化はせず、entry dict を in-place で更新する。
    決算日が未来 (今日以降) のエントリは補完対象外。
    """
    if not entries:
        return
    base_day = get_price_day(datetime.today())

    targets: List[Tuple[Dict[str, Any], date]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        dt = _parse_kessanbi(entry.get("kessanbi", ""))
        if dt is None or dt >= base_day:
            continue
        changes = entry.get("post_price_changes") or {}
        if any(not changes.get(key) for key, _ in KESSAN_REACTION_PERIODS):
            targets.append((entry, dt))
    if not targets:
        return

    log = _bulk_price_logs([code_s]).get(normalize_code_s(code_s), [])
    if not log:
        return
    for entry, dt in targets:
        existing = entry.get("post_price_changes") or {}
        calculated = _price_reactions_from_log(log, dt)
        for key, _ in KESSAN_REACTION_PERIODS:
            if not existing.get(key) and calculated.get(key):
                existing[key] = calculated[key]
        entry["post_price_changes"] = existing


def get_stock_data(code_s: str) -> Dict[str, Any]:
    """stocks_shelve から1銘柄のデータを取得する。

    存在しない場合は空 dict を返す（テンプレート側で安全に参照可能）。
    今後 detail view に stocks_shelve のフィールドを追加する際は、
    この関数経由で取得しテンプレートに渡す。
    """
    normalized = normalize_code_s(code_s)
    with ShelveDB(STOCKS_SHELVE) as db:
        return db.get(normalized) or {}


def add_stock(code_s: str) -> str:
    """銘柄を research_shelve に追加する。

    stocks_shelve からデータを取得し、スナップショット付きでレコードを作成する。

    Args:
        code_s: 銘柄コード
    Returns:
        正規化された code_s
    Raises:
        ValueError: 入力不正、stocks_shelve に未登録、既に登録済み
    """
    from datetime import datetime
    code_s = normalize_code_s(code_s)
    validate_code_s(code_s)

    # stocks_shelve から取得（ロック外で読み取り専用）
    stock = get_stock_data(code_s)
    if not stock:
        raise ValueError(f"{code_s} は株式DBに未登録です（先に make_stock_db.py で登録してください）")

    stock_name = stock.get("stock_name", "")

    # スナップショット生成（失敗してもレコード作成は続行）
    snapshots = []
    try:
        import gyoseki
        import shihyou
        import rironkabuka

        progress_expr, growth_expr = gyoseki.get_gyoseki_expr(stock)
        ir_quant = growth_expr + progress_expr

        today = get_price_day(datetime.today())
        date_yy_m = f"{today.year % 100}.{today.month}.{today.day}"

        snapshot = create_snapshot(
            date_yy_m,
            ir_quant=ir_quant,
            quality_indicators=shihyou.get_shihyo_expr(stock),
            rironkabuka_kairi=rironkabuka.get_rironkabuka_expr(stock),
            data_source="auto",
        )
        snapshots = [snapshot]
    except Exception as e:
        log_warning(f"[research] 銘柄追加時スナップショット生成失敗: {code_s} {e}")

    # 既存チェック + 書き込みをアトミックに実行
    with _flock():
        if get_research_record(code_s) is not None:
            raise ValueError(f"{code_s} は既に登録されています")
        record = create_research_record(code_s, stock_name, snapshots=snapshots)
        upsert_research_record(record)
    return code_s


def get_disclosures(code_s: str) -> List[tuple]:
    """銘柄の直近適時開示リストを返す。CSVがなければ空リスト。"""
    try:
        import disclosure
        return disclosure.load_disclosure_for_code(code_s)
    except Exception:
        return []


def has_recent_disclosure(disclosures: List[tuple], days: int = 7) -> bool:
    """直近 N 日以内の開示が1件以上あるか判定する。

    disclosures 各要素の先頭は "MM/DD" 形式の日付文字列。
    年情報は持たないため、MM/DD を「今日と同年」にしたとき
    今日より未来の日付になる場合は前年扱いとする（年跨ぎ対応）。
    """
    if not disclosures:
        return False
    today = get_price_day(datetime.today())
    cutoff = today - timedelta(days=days)
    for row in disclosures:
        date_expr = row[0] if row else ""
        m = _MM_DD_PATTERN.match(date_expr.strip())
        if not m:
            continue
        mm = int(m.group(1))
        dd = int(m.group(2))
        try:
            d = date(today.year, mm, dd)
        except ValueError:
            continue
        if d > today:
            try:
                d = date(today.year - 1, mm, dd)
            except ValueError:
                continue
        if cutoff <= d <= today:
            return True
    return False


def search_records(
    *,
    rating: Optional[str] = None,
    keyword: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """銘柄調査レコードをフィルタ検索する。"""
    return list_research_records(rating=rating, keyword=keyword)


_MM_DD_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})$")

# マークダウン風記法 → HTML 変換パターン
# **太字** → <b>太字</b>（先に処理、* と区別するため）
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
# *赤字* → <span style="color:#ff0000">赤字</span>（** 処理後に実行）
_RE_RED = re.compile(r"\*(.+?)\*")
# [テキスト](URL) → <a href="URL" target="_blank">テキスト</a>
_RE_NAMED_LINK = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
# URL自動リンク化（既に <a> タグ内でないURLを対象）
_RE_URL = re.compile(r'(?<!["\'>])(https?://[^\s<>\'"]+)')


def _markdown_to_html(text: str) -> str:
    """マークダウン風記法を HTML に変換する。

    - **太字** → <b>太字</b>
    - *赤字* → <span style="color:#ff0000">赤字</span>
    - [テキスト](URL) → <a href="URL" target="_blank">テキスト</a>
    - URL → <a href="URL" target="_blank">URL</a>
    """
    if not text:
        return text
    text = _RE_BOLD.sub(r"<b>\1</b>", text)
    text = _RE_RED.sub(r'<span style="color:#ff0000">\1</span>', text)
    text = _RE_NAMED_LINK.sub(r'<a href="\2" target="_blank">\1</a>', text)
    text = _RE_URL.sub(r'<a href="\1" target="_blank">\1</a>', text)
    return text


def _normalize_analysis_date(raw: str) -> str:
    """分析日の入力を YY/MM/DD 形式に正規化する。

    - "4/14"  → "26/4/14"  (現在の年の下2桁を補完)
    - "26/4/14" → そのまま (既に年付き)
    - "" → "" (空はそのまま)
    """
    raw = raw.strip()
    if not raw:
        return raw
    m = _MM_DD_PATTERN.match(raw)
    if m:
        yy = date.today().year % 100
        return f"{yy}/{raw}"
    return raw


def save_memo(code_s: str, form_data: dict) -> None:
    """手動メモフィールドを更新する。

    対象: overall_rating, institutional_comment, memo, openwork, cramer
    read-modify-write サイクル全体を _flock で排他する。
    upsert_research_record 内部でも _flock を取るが、fcntl.flock は
    同一プロセス・同一スレッドからの再取得をブロックしないため問題ない。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise ValueError(f"レコード未登録: {normalized}")

        new_rating = form_data.get("overall_rating", "")
        validate_rating(new_rating)
        record["overall_rating"] = new_rating
        record["institutional_comment"] = form_data.get(
            "institutional_comment", ""
        )
        record["memo"] = sanitize_html(_markdown_to_html(form_data.get("memo", "")))
        record["openwork"] = sanitize_html(_markdown_to_html(form_data.get("openwork", "")))
        record["cramer"] = form_data.get("cramer", "")

        if "analysis_date_raw" in form_data:
            record["analysis_date_raw"] = _normalize_analysis_date(
                form_data["analysis_date_raw"]
            )

        upsert_research_record(record)


def save_shikiho(code_s: str, form_data: dict) -> None:
    """四季報フィールドを更新する。

    対象: overview, shikiho_comments (最大8件、List[dict])
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise ValueError(f"レコード未登録: {normalized}")

        record["overview"] = form_data.get("overview", "")

        comments: List[Dict[str, str]] = []
        for i in range(10):
            comment = form_data.get(f"shikiho_comments_{i}", "").strip()
            period = form_data.get(f"shikiho_periods_{i}", "").strip()
            if comment:
                comments.append({"period": period, "comment": comment})
        # 8件超は古い順（先頭）を切り詰め、新しいもの（末尾）を残す
        if len(comments) > 8:
            comments = comments[-8:]
        record["shikiho_comments"] = comments

        upsert_research_record(record)


def save_ir_comments(code_s: str, form_data: dict) -> None:
    """スナップショット内の ir_comment を一括更新する。

    フォームキー形式: ir_comment_<date_yy_m> (例: ir_comment_26.4)
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise ValueError(f"レコード未登録: {normalized}")

        snapshots = record.get("snapshots") or []
        for snap in snapshots:
            date = snap.get("date_yy_m", "")
            form_key = f"ir_comment_{date}"
            if form_key in form_data:
                snap["ir_comment"] = sanitize_html(_markdown_to_html(form_data[form_key]))

        record["snapshots"] = snapshots
        upsert_research_record(record)


# =======================================================
# 市場データ / 決算カレンダー用ヘルパー (issue #127)
# =======================================================

_KESSANBI_PATTERN = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")


def _parse_kessanbi(kessanbi: str) -> Optional[date]:
    """YYYY/MM/DD 文字列を date に変換する。形式不正時は None。"""
    if not isinstance(kessanbi, str) or not _KESSANBI_PATTERN.match(kessanbi):
        return None
    try:
        return datetime.strptime(kessanbi, "%Y/%m/%d").date()
    except ValueError:
        return None


def get_market_html_parts() -> Dict[str, str]:
    """market_data.html を読み込み、決算セクション以外のパーツを dict で返す。

    返り値のキー:
      - "available": bool 相当の "1" or "" （ファイル存在判定）
      - "css": <style> タグの中身
      - "header": <h1> の HTML
      - "body_without_kessan": <body> 内のコンテンツのうち、
         <h2>決算日</h2> 以下のブロック（次の <h2> 直前まで、または
         <details><summary>▶ 済の決算を表示...</summary> 含む）を除去したもの。
         <h1> は除外し、決算セクションのあった位置にプレースホルダ
         "<!--KESSAN_PLACEHOLDER-->" を挿入。
      - "footer": <footer> タグ

    ファイル未存在時は {"available": ""} を返す。
    BeautifulSoup が未インストールの場合も {"available": ""} を返す。
    """
    try:
        from ks_util import DATA_DIR
        data_dir = DATA_DIR
    except Exception:
        data_dir = os.environ.get("KS_DATA_DIR", "")
    html_path = os.path.join(data_dir, "code_rank_data", "market_data.html")
    if not os.path.exists(html_path):
        return {"available": ""}
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log_warning("[market] BeautifulSoup 未インストールのため market_data.html を読み込めません")
        return {"available": ""}

    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except OSError as e:
        log_warning(f"[market] market_data.html 読み込み失敗: {e}")
        return {"available": ""}

    soup = BeautifulSoup(html_content, "html.parser")

    # CSS
    style_tag = soup.find("style")
    css = style_tag.string if style_tag and style_tag.string else ""

    # h1
    h1_tag = soup.find("h1")
    header_html = str(h1_tag) if h1_tag else ""

    # 決算セクションを除去してプレースホルダ挿入
    # 構造: <h2>決算日</h2> の直後に <div class="kessan-grid"> と <details>
    # （過去決算折りたたみ）が続く。次の <h2> までを対象にする。
    body = soup.find("body")
    if body is None:
        return {"available": ""}

    kessan_h2 = None
    for h2 in body.find_all("h2"):
        if h2.get_text(strip=True) == "決算日":
            kessan_h2 = h2
            break

    if kessan_h2 is not None:
        to_remove = []
        # h2決算日 から 次の h2 or footer or 末尾までを削除対象に
        sibling = kessan_h2
        while sibling is not None:
            next_sib = sibling.next_sibling
            # 次要素が <h2>（決算以外）または <footer> なら停止
            if sibling is not kessan_h2 and hasattr(sibling, "name"):
                if sibling.name == "h2" or sibling.name == "footer":
                    break
            to_remove.append(sibling)
            sibling = next_sib
        # プレースホルダを h2決算日 の位置に挿入
        placeholder = soup.new_tag("div", id="kessan-placeholder-mark")
        placeholder.string = "__KESSAN_PLACEHOLDER__"
        kessan_h2.insert_before(placeholder)
        for el in to_remove:
            el.extract()

    # h1 も body から除く（テンプレート側で header として別途描画）
    if h1_tag is not None:
        h1_tag.extract()

    # footer 抽出
    footer_tag = body.find("footer")
    footer_html = str(footer_tag) if footer_tag else ""
    if footer_tag is not None:
        footer_tag.extract()

    body_html = body.decode_contents()

    return {
        "available": "1",
        "css": css or "",
        "header": header_html,
        "body_without_kessan": body_html,
        "footer": footer_html,
    }


def get_disclosure_html_parts() -> Dict[str, str]:
    """disclosure_data.html を読み込み、CSS / header / body / footer を dict で返す。

    返り値のキー:
      - "available": "1" or "" （ファイル存在判定）
      - "css": <style> タグの中身
      - "header": <h1> の HTML
      - "body": <body> 内のコンテンツ（h1, footer を除去したもの）
      - "footer": <footer> タグ

    ファイル未存在時または BeautifulSoup 未インストール時は {"available": ""}。
    """
    try:
        from ks_util import DATA_DIR
        data_dir = DATA_DIR
    except Exception:
        data_dir = os.environ.get("KS_DATA_DIR", "")
    html_path = os.path.join(data_dir, "code_rank_data", "disclosure_data.html")
    if not os.path.exists(html_path):
        return {"available": ""}
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log_warning("[disclosure] BeautifulSoup 未インストールのため disclosure_data.html を読み込めません")
        return {"available": ""}

    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except OSError as e:
        log_warning(f"[disclosure] disclosure_data.html 読み込み失敗: {e}")
        return {"available": ""}

    soup = BeautifulSoup(html_content, "html.parser")

    style_tag = soup.find("style")
    css = style_tag.string if style_tag and style_tag.string else ""

    h1_tag = soup.find("h1")
    header_html = str(h1_tag) if h1_tag else ""

    body = soup.find("body")
    if body is None:
        return {"available": ""}

    if h1_tag is not None:
        h1_tag.extract()

    footer_tag = body.find("footer")
    footer_html = str(footer_tag) if footer_tag else ""
    if footer_tag is not None:
        footer_tag.extract()

    body_html = body.decode_contents()

    return {
        "available": "1",
        "css": css or "",
        "header": header_html,
        "body": body_html,
        "footer": footer_html,
    }


def _split_log_around_kessanbi(
    price_log: List,
    kessanbi_dt: date,
) -> Tuple[Optional[int], List[Tuple[date, int]]]:
    """price_log を「決算日以下の最新営業日終値」と「決算日より後の昇順タプル列」に分ける。

    複数期間の反応率計算でソート/分割を共有するための共通前処理。
    """
    if not price_log:
        return None, []
    try:
        sorted_log = sorted(price_log, key=lambda x: x[0])  # 昇順
    except (TypeError, IndexError):
        return None, []

    before_price: Optional[int] = None
    after_entries: List[Tuple[date, int]] = []
    for entry in sorted_log:
        try:
            entry_dt, entry_pr = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        if not isinstance(entry_dt, date):
            continue
        if entry_dt <= kessanbi_dt:
            # 昇順なので最後に上書きされた値が「決算日以下の最新営業日」
            before_price = entry_pr
        else:
            after_entries.append((entry_dt, entry_pr))
    return before_price, after_entries


def _format_reaction(before_price: Optional[int], after_price: int) -> str:
    """前営業日終値 → N営業日後終値の変動率を符号付き文字列で返す。失敗時は ""。"""
    if before_price is None or before_price == 0:
        return ""
    try:
        change = (float(after_price) / float(before_price) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return ""
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}"


def _price_reaction_from_log(
    price_log: List,
    kessanbi_dt: date,
    *,
    n_business_days: int = 1,
) -> str:
    """price_log と決算日 date から N 営業日後変動率を算出する内部関数。

    price_log: [(date, int終値), ...]
    n=1 → 決算日翌営業日終値 / 決算日以下の最新営業日終値 - 1
    n=5 → 5営業日後終値 / 決算日以下の最新営業日終値 - 1
    log が不足する場合は "".

    複数期間を一括計算する場合は _price_reactions_from_log を使うとソートを共有できる。
    """
    if n_business_days < 1:
        return ""
    before_price, after_entries = _split_log_around_kessanbi(price_log, kessanbi_dt)
    if len(after_entries) < n_business_days:
        return ""
    return _format_reaction(before_price, after_entries[n_business_days - 1][1])


def _price_reactions_from_log(
    price_log: List,
    kessanbi_dt: date,
    periods: Tuple[Tuple[str, int], ...] = KESSAN_REACTION_PERIODS,
) -> Dict[str, str]:
    """price_log を1回だけソート/分割し、複数期間の反応率を一括算出する。

    各期間で取得不可なら値は ""。
    """
    before_price, after_entries = _split_log_around_kessanbi(price_log, kessanbi_dt)
    result: Dict[str, str] = {}
    for key, n in periods:
        if n < 1 or len(after_entries) < n:
            result[key] = ""
            continue
        result[key] = _format_reaction(before_price, after_entries[n - 1][1])
    return result


def calc_price_reactions(code_s: str, kessanbi: str) -> Dict[str, str]:
    """決算日前営業日終値と複数期間後の終値から株価変動率を算出する。

    Args:
        code_s: 銘柄コード
        kessanbi: YYYY/MM/DD 形式

    Returns:
        {"1d": "+3.2", "5d": "+5.1"} 形式。各期間で取得不可時は "" を入れる。
    """
    kessanbi_dt = _parse_kessanbi(kessanbi)
    if kessanbi_dt is None:
        return {key: "" for key, _ in KESSAN_REACTION_PERIODS}
    stock = get_stock_data(code_s)
    log = stock.get("price_log") or []
    return _price_reactions_from_log(log, kessanbi_dt)


def calc_price_reaction(code_s: str, kessanbi: str) -> str:
    """[後方互換] 決算日翌営業日の変動率のみを返す。

    新規呼び出しは calc_price_reactions を使うこと。
    既存テスト・移行期コードからの呼び出しのため残置している。
    """
    return calc_price_reactions(code_s, kessanbi).get("1d", "")


def _bulk_price_logs(code_list: List[str]) -> Dict[str, List]:
    """stocks_shelve を1回だけ開いて複数銘柄の price_log を dict で返す。"""
    if not code_list:
        return {}
    result: Dict[str, List] = {}
    normalized_codes = set()
    for c in code_list:
        try:
            normalized_codes.add(normalize_code_s(c))
        except Exception:
            continue
    with ShelveDB(STOCKS_SHELVE) as db:
        for code in normalized_codes:
            rec = db.get(code)
            if rec:
                result[code] = rec.get("price_log") or []
    return result


def _sort_kessan_comments(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """kessan_comments を kessanbi 昇順に安定ソート。"""
    def _key(entry):
        dt = _parse_kessanbi(entry.get("kessanbi", ""))
        return dt or date.min
    return sorted(comments, key=_key)


_FORM_TRUE_VALUES = frozenset({"1", "true", "True", "on", "yes"})
_FORM_FALSE_VALUES = frozenset({"0", "false", "False", "off", "no"})


def _is_possess_now(code_s: str) -> bool:
    """code_s が現在の保有リスト (my_watch_list.txt の H プレフィックス) に含まれるか。

    parse 失敗時は False (kessan_matagi を誤って立てない安全側に倒す)。
    """
    try:
        import portfolio
        _, possess_list = portfolio.parse_my_portforio()
        return code_s in set(possess_list)
    except Exception as e:
        log_warning(f"[kessan_matagi] parse_my_portforio 失敗: {e}")
        return False


def _persist_kessan_held_flags(
    targets: List[Tuple[str, str, int, Dict[str, bool]]],
) -> None:
    """指定された (code_s, kessanbi, quarter) の保有系フラグを True に永続化する。

    updates dict は {"held_before_kessan": True, ...} のような True 立ち上げ指示。
    True は False に下げない (True だけを追記する一方向更新)。

    並行書き込み安全対応:
      - ロック下で get_research_record() による **再取得** を行い、
        呼び出し元が保持していた可能性のある stale な record は使わない。
      - updates の指定キー以外には触らない (他フィールドの並行編集は温存)。
      - 該当エントリが見つからない / 全指定キーが既に True ならスキップ。

    同一 code_s の複数 (kessanbi, quarter) を 1 回の lock にまとめる。
    """
    # code_s 単位でまとめる
    grouped: Dict[str, List[Tuple[str, int, Dict[str, bool]]]] = {}
    for code_s, kessanbi, quarter, updates in targets:
        grouped.setdefault(code_s, []).append((kessanbi, quarter, updates))

    for code_s, items in grouped.items():
        try:
            with _flock():
                record = get_research_record(code_s)
                if record is None:
                    continue
                comments = list(record.get("kessan_comments") or [])
                changed = False
                for kessanbi, quarter, updates in items:
                    for existing in comments:
                        if (
                            existing.get("kessanbi") == kessanbi
                            and int(existing.get("quarter", 0) or 0) == quarter
                        ):
                            for key, new_val in updates.items():
                                # True のみ追記 (False 降格しない)
                                if new_val and not existing.get(key):
                                    existing[key] = True
                                    changed = True
                            # AND 判定を permanent 側でも保証
                            if (
                                existing.get("held_before_kessan")
                                and existing.get("held_after_kessan")
                                and not existing.get("kessan_matagi")
                            ):
                                existing["kessan_matagi"] = True
                                changed = True
                            break
                if changed:
                    record["kessan_comments"] = comments
                    upsert_research_record(record)
        except Exception as e:
            log_warning(f"[kessan_matagi] 永続化失敗 code={code_s}: {e}")


def _parse_form_tristate_bool(raw: Any) -> Optional[bool]:
    """フォーム値を bool / None (=未指定) に正規化する。

    - True/False をそのまま受理
    - "1"/"true"/"on"/"yes" → True、"0"/"false"/"off"/"no" → False
    - None, "" は未指定として None を返す
    - それ以外は ValueError
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s == "":
            return None
        if s in _FORM_TRUE_VALUES:
            return True
        if s in _FORM_FALSE_VALUES:
            return False
    raise ValueError(f"bool として解釈不能: {raw!r}")


def _validate_kessan_comment_input(
    form_data: dict,
) -> Tuple[str, int, str, str, str, Optional[bool]]:
    """フォーム入力を検証し、正規化した値を返す。

    Returns:
        (kessanbi, quarter, pre_expectation, pre_outlook, post_comment,
         kessan_matagi_override)
        kessan_matagi_override は None / True / False。
        None は「フォームに指定なし」で、save 側の自動判定を使う。
    Raises:
        ValueError: 入力不正
    """
    kessanbi = (form_data.get("kessanbi") or "").strip()
    if _parse_kessanbi(kessanbi) is None:
        raise ValueError(f"kessanbi は YYYY/MM/DD 形式: got {kessanbi!r}")

    quarter_raw = form_data.get("quarter", "")
    try:
        quarter = int(quarter_raw)
    except (TypeError, ValueError):
        raise ValueError(f"quarter は整数: got {quarter_raw!r}")
    if quarter < 0 or quarter > 4:
        raise ValueError(f"quarter は 0〜4: got {quarter}")

    pre_expectation = (form_data.get("pre_expectation") or "").strip()
    if pre_expectation not in VALID_EXPECTATIONS:
        raise ValueError(f"pre_expectation 不正: {pre_expectation!r}")

    pre_outlook = form_data.get("pre_outlook", "") or ""
    post_comment = form_data.get("post_comment", "") or ""

    kessan_matagi_override: Optional[bool]
    if "kessan_matagi" in form_data:
        try:
            kessan_matagi_override = _parse_form_tristate_bool(
                form_data.get("kessan_matagi")
            )
        except ValueError as e:
            raise ValueError(f"kessan_matagi 不正: {e}")
    else:
        kessan_matagi_override = None

    return (
        kessanbi, quarter, pre_expectation, pre_outlook, post_comment,
        kessan_matagi_override,
    )


def save_kessan_comment(code_s: str, form_data: dict) -> Dict[str, Any]:
    """決算コメントを1件 upsert する。

    - 同じ (kessanbi, quarter) のエントリが既にあれば上書き
    - なければ追加
    - 12件超過時は最古（kessanbi 昇順の先頭）を削除
    - research_shelve 未登録なら add_stock() で自動登録
    - post_price_changes は price_log から各期間 (1d/5d) を自動計算してスナップショット化

    Returns:
        保存したエントリ dict
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)

    (
        kessanbi, quarter, pre_expectation, pre_outlook, post_comment,
        kessan_matagi_override,
    ) = _validate_kessan_comment_input(form_data)

    # 期間別変動率をスナップショット算出
    post_price_changes = calc_price_reactions(normalized, kessanbi)

    # 現在保有中か (kessan_matagi 初期値判定用)
    is_possess_now = _is_possess_now(normalized)

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            # 自動登録（_flock を再入しないよう add_stock の内部ロックに依存）
            add_stock(normalized)
            record = get_research_record(normalized)
            if record is None:
                raise ValueError(f"レコード登録失敗: {normalized}")

        comments: List[Dict[str, Any]] = list(record.get("kessan_comments") or [])
        # 同一 (kessanbi, quarter) を探す
        target_idx = None
        for i, entry in enumerate(comments):
            if (
                entry.get("kessanbi") == kessanbi
                and int(entry.get("quarter", 0) or 0) == quarter
            ):
                target_idx = i
                break

        # 既存 held フラグを引き継ぐ (True は下げない)
        existing_held_before = False
        existing_held_after = False
        existing_matagi = False
        if target_idx is not None:
            existing = comments[target_idx]
            existing_held_before = bool(existing.get("held_before_kessan", False))
            existing_held_after = bool(existing.get("held_after_kessan", False))
            existing_matagi = bool(existing.get("kessan_matagi", False))

        # 決算前後の切り分け: kessanbi と現在日の比較で held_before/after を更新
        kessanbi_dt = _parse_kessanbi(kessanbi)
        today = get_price_day(datetime.today())
        held_before = existing_held_before
        held_after = existing_held_after
        if is_possess_now and kessanbi_dt is not None:
            if kessanbi_dt >= today:
                held_before = True
            else:
                held_after = True

        # kessan_matagi の確定:
        #   1. form_data に明示指定があればそれを優先 (手動トグル)
        #   2. 既存エントリが True ならそれを維持
        #   3. held_before AND held_after なら True (AND 判定)
        if kessan_matagi_override is not None:
            kessan_matagi = kessan_matagi_override
        elif existing_matagi:
            kessan_matagi = True
        else:
            kessan_matagi = bool(held_before and held_after)

        new_entry: Dict[str, Any] = {
            "kessanbi": kessanbi,
            "quarter": quarter,
            "pre_expectation": pre_expectation,
            "pre_outlook": pre_outlook,
            "post_price_changes": dict(post_price_changes),
            "post_comment": post_comment,
            "kessan_matagi": kessan_matagi,
            "held_before_kessan": held_before,
            "held_after_kessan": held_after,
        }
        if target_idx is not None:
            # 既存エントリ上書き。各期間ごとにリアルタイム計算失敗時は既存値を優先
            # （旧 post_price_change のみ持つレコードでも 1d 値を引き継ぐ）
            existing = comments[target_idx]
            existing_changes = normalize_kessan_post_price_changes(existing)
            # 未知キー (例: "pts" — 別経路で書き込まれる) を保持するため
            # 既存 dict をベースに 1d/5d だけ上書きマージする
            merged_changes: Dict[str, str] = dict(existing_changes)
            for key, _ in KESSAN_REACTION_PERIODS:
                new_v = post_price_changes.get(key, "")
                old_v = existing_changes.get(key, "")
                merged_changes[key] = new_v if new_v else old_v
            new_entry["post_price_changes"] = merged_changes
            comments[target_idx] = new_entry
        else:
            comments.append(new_entry)

        # 昇順ソート + 12件超の最古を削除
        comments = _sort_kessan_comments(comments)
        if len(comments) > MAX_KESSAN_COMMENTS:
            comments = comments[-MAX_KESSAN_COMMENTS:]

        record["kessan_comments"] = comments
        upsert_research_record(record)

    return new_entry


def upsert_kessan_pts_change(
    code_s: str,
    kessanbi: str,
    quarter: int,
    pts_value: str,
) -> Dict[str, Any]:
    """当日決算銘柄の kessan_comments に PTS 騰落率を upsert する。

    - 既存 (kessanbi, quarter) エントリがあれば post_price_changes['pts'] のみ更新
    - 無ければ最小限のエントリを新規作成
    - レコード自体が無ければ add_stock() で先行登録
    - quarter は make_stock_db 側で stock['kessan_quarter'] から渡される

    Args:
        code_s: 銘柄コード
        kessanbi: YYYY/MM/DD 形式
        quarter: 1〜4 (0 は未取得扱い)
        pts_value: "+2.5" 形式 (% 記号は除去済み、符号は保持)

    Returns:
        upsert したエントリ dict
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if _parse_kessanbi(kessanbi) is None:
        raise ValueError(f"kessanbi は YYYY/MM/DD 形式: got {kessanbi!r}")
    pts_str = str(pts_value) if pts_value is not None else ""

    # add_stock は内部で _flock を取るため _flock 外で呼ぶ
    if get_research_record(normalized) is None:
        try:
            add_stock(normalized)
        except ValueError:
            # 競合で先に登録されたケースは続行 (再取得時に拾える)
            pass

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise ValueError(f"レコード登録失敗: {normalized}")

        comments: List[Dict[str, Any]] = list(record.get("kessan_comments") or [])
        target_idx = None
        for i, entry in enumerate(comments):
            if (
                entry.get("kessanbi") == kessanbi
                and int(entry.get("quarter", 0) or 0) == int(quarter or 0)
            ):
                target_idx = i
                break

        if target_idx is not None:
            existing = comments[target_idx]
            ppc = dict(existing.get("post_price_changes") or {})
            ppc["pts"] = pts_str
            existing["post_price_changes"] = ppc
            new_entry = existing
        else:
            new_entry = {
                "kessanbi": kessanbi,
                "quarter": int(quarter or 0),
                "pre_expectation": "",
                "pre_outlook": "",
                "post_price_changes": {"pts": pts_str},
                "post_comment": "",
                "kessan_matagi": False,
                "held_before_kessan": False,
                "held_after_kessan": False,
            }
            comments.append(new_entry)

        comments = _sort_kessan_comments(comments)
        if len(comments) > MAX_KESSAN_COMMENTS:
            comments = comments[-MAX_KESSAN_COMMENTS:]

        record["kessan_comments"] = comments
        upsert_research_record(record)

    return new_entry


def get_kessan_comment(code_s: str, kessanbi: str) -> Optional[Dict[str, Any]]:
    """特定の (code_s, kessanbi) のコメントエントリを返す。未登録/未存在は None。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    record = get_research_record(normalized)
    if record is None:
        return None
    for entry in record.get("kessan_comments") or []:
        if entry.get("kessanbi") == kessanbi:
            return entry
    return None


def get_market_kessan_data() -> Dict[str, Any]:
    """決算カレンダー表示用データを構築する。

    用語定義（本モジュール共通）:
      - ウォッチリスト: my_watch_list.txt 上の未保有銘柄
      - 保有銘柄: my_watch_list.txt 上の H プレフィックス付き銘柄
      - ポートフォリオ: ウォッチリスト ∪ 保有銘柄（= 日常的に追跡する対象）

    - pf_kessan_shelve から全ポートフォリオ銘柄の kessanbi (YYYY/MM/DD) を取得
    - research_shelve の kessan_comments をマージ（ただし現在のポートフォリオに
      含まれる銘柄のみ。ポートフォリオから外した銘柄のコメントは /market には
      表示せず、銘柄詳細ページ側で閲覧する想定）
    - 基準日は get_price_day() で判定し、過去/未来に分類
    - 未来は (date_str, [stock dict, ...]) のリスト、日付昇順
    - 過去は (date_str, [stock dict, ...]) のリスト、日付降順

    Returns:
        {
          "base_day": date,
          "future_entries": [(kessanbi_str, [stock dict]), ...],
          "past_entries":  [(kessanbi_str, [stock dict]), ...],
        }
        各 stock dict:
          code_s, stock_name, kessanbi, quarter,
          pre_expectation, pre_outlook, post_price_changes, post_comment,
          has_comment (bool)
        post_price_changes は {"1d": str, "5d": str} の dict。取得不可期間は ""
    """
    import kessan  # 遅延 import (sys.path 解決後)
    try:
        pf_dict = kessan.load_pf_kessan_db() or {}
    except Exception as e:
        log_warning(f"[market] load_pf_kessan_db 失敗: {e}")
        pf_dict = {}

    # ポートフォリオ (= ウォッチリスト ∪ 保有銘柄) を取得
    possess_set: set = set()
    portfolio_set: set = set()
    try:
        import portfolio
        watch_list, possess_list = portfolio.parse_my_portforio()
        possess_set = set(possess_list)
        portfolio_set = set(watch_list) | set(possess_list)
    except Exception as e:
        log_warning(f"[market] parse_my_portforio 失敗: {e}")

    base_day = get_price_day(datetime.today())

    # (code_s, kessanbi) をキーに統合
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for key, v in pf_dict.items():
        code_s = (v.get("code_s") or key or "").strip()
        if not code_s:
            continue
        # pf_kessan_shelve にはポートフォリオ外の卒業銘柄が残留している
        # ケースがあるため、現在のポートフォリオでフィルタする。
        # portfolio_set が空（parse 失敗時）はフィルタしない安全側に倒す。
        if portfolio_set and code_s not in portfolio_set:
            continue
        kessanbi = (v.get("kessanbi") or "").strip()
        if not kessanbi or _parse_kessanbi(kessanbi) is None:
            continue
        merged_key = (code_s, kessanbi)
        merged[merged_key] = {
            "code_s": code_s,
            "stock_name": v.get("stock_name", ""),
            "kessanbi": kessanbi,
            "quarter": v.get("kessan_quarter", 0) or 0,
            "pre_expectation": "",
            "pre_outlook": "",
            "post_price_changes": {key: "" for key, _ in KESSAN_REACTION_PERIODS},
            "post_comment": "",
            "has_comment": False,
            "is_possess": code_s in possess_set,
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }

    # research_shelve のコメント済みエントリをマージ
    try:
        records = list_research_records()
    except Exception as e:
        log_warning(f"[market] list_research_records 失敗: {e}")
        records = []

    for rec in records:
        code_s = rec.get("code_s", "")
        if not code_s:
            continue
        # ポートフォリオ外の銘柄コメントは /market に表示しない
        # （銘柄詳細ページ側で過去ログとして閲覧する想定 — issue #131）
        # portfolio_set が空（parse 失敗時）はフィルタしない安全側に倒す
        if portfolio_set and code_s not in portfolio_set:
            continue
        for entry in rec.get("kessan_comments") or []:
            kessanbi = entry.get("kessanbi", "")
            if not kessanbi:
                continue
            merged_key = (code_s, kessanbi)
            quarter = entry.get("quarter", 0) or 0
            base = merged.get(merged_key)
            stock_name = (base or {}).get("stock_name") or rec.get("stock_name", "")
            merged[merged_key] = {
                "code_s": code_s,
                "stock_name": stock_name,
                "kessanbi": kessanbi,
                "quarter": quarter if quarter else (base or {}).get("quarter", 0),
                "pre_expectation": entry.get("pre_expectation", "") or "",
                "pre_outlook": entry.get("pre_outlook", "") or "",
                "post_price_changes": normalize_kessan_post_price_changes(entry),
                "post_comment": entry.get("post_comment", "") or "",
                "has_comment": bool(
                    entry.get("pre_outlook") or entry.get("post_comment")
                    or entry.get("pre_expectation")
                ),
                "is_possess": code_s in possess_set,
                "kessan_matagi": bool(entry.get("kessan_matagi", False)),
                "held_before_kessan": bool(entry.get("held_before_kessan", False)),
                "held_after_kessan": bool(entry.get("held_after_kessan", False)),
            }

    # 過去エントリで post_price_changes のいずれかの期間が空のものだけ
    # 一括で price_log を取得し補完計算する
    past_codes_need_calc = set()
    for entry in merged.values():
        dt = _parse_kessanbi(entry["kessanbi"])
        if dt is None:
            continue
        if dt >= base_day:
            continue
        changes = entry.get("post_price_changes") or {}
        if any(not changes.get(key) for key, _ in KESSAN_REACTION_PERIODS):
            past_codes_need_calc.add(entry["code_s"])

    price_logs_cache = _bulk_price_logs(list(past_codes_need_calc)) if past_codes_need_calc else {}

    # 日付ごとにグループ化
    future_groups: Dict[str, List[Dict[str, Any]]] = {}
    past_groups: Dict[str, List[Dict[str, Any]]] = {}
    today_groups: Dict[str, List[Dict[str, Any]]] = {}
    # kessan_matagi 関連フィールドで新たに True 化した per-entry を記録し、
    # ループ後に専用関数で shelve に永続化する。
    # (stale rec を直接 upsert すると並行編集を上書きするため、lock 下で再取得する)
    # targets: [(code_s, kessanbi, quarter, {"held_before_kessan": True, ...}), ...]
    persist_targets: List[Tuple[str, str, int, Dict[str, bool]]] = []
    for entry in merged.values():
        kessanbi = entry["kessanbi"]
        dt = _parse_kessanbi(kessanbi)
        if dt is None:
            continue
        # 過去の変動率を計算できれば補完（保存済み値が無い期間のみ、キャッシュ利用）
        # 各銘柄の price_log は1回だけソート/分割して全期間で共有する
        if dt < base_day:
            existing_changes = entry.get("post_price_changes") or {}
            if any(not existing_changes.get(key) for key, _ in KESSAN_REACTION_PERIODS):
                log = price_logs_cache.get(entry["code_s"], [])
                calculated = _price_reactions_from_log(log, dt)
                for key, _ in KESSAN_REACTION_PERIODS:
                    if not existing_changes.get(key) and calculated.get(key):
                        existing_changes[key] = calculated[key]
                entry["post_price_changes"] = existing_changes

        # 前後保有フラグのスナップショット化
        # - 未来エントリ (dt >= base_day): is_possess=True なら held_before_kessan=True
        # - 過去エントリ (dt <  base_day): is_possess=True なら held_after_kessan=True
        # True は False に下げない (過去の保有痕跡は不可逆保持)
        updates: Dict[str, bool] = {}
        if entry.get("is_possess"):
            if dt >= base_day and not entry.get("held_before_kessan"):
                entry["held_before_kessan"] = True
                updates["held_before_kessan"] = True
            elif dt < base_day and not entry.get("held_after_kessan"):
                entry["held_after_kessan"] = True
                updates["held_after_kessan"] = True

        # AND 判定: 決算前保有 & 決算後保有 の両方満たせば kessan_matagi=True 確定
        if (
            entry.get("held_before_kessan")
            and entry.get("held_after_kessan")
            and not entry.get("kessan_matagi")
        ):
            entry["kessan_matagi"] = True
            updates["kessan_matagi"] = True

        if updates:
            persist_targets.append((
                entry["code_s"],
                entry["kessanbi"],
                int(entry.get("quarter", 0) or 0),
                updates,
            ))

        # 表示振り分けはカレンダー上の今日基準で 3 群に分ける:
        # - past_groups (dt < today_cal): 過去決算 (反応コメ・株価変動率を表示)
        # - today_groups (dt == today_cal): 当日決算。中身は past 相当で
        #   反応コメ・決算またぎを当日中に編集できるが、表示位置はカード扱いで
        #   future の前に置く。
        # - future_groups (dt > today_cal): 未来決算 (事前見通しのみ編集)
        # held_before/after の判定はこれより上の base_day ベースを維持
        # (当日中の保有は「決算前保有」として扱うため)。
        today_cal = datetime.today().date()
        if dt < today_cal:
            past_groups.setdefault(kessanbi, []).append(entry)
        elif dt == today_cal:
            today_groups.setdefault(kessanbi, []).append(entry)
        else:
            future_groups.setdefault(kessanbi, []).append(entry)

    if persist_targets:
        _persist_kessan_held_flags(persist_targets)

    # 銘柄コード順にカード内ソート
    for d in (
        list(future_groups.values())
        + list(today_groups.values())
        + list(past_groups.values())
    ):
        d.sort(key=lambda e: e["code_s"])

    future_entries = sorted(
        future_groups.items(),
        key=lambda kv: _parse_kessanbi(kv[0]) or date.max,
    )
    today_entries = sorted(
        today_groups.items(),
        key=lambda kv: _parse_kessanbi(kv[0]) or date.max,
    )
    past_entries_all = sorted(
        past_groups.items(),
        key=lambda kv: _parse_kessanbi(kv[0]) or date.min,
        reverse=True,
    )
    # 過去7日間は常時表示、それ以前は details で折りたたみ
    recent_cutoff = base_day - timedelta(days=7)
    recent_past_entries: List = []
    older_past_entries: List = []
    for kv in past_entries_all:
        dt = _parse_kessanbi(kv[0]) or date.min
        if dt >= recent_cutoff:
            recent_past_entries.append(kv)
        else:
            older_past_entries.append(kv)

    return {
        "base_day": base_day,
        "future_entries": future_entries,
        "today_entries": today_entries,
        "past_entries": past_entries_all,  # 後方互換
        "recent_past_entries": recent_past_entries,
        "older_past_entries": older_past_entries,
    }
