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
# **赤字** → <span style="color:#ff0000">赤字</span>（先に処理、* と区別するため）
_RE_RED = re.compile(r"\*\*(.+?)\*\*")
# *太字* → <b>太字</b>（** 処理後に実行）
_RE_BOLD = re.compile(r"\*(.+?)\*")
# [テキスト](URL) → <a href="URL" target="_blank">テキスト</a>
_RE_NAMED_LINK = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
# URL自動リンク化（既に <a> タグ内でないURLを対象）
_RE_URL = re.compile(r'(?<!["\'>])(https?://[^\s<>\'"]+)')


def _markdown_to_html(text: str) -> str:
    """マークダウン風記法を HTML に変換する。

    - **赤字** → <span style="color:#ff0000">赤字</span>
    - *太字* → <b>太字</b>
    - [テキスト](URL) → <a href="URL" target="_blank">テキスト</a>
    - URL → <a href="URL" target="_blank">URL</a>
    """
    if not text:
        return text
    text = _RE_RED.sub(r'<span style="color:#ff0000">\1</span>', text)
    text = _RE_BOLD.sub(r"<b>\1</b>", text)
    text = _RE_NAMED_LINK.sub(r'<a href="\2" target="_blank">\1</a>', text)
    text = _RE_URL.sub(r'<a href="\1" target="_blank">\1</a>', text)
    return text


def theme_news_md_to_html(text: str) -> str:
    """theme-news history の markdown を HTML に変換しサニタイズする (issue #165)。

    skill 出力は見出し・箇条書き・表を含む正式な markdown なので、
    `markdown` パッケージで HTML 化してから既存サニタイザに通す。
    対応: 見出し (h2/h3)、箇条書き (-, 1.)、太字 (**), 強調 (*),
          インラインコード (`), リンク [text](url), テーブル

    また skill 出力の長文 li を読みやすくするため、全角中点 `・` の前に <br> を挿入する。
    skill 側は文を `・` で繋いで 1 行に詰める癖があり、そのままだと折返しの長文になる。
    """
    if not text:
        return ""
    import markdown as _md
    html = _md.markdown(text, extensions=["extra", "sane_lists"])
    import re
    # skill 出力先頭の `# 見出し` (= ファイルタイトル) は /market summary で既に日付を
    # 表示しているので冗長。<h1>...</h1> を削除する (sanitize_html で h1 をエスケープ
    # して `<h1>` 文字列が出るのを防ぐ目的も兼ねる)。
    html = re.sub(r"<h1>.*?</h1>\s*", "", html, count=1, flags=re.DOTALL)
    # 文の区切りで使われている全角中点を改行ポイントにする。
    # 先頭 `・` (li 直後) には br を入れないよう、`>・` の直後は対象外。
    html = re.sub(r"(?<![>\s])・", "<br>・", html)
    # 「+」「→」の前に <wbr> を挟んで、文が長くても自然な位置で折り返せるようにする。
    # skill 出力で「日経+1.9%+KOSPI+4%+Samsungスト中止」のような連結が頻出するため。
    html = re.sub(r"(?<=[ぁ-んァ-ヶー一-龯%])([+→])", r"<wbr>\1", html)
    # Sources セクション (`## Sources` から末尾まで) を <details class="sources"> で
    # 折りたたむ。skill 出力末尾に必ず「## Sources」見出しが入る規約に依存。
    # マッチしなければ何もしない (旧 history で Sources 無いものは素通り)。
    def _wrap_sources(match: re.Match) -> str:
        # Sources セクション内の <a href="..."> は外部サイト前提なので別タブで開く。
        body = re.sub(
            r'<a\s+href="(https?://[^"]+)">',
            r'<a href="\1" target="_blank" rel="noopener">',
            match.group(1),
        )
        return f'<details class="sources"><summary>📎 Sources を表示</summary>{body}</details>'

    html = re.sub(
        r'<h2>Sources</h2>\s*(<ul>.*?</ul>)',
        _wrap_sources,
        html,
        count=1,
        flags=re.DOTALL,
    )
    return sanitize_html(html)


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


def save_stock_name_prev(code_s: str, value: str) -> None:
    """detail ページの inline 編集から呼ばれる stock_name_prev 単体更新 (issue #236)。

    - 空文字 (前後 strip 後) なら None にリセット (= 手動エイリアス解除、次回 sync_stock_name で
      自動退避が再び有効になる)
    - 未登録 code_s は KeyError
    - 他フィールドは _flock 区間内で最新値を保持
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    cleaned = (value or "").strip() or None

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise KeyError(f"research_shelve: {normalized} は未登録です")
        record["stock_name_prev"] = cleaned
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


def save_corporate_url_override(code_s: str, url: str) -> str:
    """会社HP URL の手動上書きを保存する (issue #208)。

    空文字を渡すと上書きをクリアする (デフォルトに戻る)。
    入力値が stocks_shelve.corporate_url と同一の場合も、上書きとして固定すると
    今後の corporate_url 自動更新を遮断してしまうため、空文字扱い (= デフォルト
    継続) として保存する。
    URL は事前にバリデーション済みであることを呼び出し側で保証する。

    Returns:
        実際に保存された値 (空文字なら override クリア、それ以外なら上書き値)。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)

    cleaned = url.strip()
    if cleaned:
        stock = get_stock_data(normalized) or {}
        default_url = (stock.get("corporate_url") or "").strip()
        if default_url and cleaned == default_url:
            cleaned = ""

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise ValueError(f"レコード未登録: {normalized}")
        record["corporate_url_override"] = cleaned
        upsert_research_record(record)
    return cleaned


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
      - "body": <body> 内のコンテンツのうち、<h2>決算日</h2> 以下のブロック
         （次の <h2> 直前まで）を除去したもの。<h1>/footer も除外。
         issue #213: 決算日セクションは /disclosure に移設したため、市場ページでは抑制する。
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
        "body": body_html,
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
        # マッチング 3 段 (issue #207):
        # 1) (kessanbi, quarter) 完全一致 → そのエントリ
        # 2) 完全一致なし & 引数 quarter==0 → 同 kessanbi 内で quarter 最大のエントリ
        #    (cron 経路で kessan_quarter 取得失敗 → q=0 フォールバックが
        #     既に手動メモ済みの quarter エントリと別エントリで append される事故防止)
        # 3) それも無ければ新規 append
        target_idx = None
        for i, entry in enumerate(comments):
            if (
                entry.get("kessanbi") == kessanbi
                and int(entry.get("quarter", 0) or 0) == int(quarter or 0)
            ):
                target_idx = i
                break
        if target_idx is None and int(quarter or 0) == 0:
            # 同 kessanbi 内で quarter が最大のエントリにフォールバックマージ
            best_idx = None
            best_q = -1
            for i, entry in enumerate(comments):
                if entry.get("kessanbi") != kessanbi:
                    continue
                q = int(entry.get("quarter", 0) or 0)
                if q > best_q:
                    best_q = q
                    best_idx = i
            if best_idx is not None:
                target_idx = best_idx

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


def _is_empty_placeholder(entry: Dict[str, Any]) -> bool:
    """pf_kessan_shelve 由来の空ベース行 (= 実データを何も持たないプレースホルダ) か判定する。

    pf-only ベースは has_comment=False かつ価格反応も held_* / kessan_matagi も無い。
    research 側のエントリが「メモなしだが反応率/held フラグあり」のとき、こちらを
    優先するための補助判定 (issue #207 codex P1 review 反映)。
    """
    if entry.get("has_comment"):
        return False
    if entry.get("kessan_matagi"):
        return False
    if entry.get("held_before_kessan") or entry.get("held_after_kessan"):
        return False
    ppc = entry.get("post_price_changes") or {}
    for v in ppc.values():
        if (v or "").strip():
            return False
    return True


def _select_market_kessan_winner(
    cur: Dict[str, Any], cand: Dict[str, Any]
) -> Dict[str, Any]:
    """同 (code_s, kessanbi) で複数 kessan_comments エントリが来たときの優先順位 (issue #207)。

    1. cur が pf-only プレースホルダなら cand を採用 (research 側の実データを優先、
       codex P1 review: kessan_matagi / held_* / post_price_changes が捨てられないように)
    2. has_comment=True を優先
    3. 両方 has_comment が同じなら quarter 大優先
    4. quarter も同じなら cur (= 既存挙動互換、最初に見たもの)
    """
    if _is_empty_placeholder(cur):
        return cand
    cur_has = bool(cur.get("has_comment"))
    cand_has = bool(cand.get("has_comment"))
    if cur_has != cand_has:
        return cand if cand_has else cur
    cur_q = int(cur.get("quarter", 0) or 0)
    cand_q = int(cand.get("quarter", 0) or 0)
    if cand_q > cur_q:
        return cand
    return cur


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
            "stock_name_prev": None,  # stocks_shelve 由来は旧名情報を持たない
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
            cur = merged.get(merged_key)
            stock_name = (cur or {}).get("stock_name") or rec.get("stock_name", "")
            # research_shelve 側の旧名 (issue #183)。stocks_shelve 由来の cur に
            # 上書きされた場合でも rec の prev を併記表示できるよう保持する。
            stock_name_prev = (cur or {}).get("stock_name_prev") or rec.get("stock_name_prev")
            cand = {
                "code_s": code_s,
                "stock_name": stock_name,
                "stock_name_prev": stock_name_prev,
                "kessanbi": kessanbi,
                "quarter": quarter if quarter else (cur or {}).get("quarter", 0),
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
            # issue #207: 同 (code_s, kessanbi) で複数 quarter エントリが併存する場合、
            # has_comment 優先 → quarter 大優先で winner を選ぶ。PTS は別チャネルで OR マージ。
            winner = _select_market_kessan_winner(cur, cand) if cur else cand
            winner_pts = (winner.get("post_price_changes") or {}).get("pts") or ""
            if not winner_pts:
                # winner に PTS が無ければ他のソース (cur / cand) から拾う
                for src in (cur, cand):
                    if not src:
                        continue
                    src_pts = (src.get("post_price_changes") or {}).get("pts") or ""
                    if src_pts:
                        winner.setdefault("post_price_changes", {})["pts"] = src_pts
                        break
            merged[merged_key] = winner

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

        # 表示振り分けは Shintakane 基準時刻 (17時) で 3 群に分ける:
        # - past_groups (dt < base_day): 過去決算 (反応コメ・株価変動率を表示)
        # - today_groups (dt == base_day): 当日決算。中身は past 相当で
        #   反応コメ・決算またぎを当日中に編集できるが、表示位置はカード扱いで
        #   future の前に置く。
        # - future_groups (dt > base_day): 未来決算 (事前見通しのみ編集)
        # 例: 5/15 0:10 では base_day=5/14 のため 5/14 カードは当日扱い、
        # 5/15 17:00 以降は base_day=5/15 となり 5/14 カードは過去扱いになる。
        if dt < base_day:
            past_groups.setdefault(kessanbi, []).append(entry)
        elif dt == base_day:
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
    # 90日 (約1四半期) より前の決算は表示しない (履歴が貯まり続けるとDOM/メモリが肥大化するため)
    recent_cutoff = base_day - timedelta(days=7)
    older_cutoff = base_day - timedelta(days=90)
    recent_past_entries: List = []
    older_past_entries: List = []
    for kv in past_entries_all:
        dt = _parse_kessanbi(kv[0]) or date.min
        if dt < older_cutoff:
            continue
        if dt >= recent_cutoff:
            recent_past_entries.append(kv)
        else:
            older_past_entries.append(kv)

    return {
        "base_day": base_day,
        "future_entries": future_entries,
        "today_entries": today_entries,
        # 後方互換: 90日カットオフ後の全過去エントリ (空状態判定で使うため
        # recent + older を合成。past_entries_all をそのまま返すと、90日超
        # しか無いケースで空状態メッセージが出ず画面が空白になる)
        "past_entries": recent_past_entries + older_past_entries,
        "recent_past_entries": recent_past_entries,
        "older_past_entries": older_past_entries,
    }


def _bulk_get_stock_data(code_list: List[str]) -> Dict[str, Dict[str, Any]]:
    """stocks_shelve を 1 度だけ open して複数銘柄をまとめて取得する。

    `get_stock_data` を N 回呼ぶと N 回 open/close するため、一覧画面用のバルク版。
    """
    result: Dict[str, Dict[str, Any]] = {}
    with ShelveDB(STOCKS_SHELVE) as db:
        for code_s in code_list:
            if not code_s:
                continue
            normalized = normalize_code_s(code_s)
            result[code_s] = db.get(normalized) or {}
    return result


def resolve_stock_name(code_s: str) -> str:
    """銘柄名を stocks_shelve → research_shelve → "" の優先順で取得する。

    portfolio_shelve は銘柄名を持たないため、表示時はこの関数経由で都度取得する。
    """
    from db_shelve import RESEARCH_SHELVE  # 遅延 import (循環回避)

    if not code_s:
        return ""
    normalized = normalize_code_s(code_s)
    with ShelveDB(STOCKS_SHELVE) as db:
        rec = db.get(normalized)
        if rec and rec.get("stock_name"):
            return rec["stock_name"]
    with ShelveDB(RESEARCH_SHELVE) as db:
        rec = db.get(normalized)
        if rec and rec.get("stock_name"):
            return rec["stock_name"]
    return ""


def _bulk_resolve_stock_names(code_list: List[str]) -> Dict[str, str]:
    """複数 code_s 分の銘柄名をバルク取得する (一覧画面用)。"""
    from db_shelve import RESEARCH_SHELVE  # 遅延 import (循環回避)

    result: Dict[str, str] = {c: "" for c in code_list if c}
    if not result:
        return result

    with ShelveDB(STOCKS_SHELVE) as db:
        for c in list(result.keys()):
            rec = db.get(normalize_code_s(c))
            if rec and rec.get("stock_name"):
                result[c] = rec["stock_name"]

    missing = [c for c, n in result.items() if not n]
    if missing:
        with ShelveDB(RESEARCH_SHELVE) as db:
            for c in missing:
                rec = db.get(normalize_code_s(c))
                if rec and rec.get("stock_name"):
                    result[c] = rec["stock_name"]
    return result


def _bulk_resolve_stock_name_prevs(code_list: List[str]) -> Dict[str, Optional[str]]:
    """複数 code_s 分の旧銘柄名 (research_shelve.stock_name_prev) をバルク取得する (issue #183)。

    旧名がないか research_shelve に未登録の銘柄は None。
    """
    from db_shelve import RESEARCH_SHELVE  # 遅延 import (循環回避)

    result: Dict[str, Optional[str]] = {c: None for c in code_list if c}
    if not result:
        return result

    with ShelveDB(RESEARCH_SHELVE) as db:
        for c in list(result.keys()):
            rec = db.get(normalize_code_s(c))
            if rec:
                prev = rec.get("stock_name_prev")
                if isinstance(prev, str) and prev:
                    result[c] = prev
    return result


# issue #178: status 内部値 → URL クエリ / 表示ラベルの対応表 (helpers 内部利用のみ)。
# routes/portfolio.py の同名定数とは独立に保持し、循環 import を避ける。
_PORTFOLIO_STATUS_QUERY = {
    "1保": "hold",
    "2準": "semi",
    "3監": "watch",
}
_PORTFOLIO_STATUS_LABEL = {
    "1保": "保有",
    "2準": "準保有",
    "3監": "監視",
}


def _gyoutai_first_line(row: Dict[str, Any]) -> str:
    """row の memo.gyoutai_themes の先頭要素を返す (issue #178 業態順ソート用)。

    空 list / 先頭が空文字 / themes 自体が無い場合は "" を返す。
    """
    themes = (row.get("memo") or {}).get("gyoutai_themes") or []
    if not themes:
        return ""
    head = themes[0]
    if not isinstance(head, str):
        return ""
    return head.strip()


def list_portfolio_with_indicators(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """portfolio_shelve のレコード列に stocks_shelve から最新指標を補完する (Phase 3b)。

    銘柄名は portfolio_shelve に保存されていないため stocks_shelve / research_shelve から
    都度取得してマージする (要件 §4 の延長)。

    Args:
        records: portfolio_shelve.list_records の戻り値 (既に status 等で絞り込み済み)

    Returns:
        各 dict: portfolio レコード + {stock_name, rank, kessanbi_md, per, market_cap,
                                     dividend, rs, sales_growth, profit_growth,
                                     quarter, progress_diff, trend_template, tags,
                                     theoretical_diff, gyoseki, indicators_raw,
                                     status_query, status_label}
        並び順は業態 1 行目昇順 → 順位昇順 → コード (issue #215: 順位ソート廃止、空業態/None順位は末尾)。
    """
    if not records:
        return []

    code_list = [r.get("code_s", "") for r in records]
    stock_map = _bulk_get_stock_data(code_list)
    name_map = _bulk_resolve_stock_names(code_list)
    name_prev_map = _bulk_resolve_stock_name_prevs(code_list)  # issue #183
    today = date.today()  # 全 row 共通の基準日 (issue #177)

    # issue #227: 株価 + RSライン 統合チャート用に market_db を1回だけロード。
    # 失敗時は None で進める (株価のみのチャートが描画される)
    try:
        from make_market_db import get_market_db  # 遅延 import (循環回避)
        market_db = get_market_db()
    except Exception:  # noqa: BLE001
        market_db = None

    rows: List[Dict[str, Any]] = []
    for rec in records:
        code_s = rec.get("code_s", "")
        row = dict(rec)
        row["stock_name"] = name_map.get(code_s, "") or rec.get("stock_name", "")  # 旧データ互換
        row["stock_name_prev"] = name_prev_map.get(code_s)  # issue #183
        stock = stock_map.get(code_s, {})
        row.update(_extract_indicators_for_portfolio(stock))
        # issue #227: 3点ミニチャート (svg + tooltip)
        row["price_rs_chart"] = build_stock_chart_payload(stock, market_db, mode="mini")
        row["styles"] = compute_cell_styles(row, today=today)
        # issue #178: ステータス列 (badge) 表示用の query / label を埋める
        status = rec.get("status", "")
        row["status_query"] = _PORTFOLIO_STATUS_QUERY.get(status, "")
        row["status_label"] = _PORTFOLIO_STATUS_LABEL.get(status, status)
        # issue #178: 業態境界判定用 (template から再計算しないで済むよう)
        row["gyoutai_first"] = _gyoutai_first_line(row)
        # issue #269: 保有株数とポジション量 (1保 のみ意味を持つ)
        qty = rec.get("qty", 0) or 0
        price = stock.get("price") if isinstance(stock, dict) else None
        if status == "1保" and qty > 0 and isinstance(price, (int, float)) and price > 0:
            row["position_value"] = float(price) * qty
        else:
            row["position_value"] = 0.0
        row["qty"] = qty
        row["position_ratio"] = 0.0  # 後段で 1保 群の max に対する相対比で埋め直す
        rows.append(row)

    # issue #269: 1保 銘柄の最大ポジション量を基準に position_ratio (0-100) を計算
    max_position = max(
        (r["position_value"] for r in rows if r.get("status") == "1保"),
        default=0.0,
    )
    if max_position > 0:
        for r in rows:
            if r.get("status") == "1保" and r["position_value"] > 0:
                r["position_ratio"] = r["position_value"] / max_position * 100.0

    # 業態順: 業態 1 行目 (空は末尾) → 順位昇順 (None は末尾) → コード
    rows.sort(key=lambda r: (
        r["gyoutai_first"] == "",
        r["gyoutai_first"],
        r.get("rank") is None,
        r.get("rank") or 0,
        r.get("code_s", ""),
    ))
    return rows


def collect_gyoutai_theme_choices(records: List[Dict[str, Any]]) -> List[str]:
    """portfolio_shelve 全レコードの memo.gyoutai_themes をフラット化して候補リストを返す (issue #187)。

    空要素除去・ユニーク化・アルファベット/五十音昇順ソート済み。
    datalist の選択肢として使う想定。
    """
    seen = set()
    for rec in records:
        memo = rec.get("memo") or {}
        for theme in (memo.get("gyoutai_themes") or []):
            if not isinstance(theme, str):
                continue
            t = theme.strip()
            if t:
                seen.add(t)
    return sorted(seen)


def _format_tags(stock: Dict[str, Any]) -> str:
    """code_rank.csv「タグ」列と同じ表記を返す。

    make_stock_db.make_signal() の tags リストを "/" join する。
    market_db を渡さないので R高/強乖/弱乖 タグは出ない (Phase 4 送り)。
    """
    if not stock:
        return "—"
    try:
        from make_stock_db import make_signal  # 遅延 import
        _signal, tags = make_signal(stock)
    except Exception:
        return "—"
    return "/".join(tags) if tags else "—"


def _format_theoretical_diff(stock: Dict[str, Any]) -> str:
    """理論株価乖離率の表示 (code_rank.csv「理論株価(乖離率|上限,下限))」列の最初の値)。

    rironkabuka.get_rironkabuka_kairi(stock) を再利用して同じ計算ロジックを使う。
    返り値の tuple の先頭が乖離率 (= `(理論株価 - 株価) / 株価 * 100`)。
    """
    if not stock:
        return "—"
    try:
        from rironkabuka import get_rironkabuka_kairi  # 遅延 import
        kairi, _up, _down, _preceding = get_rironkabuka_kairi(stock)
    except Exception:
        return "—"
    if not stock.get("price") or not stock.get("rironkabuka"):
        return "—"
    # "%" 表記は列ヘッダ側 ("理論株価乖離(%)") に集約 (issue #177)、値は数値のみ
    return f"{int(kairi)}"


def _annual_growth(stock: Dict[str, Any]) -> tuple:
    """gyoseki.calc_annual_growth を遅延 import で呼んで (sales%, profit%) を返す。

    code_rank.csv の業績列 [A]X%,Y% の X, Y。取れなければ (None, None)。
    """
    if not stock:
        return (None, None)
    try:
        from gyoseki import calc_annual_growth  # 遅延 import (循環回避)
        result = calc_annual_growth(stock)
    except Exception:
        return (None, None)
    if not result:
        return (None, None)
    # result = (年度, 売上%, 営利%)
    return result[1], result[2]


def _extract_buy_collection_labels(stock: Dict[str, Any]) -> tuple:
    """個別銘柄の sell_pressure_ratio / sell_pressure_ratio_w から
    買い集め評価 (週, 日) のアルファベットラベルを返す。

    price.get_spr_expr の出力 (例 "47,32,D,E") からアルファベット部分のみ抽出。
    片方/両方欠損は (None, None) や (週ラベル, None) で返す。
    """
    if not stock:
        return (None, None)
    sprs = stock.get("sell_pressure_ratio") or []
    sprs_w = stock.get("sell_pressure_ratio_w") or []
    if not sprs:
        return (None, None)
    try:
        from price import get_spr_expr  # 遅延 import (循環回避)
        full = get_spr_expr(sprs, sprs_w)
    except Exception:
        return (None, None)
    parts = full.split(",")
    letters = [p for p in parts if p and not p.lstrip("+-").isdigit()]
    sprw = letters[0] if len(letters) > 0 else None
    sprbg = letters[1] if len(letters) > 1 else None
    return (sprw, sprbg)


def _build_spr_gauge_for_stock(stock: Dict[str, Any]) -> Dict[str, str]:
    """個別銘柄の sell_pressure_ratio / stddev_volatility から需給バランスゲージを組み立てる。

    個別銘柄の price 指標は:
      sell_pressure_ratio = [spr_20, spr_5, buygather]  (price.py 参照)
      stddev_volatility   = [rv_20, rv_5]
    市場指数 (make_market_db) の spr_20 / spr_5 / rv_20 / rv_5 と形式が違うため、
    ここで取り出して build_spr_gauge_svg / build_spr_gauge_tooltip に渡す。
    買い集め評価 (週/日) はバー背景の濃淡と tooltip に併記する。
    """
    from make_market_db import build_spr_gauge_svg, build_spr_gauge_tooltip  # 遅延 import (循環回避)
    sprs = stock.get("sell_pressure_ratio") or []
    vols = stock.get("stddev_volatility") or []
    spr_20 = sprs[0] if len(sprs) > 0 else None
    spr_5 = sprs[1] if len(sprs) > 1 else None
    rv_20 = vols[0] if len(vols) > 0 else None
    rv_5 = vols[1] if len(vols) > 1 else None
    sprw_label, sprbg_label = _extract_buy_collection_labels(stock)
    return {
        "svg": build_spr_gauge_svg(spr_20, rv_20, spr_5, rv_5, sprw_label, sprbg_label),
        "tooltip": build_spr_gauge_tooltip(
            spr_20, rv_20, spr_5, rv_5, sprw_label, sprbg_label,
        ),
    }


def _progress_quarter_and_diff(stock: Dict[str, Any]) -> tuple:
    """gyoseki.calc_progress_rate から (quarter_label, diff_str) を返す。

    code_rank.csv 進捗率列 [P]3Q70%(72%),62%(44%) を分解:
    - quarter_label: "3Q" などの文字列。
        - quarter=0 (1Q 未発表 / 新年度開始直後) は "0Q" として明示
        - calc_progress_rate が値を返さない (データ不足) ときは "—"
    - diff_str: "(sales-sales_pre)/(profit-profit_pre)" を整数化 (例: "-2/+18")
                取れなければ "—"
    """
    if not stock:
        return ("—", "—")
    try:
        from gyoseki import calc_progress_rate  # 遅延 import (循環回避)
        progress = calc_progress_rate(stock)
    except Exception:
        return ("—", "—")
    if not isinstance(progress, dict) or "quarter" not in progress:
        return ("—", "—")
    quarter = progress.get("quarter", 0)
    if not isinstance(quarter, int) or quarter < 0:
        return ("—", "—")
    quarter_label = f"{quarter}Q"
    sales = progress.get("sales")
    sales_pre = progress.get("sales_pre")
    profit = progress.get("profit")
    profit_pre = progress.get("profit_pre")
    if not all(isinstance(v, (int, float)) for v in (sales, sales_pre, profit, profit_pre)):
        return (quarter_label, "—")
    sales_diff = round(sales - sales_pre)
    profit_diff = round(profit - profit_pre)
    diff_str = f"{sales_diff:d}/{profit_diff:d}"
    return (quarter_label, diff_str)


def _format_kessanbi_md(kessanbi: Any) -> str:
    """kessanbi (YYYY/MM/DD) を MM/DD 形式に整形して返す。空なら "—"。"""
    if not kessanbi or not isinstance(kessanbi, str):
        return "—"
    parts = kessanbi.split("/")
    if len(parts) >= 3:
        return f"{parts[1]}/{parts[2]}"
    return kessanbi


def _format_per(per: Any) -> str:
    """PER を表示用文字列に整形する。

    - 二桁以上 (>= 10): 整数表記 (例: 25, 30)
    - 一桁 (< 10): 小数1桁 (例: 5.3, 3.0)
    - 数値でない: "—"
    """
    if not isinstance(per, (int, float)):
        return "—"
    if per >= 10:
        return f"{per:.0f}"
    return f"{per:.1f}"


def _theoretical_diff_raw(stock: Dict[str, Any]) -> Optional[float]:
    """理論株価乖離率の生値 (整数化前) を返す。取れなければ None。"""
    if not stock or not stock.get("price") or not stock.get("rironkabuka"):
        return None
    try:
        from rironkabuka import get_rironkabuka_kairi  # 遅延 import
        kairi, _up, _down, _preceding = get_rironkabuka_kairi(stock)
    except Exception:
        return None
    return kairi if isinstance(kairi, (int, float)) else None


def _progress_diff_eiri_raw(stock: Dict[str, Any]) -> Optional[float]:
    """進捗率乖離 (営利) の生値を返す。"3/15" の右側 = profit - profit_pre。"""
    if not stock:
        return None
    try:
        from gyoseki import calc_progress_rate  # 遅延 import
        progress = calc_progress_rate(stock)
    except Exception:
        return None
    if not isinstance(progress, dict):
        return None
    profit = progress.get("profit")
    profit_pre = progress.get("profit_pre")
    if not all(isinstance(v, (int, float)) for v in (profit, profit_pre)):
        return None
    return profit - profit_pre


def _market_cap_category(billion_yen: Optional[float]) -> Optional[str]:
    """時価総額 (億円) からカテゴリ名を返す。スプシの IFS と同じ閾値。"""
    if not isinstance(billion_yen, (int, float)):
        return None
    if billion_yen < 100:
        return "極小"
    if billion_yen < 400:
        return "小"
    if billion_yen < 1000:
        return "中"
    if billion_yen < 3000:
        return "大"
    return "特大"


def _gyoseki_quarity_expr_safe(stock: Dict[str, Any]) -> str:
    """gyoseki.get_gyoseki_quarity_expr を安全に呼ぶ (例外時は空文字)。

    末尾に "<C3>" タグが付いていれば 3Q 連続利益率向上の意味。
    """
    if not stock:
        return ""
    try:
        from gyoseki import get_gyoseki_quarity_expr  # 遅延 import
        return get_gyoseki_quarity_expr(stock) or ""
    except Exception:
        return ""


# issue #204: gyoseki_quarity_expr を [A]…/[Q]…/<C3> の各セグメントに分解する
_GYOSEKI_EXPR_RE = re.compile(
    r"\[A\](?P<annual>[-\d]+±\d+%,[-\d]+±\d+%)"
    r"\[Q\](?P<quarter>[-\d]+±\d+%,[-\d]+±\d+%)"
    r"(?P<c3><C3>)?"
)


def parse_gyoseki_quarity_expr(expr: str) -> Dict[str, Any]:
    """gyoseki_quarity_expr を tooltip 生成用に分解する。

    入力例: "[A]5±8%,2±6%[Q]-5±12%,1±8%<C3>"
    返り値: {
        "sales_5y": "5±8%", "profit_5y": "2±6%",     # [A] = 過去5年年度平均
        "sales_4q": "-5±12%", "profit_4q": "1±8%",   # [Q] = 過去4Q平均
        "has_c3": True,                              # 3Q連続利益率向上タグ
    }
    パース失敗時は全フィールドが None / has_c3 = False の dict を返す。
    """
    empty = {"sales_5y": None, "profit_5y": None, "sales_4q": None, "profit_4q": None, "has_c3": False}
    if not expr:
        return empty
    m = _GYOSEKI_EXPR_RE.search(expr)
    if not m:
        return empty
    sales_5y, profit_5y = m.group("annual").split(",", 1)
    sales_4q, profit_4q = m.group("quarter").split(",", 1)
    return {
        "sales_5y": sales_5y,
        "profit_5y": profit_5y,
        "sales_4q": sales_4q,
        "profit_4q": profit_4q,
        "has_c3": bool(m.group("c3")),
    }


def build_gyoseki_tooltips(expr: str) -> Dict[str, str]:
    """gyoseki_quarity_expr から 3 セル分の tooltip 文字列を生成する (issue #204)。

    返り値キー: sales_growth / profit_growth / progress_diff
    データが無いセルは空文字。Jinja2 側で空なら title 属性を出さない想定。
    """
    parsed = parse_gyoseki_quarity_expr(expr)
    sales_tip = f"5年平均: {parsed['sales_5y']}" if parsed["sales_5y"] else ""
    profit_tip = f"5年平均: {parsed['profit_5y']}" if parsed["profit_5y"] else ""

    parts: List[str] = []
    if parsed["sales_4q"] and parsed["profit_4q"]:
        parts.append(f"4Q平均: 売上{parsed['sales_4q']} / 利益{parsed['profit_4q']}")
    if parsed["has_c3"]:
        parts.append("[3Q連続利益率向上]")
    progress_tip = " ".join(parts)

    return {
        "sales_growth": sales_tip,
        "profit_growth": profit_tip,
        "progress_diff": progress_tip,
    }


# ==================================================
# 株価 + RSライン 統合スパークライン (issue #227)
# ==================================================
# オニール式 IBD の RS ライン (個別株価/TOPIX 比率) を価格チャートに重ねる。
# 単位は非表示で「方向だけ見せる」思想に従い、各系列を min-max で 0-1 に
# 正規化してから同じ viewBox に描画する。
#
# - portfolio: 80x24px 3点 (t-20, t-5, t-0) の簡易チャート
# - detail   : 400x120px 20点フルチャート + 軸ガイド

_SPARK_LOOKBACK = 20  # 分析対象本数 (price_log/price_week_log の有効長)。mode により日足/週足
_SPARK_RECENT = 5     # 末尾強調期間 (mode により直近5営業日/5週)

# 配色: 株価 (緑=上昇/赤=下降/灰=横ばい)、RS (青=上昇/オレンジ=下降/灰=横ばい)
_PRICE_COLORS = {"up": "#2e7d32", "down": "#c62828", "flat": "#999"}
_PRICE_FADED = {"up": "#a5d6a7", "down": "#ef9a9a", "flat": "#ccc"}
_RS_COLORS = {"up": "#1976d2", "down": "#ef6c00", "flat": "#999"}
_RS_FADED = {"up": "#90caf9", "down": "#ffcc80", "flat": "#ccc"}
_BLUE_DOT = "#1976d2"

# portfolio ミニチャート: 20日株価騰落率による線色 (|r20| < 10% 灰 / < 20% 淡 / ≥ 20% 濃)
_MINI_LINE_NEUTRAL = "#999"
_MINI_LINE_POS_FAINT = "#9be29b"
_MINI_LINE_POS_STRONG = "#2e7d32"
_MINI_LINE_NEG_FAINT = "#f4c7c3"
_MINI_LINE_NEG_STRONG = "#c62828"

_FLAT_THRESHOLD_PERCENT_PER_DAY = 0.05  # 傾き |x| < 0.05%/日 は flat 扱い


def _mini_line_color_by_return(price_asc: List[float]) -> str:
    """portfolio ミニチャート用に 20日株価騰落率の符号と大きさで線色を決める。

    |r20| < 10% は灰 (中立)、10-20% は淡色、≥ 20% は濃色。
    データ不足 (2点未満 or 始値<=0) は灰。
    """
    if len(price_asc) < 2:
        return _MINI_LINE_NEUTRAL
    p0 = price_asc[0]
    p1 = price_asc[-1]
    if not isinstance(p0, (int, float)) or not isinstance(p1, (int, float)) or p0 <= 0:
        return _MINI_LINE_NEUTRAL
    r20_pct = (p1 / p0 - 1.0) * 100.0
    abs_r = abs(r20_pct)
    if abs_r < 10.0:
        return _MINI_LINE_NEUTRAL
    if r20_pct > 0:
        return _MINI_LINE_POS_FAINT if abs_r < 20.0 else _MINI_LINE_POS_STRONG
    return _MINI_LINE_NEG_FAINT if abs_r < 20.0 else _MINI_LINE_NEG_STRONG


def compute_slope_per_day(values: List[float]) -> Optional[float]:
    """末尾 n 点の線形回帰の傾きを末尾値で正規化した %/日 を返す。

    values は時系列の昇順 (古い→新しい) を期待する。
    n < 2 や末尾値が 0 の場合は None。
    """
    if not values or len(values) < 2:
        return None
    last = values[-1]
    if not last:
        return None
    n = len(values)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    numerator = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    denominator = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if denominator == 0:
        return None
    slope_abs = numerator / denominator
    return (slope_abs / last) * 100.0


def _slope_direction(slope_pct_per_day: Optional[float]) -> str:
    """傾き %/日 を up/down/flat に分類する。None は flat 扱い。"""
    if slope_pct_per_day is None:
        return "flat"
    if slope_pct_per_day > _FLAT_THRESHOLD_PERCENT_PER_DAY:
        return "up"
    if slope_pct_per_day < -_FLAT_THRESHOLD_PERCENT_PER_DAY:
        return "down"
    return "flat"


def normalize_minmax(values: List[float], height: float) -> List[float]:
    """min-max 正規化して SVG y 座標 (0 = top, height = bottom) に変換する。

    定数列 (min == max) は height/2 (中央) 一定で返す。
    SVG は y が下向きなので、最大値が上に来るよう反転する。
    """
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [height / 2.0] * len(values)
    span = hi - lo
    return [height - (v - lo) / span * height for v in values]


def to_log_scale(values: List[float]) -> List[float]:
    """株価を対数軸で表示するため自然対数に変換 (IBD MarketSmith 準拠)。

    対数軸では +X% の動きが価格レンジに関係なく同じ縦距離になるため、
    急騰急落銘柄でも「率」として正しい形が見える。
    0 以下の値や空入力は空リストを返す。
    """
    import math
    if not values:
        return []
    if any(v <= 0 for v in values):
        return []
    return [math.log(v) for v in values]


def to_base_index(values: List[float]) -> List[float]:
    """先頭値を 1.0 とする倍率列に変換する。

    各値を values[0] で割って [1.0, 1.05, 0.98, ...] のような形にする。
    issue #227: 株価と RSライン を共通スケールで重ねる前段。
    先頭値が 0 や負、または values が空なら空リストを返す。
    """
    if not values or values[0] <= 0:
        return []
    base = values[0]
    return [v / base for v in values]


def normalize_shared_y(
    series_list: List[List[float]], height: float
) -> List[List[float]]:
    """複数系列を共通スケールで SVG y 座標に変換する (issue #227)。

    各系列を to_base_index で「先頭=1.0」に揃えた後、全系列の min-max を
    共通に取って同じ y 範囲に落とす。これにより:
      - 期間先頭が完全に揃う (両線が同じ位置から始まる)
      - 期間内の増減幅の差がそのまま線の上下差として見える
    定数列のみで構成される場合は中央線一定で返す。
    """
    indexed = [to_base_index(s) for s in series_list]
    valid_points: List[float] = []
    for s in indexed:
        valid_points.extend(s)
    if not valid_points:
        return [[] for _ in series_list]
    lo = min(valid_points)
    hi = max(valid_points)
    if hi == lo:
        return [[height / 2.0] * len(s) for s in indexed]
    span = hi - lo
    return [
        [height - (v - lo) / span * height for v in s] if s else []
        for s in indexed
    ]


def _asc_series_from_log(log: List, count: int) -> List[float]:
    """price_log / rs_line のような (date, value) タプル列 (新しい順) から
    末尾 count 件を時系列昇順 (古い→新しい) の値だけのリストにする。"""
    if not log:
        return []
    sliced = log[:count]
    # 入力は新しい順。逆順にして昇順にする
    return [float(v) for _, v in reversed(sliced) if v is not None]


def _format_total_change(values: List[float], window: int) -> str:
    """末尾 window 営業日 (offset) における合計騰落率を +X.X% / — に整形。

    window=5 なら「5営業日前→今日」の変化なので、内部では window+1 点を取り
    tail[0]→tail[-1] を計算する。既存の (20,5) 指標 (offset=5) と一致させる。
    データ不足時は取得できるだけ取って計算する (例: 3点しかなくて window=5 → 全3点)。
    データ不足や 0除算の場合は "—"。
    """
    if not values or len(values) < 2 or window <= 0:
        return "—"
    needed = window + 1
    tail = values[-needed:] if len(values) > needed else values
    base = tail[0]
    if base == 0:
        return "—"
    pct = (tail[-1] - base) / base * 100
    return f"{pct:+.1f}%"


def _build_chart_tooltip(
    price_values: List[float],
    rs_values: List[float],
    has_blue_dot: bool,
    unit_label: str = "日",
) -> str:
    """チャート tooltip (title 属性向け) を生成する。

    20本 / 5本 の合計騰落率を見せる。平均ではなく合計なので、期間内の動きが
    そのまま % で読める (例: 「20で +5%, 5で -26%」のような乖離パターンが分かる)。
    unit_label は "日" (日足 mini) / "週" (週足 full) を切り替える。
    """
    lines = [
        f"株価: 20{unit_label} {_format_total_change(price_values, _SPARK_LOOKBACK)}, "
        f"5{unit_label} {_format_total_change(price_values, _SPARK_RECENT)}",
        f"RSライン: 20{unit_label} {_format_total_change(rs_values, _SPARK_LOOKBACK)}, "
        f"5{unit_label} {_format_total_change(rs_values, _SPARK_RECENT)}",
    ]
    if has_blue_dot:
        lines.append("新高値: ●")
    return "\n".join(lines)


def _svg_polyline(points: List[tuple], color: str, width: float, dasharray: Optional[str] = None) -> str:
    """polyline 1 本を SVG 文字列で返す (points が 2 点未満は空文字)。"""
    if len(points) < 2:
        return ""
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    dash = f' stroke-dasharray="{dasharray}"' if dasharray else ""
    return (
        f'<polyline points="{pts}" fill="none" '
        f'stroke="{color}" stroke-width="{width}"{dash} '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )


def _svg_circle(cx: float, cy: float, r: float, color: str) -> str:
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{color}"/>'


def _format_price_axis(value: float) -> str:
    """株価軸ラベル (円)。1000円超は K 表記、それ以下は整数。"""
    if value >= 10000:
        return f"{value / 1000:.1f}K"
    if value >= 1000:
        return f"{int(round(value))}"
    return f"{value:.1f}"


def _format_pct_axis(pct: float) -> str:
    """RS変化率ラベル (%)。+/- 付き、絶対値10%未満は小数1桁、それ以上は整数。"""
    if abs(pct) < 10:
        return f"{pct:+.1f}%"
    return f"{pct:+.0f}%"


def build_price_rs_chart_mini(
    price_log: List,
    rs_line: List,
    has_blue_dot: bool,
    width: int = 80,
    height: int = 24,
) -> tuple:
    """portfolio 用 3 点 (t-20, t-5, t-0) 簡易チャート SVG と tooltip を返す。

    RSライン (対TOPIX) のみを縦帯フルに使って描画する。株価の絶対方向は
    一覧の別列 (RS / 騰落率) で読めるため、ミニチャートは「対TOPIXの形」
    だけを大きく見せる方針 (情報絞り)。tooltip には引き続き株価情報も含める。

    Args:
        price_log: stocks_shelve['price_log'] (新しい順 [(date, close), ...])
        rs_line  : compute_rs_line() 戻り値 (新しい順 [(date, ratio), ...])
        has_blue_dot: RSライン新高値ならTrue
        width / height: SVG サイズ

    Returns:
        (svg_str, tooltip_str). データ不足 (RS 2 点未満) なら SVG は "—"。
    """
    price_asc = _asc_series_from_log(price_log, _SPARK_LOOKBACK)
    rs_asc = _asc_series_from_log(rs_line, _SPARK_LOOKBACK)

    if len(rs_asc) < 2:
        return ("—", "")

    tooltip = _build_chart_tooltip(price_asc, rs_asc, has_blue_dot)

    pad_x = 4
    pad_y = 3
    inner_w = width - pad_x * 2
    inner_h = height - pad_y * 2

    # 3 点: t-20 (= asc[0]), t-5 (= 5 営業日前, asc[-(_SPARK_RECENT+1)]), t-0 (= asc[-1])
    # _SPARK_RECENT=5 は「営業日数(offset)」の意味なので、点としては末尾から 6 本目を指す。
    def _three_points(asc: List[float]) -> Optional[List[float]]:
        if len(asc) < 2:
            return None
        t0 = asc[-1]
        if len(asc) >= _SPARK_RECENT + 1:
            t5 = asc[-(_SPARK_RECENT + 1)]
        else:
            t5 = asc[len(asc) // 2]
        t20 = asc[0]
        return [t20, t5, t0]

    rs_pts_raw = _three_points(rs_asc)
    if rs_pts_raw is None:
        return ("—", "")

    # x 座標は等間隔 3 点
    xs = [pad_x, pad_x + inner_w / 2, pad_x + inner_w]

    # 線色は 20日株価騰落率ベース (|r20| < 10% 灰 / < 20% 淡 / ≥ 20% 濃)
    line_color = _mini_line_color_by_return(price_asc)
    # 緑系 (株価プラス) は淡色で細く見えるため少し太くする
    is_green = line_color in (_MINI_LINE_POS_FAINT, _MINI_LINE_POS_STRONG)
    line_w = 2.0 if is_green else 1.5

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">']

    # RSライン (実線, 主役) - 縦帯フル
    ys = normalize_minmax(rs_pts_raw, inner_h)
    ys = [y + pad_y for y in ys]
    points = list(zip(xs, ys))
    parts.append(_svg_polyline(points, line_color, line_w))
    # 末尾点: Blue Dot or 通常マーカー (通常マーカーは線色連動)
    if has_blue_dot:
        parts.append(_svg_circle(points[-1][0], points[-1][1], 2.5, _BLUE_DOT))
    else:
        parts.append(_svg_circle(points[-1][0], points[-1][1], 1.6, line_color))

    parts.append("</svg>")
    return ("".join(parts), tooltip)


def build_price_rs_chart_full(
    price_log: List,
    rs_line: List,
    has_blue_dot: bool,
    width: int = 400,
    height: int = 120,
) -> tuple:
    """詳細ページ用 20 週フルチャート SVG と tooltip を返す (週足 20 本ベース)。

    株価と RSライン を「基準週=0% 起点の累積騰落率 (%)」に揃え、同一パネル・共通Y軸で重ねる。
      - 2本の縦差 ≒ TOPIX 騰落率 (TOPIXより強い/弱いが交差・乖離として直読できる)
      - 0% の水平基準線を薄く描画
      - 末尾 5 週部分は太く濃色で強調 (現状踏襲)
      - 軸ガイド (基準週 / 5週前 / 今日) と日付ラベルは現状踏襲
      - Blue Dot は RS ライン末尾の青丸 (r=4)
      - Y軸ラベルは共通スケール (灰色) で 1 系統のみ
      - 末尾現在値ラベルは系列色 (緑/青) で末尾点の左に表示

    末尾 1 本は Case A (両週足あり + 日足が両週足より新しい) のみ今週仮終値 (= 最新日足) になる。
    """
    price_asc_raw = _asc_series_from_log(price_log, _SPARK_LOOKBACK)
    rs_asc_raw = _asc_series_from_log(rs_line, _SPARK_LOOKBACK)

    if len(price_asc_raw) < 2 and len(rs_asc_raw) < 2:
        return ("", "")

    # tooltip は元の比率列ベース (現状踏襲)
    tooltip = _build_chart_tooltip(price_asc_raw, rs_asc_raw, has_blue_dot, unit_label="週")

    # 基準週を揃える: 両系列の末尾 (= 今日) は同じ前提なので min 長で末尾揃え。
    # これにより price_asc[0] と rs_asc[0] は必ず同じ「基準週」の値になり、
    # 縦差が常に TOPIX 騰落率の累積として読める。
    rs_available = len(rs_asc_raw) >= 2 and rs_asc_raw[0] > 0
    if rs_available:
        n_align = min(len(price_asc_raw), len(rs_asc_raw))
        price_asc = price_asc_raw[-n_align:]
        rs_asc = rs_asc_raw[-n_align:]
    else:
        # RS データなし: 株価のみで % 描画する
        price_asc = price_asc_raw
        rs_asc = []

    if len(price_asc) < 2 or price_asc[0] <= 0:
        return ("", "")

    # % 変換 (基準週=0%)
    p_base = price_asc[0]
    price_pct = [(p / p_base - 1.0) * 100.0 for p in price_asc]
    if rs_asc:
        r_base = rs_asc[0]
        rs_pct = [(r / r_base - 1.0) * 100.0 for r in rs_asc]
    else:
        rs_pct = []

    # Y軸ラベルのため左に余白。右側は末尾点インラインラベルに任せるので最小限。
    pad_left = 36
    pad_right = 8
    pad_y_top = 14
    pad_y_bottom = 14
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_y_top - pad_y_bottom
    pad_x = pad_left

    chart_top = pad_y_top
    chart_h = inner_h

    # X 座標: 表示本数 (基準週揃え後の長さ) を基準に右端揃え
    n = len(price_asc)
    step = inner_w / max(n - 1, 1)

    def _xs_for(length: int) -> List[float]:
        return [pad_x + inner_w - step * (length - 1 - i) for i in range(length)]

    # 共通 Y スケール (% 統一)
    all_pct = list(price_pct) + list(rs_pct)
    y_min = min(all_pct)
    y_max = max(all_pct)
    # 0% 基準線を必ず含める
    y_min = min(y_min, 0.0)
    y_max = max(y_max, 0.0)
    if y_max == y_min:
        # 完全フラット: ±1% のダミーレンジ
        y_min, y_max = -1.0, 1.0
    y_span = y_max - y_min

    def _y_for(pct: float) -> float:
        # SVG y は下向き。pct=y_max が chart_top, pct=y_min が chart_top+chart_h。
        return chart_top + chart_h - (pct - y_min) / y_span * chart_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" style="display:block;">'
    ]

    # 背景枠
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa" stroke="#e0e0e0"/>'
    )

    # 軸ガイド: t-20週前/t-5週前/今日 の縦線
    x_today = pad_x + inner_w
    x_t20 = pad_x
    x_t5_raw = pad_x + inner_w - step * _SPARK_RECENT
    x_t5 = max(x_t5_raw, pad_x)
    show_t5_guide = x_t5_raw >= pad_x + 1
    guide_xs = [x_t20, x_today]
    if show_t5_guide:
        guide_xs.insert(1, x_t5)
    for gx in guide_xs:
        parts.append(
            f'<line x1="{gx:.1f}" y1="{chart_top}" x2="{gx:.1f}" y2="{chart_top + chart_h}" '
            f'stroke="#e0e0e0" stroke-width="0.5"/>'
        )

    # 0% 基準線 (水平、薄い破線)
    y_zero = _y_for(0.0)
    parts.append(
        f'<line x1="{pad_x:.1f}" y1="{y_zero:.1f}" x2="{x_today:.1f}" y2="{y_zero:.1f}" '
        f'stroke="#ccc" stroke-width="0.5" stroke-dasharray="1,2"/>'
    )

    # 日付ラベル (price_log の日付を参照)
    # 基準週揃え後の表示窓は price_log[:n]
    try:
        today_label = price_log[0][0].strftime("%m/%d") if price_log else ""
        t5_idx = min(_SPARK_RECENT, len(price_log) - 1) if price_log else -1
        t20_idx = min(n - 1, len(price_log) - 1) if price_log else -1
        t5_label = price_log[t5_idx][0].strftime("%m/%d") if t5_idx >= 0 else ""
        t20_label = price_log[t20_idx][0].strftime("%m/%d") if t20_idx >= 0 else ""
    except Exception:
        today_label = t5_label = t20_label = ""
    label_y = height - 2
    parts.append(
        f'<text x="{x_t20:.1f}" y="{label_y}" font-size="9" fill="#888" text-anchor="start">{t20_label}</text>'
    )
    if show_t5_guide and t5_label:
        parts.append(
            f'<text x="{x_t5:.1f}" y="{label_y}" font-size="9" fill="#888" text-anchor="middle">{t5_label}</text>'
        )
    parts.append(
        f'<text x="{x_today:.1f}" y="{label_y}" font-size="9" fill="#888" text-anchor="end">{today_label}</text>'
    )

    # 凡例 (上端)
    parts.append(
        '<text x="' + str(pad_x) + '" y="10" font-size="9" fill="#666">'
        '<tspan fill="#2e7d32">━ 株価 (騰落率%)</tspan>'
        '<tspan dx="6" fill="#1976d2" font-style="italic">┄ RSライン (対TOPIX, 騰落率%)</tspan>'
        '</text>'
    )

    # 共通 Y 軸ラベル (灰色, 系列に紐づかない)
    label_x = pad_left - 2
    parts.append(
        f'<text x="{label_x}" y="{chart_top + 3:.1f}" font-size="8" '
        f'fill="#666" text-anchor="end">{_format_pct_axis(y_max)}</text>'
    )
    parts.append(
        f'<text x="{label_x}" y="{chart_top + chart_h - 1:.1f}" font-size="8" '
        f'fill="#666" text-anchor="end">{_format_pct_axis(y_min)}</text>'
    )

    _recent_pts = _SPARK_RECENT + 1

    # 株価線 (実線)
    # 方向判定は元系列 (price_asc) で算出 (% 系列だと末尾値が 0 近傍になると flat に流れやすい)
    price_slope_full = compute_slope_per_day(price_asc)
    price_slope_recent = (
        compute_slope_per_day(price_asc[-_recent_pts:])
        if len(price_asc) >= _recent_pts
        else None
    )
    price_dir_full = _slope_direction(price_slope_full)
    price_dir_recent = _slope_direction(price_slope_recent)

    xs = _xs_for(n)
    price_ys = [_y_for(p) for p in price_pct]
    price_points = list(zip(xs, price_ys))
    parts.append(_svg_polyline(price_points, _PRICE_FADED[price_dir_full], 1.5))
    if len(price_points) >= _recent_pts:
        parts.append(_svg_polyline(price_points[-_recent_pts:], _PRICE_COLORS[price_dir_recent], 2.2))
    parts.append(_svg_circle(price_points[-1][0], price_points[-1][1], 2.5, _PRICE_COLORS[price_dir_recent]))

    # 株価末尾現在値ラベル
    price_now_x = price_points[-1][0]
    price_now_y = price_points[-1][1]
    parts.append(
        f'<text x="{price_now_x - 4:.1f}" y="{price_now_y + 3:.1f}" font-size="9" '
        f'fill="#2e7d32" font-weight="bold" text-anchor="end">{_format_pct_axis(price_pct[-1])}</text>'
    )

    # RS線 (点線)
    if rs_pct:
        rs_slope_full = compute_slope_per_day(rs_asc)
        rs_slope_recent = (
            compute_slope_per_day(rs_asc[-_recent_pts:])
            if len(rs_asc) >= _recent_pts
            else None
        )
        rs_dir_full = _slope_direction(rs_slope_full)
        rs_dir_recent = _slope_direction(rs_slope_recent)

        rs_ys = [_y_for(r) for r in rs_pct]
        rs_points = list(zip(xs, rs_ys))
        parts.append(_svg_polyline(rs_points, _RS_FADED[rs_dir_full], 1.0, dasharray="2,2"))
        if len(rs_points) >= _recent_pts:
            parts.append(_svg_polyline(rs_points[-_recent_pts:], _RS_COLORS[rs_dir_recent], 1.5, dasharray="2,1"))
        if has_blue_dot:
            parts.append(_svg_circle(rs_points[-1][0], rs_points[-1][1], 4.0, _BLUE_DOT))
        else:
            parts.append(_svg_circle(rs_points[-1][0], rs_points[-1][1], 2.0, _RS_COLORS[rs_dir_recent]))

        # RS末尾現在値ラベル (株価ラベルと重なる場合は上下にずらす)
        rs_now_x = rs_points[-1][0]
        rs_now_y = rs_points[-1][1]
        offset = 8 if has_blue_dot else 4
        # 株価ラベルと縦に近い場合は RS ラベルを少し上下にずらす
        rs_label_dy = 3
        if abs(rs_now_y - price_now_y) < 10:
            # 株価が上なら RS は下、株価が下なら RS は上にずらす
            rs_label_dy = 12 if rs_now_y >= price_now_y else -6
        parts.append(
            f'<text x="{rs_now_x - offset:.1f}" y="{rs_now_y + rs_label_dy:.1f}" font-size="9" '
            f'fill="#1976d2" font-weight="bold" text-anchor="end">{_format_pct_axis(rs_pct[-1])}</text>'
        )

    parts.append("</svg>")
    return ("".join(parts), tooltip)


def build_stock_chart_payload(
    stock: Dict[str, Any],
    market_db: Optional[Dict[str, Any]],
    mode: str = "mini",
) -> Dict[str, Any]:
    """stock + market_db からチャート用 svg + tooltip + blue_dot 情報を組み立てる。

    mode: "mini" (portfolio 用、日足ベース) or "full" (detail 用、週足ベース issue #239)
    market_db が None の場合は RS ラインが空で描画される (株価のみ)。

    mode="full" では price_week_log を入力に使い、銘柄/TOPIX 両週足と両日足が
    揃った Case A のみ末尾に「今週仮終値」(= 最新日足) を 1 本追加する。
    片肺 (Case B) は週足 20 本のみ、両週足空 (Case C, 移行期間) は空 SVG。
    """
    from make_stock_db import (
        compute_rs_line, compute_rs_line_weekly,
        compute_rs_line_weekly_new_high_5d,
    )  # 遅延 import

    # Blue Dot は full/mini 共通で週足ベース新高値判定 (issue #239):
    # 直近5日の日足RS最高値 > 過去20週の週足RS最高値
    has_blue_dot = False
    if market_db is not None and stock:
        try:
            has_blue_dot = compute_rs_line_weekly_new_high_5d(stock, market_db)
        except Exception:  # noqa: BLE001
            has_blue_dot = False

    if mode == "full":
        price_log = _build_full_week_series(stock, market_db)
        rs_line: List = []
        if market_db is not None and stock:
            try:
                rs_line = compute_rs_line_weekly(stock, market_db)
                rs_line = _append_provisional_rs(rs_line, stock, market_db)
            except Exception:  # noqa: BLE001
                rs_line = []
        svg, tooltip = build_price_rs_chart_full(price_log, rs_line, has_blue_dot)
    else:
        price_log = (stock or {}).get("price_log") or []
        rs_line = []
        if market_db is not None and stock:
            try:
                rs_line = compute_rs_line(stock, market_db)
            except Exception:  # noqa: BLE001
                rs_line = []
        svg, tooltip = build_price_rs_chart_mini(price_log, rs_line, has_blue_dot)
    return {"svg": svg, "tooltip": tooltip, "blue_dot": has_blue_dot}


def _is_provisional_eligible(stock, market_db):
    """今週仮終値を追加すべきかと、追加用の (date, stock_close, topix_close) を返す。

    銘柄週足の最新 ISO 週より日足 (銘柄/TOPIX) が新しい週なら provisional 追加可。
    TOPIX 週足が当日分まで進んでいる非対称ケース (週初に make_market_db が
    先行して当日分を週足末尾に積むケース) でも、銘柄週足を基準にすれば
    日足側の今週分を仮終値として安全に追加できる。
    銘柄週足が空 (Case C) は追加しない (build_price_rs_chart_full の早期 return で空 SVG)。
    """
    if not stock:
        return None
    stock_week = stock.get("price_week_log") or []
    if not stock_week:
        return None  # Case C
    daily_stock = stock.get("price_log") or []
    topix = (market_db or {}).get("topix") or {}
    daily_topix = topix.get("price_log") or []
    if not daily_stock or not daily_topix:
        return None
    stock_week_iso = stock_week[0][0].isocalendar()[:2]
    if not (daily_stock[0][0].isocalendar()[:2] > stock_week_iso
            and daily_topix[0][0].isocalendar()[:2] > stock_week_iso):
        return None
    return (daily_stock[0][0], float(daily_stock[0][1]), float(daily_topix[0][1]))


def _build_full_week_series(stock, market_db):
    """detail full チャート用の株価系列 (週足 + Case A/B 時のみ今週仮終値) を返す。

    issue #239: 週足台帳に最新日足を「今週仮終値」として末尾追加する。
    銘柄/TOPIX の週足最新 ISO 週と日足の ISO 週を比較し、両日足が両週足より新しい
    ISO 週にあるときだけ追加する (RS の片肺回避)。両週足が空 (Case C, 移行期間) なら
    週足のみを返し、build_price_rs_chart_full の 2 点未満早期 return で空 SVG になる。
    """
    if not stock:
        return []
    series = list(stock.get("price_week_log") or [])
    eligible = _is_provisional_eligible(stock, market_db)
    if eligible is None:
        return series
    dt, stock_close, _ = eligible
    return [(dt, stock_close)] + series


def _append_provisional_rs(rs_line, stock, market_db):
    """rs_line の末尾 (= 先頭, 日付降順) に今週仮終値分の rs 点を追加する。

    _build_full_week_series と同じ判定条件 (両週足と両日足の ISO 週比較) を踏む。
    """
    eligible = _is_provisional_eligible(stock, market_db)
    if eligible is None:
        return rs_line
    dt, stock_close, topix_close = eligible
    try:
        rs_val = stock_close / topix_close
    except ZeroDivisionError:
        return rs_line
    return [(dt, rs_val)] + list(rs_line)


def build_trend_info(stock: Dict[str, Any]) -> Dict[str, Any]:
    """portfolio_list / detail.html / 市場セクション共通のトレンド表示情報を組み立てる。

    返り値の各キー:
        expr: ◎ / ◯ / ▲ / △ / — の単一記号
        tooltip: 不通過項目 (◯のときのみ) + 10WMA乖離率を改行で結合した文字列
        kairi_gauge_svg: -25%〜+25% のバーゲージ + 中央記号オーバーレイ SVG
    """
    from ks_util import (
        trend_symbol_from_misses, format_kairi_wma10, kairi_gauge_svg,
    )
    # trend_template が未生成 / 欠損している銘柄を「◎ (完全通過)」と誤表示しないよう、
    # 非 list を [] に変換せず、未評価として trend_symbol_from_misses に渡して "—" を返す。
    misses = (stock or {}).get("trend_template")
    expr = trend_symbol_from_misses(misses) if stock else "—"
    # 不通過項目の tooltip は ◯ (1-2件不通過) のときだけ意味があるので、それ以外は空にする。
    # ◎=全通過で項目なし、▲/△=不通過項目が多くノイズ、—=未評価。
    tooltip_src = misses if (expr == "◯" and isinstance(misses, list)) else []
    raw = (stock or {}).get("price_kairi_wma10")
    kairi_raw = raw if isinstance(raw, (int, float)) else None
    kairi_str = format_kairi_wma10(kairi_raw) or "—"
    tooltip_lines = []
    if tooltip_src:
        tooltip_lines.append("不通過: " + ",".join(tooltip_src))
    tooltip_lines.append("10WMA乖離: " + kairi_str)
    return {
        "expr": expr,
        "tooltip": "\n".join(tooltip_lines),
        "kairi_gauge_svg": kairi_gauge_svg(kairi_raw, expr),
    }


def _extract_indicators_for_portfolio(stock: Dict[str, Any]) -> Dict[str, Any]:
    """stocks_shelve の dict から portfolio 一覧表示用の指標を抽出する。

    表示用文字列 (なければ "—") に加え、色判定用の生値 *_raw と派生フィールドも
    同時に返す (issue #177 条件付き書式移植のため)。
    """
    if not stock:
        return {
            "rank": None,
            "kessanbi_md": "—",
            "kessanbi_raw": None,
            "per": "—",
            "per_raw": None,
            "market_cap": "—",
            "market_cap_raw": None,
            "market_cap_category": None,
            "dividend": "—",
            "dividend_raw": None,
            "rs": "—",
            "rs_raw": None,
            "sales_growth": "—",
            "sales_growth_raw": None,
            "profit_growth": "—",
            "profit_growth_raw": None,
            "quarter": "—",
            "progress_diff": "—",
            "progress_diff_eiri_raw": None,
            "trend_template": "—",
            "trend_template_tooltip": "—",
            "kairi_gauge_svg": "",
            "tags": "—",
            "spr_gauge": {"svg": "—", "tooltip": ""},
            "theoretical_diff": "—",
            "theoretical_diff_raw": None,
            "gyoseki_quarity_expr": "",
            "gyoseki_tooltips": {"sales_growth": "", "profit_growth": "", "progress_diff": ""},
            "gyoseki": {},
            "indicators_raw": {},
        }

    shihyo = stock.get("shihyo") or {}

    # 順位は stock_rank_log の最新値 (= 直近更新時点での順位)
    rank_log = stock.get("stock_rank_log") or []
    rank = rank_log[0][1] if rank_log else None

    per = shihyo.get("PER")
    market_cap = stock.get("market_cap") or shihyo.get("jikasogaku")
    dividend_yield = shihyo.get("dividend_yield")
    # RS 列は code_rank.csv の「モメンタム(現在.20日比/5日比)」列の先頭値 (0〜100 の momentum_pt)
    momentum_pt = stock.get("momentum_pt")
    sales_growth, profit_growth = _annual_growth(stock)
    quarter_label, progress_diff = _progress_quarter_and_diff(stock)

    trend_info = build_trend_info(stock)

    market_cap_raw = market_cap if isinstance(market_cap, (int, float)) else None
    gyoseki_quarity_expr = _gyoseki_quarity_expr_safe(stock)

    return {
        "rank": rank if isinstance(rank, int) else None,
        "kessanbi_md": _format_kessanbi_md(stock.get("kessanbi")),
        "kessanbi_raw": _parse_kessanbi(stock.get("kessanbi", "")),
        "per": _format_per(per),
        "per_raw": per if isinstance(per, (int, float)) else None,
        # 表示はカテゴリ文字列 ("極小"〜"特大")。スプシの IFS 式に対応 (issue #177)
        "market_cap": _market_cap_category(market_cap_raw) or "—",
        "market_cap_raw": market_cap_raw,
        "market_cap_category": _market_cap_category(market_cap_raw),
        # "%" 表記は列ヘッダ側 ("配当(%)") に集約 (issue #177)、値は数値のみ (小数1桁)
        "dividend": f"{dividend_yield:.1f}" if isinstance(dividend_yield, (int, float)) else "—",
        "dividend_raw": dividend_yield if isinstance(dividend_yield, (int, float)) else None,
        "rs": f"{int(momentum_pt)}" if isinstance(momentum_pt, (int, float)) else "—",
        "rs_raw": int(momentum_pt) if isinstance(momentum_pt, (int, float)) else None,
        "sales_growth": f"{int(sales_growth)}" if isinstance(sales_growth, (int, float)) else "—",
        "sales_growth_raw": sales_growth if isinstance(sales_growth, (int, float)) else None,
        "profit_growth": f"{int(profit_growth)}" if isinstance(profit_growth, (int, float)) else "—",
        "profit_growth_raw": profit_growth if isinstance(profit_growth, (int, float)) else None,
        "quarter": quarter_label,
        "progress_diff": progress_diff,
        "progress_diff_eiri_raw": _progress_diff_eiri_raw(stock),
        "trend_template": trend_info["expr"],
        "trend_template_tooltip": trend_info["tooltip"],
        "kairi_gauge_svg": trend_info["kairi_gauge_svg"],
        "tags": _format_tags(stock),
        "spr_gauge": _build_spr_gauge_for_stock(stock),
        "theoretical_diff": _format_theoretical_diff(stock),
        "theoretical_diff_raw": _theoretical_diff_raw(stock),
        "gyoseki_quarity_expr": gyoseki_quarity_expr,
        # issue #204: 売上成長/利益成長/進捗率乖離 列の tooltip
        "gyoseki_tooltips": build_gyoseki_tooltips(gyoseki_quarity_expr),
        "gyoseki": {
            "isKonki": stock.get("isKonki"),
        },
        "indicators_raw": stock,
    }


# ==================================================
# 条件付き書式 (issue #177): スプシ「保有銘柄」シートの色分けを移植
# 詳細は doc/PORTFOLIO_COLOR_RULES.md を参照
# ==================================================

PORTFOLIO_COLORS = {
    # ポジティブ系
    "薄黄": "#fce8b2",   # 良 (PER割安、配当>3、RS≧70 等)
    "濃黄": "#fbbc04",   # 強良 (順位<300、配当≧5、RS>80 等)
    "赤":   "#ea4335",   # 注目
    "緑":   "#d4f4d4",   # 株価的にプラス (需給、MA乖離率)
    # ネガティブ系
    "青":   "#4285f4",   # 警告 (強)
    "水色": "#6fa8dc",   # 薄警告 (青の弱い版)
    "薄赤": "#f4c7c3",   # 株価的にマイナス (需給、MA乖離率)
    # 中立 (データ状態)
    "薄灰": "#cccccc",   # データ古い (14日以上)
    "濃灰": "#999999",   # データ古い (1ヶ月以上)
}

def _parse_research_update_md(md_str: Optional[str], today: date) -> Optional[date]:
    """'4/27' を date オブジェクトにする。today より未来なら去年扱い。"""
    if not md_str or md_str == "—":
        return None
    try:
        m, d = md_str.split("/")
        candidate = date(today.year, int(m), int(d))
        if candidate > today:
            candidate = date(today.year - 1, int(m), int(d))
        return candidate
    except (ValueError, AttributeError):
        return None


def compute_cell_styles(row: Dict[str, Any], today: Optional[date] = None) -> Dict[str, str]:
    """row の生値から各セルの inline style 文字列を返す (issue #177)。

    Args:
        row: list_portfolio_with_indicators が組み立てた表示用 dict (raw フィールド含む)
        today: 基準日 (省略時は date.today())。
            ※ CLAUDE.md L28 は日付判定に get_price_day() を規約化しているが、
            色付けは UI 補助で日単位粒度で十分なため本機能のみ date.today() を許可
            (ユーザーと合意済み)。詳細は .claude/plans/issue-177-portfolio-color-rules.md §4-1。

    Returns:
        dict[列名, style 文字列]。色なしの列は dict に含めない。
        例: {"per": "background:#fce8b2", "rs": "background:#fbbc04",
             "tags": "background:#ea4335;color:#fff"}
    """
    if today is None:
        today = date.today()
    styles: Dict[str, str] = {}
    bg = lambda color: f"background:{PORTFOLIO_COLORS[color]}"  # noqa: E731
    bg_with_white = lambda color: f"background:{PORTFOLIO_COLORS[color]};color:#fff"  # noqa: E731

    # --- 順位 (ルール 14, 31): rank < 300 → 濃黄
    rank = row.get("rank")
    if isinstance(rank, int) and rank < 300:
        styles["rank"] = bg("濃黄")

    # --- 売上成長 (ルール 17): >= 30 → 薄黄
    sg = row.get("sales_growth_raw")
    if isinstance(sg, (int, float)) and sg >= 30:
        styles["sales_growth"] = bg("薄黄")

    # --- 利益成長 (ルール 17): >= 30 → 薄黄
    pg = row.get("profit_growth_raw")
    if isinstance(pg, (int, float)) and pg >= 30:
        styles["profit_growth"] = bg("薄黄")

    # --- PER (ルール 16): (利益成長% + 配当%) / PER > 1 → 薄黄 (PEG的指標、割安)
    # スプシ式 (I+L)/J>1 ではセル空欄は 0 として評価されるため、配当が None でも 0 扱い
    per_raw = row.get("per_raw")
    div_raw = row.get("dividend_raw")
    div_for_peg = div_raw if isinstance(div_raw, (int, float)) else 0.0
    if (
        isinstance(per_raw, (int, float)) and per_raw > 0
        and isinstance(pg, (int, float))
        and (pg + div_for_peg) / per_raw > 1
    ):
        styles["per"] = bg("薄黄")

    # --- 理論株価乖離 (ルール 15): > 50 → 薄黄
    theo = row.get("theoretical_diff_raw")
    if isinstance(theo, (int, float)) and theo > 50:
        styles["theoretical_diff"] = bg("薄黄")

    # --- 配当 (ルール 32, 33): >= 5 濃黄 / > 3 薄黄
    if isinstance(div_raw, (int, float)):
        if div_raw >= 5:
            styles["dividend"] = bg("濃黄")
        elif div_raw > 3:
            styles["dividend"] = bg("薄黄")

    # --- 進捗率乖離: <C3>タグ 赤(注目) / 営利乖離≧20 濃黄 / 両該当は左右分割
    quarity = row.get("gyoseki_quarity_expr") or ""
    eiri_raw = row.get("progress_diff_eiri_raw")
    has_c3 = "<C3>" in quarity
    hit_eiri = isinstance(eiri_raw, (int, float)) and eiri_raw >= 20
    if has_c3 and hit_eiri:
        styles["progress_diff"] = (
            f"background:linear-gradient(to right,"
            f"{PORTFOLIO_COLORS['赤']} 50%,{PORTFOLIO_COLORS['濃黄']} 50%)"
        )
    elif has_c3:
        styles["progress_diff"] = bg_with_white("赤")
    elif hit_eiri:
        styles["progress_diff"] = bg("濃黄")

    # --- 決算日 (ルール 22, 23): 更新日±1ヶ月+3Q 濃黄 / ±1ヶ月のみ 薄黄
    # 翻訳表の業務的意味は「更新日 ±1ヶ月以内」なので絶対日数差で両側判定 (codex P2 対応)
    kessanbi_raw = row.get("kessanbi_raw")
    last_update_md = (row.get("memo") or {}).get("last_research_update")
    last_update_dt = _parse_research_update_md(last_update_md, today)
    quarter = row.get("quarter") or ""
    if (
        isinstance(kessanbi_raw, date)
        and isinstance(last_update_dt, date)
        and abs((kessanbi_raw - last_update_dt).days) <= 31
    ):
        if quarter == "3Q":
            styles["kessanbi_md"] = bg("濃黄")
        else:
            styles["kessanbi_md"] = bg("薄黄")

    # --- 更新日 (ルール 1, 8): 1ヶ月以上前 濃灰 (>=30日) / 14日以上前 薄灰
    if isinstance(last_update_dt, date):
        diff_days = (today - last_update_dt).days
        if diff_days >= 30:
            styles["last_research_update"] = bg("濃灰")
        elif diff_days >= 14:
            styles["last_research_update"] = bg("薄灰")

    # --- ステージ: 2S=濃黄 / 3S=水色 / 4S=青、2つ併存は左右分割 (強い順に左)
    stage = (row.get("memo") or {}).get("stage") or ""
    stage_color_map = {"4S": "青", "3S": "水色", "2S": "濃黄"}
    stage_hits = [s for s in ("4S", "3S", "2S") if s in stage]
    if len(stage_hits) >= 2:
        left, right = stage_hits[0], stage_hits[1]
        styles["stage"] = (
            f"background:linear-gradient(to right,"
            f"{PORTFOLIO_COLORS[stage_color_map[left]]} 50%,"
            f"{PORTFOLIO_COLORS[stage_color_map[right]]} 50%)"
        )
    elif stage_hits:
        single = stage_hits[0]
        if single == "4S":
            styles["stage"] = bg_with_white("青")
        else:
            styles["stage"] = bg(stage_color_map[single])

    # --- RS (ルール 27, 28): > 80 濃黄 / >= 70 薄黄
    rs_raw = row.get("rs_raw")
    if isinstance(rs_raw, (int, float)):
        if rs_raw > 80:
            styles["rs"] = bg("濃黄")
        elif rs_raw >= 70:
            styles["rs"] = bg("薄黄")

    # --- トレンド (ルール 24, 25, 26): "◎" 濃黄 / "◯" 薄黄 / 空欄("—") 水色
    trend = row.get("trend_template") or ""
    if "◎" in trend:
        styles["trend_template"] = bg("濃黄")
    elif "◯" in trend:
        styles["trend_template"] = bg("薄黄")
    elif not trend or trend == "—":
        styles["trend_template"] = bg("水色")

    # --- シグナル (ルール 2-7): 強い色から順に評価
    tags = row.get("tags") or ""
    if any(c in tags for c in ("ポ", "ブ", "最")):
        styles["tags"] = bg_with_white("赤")
    elif any(c in tags for c in ("警", "売")):
        styles["tags"] = bg_with_white("青")
    elif "押" in tags:
        styles["tags"] = f"color:{PORTFOLIO_COLORS['青']}"

    # --- 時価総額 (ルール 29, 30): カテゴリ "中" / "大" → 薄黄 (極小/小/特大は色なし)
    cat = row.get("market_cap_category")
    if cat in ("中", "大"):
        styles["market_cap"] = bg("薄黄")

    return styles
