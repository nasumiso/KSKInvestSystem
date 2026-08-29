"""
DB読み書きヘルパー。

research_shelve のデータ取得・更新をWebアプリ用にラップする。
排他制御は research_shelve._flock() を共用し、Web側とバッチ側で
同じロックファイルを取ることでプロセス間の安全な共存を保証する。
"""

import html
import csv
import json
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from db_shelve import STOCKS_SHELVE, ShelveDB
from html_sanitizer import sanitize_html
from ks_util import get_price_day, log_warning
from research_shelve import (
    get_research_record,
    upsert_research_record,
    create_research_record,
    create_snapshot,
    to_date_yy_m,
    list_research_records,
    sort_shikiho_comments_desc,
    validate_code_s,
    normalize_code_s,
    validate_rating,
    _flock,
    _normalize_chat_links,
    normalize_kessan_post_price_changes,
    VALID_RATINGS,
    VALID_EXPECTATIONS,
    MAX_KESSAN_COMMENTS,
    KESSAN_REACTION_PERIODS,
)

_RATING_SORT_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "E": 5}
_TREND_TEMPLATE_CONDITIONS = [
    "pr>ma10", "pr>ma30,40", "ma30>ma40", "ma40Up",
    "ma10>ma30,40", "high(low)52", "RS",
]
_STAGE2_CORE_MISSES = {
    "pr>ma30,40", "ma30>ma40", "ma40Up",
}


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


def _backfill_entry_reactions(
    entry: Dict[str, Any],
    log: List,
    dt: date,
) -> Dict[str, str]:
    """1エントリの post_price_changes の空き期間を price_log から補完する。

    entry["post_price_changes"] を in-place で埋め、今回新たに埋まった期間だけを
    {key: 値} で返す (永続化ターゲット用)。何も埋まらなければ空 dict。
    """
    existing = entry.get("post_price_changes") or {}
    calculated = _price_reactions_from_log(log, dt)
    newly_filled: Dict[str, str] = {}
    for key, _ in KESSAN_REACTION_PERIODS:
        if not existing.get(key) and calculated.get(key):
            existing[key] = calculated[key]
            newly_filled[key] = calculated[key]
    entry["post_price_changes"] = existing
    return newly_filled


def _backfill_post_price_changes_for_entries(
    code_s: str,
    entries: List[Dict[str, Any]],
) -> None:
    """過去エントリの post_price_changes に欠損期間があれば price_log から補完する。

    entry dict を in-place で更新し、新たに埋まった反応率は 1d と同じく確定値として
    shelve に永続化する (price_log の30日ウィンドウから決算日が外れても消えないように)。
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

    normalized = normalize_code_s(code_s)
    log = _bulk_price_logs([code_s]).get(normalized, [])
    if not log:
        return
    ppc_persist_targets: List[Tuple[str, str, int, Dict[str, str]]] = []
    for entry, dt in targets:
        newly_filled = _backfill_entry_reactions(entry, log, dt)
        if newly_filled:
            ppc_persist_targets.append((
                normalized,
                entry.get("kessanbi", ""),
                int(entry.get("quarter", 0) or 0),
                newly_filled,
            ))
    if ppc_persist_targets:
        _persist_kessan_post_price_changes(ppc_persist_targets)


def get_stock_data(code_s: str) -> Dict[str, Any]:
    """stocks_shelve から1銘柄のデータを取得する。

    存在しない場合は空 dict を返す（テンプレート側で安全に参照可能）。
    今後 detail view に stocks_shelve のフィールドを追加する際は、
    この関数経由で取得しテンプレートに渡す。
    """
    normalized = normalize_code_s(code_s)
    with ShelveDB(STOCKS_SHELVE) as db:
        return db.get(normalized) or {}


def _latest_kessan_date_yy_m(stock: Dict[str, Any]) -> str:
    """stock の直近決算イベント日 (実績日/修正日のうち新しい方) を "YY.M.D" で返す。

    kessan_jisseki_date (発表実績日) と kessan_mod_date (修正日) を比較し、
    新しい方を採用する。どちらも無い/不正なら空文字を返す。
    新規追加は手動操作で「今の業績」を撮る用途のため、kessanbi (次回予定日に
    上書きされ得る) は使わず、実績日が無ければ呼び出し側が取得日にフォール
    バックする。決算ウィンドウ判定を行う B経路 (_collect_trigger_dates) とは
    別の用途・別ロジック。
    """
    latest = None
    for date_field in ("kessan_jisseki_date", "kessan_mod_date"):
        date_str = stock.get(date_field, "")
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d").date()
        except ValueError:
            continue
        if latest is None or dt > latest:
            latest = dt
    if latest is None:
        return ""
    return to_date_yy_m(latest)


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
        acquired_date = to_date_yy_m(today)

        # 業績 date_yy_m は直近の決算イベント日 (実績日/修正日のうち新しい方)。
        # 取得できなければ取得日にフォールバック (dedup キーが必要なため)。
        date_yy_m = _latest_kessan_date_yy_m(stock) or acquired_date

        snapshot = create_snapshot(
            date_yy_m,
            acquired_date=acquired_date,
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
    # ・ → <br> 変換は廃止。SKILL.md 側で ・ 列挙禁止（番号/- リスト使用）とすることで根本解決。
    # 「+」「→」の前に <wbr> を挟んで、文が長くても自然な位置で折り返せるようにする。
    # skill 出力で「日経+1.9%+KOSPI+4%+Samsungスト中止」のような連結が頻出するため。
    html = re.sub(r"(?<=[ぁ-んァ-ヶー一-龯%])([+→])", r"<wbr>\1", html)
    # 本文中の脚注記号 ⟨N⟩ (N=1〜2桁) を Sources へジャンプする anchor link に変換。
    # 旧履歴の `[99]` (構成銘柄数) と衝突しないよう ⟨⟩ (U+27E8/U+27E9) を採用。
    html = re.sub(
        r"⟨(\d{1,2})⟩",
        r'<a href="#thn-src-\1" class="thn-footnote">[\1]</a>',
        html,
    )
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
        # 各 <li>[N] ...</li> に id="thn-src-N" を付け、本文の脚注からジャンプ可能にする。
        body = re.sub(
            r'<li>\s*\[(\d{1,2})\]\s*',
            r'<li id="thn-src-\1">[\1] ',
            body,
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


def _today_analysis_date() -> str:
    """当日の暦日を分析日形式 "YY/M/D" (ゼロ埋めなし) で返す。

    分析日は「ユーザーが分析作業を行った日」の記録 (表示専用) なので、
    価格基準日 (ks_util.get_price_day) ではなく暦日を使う。
    """
    t = date.today()
    return f"{t.year % 100}/{t.month}/{t.day}"


def save_memo(code_s: str, form_data: dict) -> None:
    """手動メモフィールドを更新する。

    対象: overall_rating, institutional_comment, memo, inago_origin, openwork, cramer
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

        old_rating = record.get("overall_rating", "")
        old_institutional_comment = record.get("institutional_comment", "")
        old_memo = record.get("memo", "")
        old_inago_origin = record.get("inago_origin", "")
        old_openwork = record.get("openwork", "")
        old_cramer = record.get("cramer", "")

        new_rating = form_data.get("overall_rating", "")
        validate_rating(new_rating)
        record["overall_rating"] = new_rating
        record["institutional_comment"] = form_data.get(
            "institutional_comment", ""
        )
        record["memo"] = sanitize_html(_markdown_to_html(form_data.get("memo", "")))
        record["inago_origin"] = form_data.get("inago_origin", "")
        record["openwork"] = sanitize_html(_markdown_to_html(form_data.get("openwork", "")))
        record["cramer"] = form_data.get("cramer", "")
        manual_fields_changed = any([
            record["overall_rating"] != old_rating,
            record["institutional_comment"] != old_institutional_comment,
            record["memo"] != old_memo,
            record["inago_origin"] != old_inago_origin,
            record["openwork"] != old_openwork,
            record["cramer"] != old_cramer,
        ])

        # 分析日: 手動編集 (フォーム値が既存値から変化) を最優先し、
        # 触られていなければ手動メモ各項目の実変更時に当日へ自動更新する。
        # 旧データは年なし形式 ("11/13") があり、フォーム往復の年補完だけで
        # 手動編集と誤判定しないよう、比較は両辺とも正規化後で行う
        submitted_date = (
            _normalize_analysis_date(form_data["analysis_date_raw"])
            if "analysis_date_raw" in form_data
            else None
        )
        date_dirty = bool((form_data.get("analysis_date_raw__dirty") or "").strip())
        existing_date = _normalize_analysis_date(record.get("analysis_date_raw", "") or "")
        if date_dirty and submitted_date is not None and submitted_date != existing_date:
            record["analysis_date_raw"] = submitted_date
        elif manual_fields_changed:
            record["analysis_date_raw"] = _today_analysis_date()

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

        old_overview = record.get("overview", "")
        old_comments = list(record.get("shikiho_comments") or [])
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
        if record["overview"] != old_overview or comments != old_comments:
            record["analysis_date_raw"] = _today_analysis_date()

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
        changed = False
        for snap in snapshots:
            date = snap.get("date_yy_m", "")
            form_key = f"ir_comment_{date}"
            if form_key in form_data:
                new_comment = sanitize_html(_markdown_to_html(form_data[form_key]))
                if new_comment != snap.get("ir_comment", ""):
                    changed = True
                snap["ir_comment"] = new_comment

        record["snapshots"] = snapshots
        # IR分析コメントが実際に変化した保存では分析日を当日へ自動更新する
        if changed:
            record["analysis_date_raw"] = _today_analysis_date()
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
# 外部チャットリンク (chat_links) の CRUD (issue #265)
# =======================================================
# URL の http/https バリデーションはルート側で実施する
# (save_corporate_url_override と同じ層分担)。helpers は index 範囲・
# レコード存在チェックのみ行い、_flock で read-modify-write を直列化する。

def _get_record_for_chat_link(normalized: str) -> Dict[str, Any]:
    """chat_link 操作用にレコードを取得する。未登録は ValueError。"""
    record = get_research_record(normalized)
    if record is None:
        raise ValueError(f"レコード未登録: {normalized}")
    return record


def add_chat_link(code_s: str, label: str, url: str) -> List[Dict[str, str]]:
    """外部チャットリンクを末尾に追加し、保存後の全リストを返す。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    entry = {"label": (label or "").strip(), "url": (url or "").strip()}
    with _flock():
        record = _get_record_for_chat_link(normalized)
        links = _normalize_chat_links(record.get("chat_links"))
        links.append(entry)
        record["chat_links"] = links
        upsert_research_record(record)
    return links


def update_chat_link(
    code_s: str, index: int, label: str, url: str
) -> List[Dict[str, str]]:
    """index 行を上書きし、保存後の全リストを返す。範囲外は IndexError。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    entry = {"label": (label or "").strip(), "url": (url or "").strip()}
    with _flock():
        record = _get_record_for_chat_link(normalized)
        links = _normalize_chat_links(record.get("chat_links"))
        if not 0 <= index < len(links):
            raise IndexError(f"chat_links の index 範囲外: {index}")
        links[index] = entry
        record["chat_links"] = links
        upsert_research_record(record)
    return links


def delete_chat_link(code_s: str, index: int) -> List[Dict[str, str]]:
    """index 行を削除し、保存後の全リストを返す。範囲外は IndexError。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    with _flock():
        record = _get_record_for_chat_link(normalized)
        links = _normalize_chat_links(record.get("chat_links"))
        if not 0 <= index < len(links):
            raise IndexError(f"chat_links の index 範囲外: {index}")
        del links[index]
        record["chat_links"] = links
        upsert_research_record(record)
    return links


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
    """前営業日終値 → N営業日後終値の変動率を符号付き文字列で返す。失敗時は ""。

    桁数: |x| >= 10 のとき整数 (例 +16) / |x| < 10 のとき小数1桁 (例 +2.7)
    """
    if before_price is None or before_price == 0:
        return ""
    try:
        change = (float(after_price) / float(before_price) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return ""
    sign = "+" if change >= 0 else ""
    if abs(change) >= 10:
        return f"{sign}{change:.0f}"
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


def _persist_kessan_post_price_changes(
    targets: List[Tuple[str, str, int, Dict[str, str]]],
) -> None:
    """指定 (code_s, kessanbi, quarter) の post_price_changes の空き期間を確定保存する。

    targets の各 updates dict は {"5d": "+5.1", "20d": "+30"} のような
    「backfill 計算で新たに埋まった非空値」のみ。

    決算反応 (1d/5d/20d) は本来コメント記入時にスナップショット保存されるが、
    記入時にはまだ経過していない期間は空のまま残る。表示時の backfill で計算
    できたものを 1d と同じく確定値として永続化し、price_log の30日ウィンドウから
    決算日が外れても消えないようにする。

    並行書き込み安全対応 (_persist_kessan_held_flags と同じ):
      - ロック下で get_research_record() による再取得を行い stale record を使わない
      - 既存 dict の **空き期間にのみ** 書き込む (非空既存値・"pts" 等の未知キーは温存)
    """
    grouped: Dict[str, List[Tuple[str, int, Dict[str, str]]]] = {}
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
                            # 既存 dict をベースに正規化 ("pts" 等の未知キーは温存)
                            ppc = normalize_kessan_post_price_changes(existing)
                            for key, new_val in updates.items():
                                # 空き期間にのみ書き込む (非空既存値は上書きしない)
                                if new_val and not ppc.get(key):
                                    ppc[key] = new_val
                                    changed = True
                            existing["post_price_changes"] = ppc
                            break
                if changed:
                    record["kessan_comments"] = comments
                    upsert_research_record(record)
        except Exception as e:
            log_warning(f"[post_price_changes] 永続化失敗 code={code_s}: {e}")


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


_DISCLOSURE_LINK_RE = re.compile(r'^=HYPERLINK\("([^"]+)","([^"]+)"\)$')


def _parse_disclosure_csv_link(value: Any) -> Tuple[str, str]:
    """disclosure_db.csv の HYPERLINK セルを (url, text) に分解する。"""
    m = _DISCLOSURE_LINK_RE.match(str(value))
    if not m:
        return "", str(value)
    return m.group(1), m.group(2)


def _sort_disclosure_impacts_for_badge(impacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """決算カードのバッジ表示順: 強い開示を優先し、同強度なら新しい日付を優先する。"""
    strength_rank = {"strong": 0, "weak": 1}

    def sort_key(impact: Dict[str, Any]) -> Tuple[int, int]:
        date_key = str(impact.get("date", "")).replace("/", "")
        try:
            date_rank = -int(date_key)
        except ValueError:
            date_rank = 0
        return (
            strength_rank.get(impact.get("strength", ""), 9),
            date_rank,
        )

    return sorted(impacts, key=sort_key)


def _attach_market_kessan_disclosure_impacts(entries: List[Dict[str, Any]]) -> None:
    """決算カレンダーの各エントリに、近傍の重要開示バッジ情報を付与する。"""
    if not entries:
        return
    targets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in entries:
        entry["disclosure_impacts"] = []
        entry["disclosure_impact_extra_count"] = 0
        entry["disclosure_impact_tooltip"] = ""
        code_s = entry.get("code_s", "")
        kessanbi = entry.get("kessanbi", "")
        if code_s and _parse_kessanbi(kessanbi) is not None:
            targets[(code_s, kessanbi)] = entry
    if not targets:
        return

    try:
        import disclosure
    except Exception as e:
        log_warning(f"[market] disclosure import 失敗: {e}")
        return
    if not os.path.exists(disclosure.DISCLOSURE_CSV):
        return

    by_entry: Dict[Tuple[str, str], List[Dict[str, Any]]] = {key: [] for key in targets}
    try:
        with open(disclosure.DISCLOSURE_CSV, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 5 or row[0] in ("", "日付"):
                    continue
                if str(row[3]) not in ("決算", "修正"):
                    continue
                try:
                    disclosure_day = datetime.strptime(str(row[0]), "%Y%m%d").date()
                except (ValueError, TypeError):
                    continue
                _, code_s = _parse_disclosure_csv_link(row[1])
                if not code_s:
                    continue
                url, heading = _parse_disclosure_csv_link(row[4])
                impact = disclosure.classify_disclosure_impact(heading)
                if impact is None:
                    continue
                for (target_code, kessanbi), entry in targets.items():
                    if target_code != code_s:
                        continue
                    kessan_day = _parse_kessanbi(kessanbi)
                    if kessan_day is None:
                        continue
                    if kessan_day - timedelta(days=14) <= disclosure_day <= kessan_day + timedelta(days=1):
                        rec = dict(impact)
                        rec.update({
                            "heading": heading,
                            "url": url,
                            "date": disclosure_day.strftime("%Y/%m/%d"),
                        })
                        by_entry[(target_code, kessanbi)].append(rec)
    except OSError as e:
        log_warning(f"[market] disclosure_db.csv 読み込み失敗: {e}")
        return

    for key, impacts in by_entry.items():
        entry = targets[key]
        unique_impacts: List[Dict[str, Any]] = []
        seen_kinds = set()
        headlines = []
        for impact in impacts:
            headlines.append("%s %s" % (impact.get("date", ""), impact.get("heading", "")))
            kind = impact.get("kind")
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            unique_impacts.append(impact)
        if not unique_impacts:
            continue

        selected: List[Dict[str, Any]] = []
        positives = _sort_disclosure_impacts_for_badge([
            i for i in unique_impacts if i.get("tone") == "positive"
        ])
        negatives = _sort_disclosure_impacts_for_badge([
            i for i in unique_impacts if i.get("tone") == "negative"
        ])
        if positives and negatives:
            selected = [positives[0], negatives[0]]
        else:
            selected = _sort_disclosure_impacts_for_badge(unique_impacts)[:2]

        entry["disclosure_impacts"] = selected
        entry["disclosure_impact_extra_count"] = max(0, len(unique_impacts) - len(selected))
        entry["disclosure_impact_tooltip"] = "\n".join(headlines)


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
          has_comment (bool), disclosure_impacts
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

    _attach_market_kessan_disclosure_impacts(list(merged.values()))

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
    # backfill で新たに埋まった反応率を確定保存するターゲット
    # targets: [(code_s, kessanbi, quarter, {"5d": "+5.1", ...}), ...]
    ppc_persist_targets: List[Tuple[str, str, int, Dict[str, str]]] = []
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
                newly_filled = _backfill_entry_reactions(entry, log, dt)
                if newly_filled:
                    ppc_persist_targets.append((
                        entry["code_s"],
                        entry["kessanbi"],
                        int(entry.get("quarter", 0) or 0),
                        newly_filled,
                    ))

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
    if ppc_persist_targets:
        _persist_kessan_post_price_changes(ppc_persist_targets)

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


def _bulk_resolve_overall_ratings(code_list: List[str]) -> Dict[str, str]:
    """複数 code_s 分の総合評価 (research_shelve.overall_rating) をバルク取得する。"""
    from db_shelve import RESEARCH_SHELVE  # 遅延 import (循環回避)

    result: Dict[str, str] = {c: "" for c in code_list if c}
    if not result:
        return result

    valid_nonempty = VALID_RATINGS - {""}
    with ShelveDB(RESEARCH_SHELVE) as db:
        for c in list(result.keys()):
            rec = db.get(normalize_code_s(c))
            if not rec:
                continue
            rating = rec.get("overall_rating") or ""
            if rating in valid_nonempty:
                result[c] = rating
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


def _change_from_desc_series(series: List, days: int) -> Optional[float]:
    """日付降順の系列から N 営業日前比の騰落率 (%) を返す。"""
    if not series or len(series) <= days or days <= 0:
        return None
    try:
        latest = float(series[0][1])
        base = float(series[days][1])
    except (TypeError, ValueError, IndexError):
        return None
    if base == 0:
        return None
    return (latest - base) / base * 100.0


def list_portfolio_with_indicators(
    records: List[Dict[str, Any]],
    sort_key: str = "position",
) -> List[Dict[str, Any]]:
    """portfolio_shelve のレコード列に stocks_shelve から最新指標を補完する (Phase 3b)。

    銘柄名は portfolio_shelve に保存されていないため stocks_shelve / research_shelve から
    都度取得してマージする (要件 §4 の延長)。

    Args:
        records: portfolio_shelve.list_records の戻り値 (既に status 等で絞り込み済み)
        sort_key: "position" / "rank" / "gyoutai" / "rating" のいずれか。
                  不正値は position 扱い。

    Returns:
        各 dict: portfolio レコード + {stock_name, rank, kessanbi_md, per, market_cap,
                                     dividend, rs, sales_growth, profit_growth,
                                     quarter, progress_diff, trend_template, tags,
                                     theoretical_diff, gyoseki, indicators_raw,
                                     status_query, status_label}
        並び順は sort_key で切替 (issue #274)。
    """
    if not records:
        return []

    exit_positions = _build_exit_position_map(build_fill_episodes()) if any(
        r.get("status") == "1保" for r in records
    ) else {}
    try:
        import portfolio_shelve as ps
        ps.seed_trade_ideas()
        exit_rules = {
            item.get("name"): item.get("exit_rule")
            for item in ps.list_trade_ideas()
            if item.get("name")
        }
    except Exception:  # noqa: BLE001
        ps = None
        exit_rules = {}

    code_list = [r.get("code_s", "") for r in records]
    stock_map = _bulk_get_stock_data(code_list)
    name_map = _bulk_resolve_stock_names(code_list)
    name_prev_map = _bulk_resolve_stock_name_prevs(code_list)  # issue #183
    rating_map = _bulk_resolve_overall_ratings(code_list)  # issue #199
    today = date.today()  # 全 row 共通の基準日 (issue #177)

    # issue #227: 株価 + RSライン 統合チャート用に market_db を1回だけロード。
    # 失敗時は None で進める (株価のみのチャートが描画される)
    try:
        from make_market_db import get_market_db  # 遅延 import (循環回避)
        market_db = get_market_db()
    except Exception:  # noqa: BLE001
        market_db = None

    # issue #332: 前日比RSライン騰落率 (1日比) を銘柄ごとに計算するための TOPIX マップ。
    # theme_summary と同じく topix_map を1回だけ構築し compute_rs_line_changes に渡して
    # 内部の TOPIX マップ再構築 (N銘柄ぶん) を避ける。
    topix_map = None
    if market_db is not None:
        try:
            from make_stock_db import _topix_close_map  # 遅延 import
            topix_map = _topix_close_map(market_db)
        except Exception:  # noqa: BLE001
            topix_map = None

    rows: List[Dict[str, Any]] = []
    for rec in records:
        code_s = rec.get("code_s", "")
        row = dict(rec)
        row["stock_name"] = name_map.get(code_s, "") or rec.get("stock_name", "")  # 旧データ互換
        row["stock_name_prev"] = name_prev_map.get(code_s)  # issue #183
        row["overall_rating"] = rating_map.get(code_s, "")  # issue #199
        stock = stock_map.get(code_s, {})
        row.update(_extract_indicators_for_portfolio(stock))
        if rec.get("status") == "1保" and ps is not None:
            position = exit_positions.get(code_s)
            strategy = (rec.get("memo") or {}).get("trade_idea")
            exit_rule = exit_rules.get(strategy)
            if isinstance(exit_rule, dict):
                from exit_line import evaluate_exit_signal
                rule_id = json.dumps(exit_rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if position:
                    position = dict(position)
                    position["stop_loss_line"] = _weighted_stop_loss_line(exit_rule, position["fills"])
                    cycle_id = f"{position['cycle_id']}|{strategy}|{rule_id}"
                else:
                    # CSV期間外からの保有など、約定履歴がない銘柄もMAは評価する。
                    position = {}
                    # レコードの登録時刻は registered_at (created_at は theme/trade_idea 用)。
                    # 削除→再登録を別サイクルとして扱わないと旧「防歴」を引き継ぐ。
                    cycle_id = f"manual:{rec.get('registered_at') or code_s}|{strategy}|{rule_id}"
                state = ps.get_exit_alert_state(code_s, cycle_id)
                signal = evaluate_exit_signal(exit_rule, stock, position, state)
                if signal:
                    if signal["level"] == "防":
                        ps.record_exit_alert_event(code_s, cycle_id, signal)
                    _apply_exit_signal_display(row, signal)
        # 運用総額の市場別内訳用カテゴリ (日経225/TOPIX/グロース/その他)
        row["market_category"] = _classify_market_category(
            stock.get("market"), stock.get("is_nikkei225"), code_s=code_s
        )
        # issue #227: 3点ミニチャート (svg + tooltip)
        row["price_rs_chart"] = build_stock_chart_payload(stock, market_db, mode="mini")
        # RSラインの 1/5/20営業日前比を RS(20,5) 列ソート用に格納。
        if topix_map:
            try:
                from make_stock_db import compute_rs_line  # 遅延 import
                rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
            except Exception:  # noqa: BLE001
                rs_line = []
        else:
            rs_line = []
        row["rs_change_1d"] = _change_from_desc_series(rs_line, 1)
        row["rs_change_5d"] = _change_from_desc_series(rs_line, 5)
        row["rs_change_20d"] = _change_from_desc_series(rs_line, 20)
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

    if sort_key == "rank":
        rows.sort(key=lambda r: (
            r.get("rank") is None,
            r.get("rank") or 0,
            r.get("code_s", ""),
        ))
    elif sort_key == "gyoutai":
        # 業態順: 業態 1 行目 (空は末尾) → 順位昇順 (None は末尾) → コード
        rows.sort(key=lambda r: (
            r["gyoutai_first"] == "",
            r["gyoutai_first"],
            r.get("rank") is None,
            r.get("rank") or 0,
            r.get("code_s", ""),
        ))
    elif sort_key in {"rs_change_1d", "rs_change_5d", "rs_change_20d"}:
        # RSライン騰落率 降順。None (算出不可) は末尾、同値はコード順。
        rows.sort(key=lambda r: (
            r.get(sort_key) is None,
            -(r.get(sort_key) or 0.0),
            r.get("code_s", ""),
        ))
    elif sort_key == "rs":
        # RS (momentum_pt) 降順。None は末尾、同値はコード順。
        rows.sort(key=lambda r: (
            r.get("rs_raw") is None,
            -(r.get("rs_raw") or 0),
            r.get("code_s", ""),
        ))
    elif sort_key == "rating":
        # 総合評価の降順ソート。S→A→…→E、未評価は末尾、同評価はコード順。
        rows.sort(key=lambda r: (
            (r.get("overall_rating") or "") == "",
            _RATING_SORT_ORDER.get(r.get("overall_rating") or "", 999),
            r.get("code_s", ""),
        ))
    else:
        # position は保有ポジション確認用。1保以外は評価せずコード順で末尾に寄せる。
        rows.sort(key=lambda r: (
            r.get("status") != "1保",
            -(r.get("position_value") or 0),
            r.get("code_s", ""),
        ))
    return rows


def _build_exit_position_map(episodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """未決済の現物・信用買いエピソードを銘柄単位で数量加重する。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    all_by_code: Dict[str, List[Dict[str, Any]]] = {}
    blocked = set()
    for ep in episodes:
        code_s = ep.get("code_s")
        if code_s:
            all_by_code.setdefault(code_s, []).append(ep)
        if ep.get("closed") or ep.get("is_short") or not ep.get("open_pl"):
            continue
        open_pl = ep["open_pl"]
        if not code_s or ep.get("split_suspect"):
            if code_s:
                blocked.add(code_s)
            continue
        held_qty = open_pl.get("held_qty")
        if (not isinstance(held_qty, (int, float)) or isinstance(held_qty, bool)
                or held_qty <= 0):
            continue
        if not isinstance(open_pl.get("avg_cost"), (int, float)) or open_pl["avg_cost"] <= 0:
            continue
        grouped.setdefault(code_s, []).append(ep)
    result = {}
    for code_s, code_episodes in grouped.items():
        if code_s in blocked:
            continue
        held_qty = sum(ep["open_pl"]["held_qty"] for ep in code_episodes)
        avg_cost = sum(ep["open_pl"]["held_qty"] * ep["open_pl"]["avg_cost"] for ep in code_episodes) / held_qty
        hold_started_at, cycle_fills = _current_hold_cycle(all_by_code[code_s])
        # 現物・信用をまたいだ銘柄全体の連続保有をサイクルにする。最古の口座だけを
        # 先に決済しても hold_started_at は変わらないため、防御履歴を維持できる。
        cycle_id = hold_started_at or min(str(ep.get("open_date") or "") for ep in code_episodes)
        result[code_s] = {"held_qty": held_qty, "avg_cost": avg_cost, "cycle_id": cycle_id,
                          "episodes": code_episodes, "fills": cycle_fills,
                          "stop_loss_line": None}
    return result


def _current_hold_cycle(episodes: List[Dict[str, Any]]) -> tuple:
    """銘柄全体で現在も続く買い保有サイクルの開始日と fill を返す。"""
    fills_by_key = {}
    for ep in episodes:
        for fill in ep.get("fills") or []:
            key = fill.get("dedup_key") or (fill.get("seq"), fill.get("trade_date"), fill.get("trade_kind"))
            fills_by_key[key] = fill

    def _sort_key(fill):
        trade_kind = fill.get("trade_kind") or ""
        opens_position = (
            trade_kind.startswith("信用新規")
            or trade_kind == "現引"
            or (fill.get("side") == "buy" and not trade_kind.startswith("信用返済"))
        )
        return (fill.get("trade_date") or "", 0 if opens_position else 1, fill.get("seq") or 0)

    held_qty = 0
    started_at = None
    cycle_fills = []
    genbiki_pending = 0  # 現引で振り替えた未決済分。対応する現物売却とペアで無視する。
    for fill in sorted(fills_by_key.values(), key=_sort_key):
        trade_kind = fill.get("trade_kind") or ""
        side = fill.get("side")
        qty = fill.get("qty")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            continue
        if trade_kind == "現引":
            # 銘柄全体では信用から現物への振替であり、保有数は変わらない。ただし
            # 振り替えた現物を後で売却した fill も、対応する分だけペアで無視しないと
            # 「現引 buy は無視・現物 sell は減算」で保有数が実態よりズレる (issue #414)。
            genbiki_pending += qty
            continue
        if trade_kind.startswith("信用"):
            delta = qty if trade_kind.startswith("信用新規") and side == "buy" else (
                -qty if trade_kind.startswith("信用返済") and side == "sell" else 0)
        else:
            delta = qty if side == "buy" else -qty if side == "sell" else 0
            if side == "sell" and genbiki_pending > 0:
                offset = min(genbiki_pending, qty)
                genbiki_pending -= offset
                delta += offset  # 振替分の売却を相殺 (例: -qty + offset)
        if not delta:
            continue
        if held_qty <= 0 and delta > 0:
            started_at = str(fill.get("trade_date") or "")
            cycle_fills = []
        if held_qty > 0 or delta > 0:
            cycle_fills.append(fill)
        held_qty = max(0, held_qty + delta)
        if held_qty <= 0:
            started_at = None
            cycle_fills = []
    return started_at, cycle_fills


def _weighted_stop_loss_line(exit_rule: Dict[str, Any], fills: List[Dict[str, Any]]) -> Optional[float]:
    """銘柄全体の連続保有 fill を再生して損切りラインを求める。"""
    from exit_line import calc_stop_loss_line

    return calc_stop_loss_line(exit_rule, fills)


def _apply_exit_signal_display(row: Dict[str, Any], signal: Dict[str, Any]) -> None:
    """既存シグナル列へ防御シグナルと tooltip を重ねる。"""
    level = signal["level"]
    existing = row.get("signal_mark")
    row["signal_mark"] = level if not existing or existing == "—" else f"{level}/{existing}"
    reasons = "、".join(signal.get("reasons") or [])
    text = f"[{level}] {reasons}" if reasons else f"[{level}] 過去に防御シグナルあり"
    row["signal_full"] = "\n".join(x for x in (text, row.get("signal_full") or "") if x)
    row["signal_display"] = {
        "tooltip": row["signal_full"],
        "style": (
            "background:#4285f4;color:#fff;font-weight:700" if level == "防"
            else "background:#6fa8dc;color:#fff;font-weight:700" if level == "防予"
            else "background:#e8f0fe;color:#174ea6;font-weight:700"
        ),
    }


# 月足位置タグ (issue #53)。portfolio 一覧ではタグ列から外し、メモページの月足列に出す。
# タグ種別は make_stock_db.judge_monthly_position が返す 月破/月高/月低 と対応。
MONTHLY_TAG_DESCRIPTIONS = {
    "月破": "月破: 月足で低位滞留から直近3ヶ月内に3年高値をブレイク (Stage 1→2)",
    "月高": "月高: 月足で10年レンジの上位30%の高値圏 (戻り売り圧力が小さい)",
    "月低": "月低: 月足で10年レンジの下位35%以下の低位圏",
}
MONTHLY_TAGS = tuple(MONTHLY_TAG_DESCRIPTIONS)


def _format_tags(stock: Dict[str, Any], tags=None) -> str:
    """code_rank.csv「タグ」列と同じ表記から月足タグを除いて返す。

    make_stock_db.make_signal() の tags リストを "/" join する。
    tags を渡すと make_signal の再呼び出しを省略する (一覧の二重計算回避)。
    月足タグ (月破/月高/月低) は専用の月足列に出すためここでは除外する。
    """
    if not stock:
        return "—"
    if tags is None:
        try:
            from make_stock_db import make_signal  # 遅延 import
            _signal, tags = make_signal(stock)
        except Exception:
            return "—"
    tags = [t for t in (tags or []) if t not in MONTHLY_TAGS]
    return "/".join(tags) if tags else "—"


def _format_monthly_tag(tags=None) -> str:
    """portfolio メモページの月足列に出すタグ (月破/月高/月低) を返す。"""
    for tag in (tags or []):
        if tag in MONTHLY_TAGS:
            return tag
    return "—"


# タグ列ヘッダの凡例 (issue #53 の月足列と同じく、種別の意味はヘッダに集約する)。
# セル側の tooltip は「そのタグが出ている理由」を実際の値で出すため、ここは説明のみ。
# タグ種別は make_stock_db.make_signal() が返すものと対応。
TAG_DESCRIPTIONS = {
    "新": "新: 1年以上前の高値から5%以内",
    "直": "直: 3ヶ月以上前の高値から5%以内",
    "最": "最: 直近2年の週足高値を更新 (週足取得が period=2y のため)",
    "高": "高: 株探の新高値リストに直近掲載 (前日分まで)",
    "出": "出: 株探の出来高急増リストに直近掲載 (前日分まで)",
    "P": "P: 株探のPTSナイトランキングに直近掲載 (前日分まで)",
    "押": "押: 20MA押し目",
    "売": "売: RS高いのに売り圧力比率<45 と wma10割れが同時成立",
    "警": "警: RS高いのに売り圧力比率<45 か wma10割れのどちらかが成立",
    "早売": "早売: 10ma維持実績あり→10ma割れ後、最初に割れた日の安値を下回って確定",
    "急": "急: 順位が前日比で30%以上上昇",
    "昇": "昇: 順位が5日比で30%以上上昇",
}


def _format_tags_tooltip(reasons: Dict[str, str]) -> str:
    """portfolio 一覧のタグ列 tooltip 文言を返す。

    タグ種別の説明は列ヘッダ (TAG_DESCRIPTIONS) に集約済みなので、ここは
    make_signal が判定時点で書き出した「タグが出ている理由」を並べるだけ。
    理由を持たないタグ (新/直/最/押/早売) は行ごと出ない。
    """
    return "\n".join(f"{tag}: {reason}" for tag, reason in (reasons or {}).items())


def _format_signal(stock: Dict[str, Any]) -> Tuple[str, str]:
    """portfolio 一覧のシグナル列用に (表示記号, tooltip全文) を返す。"""
    if not stock:
        return ("—", "")
    try:
        from make_stock_db import extract_signals, make_signal  # 遅延 import
        signal_full, _tags = make_signal(stock)
        signals = extract_signals(stock)
    except Exception:  # noqa: BLE001
        return ("—", "")
    marks = []
    for sig in signals:
        kind = sig.get("kind")
        if kind and kind not in marks:
            marks.append(kind)
    return ("/".join(marks) if marks else "—", signal_full or "")


# ポ/ブシグナルの鮮度係数 (issue #253)。経過日数→不透明度の乗数。
# tooltip 背景色 (_build_signal_display) とチャートマーカー (項目3) で共有する。
def _signal_freshness_alpha(delta: int) -> float:
    if delta <= 2:
        return 1.0
    if delta <= 5:
        return 0.6
    return 0.35


# ポ/ブシグナルの強度バケット (issue #253)。
# tooltip 文言 (_build_signal_display) とチャートマーカーサイズ (項目3) で共有する。
def _signal_strength_bucket(kind: str, num: int) -> str:
    """シグナル種別と保存数値から強度ラベルを返す。

    ポ: num = MA10乖離率% (小さい=MAに近い良い位置=強)。3段階 (強/中/弱)。
    ブ: num = 出来高超過率% (大きい=出来高急増=強)。4段階 (特強/強/中/弱)。
        しきい値 500/200/100 は DB全体3046件の分布で校正 (特強=上位約10%)。
    """
    if kind == "ポ":
        if num >= -1:
            return "強"
        if num >= -3:
            return "中"
        return "弱"
    if kind == "週ブ":
        # num = 5日平均出来高の基準中央値比% (成立条件上 150 以上)。未較正 (issue #384)。
        if num >= 250:
            return "強"
        if num >= 200:
            return "中"
        return "弱"
    # ブ
    if num >= 500:
        return "特強"
    if num >= 200:
        return "強"
    if num >= 100:
        return "中"
    return "弱"


def _build_signal_display(stock: Dict[str, Any]) -> Dict[str, str]:
    """signal セルの tooltip と背景色 style を組み立てる (issue #253)。

    extract_signals (make_signal と同一フィルタ) が返す表示対象シグナルだけを使い、
    一覧 tooltip/背景色と詳細チャートマーカーが同じシグナル集合を見るようにする。
    背景色は最強・最新シグナルの強度×鮮度で赤系 rgba の濃淡。

    Returns:
        {"tooltip": str, "style": str}。対象シグナルなしなら空文字。
    """
    empty = {"tooltip": "", "style": ""}
    if not stock:
        return empty
    try:
        from make_stock_db import extract_signals  # 遅延 import
        signals = extract_signals(stock)
    except Exception:  # noqa: BLE001
        return empty
    strength_alpha = {"特強": 1.0, "強": 0.85, "中": 0.55, "弱": 0.30}
    tmpls = {
        "ポ": "[ポ] %s %s 押し目買い圧(MA10乖離 %d) / %d日前",
        "ブ": "[ブ] %s %s 出来高ブレイク(出来高+%d%%) / %d日前",
    }

    lines = []
    max_alpha = 0.0
    for s in signals:
        bucket = _signal_strength_bucket(s["kind"], s["num"])
        if s["kind"] == "週ブ":
            # 週ブは dry up 比率も出す (issue #384)。旧データで dryup 欠落時は ? 表示。
            dryup = s.get("dryup")
            dryup_str = "%d" % dryup if dryup is not None else "?"
            lines.append(
                "[週ブ] %s %s 乾%s%%→5日出来高 中央値比%d%% 20日高値上抜け / %d日前"
                % (s["mmdd"], bucket, dryup_str, s["num"], s["delta"])
            )
        else:
            lines.append(tmpls[s["kind"]] % (s["mmdd"], bucket, s["num"], s["delta"]))
        max_alpha = max(max_alpha,
                        strength_alpha[bucket] * _signal_freshness_alpha(s["delta"]))

    if not lines:
        return empty
    # 赤 #ea4335 → rgba
    style = "background:rgba(234,67,53,%.2f);color:#000" % max_alpha
    return {"tooltip": "\n".join(lines), "style": style}


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
_RS_RANK_COLOR = "#7b1fa2"  # RS(0~99)履歴 (右軸): RSライン青と区別する紫系

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


def _format_ma_deviation(values: List[float], window: int) -> str:
    """最新値 (values[-1]) と直近 window 本 (今日含む) の移動平均との乖離率を整形する。

    RSライン tooltip 用 (issue #283)。1 点比較ではなく移動平均基準にすることで
    ヒゲ・急変のブレを薄め、勢い・過熱の度合いを安定して見せる。
    values は古い順 (最新が末尾)。データ不足時は取得できるだけで計算。0 除算は "—"。
    """
    if not values or window <= 0:
        return "—"
    recent = values[-window:] if len(values) >= window else values
    ma = sum(recent) / len(recent)
    if ma == 0:
        return "—"
    pct = (values[-1] - ma) / ma * 100
    return f"{pct:+.1f}%"


def _format_prev_change(values: List[float]) -> str:
    """rs_line 値列 (古い順) の末尾2点から前日比 (1日比) % を整形する (issue #332)。

    前日比 = (最新 - 前日) / 前日 * 100。compute_rs_line_changes の D と同定義 (隣接2点比較)。
    2点未満・前日値0は "—"。
    """
    if not values or len(values) < 2 or values[-2] == 0:
        return "—"
    pct = (values[-1] - values[-2]) / values[-2] * 100
    return f"{pct:+.1f}%"


def _build_chart_tooltip(
    price_values: List[float],
    rs_values: List[float],
    has_blue_dot: bool,
    unit_label: str = "日",
    include_prev_change: bool = False,
    rs_rank_now: Optional[float] = None,
    full_chart: bool = False,
) -> str:
    """チャート tooltip (title 属性向け) を生成する。

    株価行は 20本 / 5本 の合計騰落率 (期間内の動きがそのまま % で読める)。
    RSライン行は移動平均乖離率 (今日 vs 直近 20本/5本平均、issue #283)。1 点比較
    だとヒゲでブレるため、平均基準で勢い・過熱を安定して見せる。
    unit_label は "日" (日足 mini) / "週" (週足 full) を切り替える。

    include_prev_change=True のとき RSライン前日比 (1日比) 行を足す (issue #332)。
    portfolio 一覧の mini チャートのみ有効化し、詳細ページ週足 (full) には出さない。

    full_chart=True のとき (詳細ページ full): 株価系列を廃止したため株価行は出さない。
    rs_rank_now があれば RS(0~99) 現在値行を先頭に足す (無ければ RSライン乖離行のみ)。
    full_chart=False (mini) は従来どおり株価行を先頭に出す。
    """
    lines = []
    if full_chart:
        if rs_rank_now is not None:
            lines.append(f"RS(0~99): {int(round(rs_rank_now))}")
    else:
        lines.append(
            f"株価: 20{unit_label} {_format_total_change(price_values, _SPARK_LOOKBACK)}, "
            f"5{unit_label} {_format_total_change(price_values, _SPARK_RECENT)}"
        )
    lines.append(
        f"RSライン乖離: 20{unit_label}平均 {_format_ma_deviation(rs_values, _SPARK_LOOKBACK)}, "
        f"5{unit_label}平均 {_format_ma_deviation(rs_values, _SPARK_RECENT)}"
    )
    if include_prev_change:
        lines.append(f"前日比: {_format_prev_change(rs_values)}")
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


def _svg_hover_rect(cx: float, cy: float, half_w: float, half_h: float, title: str = "") -> str:
    """透明 hover ターゲット矩形を返す。

    SVG title は細い polygon だと hover しづらいことがあるため、見た目とは別に
    マーカー周辺へ当たり判定を広げる。fill-opacity=0 だとブラウザによっては
    hover 判定が不安定なため、ごく薄い不透明度を使う。
    """
    t = "<title>%s</title>" % html.escape(title) if title else ""
    return (
        '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
        'fill="#fff" fill-opacity="0.001">%s</rect>'
        % (cx - half_w, cy - half_h, half_w * 2, half_h * 2, t)
    )


def _svg_triangle(cx: float, cy: float, size: float, color: str,
                  opacity: float = 1.0, title: str = "") -> str:
    """上向き三角マーカー (issue #253: ポケットピボット用)。"""
    pts = "%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (
        cx, cy - size, cx - size, cy + size, cx + size, cy + size)
    t = "<title>%s</title>" % html.escape(title) if title else ""
    return ('<polygon points="%s" fill="%s" opacity="%.2f">%s</polygon>'
            % (pts, color, opacity, t))


def _svg_diamond(cx: float, cy: float, size: float, color: str,
                 opacity: float = 1.0, title: str = "", filled: bool = True) -> str:
    """ダイヤ (菱形) マーカー (issue #253: ブレイクアウト用)。

    filled=False は輪郭のみ (中抜き)。高値追い圏の extended 候補を半透明で
    弱く見せるのに使う。
    """
    pts = "%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" % (
        cx, cy - size, cx + size, cy, cx, cy + size, cx - size, cy)
    t = "<title>%s</title>" % html.escape(title) if title else ""
    if filled:
        return ('<polygon points="%s" fill="%s" opacity="%.2f">%s</polygon>'
                % (pts, color, opacity, t))
    return ('<polygon points="%s" fill="none" stroke="%s" stroke-width="1.2" '
            'opacity="%.2f">%s</polygon>' % (pts, color, opacity, t))


def _interp_x_on_weekbars(d, window_dates, xs):
    """日付 d を週バー列の表示窓にマップした X 座標を返す。

    発生日 d を挟む週バー間で X を線形補間 (週の幅を日割り按分)。
    最古より古い → 窓外で None (drop)、最新以降 → 末尾 clamp。
    ポ/ブマーカー (_resolve_signal_markers) と RS履歴重畳 (_overlay_rs_rank) が
    同一の「実日付→週足X」按分を共用する。

    Args:
        d: マップ対象の日付 (datetime.date)
        window_dates: 週バー日付列 (昇順, datetime.date)
        xs: 各週バーの X 座標 (window_dates と同じ index)
    """
    oldest = window_dates[0]
    latest = window_dates[-1]
    if d <= oldest:
        return xs[0] if d == oldest else None  # 最古より古い → 窓外 drop
    if d >= latest:
        return xs[-1]  # 最新以降 → 末尾 clamp (はみ出し防止)
    for i in range(len(window_dates) - 1):
        d0, d1 = window_dates[i], window_dates[i + 1]
        if d0 <= d <= d1:
            span = (d1 - d0).days
            frac = (d - d0).days / span if span else 0.0
            return xs[i] + frac * (xs[i + 1] - xs[i])
    return None


def _resolve_signal_markers(stock, window_dates, xs):
    """ポ/ブシグナルを週足チャートの表示窓にマップした marker spec を返す (issue #253)。

    シグナルの抽出・フィルタ・年補完は extract_signals に委譲する。
    詳細チャートは signal 欄と同じ元シグナルを窓内で見せたいので、一覧 tooltip 用の
    7日制限は外し、週足表示窓に入るものだけをマーカー化する。X は発生日を週バー間で
    線形補間する (週足だが発生日は日単位のため週の幅を日割り按分)。窓外は drop。

    Args:
        stock: 銘柄DB dict (pocket_pivot/breakout/trend_template/access_date_price を参照)
        window_dates: 表示窓 (基準週揃え後) の週バー日付列 (昇順, datetime.date)
        xs: 各週バーの X 座標 (window_dates と同じ index)

    Returns:
        [{"kind","x","num","delta","strength"}] のリスト。窓外は drop。
    """
    if not window_dates or not xs or not stock:
        return []
    try:
        from make_stock_db import extract_signals  # 遅延 import
        # 詳細チャートのみ extended (高値追い圏で弾かれたブレイク候補) を含める。
        signals = extract_signals(stock, max_delta_days=None, include_extended=True)
    except Exception:  # noqa: BLE001
        return []
    markers = []
    for s in signals:
        x = _interp_x_on_weekbars(s["sig_date"], window_dates, xs)
        if x is None:
            continue  # 窓外 (古い) → drop
        m = {
            "kind": s["kind"], "x": x,
            "num": s["num"], "delta": s["delta"],
            "sig_date": s["sig_date"],
        }
        if s.get("extended"):
            m["extended"] = True
            # extended_per (出来高超過率) があれば通常ブレイクと同じ強度バケットで
            # マーカーサイズを決める。旧2要素データは per なし → strength を付けず
            # 描画側で固定サイズにフォールバックする。
            per = s.get("extended_per")
            if per is not None:
                m["strength"] = _signal_strength_bucket("ブ", per)
        else:
            m["strength"] = _signal_strength_bucket(s["kind"], s["num"])
        markers.append(m)
    return markers


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

    # issue #332: portfolio 一覧の mini チャートのみ前日比 (1日比) 行を tooltip に足す。
    tooltip = _build_chart_tooltip(price_asc, rs_asc, has_blue_dot, include_prev_change=True)

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


def _clean_rs_rank_points(rs_rank_log) -> List:
    """rs_rank_log を昇順 (日付昇順) に整形し、None/0以下の無効値を除外する。

    rs_rank_log: [(date, momentum_pt), ...] (日付降順, momentum_pt は 0~99)。
    返り値: [(date, value), ...] 日付昇順。0 はエラー値として除外 (get_rank_log_expr 同方針)。
    """
    if not rs_rank_log:
        return []
    pts = [(d, v) for d, v in rs_rank_log if v is not None and v > 0]
    pts.sort(key=lambda x: x[0])  # 日付昇順
    return pts


def _rs_rank_axis_bounds(values) -> tuple:
    """RS(0~99)右軸の表示レンジ (lo, hi) を 25 刻みの境界にスナップして返す。

    境界候補 = 0/25/50/75/99。データ min-max を内包する最寄りの境界帯に丸める
    (lo = min を下回る最大境界, hi = max を上回る最小境界)。これにより線が軸の
    一部に張り付かず、かつ「高RS帯/中RS帯」の水準感を保てる。
    全点が 1 帯内なら 1 帯ぶん (例 60~70 → 50~75) を確保する。
    """
    bounds = [0, 25, 50, 75, 99]
    lo_val, hi_val = min(values), max(values)
    lo = max((b for b in bounds if b <= lo_val), default=0)
    hi = min((b for b in bounds if b >= hi_val), default=99)
    if hi <= lo:  # 同一境界に張り付く場合は 1 帯ぶん広げる
        idx = bounds.index(lo)
        if idx + 1 < len(bounds):
            hi = bounds[idx + 1]
        else:
            lo = bounds[idx - 1]
    return lo, hi


def _overlay_rs_rank(parts, rank_pts, price_log, *,
                     window_dates, xs, chart_top, chart_h,
                     pad_x, inner_w, pad_right):
    """RS(0~99)履歴を右Y軸 (25刻みスナップの可変スケール) で週足軸に重畳する。

    横軸は週足スケールのまま、RS各点の日付を window_dates/xs (週バー列) に実日付で
    マップする (_interp_x_on_weekbars)。欠番は price_log の営業日カレンダー隣接で
    判定し、連続営業日でない区間は線を分割する (跨いで補間しない)。

    右軸レンジは表示期間の RS min-max を 25 刻み境界にスナップして決める
    (_rs_rank_axis_bounds)。

    Args:
        parts: SVG 文字列リスト (追記する)
        rank_pts: _clean_rs_rank_points の結果 (日付昇順, [(date, value)])
        price_log: 日足/週足台帳 (新しい順)。実営業日カレンダー突き合わせに使う
        window_dates / xs: 週バー日付列 (昇順) と各週バーの X 座標
        chart_top / chart_h: 騰落率描画域の上端 y と高さ (右軸もこの範囲を共有)
        pad_x / inner_w / pad_right: 右軸目盛りラベルの X 位置決めに使う
    Returns:
        描画したら True、点不足/窓不足で描かなければ False。
    """
    if len(rank_pts) < 2 or not window_dates or not xs:
        return False

    # 右軸レンジを表示期間の RS 値域から 25 刻み境界にスナップ。
    axis_lo, axis_hi = _rs_rank_axis_bounds([v for _, v in rank_pts])
    axis_span = axis_hi - axis_lo

    def _y_rank(v: float) -> float:
        # 右軸: [axis_lo, axis_hi] を描画域に線形マップ。v=axis_hi が chart_top。
        return chart_top + chart_h - ((v - axis_lo) / axis_span) * chart_h

    # price_log (新しい順) の日付→index マップ。連続営業日判定 (隣接 index 差=1) に使う。
    price_dates = [d for d, _ in price_log] if price_log else []
    date_to_idx = {d: i for i, d in enumerate(price_dates)}

    # 各 RS 点を (x, y, price_idx) に変換。窓外 (x=None) は drop。
    placed = []
    for d, v in rank_pts:
        x = _interp_x_on_weekbars(d, window_dates, xs)
        if x is None:
            continue  # 週足窓より古い → drop
        placed.append((x, _y_rank(v), date_to_idx.get(d)))

    if len(placed) < 2:
        return False

    # price_log の営業日隣接 (idx 差=1) で連続セグメントに分割。
    # idx が None (price_log に無い日付) はそこで切る。
    segments = []
    cur = [placed[0]]
    for prev, p in zip(placed, placed[1:]):
        pi_prev, pi = prev[2], p[2]
        contiguous = (pi_prev is not None and pi is not None and pi_prev - pi == 1)
        if contiguous:
            cur.append(p)
        else:
            segments.append(cur)
            cur = [p]
    segments.append(cur)

    # 各セグメントを個別 polyline (単色)。1点だけのセグメントは線にならないので skip。
    for seg in segments:
        if len(seg) >= 2:
            pts = [(x, y) for x, y, _ in seg]
            parts.append(_svg_polyline(pts, _RS_RANK_COLOR, 1.4))

    # 末尾点 (最新) に circle + 現在値ラベル。
    last_x, last_y, _ = placed[-1]
    last_v = rank_pts[-1][1]
    parts.append(_svg_circle(last_x, last_y, 2.2, _RS_RANK_COLOR))
    parts.append(
        f'<text x="{last_x - 4:.1f}" y="{last_y - 3:.1f}" font-size="9" '
        f'fill="{_RS_RANK_COLOR}" font-weight="bold" text-anchor="end">{int(round(last_v))}</text>'
    )

    # 右Y軸目盛り (axis_lo / axis_hi と間の 25 刻み境界)。RSライン左軸と区別する紫系。
    axis_x = pad_x + inner_w + pad_right - 2
    ticks = [v for v in (0, 25, 50, 75, 99) if axis_lo <= v <= axis_hi]
    for v in ticks:
        parts.append(
            f'<text x="{axis_x:.1f}" y="{_y_rank(v) + 3:.1f}" font-size="8" '
            f'fill="{_RS_RANK_COLOR}" text-anchor="end">{v}</text>'
        )
    return True


def build_price_rs_chart_full(
    price_log: List,
    rs_line: List,
    has_blue_dot: bool,
    width: int = 400,
    height: int = 138,
    stock: Optional[Dict[str, Any]] = None,
    rs_rank_log: Optional[List] = None,
) -> tuple:
    """詳細ページ用 20 週フルチャート SVG と tooltip を返す (週足 20 本ベース)。

    RSライン (対TOPIX) と RS(0~99)履歴 を 1 パネル・二重Y軸で重ねる:
      - 左Y軸 = RSライン (週足20週, 基準週=0% 起点の累積騰落率 %)
      - 右Y軸 = RS(0~99)履歴 (日足, RANK_LOG_DAYS=60日保持) を右端側に重畳。
        スケールは値域を 25 刻み境界にスナップ (_rs_rank_axis_bounds)
        (横軸は週足スケールのまま、RS各点は実日付で週足軸にマップ)
      - 0% の水平基準線を薄く描画
      - 末尾 5 週部分は太く濃色で強調 (RSライン)
      - 軸ガイド (基準週 / 5週前 / 今日) と日付ラベル
      - Blue Dot は RS ライン末尾の青丸 (r=4)
      - ポ/ブ発生日マーカーは X軸下のバンドに描画

    描画契約: RSライン と RS(0~99)履歴 のうち描けるものを描く。両方無ければ空SVG。
    末尾 1 本は Case A (両週足あり + 日足が両週足より新しい) のみ今週仮終値 (= 最新日足) になる。
    """
    price_asc_raw = _asc_series_from_log(price_log, _SPARK_LOOKBACK)
    rs_asc_raw = _asc_series_from_log(rs_line, _SPARK_LOOKBACK)

    # RS履歴 (右軸) 用の有効点を先に整形 (昇順, None/0以下を除外)。
    rank_pts = _clean_rs_rank_points(rs_rank_log)

    # RSライン描画可否。RSライン2点未満でも RS履歴/マーカーがあれば描画継続する
    # (空SVG 判定は週足軸の土台 price_asc < 2 で後段に一本化)。
    rs_available = len(rs_asc_raw) >= 2 and rs_asc_raw[0] > 0

    # tooltip: RSライン乖離 + RS(0~99)現在値 (株価行は廃止)
    rank_now = rank_pts[-1][1] if rank_pts else None
    tooltip = _build_chart_tooltip(
        price_asc_raw, rs_asc_raw, has_blue_dot, unit_label="週",
        rs_rank_now=rank_now, full_chart=True)

    # 週足軸 (X) は price_log (週足台帳) の日付列で決まる。RSライン有無に関わらず
    # price_asc を週バー本数の土台にする (RS履歴の実日付→週足X マッピングに必要)。
    if rs_available:
        n_align = min(len(price_asc_raw), len(rs_asc_raw))
        price_asc = price_asc_raw[-n_align:]
        rs_asc = rs_asc_raw[-n_align:]
    else:
        price_asc = price_asc_raw
        rs_asc = []

    # 週足軸の土台 (price_log) が2点未満なら週足チャートを組めない → 空SVG。
    # (RS履歴のみ単独描画はしない: 通常 price_week_log があり price_asc は埋まる)
    if len(price_asc) < 2 or price_asc[0] <= 0:
        return ("", "")

    # 描ける系列が一つも無い場合は空 SVG。株価線廃止後、RSライン不可 (market_db=None 等) で
    # かつ RS履歴も2点未満だと軸・凡例だけのデータ無しチャート枠になるのを防ぐ。
    if not rs_available and len(rank_pts) < 2:
        return ("", "")

    # ポ/ブマーカー用に表示窓の週バー日付列を price_asc と同じ手順で構築 (issue #253)。
    # _asc_series_from_log と同じく log[:LOOKBACK] を昇順化し None 値を除外、末尾 n_align 本。
    window_dates = []
    if stock:
        try:
            sliced = list(price_log)[:_SPARK_LOOKBACK]
            dates_asc = [d for d, v in reversed(sliced) if v is not None]
            window_dates = dates_asc[-len(price_asc):]
        except Exception:  # noqa: BLE001
            window_dates = []

    # % 変換 (基準週=0%) — RSラインのみ (株価系列は廃止)
    if rs_asc:
        r_base = rs_asc[0]
        rs_pct = [(r / r_base - 1.0) * 100.0 for r in rs_asc]
    else:
        rs_pct = []

    # 左に RSライン% 軸ラベル、右に RS(0~99) 軸ラベルを出すため両側に余白。
    pad_left = 36
    pad_right = 28
    pad_y_top = 14
    pad_y_bottom = 14
    # X軸の下に常設するポ/ブマーカー専用バンド (issue #253)。騰落率描画域とは独立。
    marker_band_h = 18
    inner_w = width - pad_left - pad_right
    # 騰落率描画域はバンドを除いた高さで計算 (描画域は従来の 120 ベースを維持)。
    inner_h = height - marker_band_h - pad_y_top - pad_y_bottom
    pad_x = pad_left

    chart_top = pad_y_top
    chart_h = inner_h

    # X 座標: 表示本数 (基準週揃え後の長さ) を基準に右端揃え
    n = len(price_asc)
    step = inner_w / max(n - 1, 1)

    def _xs_for(length: int) -> List[float]:
        return [pad_x + inner_w - step * (length - 1 - i) for i in range(length)]

    # 左Y スケール (RSライン% のみ)。RSライン無し時は 0% 基準のダミーレンジ。
    all_pct = list(rs_pct) if rs_pct else [0.0]
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
    # 詳細チャートは親要素 title を使わず、SVG 内で hover 領域を分ける。
    # 非シグナル要素は pointer-events を切り、チャート本体の透明 hover 面だけが
    # RS 系 tooltip を出す。マーカーバンドはポ/ブ polygon の title のみ有効にする。
    parts.append('<g pointer-events="none">')

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
    # 日付ラベルは X軸 (chart_top+chart_h) 直下に置く。その下がマーカー専用バンド。
    label_y = chart_top + chart_h + 9
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
        '<tspan fill="#1976d2" font-style="italic">┄ RSライン (対TOPIX %/左軸)</tspan>'
        '<tspan dx="6" fill="' + _RS_RANK_COLOR + '">━ RS (0~99/右軸)</tspan>'
        '</text>'
    )

    # 左Y軸ラベル (RSライン %, 灰色)
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

    xs = _xs_for(n)

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

        # RS末尾現在値ラベル
        rs_now_x = rs_points[-1][0]
        rs_now_y = rs_points[-1][1]
        offset = 8 if has_blue_dot else 4
        parts.append(
            f'<text x="{rs_now_x - offset:.1f}" y="{rs_now_y + 3:.1f}" font-size="9" '
            f'fill="#1976d2" font-weight="bold" text-anchor="end">{_format_pct_axis(rs_pct[-1])}</text>'
        )

    # RS(0~99)履歴を右Y軸 (0~99 固定) で右端側に重畳 (最前面)。
    # 横軸は週足スケールのまま、RS各点は実日付で週足軸 (window_dates/xs) にマップ。
    # 営業日カレンダー突き合わせは日足 price_log を使う (price_log 引数は週足台帳のため)。
    daily_price_log = (stock or {}).get("price_log") or []
    _overlay_rs_rank(
        parts, rank_pts, daily_price_log,
        window_dates=window_dates, xs=xs,
        chart_top=chart_top, chart_h=chart_h,
        pad_x=pad_x, inner_w=inner_w, pad_right=pad_right,
    )

    parts.append("</g>")

    # ポ/ブ発生日マーカー (issue #253): ポ=緑三角 / ブ=橙ダイヤ。
    # X は発生日を週幅で日割り按分、Y は X軸 + 日付ラベルの下に常設したマーカー専用バンド
    # (騰落率Y軸と無関係) に配置し、株価線/RS線と完全分離する。サイズは強度バケット。
    # 詳細チャートは発生日が X 位置で読めるため鮮度による半透明化は行わない。最前面に描く。
    if window_dates:
        markers = _resolve_signal_markers(stock, window_dates, xs)
        # ブは4段階 (特強/強/中/弱)、ポは3段階 (強/中/弱) で特強は使わない。
        # サイズは等差1.5。opacity は強で上限 (1.0) のため特強も 1.0。
        size_map = {"特強": 7.5, "強": 6.0, "中": 4.5, "弱": 3.0}
        opa_map = {"特強": 1.0, "強": 1.0, "中": 0.8, "弱": 0.6}
        # ポは三角・ブは菱形。視認性調整: ポは控えめに縮小、ブは強調して拡大。
        PO_SIZE_SCALE = 0.8
        BU_SIZE_SCALE = 1.8
        EXT_SIZE = 4.5  # extended の per 未保存 (旧データ) 時の固定サイズ
        # マーカー専用バンド内: ポは下段、ブはその上の段 (同発生週の重なり回避)。
        y_po = label_y + 16  # 日付ラベル baseline の下
        y_bu = y_po - 6
        for m in markers:
            # 週足チャートに日足発生日を重ねるため、X位置だけでは発生日が読み取り
            # づらい (週バーは月曜ラベルで週末終値を示すため視覚的にズレる)。
            # 発生日 (M/D) を tooltip に明示して誤読を防ぐ。
            sig_md = "%d/%d" % (m["sig_date"].month, m["sig_date"].day)
            # extended (高値追い圏で正規ブレイクから弾かれた候補): 中抜き・半透明の
            # 橙ダイヤで「対象外」を表しつつ、サイズは通常ブレイクと同じ強度バケット
            # (出来高超過率) に連動させる。per 未保存の旧データは strength を持たない
            # ため固定サイズ EXT_SIZE にフォールバック。num は MA10乖離% で tooltip に出す。
            if m.get("extended"):
                ext_size = size_map[m["strength"]] if m.get("strength") else EXT_SIZE
                ext_label = "ex/%s" % m["strength"] if m.get("strength") else "ex"
                title = "ブ(%s) %s 乖離+%d%% 高値追い圏・対象外" % (ext_label, sig_md, m["num"])
                parts.append(_svg_hover_rect(m["x"], y_bu, 8.0, 8.0, title))
                parts.append(_svg_diamond(
                    m["x"], y_bu, ext_size * BU_SIZE_SCALE, "#f57c00", 0.5, title, filled=False))
                continue
            size = size_map[m["strength"]]
            title = "%s %s (%s)" % (m["kind"], sig_md, m["strength"])
            if m["kind"] == "ポ":
                parts.append(_svg_hover_rect(m["x"], y_po, 8.0, 8.0, title))
                parts.append(_svg_triangle(m["x"], y_po, size * PO_SIZE_SCALE, "#2e7d32", 1.0, title))
            elif m["kind"] == "週ブ":
                # 週ブは通常ブ (橙) と区別して紫ダイヤ。ブと同発生週で重ならないよう上段 (issue #384)。
                y_wbu = y_bu - 6
                parts.append(_svg_hover_rect(m["x"], y_wbu, 8.0, 8.0, title))
                parts.append(_svg_diamond(m["x"], y_wbu, size * BU_SIZE_SCALE, "#8e24aa", opa_map[m["strength"]], title))
            else:  # ブ
                parts.append(_svg_hover_rect(m["x"], y_bu, 8.0, 8.0, title))
                parts.append(_svg_diamond(m["x"], y_bu, size * BU_SIZE_SCALE, "#f57c00", opa_map[m["strength"]], title))

    if tooltip:
        chart_hover_h = chart_top + chart_h + 2
        parts.append(
            '<rect x="0" y="0" width="%d" height="%.1f" fill="#fff" fill-opacity="0">'
            '<title>%s</title></rect>'
            % (width, chart_hover_h, html.escape(tooltip))
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
        rs_rank_log = (stock or {}).get("rs_rank_log") or []
        svg, tooltip = build_price_rs_chart_full(
            price_log, rs_line, has_blue_dot,
            stock=stock,
            rs_rank_log=rs_rank_log,
        )
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
    """今週仮終値の (date, stock_close, topix_close, replace) を返す。

    銘柄週足の最新 ISO 週以上に日足 (銘柄/TOPIX) が進んでいれば最新日足を反映する。
    今週の週足バーが既にある (ISO週一致) 場合は週途中の集計値なので最新日足で
    置換 (replace=True)、まだ無い場合は prepend (replace=False)。
    TOPIX 週足が当日分まで進んでいる非対称ケースでも、銘柄週足を基準にすれば
    日足側の今週分を仮終値として安全に反映できる。
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
    daily_stock_iso = daily_stock[0][0].isocalendar()[:2]
    if not (daily_stock_iso >= stock_week_iso
            and daily_topix[0][0].isocalendar()[:2] >= stock_week_iso):
        return None
    # series(週足台帳)側の置換判定: 今週バーが既にある (ISO週一致) なら置換。
    replace = daily_stock_iso == stock_week_iso
    return (daily_stock[0][0], float(daily_stock[0][1]),
            float(daily_topix[0][1]), replace)


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
    dt, stock_close, _, replace = eligible
    # replace: 今週バーが既にある → 週途中値を最新日足で置換。なければ prepend。
    rest = series[1:] if replace else series
    return [(dt, stock_close)] + rest


def _append_provisional_rs(rs_line, stock, market_db):
    """rs_line の先頭 (日付降順) に今週仮終値分の rs 点を反映する。

    追加可否は _build_full_week_series と同じ条件 (両日足が週足以上)。ただし置換判定は
    rs_line[0] 自身の ISO 週で独立に行う: compute_rs_line_weekly は TOPIX 側の同一 ISO 週が
    欠けるとその週を落とすため、price_week_log[0] が今週でも rs_line[0] が前週のことがある。
    stock_week 基準で先頭を落とすと前週 RS を誤って捨て系列長・基準週がずれる。
    """
    eligible = _is_provisional_eligible(stock, market_db)
    if eligible is None:
        return rs_line
    dt, stock_close, topix_close, _ = eligible
    try:
        rs_val = stock_close / topix_close
    except ZeroDivisionError:
        return rs_line
    rs_line = list(rs_line)
    # rs_line[0] が今週分なら置換、前週どまりなら prepend。
    rs_replace = bool(rs_line) and rs_line[0][0].isocalendar()[:2] == dt.isocalendar()[:2]
    rest = rs_line[1:] if rs_replace else rs_line
    return [(dt, rs_val)] + rest


def build_trend_info(stock: Dict[str, Any], hide_full_miss_symbol: bool = False) -> Dict[str, Any]:
    """portfolio_list / detail.html / 市場セクション共通のトレンド表示情報を組み立てる。

    返り値の各キー:
        expr: ◎ / ◯ / ▲ / △ / — の単一記号
        tooltip: 不通過/通過項目 + 10WMA乖離率を改行で結合した文字列
        kairi_gauge_svg: -25%〜+25% のバーゲージ + 中央記号オーバーレイ SVG
    """
    from ks_util import (
        trend_symbol_from_misses, format_kairi_wma10, kairi_gauge_svg,
    )
    # trend_template が未生成 / 欠損している銘柄を「◎ (完全通過)」と誤表示しないよう、
    # 非 list を [] に変換せず、未評価として trend_symbol_from_misses に渡して "—" を返す。
    misses = (stock or {}).get("trend_template")
    expr = trend_symbol_from_misses(misses) if stock else "—"
    gauge_symbol = "" if hide_full_miss_symbol and expr == "×" else expr
    # ◯ は不通過が少ないので不通過項目、△ は通過が少ないので通過項目を出す。
    # ◎=全通過で項目なし、▲=中間帯でノイズ、—=未評価。
    tooltip_src = misses if (expr == "◯" and isinstance(misses, list)) else []
    pass_src = []
    if expr == "△" and isinstance(misses, list):
        miss_set = set(misses)
        pass_src = [c for c in _TREND_TEMPLATE_CONDITIONS if c not in miss_set]
    raw = (stock or {}).get("price_kairi_wma10")
    kairi_raw = raw if isinstance(raw, (int, float)) else None
    kairi_str = format_kairi_wma10(kairi_raw) or "—"
    # 10日MA乖離 (点線マーカー) + 30日10ma維持判定
    raw_ma10 = (stock or {}).get("price_kairi_ma10")
    kairi_ma10 = raw_ma10 if isinstance(raw_ma10, (int, float)) else None
    ma10_streak = bool((stock or {}).get("ma10_above_streak_30"))
    ma10_streak_ever = bool((stock or {}).get("ma10_streak_ever"))
    tooltip_lines = []
    core_misses = _STAGE2_CORE_MISSES & set(misses) if isinstance(misses, list) else set()
    if core_misses:
        tooltip_lines.append("Stage2コア未達: " + ",".join(
            condition for condition in _TREND_TEMPLATE_CONDITIONS
            if condition in core_misses
        ))
    if tooltip_src:
        tooltip_lines.append("不通過: " + ",".join(tooltip_src))
    if pass_src:
        tooltip_lines.append("通過: " + ",".join(pass_src))
    tooltip_lines.append("10WMA乖離: " + kairi_str)
    tooltip_lines.append("10日MA乖離: " + (format_kairi_wma10(kairi_ma10) or "—"))
    if ma10_streak:
        tooltip_lines.append("赤太点線: 10ma 30日維持中")
    elif ma10_streak_ever:
        tooltip_lines.append("黒太点線: 10ma 30日維持実績あり")
    return {
        "expr": expr,
        "tooltip": "\n".join(tooltip_lines),
        "kairi_gauge_svg": kairi_gauge_svg(
            kairi_raw, gauge_symbol, kairi_ma10=kairi_ma10,
            ma10_streak=ma10_streak, ma10_streak_ever=ma10_streak_ever,
        ),
    }


@lru_cache(maxsize=1024)
def _is_nikkei225_from_cached_master_html(code_s: str) -> bool:
    """既存 stocks_shelve 互換用に株探基本情報HTMLキャッシュから225区分を読む。"""
    try:
        from ks_util import DATA_DIR
        from master import _NIKKEI225_RE  # 取得側と同じ判定式を使う
    except Exception:  # noqa: BLE001
        return False
    path = os.path.join(
        DATA_DIR,
        "stock_data",
        "kabutan",
        "base",
        f"https:__kabutanjp_stock_?code={code_s}.html",
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return bool(_NIKKEI225_RE.search(f.read()))
    except OSError:
        return False


def _classify_market_category(
    market: Optional[str],
    is_nikkei225: Any,
    *,
    code_s: Optional[str] = None,
) -> str:
    """保有銘柄の運用総額内訳用に、市場カテゴリを判定する。

    日経225 → グロース → プライム/TOPIX (225除外済み) → その他 の順。

    is_nikkei225 が None の旧DBは、株探基本情報HTMLキャッシュから225区分を補完する。
    明示的な False は更新済みデータとして尊重し、キャッシュ補完しない。

    実DB (stocks_shelve) の market 値は株探由来の全角短縮形
    (東証Ｐ / 東証Ｇ / 東証Ｓ 等) で保存される。念のため長い表記
    (東証プライム / 東証グロース) も前方一致で吸収する。
    """
    market = market or ""
    if is_nikkei225 is None and code_s:
        is_nikkei225 = _is_nikkei225_from_cached_master_html(code_s)
    if is_nikkei225:
        return "日経225"
    if market.startswith(("東証Ｇ", "東証グロース")):
        return "グロース"
    if market.startswith(("東証Ｐ", "東証プライム")):
        return "TOPIX"
    return "その他"


def summarize_hold_positions(
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """保有 (1保) の運用総額と市場別内訳を集計する (issue #362)。

    list_portfolio_with_indicators の position_value と同一条件
    (1保 かつ qty > 0 かつ price > 0) で集計する。指標計算・チャート生成を
    伴わないため、日次バッチからも軽量に呼べる。

    Returns:
        {"total_value": float, "category_values": {"日経225": float, ...}}
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    records = ps.list_records(status="1保", db_path=db_path)
    code_list = [r.get("code_s", "") for r in records]
    stock_map = _bulk_get_stock_data(code_list)

    category_values = {"日経225": 0.0, "TOPIX": 0.0, "グロース": 0.0, "その他": 0.0}
    total_value = 0.0
    for rec in records:
        code_s = rec.get("code_s", "")
        qty = rec.get("qty", 0) or 0
        stock = stock_map.get(code_s) or {}
        price = stock.get("price") if isinstance(stock, dict) else None
        if qty <= 0 or not isinstance(price, (int, float)) or price <= 0:
            continue
        value = float(price) * qty
        category = _classify_market_category(
            stock.get("market"), stock.get("is_nikkei225"), code_s=code_s
        )
        category_values[category] = category_values.get(category, 0.0) + value
        total_value += value
    return {"total_value": total_value, "category_values": category_values}


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
            "trend_template_misses": None,
            "trend_template_tooltip": "—",
            "kairi_gauge_svg": "",
            "tags": "—",
            "signal_mark": "—",
            "signal_full": "",
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

    trend_info = build_trend_info(stock, hide_full_miss_symbol=True)
    trend_misses = stock.get("trend_template")
    trend_expr = trend_info["expr"]

    market_cap_raw = market_cap if isinstance(market_cap, (int, float)) else None
    gyoseki_quarity_expr = _gyoseki_quarity_expr_safe(stock)

    # make_signal は tags 列とシグナル表示の両方で使うため1回だけ呼ぶ (issue #253)
    # tag_reasons はタグ列 tooltip 用の発生理由 (判定時点の値で make_signal が書き込む)
    tag_reasons: Dict[str, str] = {}
    try:
        from make_stock_db import make_signal  # 遅延 import
        _signal, _tags = make_signal(stock, reasons=tag_reasons)
    except Exception:  # noqa: BLE001
        _tags = None
    signal_mark, signal_full = _format_signal(stock)
    # tags / monthly_tag は表示値と tooltip の両方で使うため1回だけ組み立てる
    tags_expr = _format_tags(stock, _tags)
    monthly_tag = _format_monthly_tag(_tags)

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
        "trend_template": "" if trend_expr == "×" else trend_expr,
        "trend_template_misses": trend_misses if isinstance(trend_misses, list) else None,
        "trend_template_tooltip": trend_info["tooltip"],
        "kairi_gauge_svg": trend_info["kairi_gauge_svg"],
        "tags": tags_expr,
        "tags_tooltip": _format_tags_tooltip(tag_reasons),
        # 月足位置タグ (issue #53) はタグ列から分離し、メモページの月足列に出す
        "monthly_tag": monthly_tag,
        "monthly_tag_tooltip": MONTHLY_TAG_DESCRIPTIONS.get(monthly_tag, ""),
        "signal_mark": signal_mark,
        "signal_full": signal_full,
        "signal_display": _build_signal_display(stock),  # issue #253: tooltip+背景色
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

    # --- トレンド: Stage 2コア未達は黄系の記号表示より優先する。
    trend = row.get("trend_template") or ""
    trend_misses = row.get("trend_template_misses")
    trend_miss_set = set(trend_misses) if isinstance(trend_misses, list) else set()
    core_miss_count = len(_STAGE2_CORE_MISSES & trend_miss_set)
    if trend_misses is None:
        styles["trend_template"] = bg("赤")
    elif core_miss_count == len(_STAGE2_CORE_MISSES):
        styles["trend_template"] = bg("青")
    elif core_miss_count:
        styles["trend_template"] = bg("水色")
    elif "◎" in trend:
        styles["trend_template"] = bg("濃黄")
    elif "◯" in trend:
        styles["trend_template"] = bg("薄黄")

    # --- シグナル (ルール 2-7): 強い色から順に評価
    tags = row.get("tags") or ""
    if "最" in tags:
        styles["tags"] = bg_with_white("赤")
    elif "売" in tags:
        # 売 (2条件成立) は青、警 (1条件のみ) は水色。トレンド列の強弱と同じ対応。
        styles["tags"] = bg_with_white("青")
    elif "警" in tags:
        styles["tags"] = bg_with_white("水色")
    elif "押" in tags:
        styles["tags"] = f"color:{PORTFOLIO_COLORS['青']}"
    signal_mark = row.get("signal_mark") or ""
    if signal_mark and signal_mark != "—":
        sig_style = (row.get("signal_display") or {}).get("style")
        styles["signal"] = sig_style or bg_with_white("赤")

    # --- 月足: 月破 (低位滞留からのブレイク) だけ薄赤でやや目立たせる
    if row.get("monthly_tag") == "月破":
        styles["monthly_tag"] = bg("薄赤")

    # --- 時価総額 (ルール 29, 30): カテゴリ "中" / "大" → 薄黄 (極小/小/特大は色なし)
    cat = row.get("market_cap_category")
    if cat in ("中", "大"):
        styles["market_cap"] = bg("薄黄")

    return styles


# ==================================================
# issue #219: 銘柄詳細ページ「現在の調査材料」セクション
# ==================================================

# code_rank.csv の元ラベル → UI 短縮ラベル
# 値そのものに意味が埋め込まれている列は空文字 (ラベル省略してそのまま値を出す)
_CR_LABEL_MAP = {
    "タグ": "タグ",
    "順位": "順位",
    "過去順位(1日/5日前)": "過去",
    "シグナル": "シグナル",
    "総合PT": "総合PT",
    "プロフィット/クォリティ": "プロフィット",
    "バリュー/サイズ": "バリュー",
    "モメンタム(現在.20日比/5日比)": "モメンタム",
    "ファンダメンタル": "ファンダ",
    "トレンドテンプレート": "トレンド",
    "ローソク足ボラティリティ(20,5)": "ボラ",
    "売り圧力レシオ(20,5) 買い集め(週,日) 50DMA乖離率": "売り圧/買集/50DMA",
    "業績(今季/今四半期 売上/営利成長率)": "売上/営利成長率",
    "進捗率(現四半期/売上(前年)利益(前年)": "進捗",
    "指標(時価総額|PER|EVR|ROE|売上高営業利益率|有利子負債自己負債比率|自己資本比率)": "",
    "理論株価(乖離率|上限,下限))": "",
    "過去業績(5年増収増益 4Q増収増益率)": "",
    "信用(倍率|出来高買残比)": "",
    "テーマ": "",
    "更新日(業績|指標|価格)": "更新日",
    "セクター": "セクター",
}

# グループ定義: (グループ名, [code_rank.csv 元ラベル, ...])
_CR_GROUPS = [
    ("ランク", ["タグ", "順位", "過去順位(1日/5日前)", "シグナル"]),
    ("スコア", [
        "総合PT", "プロフィット/クォリティ", "バリュー/サイズ",
        "モメンタム(現在.20日比/5日比)", "ファンダメンタル",
    ]),
    ("テクニカル", [
        "トレンドテンプレート",
        "ローソク足ボラティリティ(20,5)",
        "売り圧力レシオ(20,5) 買い集め(週,日) 50DMA乖離率",
    ]),
    ("業績", [
        "業績(今季/今四半期 売上/営利成長率)",
        "進捗率(現四半期/売上(前年)利益(前年)",
    ]),
    ("指標", ["指標(時価総額|PER|EVR|ROE|売上高営業利益率|有利子負債自己負債比率|自己資本比率)"]),
    ("理論株価", ["理論株価(乖離率|上限,下限))"]),
    ("過去業績", ["過去業績(5年増収増益 4Q増収増益率)"]),
    ("信用", ["信用(倍率|出来高買残比)"]),
    ("テーマ", ["テーマ"]),
    ("更新日", ["更新日(業績|指標|価格)", "セクター"]),
]


def get_current_research_data(code_s, stock_data=None, portfolio_status=None):
    """銘柄詳細ページ用に「現在の調査材料」(= code_rank.csv 相当) を取得する。

    stocks_shelve から都度計算する read-only ヘルパ。
    戻り値: ``[(group_name, [(short_label, value), ...]), ...]``。
    stocks_shelve 未登録や必要キー欠落時は None を返す。

    Args:
        code_s: 銘柄コード
        stock_data: detail.py 側で既に取得済みなら渡す (二重 open 回避)
        portfolio_status: portfolio_shelve の status 文字列 ("1保"/"2準"/"3監")
                          detail.py の portfolio_status を渡す (parse_my_portforio
                          の全件スキャン回避 + excluded 整合)

    issue #219.
    """
    import make_stock_db
    import make_market_db

    if stock_data is None:
        stock_data = get_stock_data(code_s)
    if not stock_data:
        return None
    # スコア計算 (list_all_db と同じ make_stock_db.compute_total_pt を共有)
    try:
        gyoseki_pt = int(stock_data["score_gyoseki"])
        shihyo_pt = stock_data["shihyo_pt"]
        mom_pt = stock_data.get("momentum_pt", 0)
        funda_pt = stock_data.get("funda_pt", 0)
        total_pt = make_stock_db.compute_total_pt(gyoseki_pt, shihyo_pt, mom_pt, funda_pt)
    except (KeyError, TypeError):
        return None

    # 現在順位は stock_rank_log の先頭 (最新)。make_stock_db.get_rank_log で取得
    rank_entry = make_stock_db.get_rank_log(stock_data, "stock_rank_log", 0)
    rank = rank_entry[1] if rank_entry and len(rank_entry) >= 2 else ""

    # ポートフォリオ ports は detail.py 既取得の portfolio_status から組み立てる。
    # 値マッピング: "1保" -> 保, "3監"/"2準" -> 監 (CSV ロジックと同じ)
    pf_stocks = [code_s] if portfolio_status in ("3監", "2準") else []
    possess_list = [code_s] if portfolio_status == "1保" else []

    market_db = make_market_db.get_market_db()

    row_dict = make_stock_db.build_code_rank_row(
        code_s,
        stock_data,
        total_pt=total_pt,
        gyoseki_pt=gyoseki_pt,
        shihyo_pt=shihyo_pt,
        mom_pt=mom_pt,
        funda_pt=funda_pt,
        rank=rank,
        pf_stocks=pf_stocks,
        possess_list=possess_list,
        market_db=market_db,
    )

    # グループ構造に整形 (空グループは行ごと省略)
    groups = []
    for group_name, keys in _CR_GROUPS:
        items = []
        for key in keys:
            value = row_dict.get(key, "")
            # 数値は str 化、その他はそのまま
            if isinstance(value, (int, float)):
                value = str(value)
            short_label = _CR_LABEL_MAP.get(key, key)
            items.append((short_label, value))
        # 全 item の value が空ならグループ自体スキップ
        if any(v for _, v in items):
            groups.append((group_name, items))
    return groups


# ===========================================
# 業態テーマ別 RS サマリー (issue #283)
# ===========================================

# sort_key (UI/URL) → 並べ替えに使う集計フィールド名。許可キーの真実はここに集約し、
# ルート側の allowlist もこの keys() を参照する (二重定義を避ける)。
THEME_SUMMARY_SORT_FIELDS = {
    "momentum": "momentum_pt_avg",
    "dev_1d": "dev_1d_avg",
    "dev_a": "dev_a_avg",
    "dev_b": "dev_b_avg",
}


def _avg_or_none(values: List[float]) -> Optional[float]:
    """None を含まない値リストの平均。空なら None。"""
    return sum(values) / len(values) if values else None


def build_portfolio_theme_summary(
    records: Optional[List[Dict[str, Any]]] = None,
    sort_key: str = "momentum",
) -> List[Dict[str, Any]]:
    """portfolio_shelve のユニバースを memo['gyoutai_themes'] でグルーピングし、
    テーマごとの中長期 (momentum_pt) + 短期 (rs_line スロープ) 集約指標と
    上位リーダー株を返す (issue #283)。

    Args:
        records: portfolio_shelve.list_records(include_excluded=False) の戻り値。
            None なら関数内で取得する (テスト時に注入できるよう引数化)。
        sort_key: "momentum" | "dev_1d" | "dev_a" | "dev_b"。並べ替えキー。

    Returns:
        list[dict]: 各テーマの集約 dict。並び順は sort_key に従う
            (None は末尾) → member_count 降順 → テーマ名昇順。
    """
    if records is None:
        import portfolio_shelve as ps  # 遅延 import (循環回避)
        records = ps.list_records(include_excluded=False)

    # テーマ → [code_s, ...] の逆引き (スロット最大2を両方展開、空は無視)
    theme_to_codes: Dict[str, List[str]] = {}
    # 銘柄が属するテーマ数 (ポジションの按分用。2テーマ銘柄は 50/50 で各テーマに計上)
    theme_count_by_code: Dict[str, int] = {}
    for rec in records:
        code_s = rec.get("code_s", "")
        if not code_s:
            continue
        themes = (rec.get("memo") or {}).get("gyoutai_themes") or []
        for t in themes:
            if not isinstance(t, str):
                continue
            name = t.strip()
            if not name:
                continue
            theme_to_codes.setdefault(name, []).append(code_s)
            theme_count_by_code[code_s] = theme_count_by_code.get(code_s, 0) + 1
    if not theme_to_codes:
        return []

    # status ラベル参照用に code_s → status を引けるようにする
    status_by_code = {r.get("code_s", ""): r.get("status", "") for r in records}

    all_codes = sorted({c for codes in theme_to_codes.values() for c in codes})
    # ポジション分母 (全保有合計) 計算用に、テーマ未設定の 1保 銘柄も price を引く
    holding_codes = {
        r.get("code_s", "") for r in records
        if r.get("status") == "1保" and (r.get("qty", 0) or 0) > 0 and r.get("code_s")
    }
    stock_map = _bulk_get_stock_data(sorted(set(all_codes) | holding_codes))
    name_map = _bulk_resolve_stock_names(all_codes)

    # 銘柄単位の保有ポジション (1保 × qty>0 × price>0 のみ)。
    # 条件は list_portfolio_with_indicators の position_value と同一。
    position_value_by_code: Dict[str, float] = {}
    for rec in records:
        code_s = rec.get("code_s", "")
        qty = rec.get("qty", 0) or 0
        if rec.get("status") != "1保" or not code_s or qty <= 0:
            continue
        price = (stock_map.get(code_s) or {}).get("price")
        if isinstance(price, (int, float)) and price > 0:
            position_value_by_code[code_s] = float(price) * qty
    total_position_value = sum(position_value_by_code.values())

    # rs_line 計算の共有リソース (issue #283: N+1 回避、再計算回避)
    try:
        from make_market_db import get_market_db  # 遅延 import (循環回避)
        market_db = get_market_db()
    except Exception:  # noqa: BLE001
        market_db = None
    topix_map = None
    if market_db is not None:
        try:
            from make_stock_db import _topix_close_map  # 遅延 import
            topix_map = _topix_close_map(market_db)
        except Exception:  # noqa: BLE001
            topix_map = None

    # 銘柄ごとの rs_line スロープ (A, B, D) を 1 回だけ計算してキャッシュ。
    # 公開 API compute_rs_line_changes を使う (private 関数には依存しない)。
    # topix_map を渡すことで内部 compute_rs_line の TOPIX マップ再構築を避ける。
    # (a, b) = 今日 vs 直近5日/20日移動平均の乖離率 = 勢いオシレーター。
    # d = 前日比 = 当日の瞬間的な強さ。
    dev_by_code: Dict[str, tuple] = {}
    if market_db is not None and topix_map:
        from make_stock_db import compute_rs_line_changes  # 遅延 import
        for code_s in all_codes:
            stock = stock_map.get(code_s) or {}
            try:
                a, b, d = compute_rs_line_changes(stock, market_db, topix_map=topix_map)
            except Exception:  # noqa: BLE001
                a, b, d = None, None, None
            dev_by_code[code_s] = (a, b, d)

    result: List[Dict[str, Any]] = []
    for theme, codes in theme_to_codes.items():
        members: List[Dict[str, Any]] = []
        mom_values: List[float] = []
        dev_a_values: List[float] = []
        dev_b_values: List[float] = []
        dev_1d_values: List[float] = []
        for code_s in codes:
            stock = stock_map.get(code_s) or {}
            mom = stock.get("momentum_pt")
            if isinstance(mom, (int, float)):
                mom_values.append(float(mom))
            a, b, d = dev_by_code.get(code_s, (None, None, None))
            if a is not None:
                dev_a_values.append(a)
            if b is not None:
                dev_b_values.append(b)
            if d is not None:
                dev_1d_values.append(d)
            members.append({
                "code_s": code_s,
                "stock_name": name_map.get(code_s, "") or "",
                "momentum_pt": float(mom) if isinstance(mom, (int, float)) else None,
                "status": _PORTFOLIO_STATUS_LABEL.get(
                    status_by_code.get(code_s, ""), status_by_code.get(code_s, "")
                ),
                # 展開リストのバッジ色分け用 (hold/semi/watch)
                "status_query": _PORTFOLIO_STATUS_QUERY.get(
                    status_by_code.get(code_s, ""), ""
                ),
            })
        # members を momentum_pt 降順 (None 末尾) → code_s 昇順で並べる
        members.sort(key=lambda m: (
            m["momentum_pt"] is None,
            -(m["momentum_pt"] or 0),
            m["code_s"],
        ))
        # members は momentum_pt 降順済み。先頭から非 None 上位 3 件がリーダー株
        leaders = [m for m in members if m["momentum_pt"] is not None][:3]
        # テーマ内 1保 銘柄の合計ポジション。2テーマ所属銘柄は 50/50 で按分
        # (テーマ間の pct 合計は 100% を超えない)
        position_value = sum(
            position_value_by_code.get(c, 0.0) / (theme_count_by_code.get(c) or 1)
            for c in codes
        )
        result.append({
            "theme": theme,
            "member_count": len(codes),
            "momentum_pt_avg": _avg_or_none(mom_values),
            "momentum_pt_max": max(mom_values) if mom_values else None,
            "dev_1d_avg": _avg_or_none(dev_1d_values),
            "dev_a_avg": _avg_or_none(dev_a_values),
            "dev_b_avg": _avg_or_none(dev_b_values),
            "leaders": leaders,
            "members": members,
            "position_value": position_value,
            "position_pct": (
                position_value / total_position_value * 100.0
                if total_position_value > 0 else 0.0
            ),
            "position_ratio": 0.0,
        })

    # 最大ポジションのテーマ = 100 として塗り幅を正規化 (portfolio 状態列と同方式)
    max_theme_position = max((r["position_value"] for r in result), default=0.0)
    if max_theme_position > 0:
        for r in result:
            r["position_ratio"] = r["position_value"] / max_theme_position * 100.0

    # ソート: sort_key 降順 (None 末尾) → member_count 降順 → テーマ名昇順
    primary = THEME_SUMMARY_SORT_FIELDS.get(sort_key, "momentum_pt_avg")
    result.sort(key=lambda r: (
        r[primary] is None,
        -(r[primary] or 0),
        -r["member_count"],
        r["theme"],
    ))
    return result


_SIDE_LABELS = {"buy": "買", "sell": "売"}


# ===========================================
# issue #387 Phase4b: fill 基準の建玉ラウンド・エピソード再構成
# ===========================================

# trade_kind → 口座種別 ("現物" / "信用")。現引は現物ラウンドに合流させる。
def _fill_account_kind(trade_kind: str) -> str:
    tk = trade_kind or ""
    if tk.startswith("信用"):
        return "信用"
    return "現物"  # 現物 / 現物(単元未満) / 現引


def _episode_pl_from_round(rnd: dict) -> Optional[dict]:
    """クローズ済み建玉ラウンドから calc_trade_summary 互換の損益 dict を作る。

    現物: 平均取得単価法。買い (buy=買付/現引) で加重平均取得単価を積み、売り (sell) で
      実現損益 = Σ(sell_price - avg_cost) * sell_qty。amount = 総取得コスト (金額加重の重み)。
    信用: 各返済 fill が単独で損益確定。楽天=約定単価-建単価(tate_price)、SBI=settle_pl。
      amount = 建玉コスト (Σ tate_price*qty、無ければ約定金額)。

    保有日数は2レイヤーある:
      - ラウンド単位 (戻り値の hold_days): open_date〜close_date。現物・信用とも算出する。
      - fill 単位 (f["hold_days"]): 建日〜決済日。**信用のみ**。信用は返済 fill と建玉が
        tate_date/tate_price で1対1に対応するため個別に出せるが、現物は売却 fill が
        どの買いに対応するかCSVに情報が無く、平均取得単価法で損益を近似するため
        建日を紐付けられない。テンプレート側も現物行は日数列を空欄にする。

    Returns: {return_pct, hold_days, avg_cost, amount, profit_amount, ...} / 算出不能なら None
    """
    fills = rnd["fills"]
    if not fills:
        return None
    kind = rnd["kind"]
    total_cost = 0.0        # 取得コスト合計 (現物=買い金額, 信用=建玉金額) → 金額加重の重み
    realized = 0.0          # 実現損益額

    if kind == "現物":
        held_qty = 0
        avg_cost = 0.0
        cost_basis_total = 0.0  # 買いで積んだ延べ取得コスト (return_pct の分母 amount)
        for f in fills:
            qty = f["qty"]
            price = f["price"]
            if f["side"] == "buy":
                new_qty = held_qty + qty
                avg_cost = (avg_cost * held_qty + price * qty) / new_qty if new_qty else 0.0
                held_qty = new_qty
                cost_basis_total += price * qty
            else:  # sell
                sell_qty = min(qty, held_qty) if held_qty > 0 else qty
                if held_qty > 0 and avg_cost > 0:
                    fill_pl = (price - avg_cost) * sell_qty
                    f["fill_pl"] = round(fill_pl)
                    f["fill_return_pct"] = fill_pl / (avg_cost * sell_qty) * 100
                realized += (price - avg_cost) * sell_qty
                held_qty -= qty
        total_cost = cost_basis_total
    else:  # 信用
        # 買建は返済 sell で、売建 (空売り) は返済 buy で損益が確定する。
        # 売建は「高く売って安く買い戻す」ので損益の符号が買建と逆になる。
        is_short = rnd.get("is_short", False)
        settle_side = "buy" if is_short else "sell"
        for f in fills:
            if f["side"] != settle_side:
                continue  # 建玉側の fill。損益は返済 fill 側で確定
            qty = f["qty"]
            price = f["price"]
            settle_pl = f.get("settle_pl")
            tate_price = f.get("tate_price")
            if settle_pl is not None:
                realized += settle_pl
                f["fill_pl"] = settle_pl
                total_cost += (tate_price or price) * qty
            elif tate_price is not None:
                # 売建は (建単価 - 買戻単価)、買建は (返済単価 - 建単価)
                fill_pl = ((tate_price - price) if is_short
                           else (price - tate_price)) * qty
                realized += fill_pl
                f["fill_pl"] = round(fill_pl)
                total_cost += tate_price * qty
            else:
                # 建単価も決済損益も無い → 損益不能
                return None
            if tate_price is not None:
                f["fill_return_pct"] = f["fill_pl"] / (tate_price * qty) * 100
            tate_date = f.get("tate_date")
            if tate_date:
                try:
                    f["hold_days"] = (date.fromisoformat(f["trade_date"]) - date.fromisoformat(tate_date)).days
                except (ValueError, TypeError):
                    pass

    if total_cost <= 0:
        return None
    return_pct = realized / total_cost * 100
    try:
        hold_days = (date.fromisoformat(rnd["close_date"])
                     - date.fromisoformat(rnd["open_date"])).days
    except (ValueError, TypeError, KeyError):
        hold_days = None
    # avg_cost = amount (取得/建玉コスト) ÷ その コストに対応する株数。
    # 現物は買い株数、信用は「返済した建玉」の株数で割る。信用の amount は返済 fill の
    # 建玉コストだけを積むため、現引で現物へ振り替えた分は amount に入らない。分母を
    # 建玉株数にすると現引がある銘柄で粒度が食い違い avg_cost が実態より低く出る。
    if kind == "信用":
        settle_side = "buy" if rnd.get("is_short") else "sell"
        open_qty = sum(f["qty"] for f in fills if f["side"] == settle_side)
    else:
        open_qty = sum(f["qty"] for f in fills if f["side"] == "buy")
    return {
        "return_pct": return_pct,
        "hold_days": hold_days if hold_days is not None else 0,
        "avg_cost": total_cost / max(open_qty, 1),
        "amount": total_cost,
        "profit_amount": round(realized),
    }


def _episode_open_pl(rnd: dict, current_price: Optional[float]) -> Optional[dict]:
    """保有中 (未クローズ) 建玉ラウンドの実現損益 (部分売り分) と含み損益を計算する。

    実現損益: ラウンド内で既に売った分の確定損益 (現物=平均取得単価法、信用=建単価/settle_pl)。
    含み損益: 残っている建玉 × (current_price - 取得基準単価)。current_price は price_log の
      直近終値。取得基準は 現物=平均取得単価、信用=平均建単価。current_price が無ければ
      含みは None (実現分は出す)。

    Returns: {realized, unrealized, held_qty, avg_cost, cost_basis_total, return_pct}
      / 建玉も売りも無ければ None。unrealized は current_price 不明なら None。

    return_pct は保有中ラウンドの暫定リターン = (実現 + 含み) / 延べ取得コスト。
    クローズ済みの pl.return_pct と分母の考え方を揃えてあり、同じ列に並べて比較できる。
    含みが出せない (現在値不明・建玉方向が交錯) 場合は None。
    """
    fills = rnd["fills"]
    if not fills:
        return None
    kind = rnd["kind"]

    cost_basis_total = 0.0  # 延べ取得コスト (return_pct の分母、クローズ済み amount と同義)
    if kind == "現物":
        held_qty = 0
        avg_cost = 0.0
        realized = 0.0
        for f in fills:
            qty = f["qty"]
            price = f["price"]
            if f["side"] == "buy":
                new_qty = held_qty + qty
                avg_cost = (avg_cost * held_qty + price * qty) / new_qty if new_qty else 0.0
                held_qty = new_qty
                cost_basis_total += price * qty
            else:  # sell (部分売り)
                sell_qty = min(qty, held_qty) if held_qty > 0 else qty
                if held_qty > 0 and avg_cost > 0:
                    fill_pl = (price - avg_cost) * sell_qty
                    f["fill_pl"] = round(fill_pl)
                    f["fill_return_pct"] = fill_pl / (avg_cost * sell_qty) * 100
                realized += (price - avg_cost) * sell_qty
                held_qty -= qty
        cost_basis = avg_cost
    else:  # 信用
        # 建玉側と決済側は買建/売建で逆になる。売建 (空売り) は新規売で建て返済買で閉じる。
        is_short = rnd.get("is_short", False)
        open_side = "sell" if is_short else "buy"
        settle_side = "buy" if is_short else "sell"
        # 建玉方向と逆の返済が混ざると held_qty が実態とずれ含み評価が不正確になるため、
        # そのラウンドは含みを算出しない (安全側)。売建ラウンドを分離した今、これは
        # 「売建が無いのに返済買がある」等の想定外パターンのみが該当する。
        has_reverse_settle = any(
            f["side"] == open_side and (f.get("trade_kind") or "").startswith("信用返済")
            for f in fills
        )
        held_qty = 0
        avg_cost = 0.0
        realized = 0.0
        for f in fills:
            qty = f["qty"]
            price = f["price"]
            if f["side"] == open_side and (f.get("trade_kind") or "").startswith("信用新規"):
                new_qty = held_qty + qty
                avg_cost = (avg_cost * held_qty + price * qty) / new_qty if new_qty else 0.0
                held_qty = new_qty
                cost_basis_total += price * qty
            elif (f.get("trade_kind") or "") == "現引" and not is_short:
                # 現引は建玉を現物へ振り替える。信用側では建玉が減るだけで損益は
                # 確定しない (取得原価ごと現物ラウンドへ持ち越す)。ここで減算しないと
                # 現引後も建玉が残るラウンドで held_qty が実態より多くなる。
                # 振り替えた分の取得コストも現物ラウンド側で積み直されるので、信用側の
                # 分母から抜く (抜かないと銘柄単位の通算で同じコストを二重計上する)。
                held_qty -= qty
                cost_basis_total -= avg_cost * qty
            elif f["side"] == settle_side:  # 信用返済 (部分返済)
                settle_pl = f.get("settle_pl")
                tate_price = f.get("tate_price")
                if settle_pl is not None:
                    realized += settle_pl
                    f["fill_pl"] = settle_pl
                elif tate_price is not None:
                    # 売建は (建単価 - 買戻単価)、買建は (返済単価 - 建単価)
                    fill_pl = ((tate_price - price) if is_short
                               else (price - tate_price)) * qty
                    realized += fill_pl
                    f["fill_pl"] = round(fill_pl)
                else:
                    # 建単価不明時は平均建単価で近似
                    fill_pl = ((avg_cost - price) if is_short
                               else (price - avg_cost)) * qty
                    realized += fill_pl
                    f["fill_pl"] = round(fill_pl)
                if tate_price is not None:
                    f["fill_return_pct"] = f["fill_pl"] / (tate_price * qty) * 100
                tate_date = f.get("tate_date")
                if tate_date:
                    try:
                        f["hold_days"] = (date.fromisoformat(f["trade_date"]) - date.fromisoformat(tate_date)).days
                    except (ValueError, TypeError):
                        pass
                held_qty -= qty
        cost_basis = avg_cost
        if has_reverse_settle:
            return {
                "realized": round(realized),
                "unrealized": None,  # 建玉方向が交錯し含み評価不能
                "held_qty": held_qty if held_qty > 0 else 0,
                "avg_cost": cost_basis,
                "cost_basis_total": cost_basis_total,
                "return_pct": None,  # 含みが出せないので暫定リターンも出せない
            }

    if _is_qty_closed(held_qty):
        # 保有中扱いだが実質建玉が残っていない (空売り等・分割換算後の丸め誤差含む) → 含み対象なし
        return {
            "realized": round(realized),
            "unrealized": None,
            "held_qty": held_qty if held_qty > 0 else 0,
            "avg_cost": cost_basis,
            "cost_basis_total": cost_basis_total,
            # 建玉が残っていないので実現分だけで暫定リターンが確定する
            "return_pct": (realized / cost_basis_total * 100) if cost_basis_total > 0 else None,
        }

    unrealized = None
    if current_price is not None and cost_basis > 0:
        # 売建 (空売り) は現在値が下がるほど含み益なので符号が逆になる
        diff = ((cost_basis - current_price) if rnd.get("is_short")
                else (current_price - cost_basis))
        unrealized = round(diff * held_qty)

    return_pct = None
    if unrealized is not None and cost_basis_total > 0:
        return_pct = (realized + unrealized) / cost_basis_total * 100

    return {
        "realized": round(realized),
        "unrealized": unrealized,
        "held_qty": held_qty,
        "avg_cost": cost_basis,
        "cost_basis_total": cost_basis_total,
        "return_pct": return_pct,
    }


# ===========================================
# issue #398: 株式分割・併合対応
# ===========================================

_SPLIT_PRICE_JUMP_RATIO = 3.0  # 隣接単価がこの倍数以上/以下に飛べば分割・併合の疑い
_SPLIT_QTY_ZERO_TOL = 1e-6  # 換算後 float qty のクローズ判定許容誤差


def _is_qty_closed(qty: float) -> bool:
    """建玉が0に戻ったとみなせるか判定する (qty <= 0 の許容誤差付き版)。

    分割・併合換算で qty が float になった場合、丸め誤差で厳密な0にならず
    5.55e-17 のような残差が残ってクローズ判定を取り逃す (issue #398)。
    整数 fill のみの既存経路では qty <= 0 と等価に振る舞う。
    """
    return qty <= _SPLIT_QTY_ZERO_TOL


def _is_fractional_residual(qty: float) -> bool:
    """分割・併合後の端株精算で消える想定の1株未満残高か判定する。"""
    return _SPLIT_QTY_ZERO_TOL < qty < 1.0


def _is_genbutsu_qty_closed(qty: float) -> bool:
    """現物残高が実質クローズ済みか判定する。

    分割・併合比率によって 0.3333 株のような端株が残る場合、証券会社CSVには
    整数株の売却だけが出て端株精算が fill として入らないことがある。残高上は
    クローズ扱いにするが、損益は精算額を確認できないため別途 suspect にする。
    """
    return _is_qty_closed(qty) or _is_fractional_residual(qty)


def _detect_price_jumps(fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """現物 fill を約定日順に見て、建玉が継続したまま隣接単価が3倍以上/1/3以下に
    飛ぶ箇所を検出する。

    分割・併合は取引として記録されないため、単価の断絶が唯一の痕跡になる。
    133件の実データで検証済み (誤検出0件、1491 中外鉱業のみ検出、issue #398)。

    建玉を一度売り切ってから (残高0) 数年後に買い直した場合、その間の株価変動は
    分割・併合と無関係な通常の値上がり・値下がりであり分割候補ではない
    (PRレビュー対応)。残高を追跡し、直前の fill で残高が0になっていた場合は
    ジャンプ判定をスキップする。

    Returns: [{"before_date", "before_price", "after_date", "after_price"}]
    """
    genbutsu = sorted(
        (f for f in fills if not (f.get("trade_kind") or "").startswith("信用")),
        key=lambda f: (f.get("trade_date") or "", f.get("seq") or 0),
    )
    jumps = []
    held_qty = 0.0
    for a, b in zip(genbutsu, genbutsu[1:]):
        held_qty += a["qty"] if a["side"] == "buy" else -a["qty"]
        pa, pb = a.get("price"), b.get("price")
        if pa and pb and not _is_genbutsu_qty_closed(held_qty):
            ratio = pb / pa
            if ratio >= _SPLIT_PRICE_JUMP_RATIO or ratio <= 1 / _SPLIT_PRICE_JUMP_RATIO:
                jumps.append({
                    "before_date": a.get("trade_date"),
                    "before_price": pa,
                    "after_date": b.get("trade_date"),
                    "after_price": pb,
                })
    return jumps


def _uncovered_jumps(jumps: List[Dict[str, Any]],
                     events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """検知した単価ジャンプのうち、登録済みイベントでカバーされていないものを返す。

    「登録済みイベントが1件でもあれば安全」という判定は粗く、同一銘柄で後日
    発生した別の分割・併合を見逃す (PRレビュー対応)。ジャンプの日付境界
    (before_date, after_date] に ex_date が入る登録イベントがあればカバー済み。
    """
    return [
        jump for jump in jumps
        if not any(jump["before_date"] < ev["ex_date"] <= jump["after_date"]
                   for ev in events)
    ]


def _jump_affects_episode(jump: Dict[str, Any], ep: Dict[str, Any]) -> bool:
    """未カバーの単価ジャンプが、このエピソードの残高・損益に影響するか。

    ジャンプの (before_date, after_date] がエピソードの期間 [open_date, close_date]
    (保有中は close_date なし=無期限) と重なる場合のみ影響する。分割前に完結した
    無関係なラウンドまで split_suspect で隠さないため (PRレビュー対応)。
    """
    close_date = ep.get("close_date") or "9999-12-31"  # 保有中は無期限
    return jump["before_date"] < close_date and jump["after_date"] >= ep["open_date"]


def _split_event_affects_episode(ex_date: str, ep: Dict[str, Any]) -> bool:
    """pending の ex_date がエピソード期間に含まれるか判定する。"""
    close_date = ep.get("close_date") or "9999-12-31"  # 保有中は無期限
    return ep["open_date"] < ex_date <= close_date


def _apply_split_adjustments(fills: List[Dict[str, Any]],
                             events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """現物 fill のうち各イベントの ex_date より前のものを比率換算したコピーを返す。

    events は ex_date 昇順。各 fill には、自身の trade_date より後の ex_date を
    持つ全イベントの比率を掛け合わせた累積比率を適用する (古いイベントから順に)。
    数量 = qty * cum_ratio、単価 = price / cum_ratio、amount は不変。
    信用 fill・現引は素通しする (元の fill dict は変更しない)。

    現引は「信用建玉の現物化」で、qty は信用新規側の shinyo_qty 減算と現物側の
    genbutsu_qty 加算の両方に同じ値で使われる (_build_code_episodes)。現引だけ
    換算すると信用新規(未換算)と現引(換算後)で株数基準がずれ、shinyo_qty が
    0に戻らずクローズを取り逃す。現引を除外し分割前基準のまま扱う (簡易な安全策)。

    既知の限界 (PRレビュー #405 で指摘、対応複雑度とのバランスで見送り): 現引後に
    現物のまま分割・併合をまたいで売却すると、現引 fill (未換算) と売却 fill (換算後)
    の株数基準がずれ、端数が誤って保有中に残る可能性がある。現時点の実データでは
    分割検知銘柄 (1491, 9252) に現引が絡むケースは無い。単価変化が3倍以上/1/3以下なら
    _detect_price_jumps が検知するが、それ未満の比率では split_suspect も付かず
    残高が誤ったまま表示されうる。再発したら現引 fill に現物側専用の換算済み
    qty/price を別キーで持たせ、_build_code_episodes 側で使い分ける対応が必要。
    """
    if not events:
        return fills
    adjusted = []
    for f in fills:
        tk = f.get("trade_kind") or ""
        if tk.startswith("信用") or tk == "現引":
            adjusted.append(f)
            continue
        trade_date = f.get("trade_date") or ""
        cum_ratio = 1.0
        for ev in events:
            if trade_date < ev["ex_date"]:
                cum_ratio *= ev["ratio"]
        if cum_ratio == 1.0:
            adjusted.append(f)
            continue
        g = dict(f)
        g["qty"] = f["qty"] * cum_ratio
        g["price"] = f["price"] / cum_ratio
        adjusted.append(g)
    return adjusted


def _build_code_episodes(code_s: str, stock_name: str,
                         fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """1銘柄の fill (信用+現物混在、約定日昇順) を建玉ラウンドのエピソードに分ける。

    信用ラウンドと現物ラウンドを並行管理し、**現引で信用→現物へ建玉を振り替える**:
      - 信用新規買 (buy): 信用建玉を積む
      - 信用返済売 (sell): 信用建玉を減らし、建玉0で信用ラウンドをクローズ (損益確定)
      - 現引 (buy): 信用建玉が残っていればその分を信用ラウンドから抜き (振替、信用側は
        損益計上しない=現物へ持ち越し)、現物ラウンドに現引 buy を積む。信用建玉が0に
        なれば信用ラウンドをクローズ
      - 現物買 (buy): 現物建玉を積む
      - 現物売 (sell): 現物建玉を減らし、建玉0で現物ラウンドをクローズ
    保有0に戻らず残った建玉は保有中エピソードになる。取込対象期間より前に建てた
    信用玉の返済は建約定日で判別し、当期の信用新規と相殺せず期首持越しとして分ける。
    """
    # 約定日昇順。同日内は建玉を作る側 (信用新規・現引・現物買) を先に、玉を減らす側
    # (売り・返済) を後に処理する。信用売建の新規売も先にし、同日の返済買より前に
    # 建玉を作る。現引で現物化してから同日に売るケースにも対応する (6366 相当)。
    def _sort_key(f):
        tk = f.get("trade_kind") or ""
        opens_position = (
            tk.startswith("信用新規")
            or tk == "現引"
            or (f["side"] == "buy" and not tk.startswith("信用返済"))
        )
        return (f.get("trade_date") or "", 0 if opens_position else 1, f.get("seq") or 0)
    fills = sorted(fills, key=_sort_key)
    episodes: List[Dict[str, Any]] = []

    shinyo_fills: List[Dict[str, Any]] = []  # 現ラウンドの信用 fill
    shinyo_qty = 0
    shinyo_peak = 0
    # 信用売建 (空売り) は買建とは別ラウンドで追跡する。新規売で建て、返済買で閉じる。
    # 同一銘柄で買建と売建を同時に持ちうる (両建て) ため、状態を分けないと
    # 売建が買建の建玉を打ち消してラウンドが誤って閉じる (issue #387 レビュー対応)。
    short_fills: List[Dict[str, Any]] = []
    short_qty = 0
    short_peak = 0
    genbutsu_fills: List[Dict[str, Any]] = []  # 現ラウンドの現物 fill
    genbutsu_qty = 0
    genbutsu_peak = 0
    genbutsu_fractional_residual = False
    # 取込済みの信用新規日。これに無い建約定日の返済は、取込前からの持越し玉である。
    shinyo_open_dates = {
        f.get("trade_date")
        for f in fills
        if (f.get("trade_kind") or "").startswith("信用新規")
        and f["side"] == "buy"
        and f.get("trade_date")
    }
    short_open_dates = {
        f.get("trade_date")
        for f in fills
        if (f.get("trade_kind") or "").startswith("信用新規")
        and f["side"] == "sell"
        and f.get("trade_date")
    }
    carry_over_shinyo: Dict[str, List[Dict[str, Any]]] = {}
    carry_over_short_by_tate: Dict[str, List[Dict[str, Any]]] = {}
    carry_over_short_unknown: List[List[Dict[str, Any]]] = []

    def close_shinyo():
        nonlocal shinyo_fills, shinyo_qty, shinyo_peak
        if shinyo_fills:
            episodes.append(_finalize_round(code_s, "信用", stock_name, shinyo_fills, shinyo_peak))
        shinyo_fills = []
        shinyo_qty = 0
        shinyo_peak = 0

    def close_short():
        nonlocal short_fills, short_qty, short_peak
        if short_fills:
            episodes.append(_finalize_round(
                code_s, "信用", stock_name, short_fills, short_peak, is_short=True))
        short_fills = []
        short_qty = 0
        short_peak = 0

    def close_genbutsu():
        nonlocal genbutsu_fills, genbutsu_qty, genbutsu_peak, genbutsu_fractional_residual
        if genbutsu_fills:
            ep = _finalize_round(code_s, "現物", stock_name, genbutsu_fills, genbutsu_peak)
            if genbutsu_fractional_residual:
                ep["split_fractional_residual"] = True
            episodes.append(ep)
        genbutsu_fills = []
        genbutsu_qty = 0
        genbutsu_peak = 0
        genbutsu_fractional_residual = False

    # 銘柄全体の同時保有ピーク (信用買建 + 現物)。ラウンド単位の qty_peak は口座ごと・
    # ラウンドごとの最大なので、信用と現物を同時に持つ銘柄の実際のピークを表せない。
    # 建玉を増減させる各分岐が continue を使うため、次の fill を処理する前に前回の
    # 反映結果を拾う (ループ末尾では拾えない)。
    code_qty_peak = 0

    for f in fills:
        code_qty_peak = max(code_qty_peak, shinyo_qty + genbutsu_qty)
        tk = f.get("trade_kind") or ""
        qty = f["qty"]
        if tk == "現引":
            # 信用建玉 → 現物へ振替。信用側は残っていれば現引分だけ減らす。
            if shinyo_qty > 0:
                # 現引を信用ラウンドの「終了イベント」として明細・日付に反映する。
                # 損益 (_episode_pl_from_round) は返済 sell のみ集計するので side=buy の
                # 現引を加えても損益は不変。close_date/last_trade_date が最後の信用新規日
                # ではなく現引日になる (P2 レビュー対応)。
                shinyo_fills.append(f)
                shinyo_qty -= qty
                if shinyo_qty <= 0:
                    close_shinyo()  # 現引で信用建玉が尽きたらクローズ (損益は現物へ)
            # 現物ラウンドに現引 buy を積む (price=実質取得原価)
            genbutsu_fills.append(f)
            genbutsu_qty += qty
            genbutsu_peak = max(genbutsu_peak, genbutsu_qty)
        elif tk.startswith("信用"):
            tate_date = f.get("tate_date")
            # 売建 (新規売) と、それを閉じる返済買は空売りラウンド側で処理する。
            # 対応する新規売が取込範囲に無い返済買は、期首持越しの売建を閉じたもの。
            if tk.startswith("信用返済") and f["side"] == "buy":
                # 建約定日があれば対応する新規売の有無で確定できる。SBI のように
                # 建約定日が無い場合は、買建が残っていれば従来どおり想定外の混在として
                # 買建側に残し含み評価を無効化する。両方の建玉が無いときだけ持越し売建。
                if ((tate_date and tate_date not in short_open_dates)
                        or (not tate_date and short_qty <= 0 and shinyo_qty <= 0)):
                    if tate_date:
                        carry_over_short_by_tate.setdefault(tate_date, []).append(f)
                    else:
                        # SBI は建約定日を持たないため、返済ごとに独立した期首持越しとする。
                        carry_over_short_unknown.append([f])
                    continue
            is_short_side = (
                (tk.startswith("信用新規") and f["side"] == "sell")
                or (tk.startswith("信用返済") and f["side"] == "buy" and short_qty > 0)
            )
            if is_short_side:
                short_fills.append(f)
                if f["side"] == "sell":
                    short_qty += qty      # 新規売 = 建てる
                else:
                    short_qty -= qty      # 返済買 = 閉じる
                short_peak = max(short_peak, short_qty)
                if short_qty <= 0:
                    close_short()
                continue
            if (tk.startswith("信用返済") and f["side"] == "sell"
                    and tate_date and tate_date not in shinyo_open_dates):
                # 当期に対応する信用新規が無い返済は、当期の建玉を減らしてはいけない。
                # 同一建約定日の分割返済は一つの期首持越しエピソードにまとめる。
                carry_over_shinyo.setdefault(tate_date, []).append(f)
                continue
            shinyo_fills.append(f)
            if f["side"] == "buy":
                shinyo_qty += qty
            else:
                shinyo_qty -= qty
            shinyo_peak = max(shinyo_peak, shinyo_qty)
            if shinyo_qty <= 0:
                close_shinyo()
        else:  # 現物 / 現物(単元未満)
            genbutsu_fills.append(f)
            if f["side"] == "buy":
                genbutsu_qty += qty
            else:
                genbutsu_qty -= qty
            genbutsu_peak = max(genbutsu_peak, genbutsu_qty)
            if _is_genbutsu_qty_closed(genbutsu_qty):
                genbutsu_fractional_residual = _is_fractional_residual(genbutsu_qty)
                close_genbutsu()

    code_qty_peak = max(code_qty_peak, shinyo_qty + genbutsu_qty)  # 最後の fill の反映分

    # 保有中 (残った建玉)
    if shinyo_fills:
        episodes.append(_finalize_round(code_s, "信用", stock_name, shinyo_fills, shinyo_peak, closed=False))
    if short_fills:
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, short_fills, short_peak, closed=False, is_short=True))
    if genbutsu_fills:
        episodes.append(_finalize_round(code_s, "現物", stock_name, genbutsu_fills, genbutsu_peak, closed=False))
    for tate_date, carry_over_fills in carry_over_shinyo.items():
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, carry_over_fills, 0,
            carry_over=True, open_date=tate_date,
        ))
    for tate_date, carry_over_fills in carry_over_short_by_tate.items():
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, carry_over_fills, 0,
            carry_over=True, open_date=tate_date, is_short=True,
        ))
    for carry_over_fills in carry_over_short_unknown:
        episodes.append(_finalize_round(
            code_s, "信用", stock_name, carry_over_fills, 0,
            carry_over=True, is_short=True,
        ))

    # 銘柄単位ビューが使う同時保有ピーク。全エピソードで同じ値 (銘柄の属性)。
    for ep in episodes:
        ep["code_qty_peak"] = code_qty_peak

    return episodes


def fill_date_range_by_broker(db_path: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """証券会社別の取込済み fill の最古・最新約定日を返す (issue #387、取込タイミング参考)。

    Returns: {"楽天": {"first": "2026-01-05", "last": "2026-07-31"}, "SBI": {...}}。
    broker 未設定の既存 fill は「楽天」に寄せる (表示補完と整合)。
    取込 fill が無ければ空 dict。
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    ranges: Dict[str, Dict[str, str]] = {}
    for f in ps.list_fills(db_path=db_path):
        td = f.get("trade_date")
        if not td:
            continue
        broker = f.get("broker") or "楽天"
        r = ranges.setdefault(broker, {"first": td, "last": td})
        if td < r["first"]:
            r["first"] = td
        if td > r["last"]:
            r["last"] = td
    return ranges


def build_fill_episodes(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """全 fill を建玉ラウンド単位のエピソードに再構成する (issue #387 Phase4b)。

    銘柄ごとに信用・現物を同一時系列で処理し、保有 (建玉) 株数が 0 → 建 → 0 に戻る
    1 サイクルを 1 エピソードとする。**現引は信用建玉を現物へ振り替える** (信用側の
    建玉を減らし現物側に取得原価で積む)。信用は返済 fill の建単価/決済損益で損益確定。

    各エピソード dict:
      code_s, stock_name, kind ("現物"/"信用"), open_date, close_date,
      last_trade_date (ラウンド内の最終約定日), qty_peak (最大建玉),
      closed (bool), fills (内部の個別 fill 明細リスト),
      carry_over (bool: 取込対象期間より前の信用建玉の返済),
      pl (クローズ済みのみ: _episode_pl_from_round の結果, 未クローズは None),
      open_pl (保有中のみ: realized/unrealized/held_qty/avg_cost),
      split_suspect (bool, 現物のみ: 単価ジャンプ検知だが split_adj 未登録、issue #398。
        残高・損益が分割・併合未換算で誤っている可能性があるため画面上は数値を隠す)

    Returns: 最終約定日 (買い増し・部分売り含むラウンド内の最新の取引日) 降順の
    エピソードリスト。保有中エピソードも最後に約定した日で並ぶ。
    """
    import portfolio_shelve as ps  # 遅延 import (循環回避)

    all_fills = ps.list_fills(db_path=db_path)
    if not all_fills:
        return []

    # 銘柄コードでグループ化 (信用・現物を同一時系列で処理するため口座種別で分けない)
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for f in all_fills:
        by_code.setdefault(f["code_s"], []).append(f)

    names = _bulk_resolve_stock_names(list(by_code.keys()))

    # issue #398: 分割・併合の疑いがある銘柄を検知し、登録済みイベントがあれば
    # 現物 fill を換算したコピーに差し替えてからエピソード再構成に渡す。
    # 未カバーのジャンプがあれば換算せず既存動作を維持し、その期間と重なる
    # エピソードにのみ split_suspect を付与する (PRレビュー対応:
    # 「登録済みイベントが1件でもあれば安全」という銘柄単位の判定は粗く、同一銘柄で
    # 後日発生した別の分割・併合を見逃す。また銘柄単位で全エピソードに付けると、
    # 分割前に完結した無関係なラウンドの正しい損益まで隠してしまう)。
    # pending_review は --check-splits の (a)単価ジャンプ/(b)エピソード期間総当たりの検知結果を
    # 拒否リストとして反映する (webapp は yfinance を呼ばないため (b) を自力では検知できない)。
    # pending_review の ex_dates がエピソード期間に含まれる場合は、クローズ済みでも
    # split_suspect を維持する。2:1 分割など単価ジャンプ閾値未満のイベントは
    # クローズ後に警告が外れると誤った実現損益が集計へ戻ってしまうため。
    all_split_adj = ps.list_all_split_adjustments(db_path=db_path)
    pending_events = ps.list_pending_review_events(db_path=db_path)
    episodes: List[Dict[str, Any]] = []
    for code_s, fills in by_code.items():
        events = all_split_adj.get(code_s, [])
        if events:
            fills = _apply_split_adjustments(fills, events)
        # ジャンプ検知は換算後の fills に対して行う (PRレビュー対応: 未換算のまま
        # 検知すると、登録済みイベントで残高の基準が変わった後の残高追跡が崩れ、
        # 別の未登録イベントのジャンプを見逃す)。
        jumps = _detect_price_jumps(fills)
        code_episodes = _build_code_episodes(code_s, names.get(code_s, ""), fills)
        uncovered = _uncovered_jumps(jumps, events)
        pending_dates = pending_events.get(code_s, [])
        for ep in code_episodes:
            if ep["kind"] != "現物":
                continue
            if ep.get("split_fractional_residual"):
                ep["split_suspect"] = True
            elif any(_jump_affects_episode(j, ep) for j in uncovered):
                ep["split_suspect"] = True
            elif any(d != "unknown" and _split_event_affects_episode(d, ep)
                     for d in pending_dates):
                ep["split_suspect"] = True
            elif "unknown" in pending_dates and not ep["closed"]:
                ep["split_suspect"] = True
        episodes.extend(code_episodes)

    # 保有中エピソードに実現損益 (部分売り分) と含み損益 (残玉評価) を付与 (issue #387 Phase4b)。
    # 含みは price_log の直近終値を現在値とする。銘柄をバルク取得して N+1 を避ける。
    open_codes = {e["code_s"] for e in episodes if not e["closed"]}
    latest_prices: Dict[str, Optional[float]] = {}
    if open_codes:
        price_logs = _bulk_price_logs(list(open_codes))
        for code_s, log in price_logs.items():
            if log:
                latest = max(log, key=lambda x: x[0])  # (date, close) の最新
                latest_prices[code_s] = float(latest[1])
    for ep in episodes:
        if not ep["closed"]:
            # 往復行 (issue #421) の保有中ロットも現在値で含みを出すため保持する
            ep["current_price"] = latest_prices.get(ep["code_s"])
            ep["open_pl"] = _episode_open_pl(ep, ep["current_price"])

    # 建玉ラウンド単位の振り返りメモ (issue #387 Phase2) を一括で紐付ける。
    # メモは fill と独立レイヤーに保存され、エピソードキーで対応する。
    memos = ps.list_fill_memos(db_path=db_path)
    for ep in episodes:
        ep["episode_key"] = ps.fill_episode_key(
            ep["code_s"], ep["kind"], ep["first_seq"]
        )
        ep["review_memo"] = memos.get(ep["episode_key"], "")

    # 最終約定日 (最新の取引がある順) 降順、同日は銘柄コード昇順
    episodes.sort(key=lambda e: e["code_s"])
    episodes.sort(key=lambda e: e["last_trade_date"], reverse=True)
    return episodes


def _round_trip_days(open_date: Optional[str], close_date: Optional[str]) -> Optional[int]:
    """建日〜決済日の保有日数。どちらか欠けるか不正な日付なら None。"""
    if not open_date or not close_date:
        return None
    try:
        return (date.fromisoformat(close_date) - date.fromisoformat(open_date)).days
    except (ValueError, TypeError):
        return None


def _make_round_trip(open_fill: Optional[Dict[str, Any]], close_fill: Optional[Dict[str, Any]],
                     qty: float, open_date: Optional[str],
                     open_price: Optional[float]) -> Dict[str, Any]:
    """往復1行を組み立てる (issue #421)。

    open 側 (建て) と close 側 (決済) のどちらかが欠ける行もある:
      - close_fill=None: 未決済で残っている建玉 → 「保有中」行
      - open_fill=None:  建玉を特定できない決済 (建情報なしの信用返済、期首持越し)
                         → 売りのみ行
    損益は呼び出し側が close_fill の fill_pl / fill_return_pct から埋めるか、
    現物 FIFO のようにロット単位で計算した値を渡す。
    """
    close_date = close_fill.get("trade_date") if close_fill else None
    return {
        "open_date": open_date,
        "close_date": close_date,
        "qty": qty,
        "open_price": open_price,
        "close_price": close_fill.get("price") if close_fill else None,
        "hold_days": _round_trip_days(open_date, close_date),
        "broker": (close_fill or open_fill or {}).get("broker", ""),
        "closed": close_fill is not None,
        "genbiki": False,  # 現引による現物への振替 (決済ではないので損益を出さない)
        "unrealized": False,  # pl が含み損益 (未確定) かどうか
        "pl": None,
        "return_pct": None,
    }


def _match_open_lots(open_pool: List[Dict[str, Any]], tate_date: Optional[str],
                     tate_price: Optional[float],
                     broker: Optional[str] = None) -> List[Dict[str, Any]]:
    """建日・建単価に一致する建玉を古い順に返す (issue #421)。

    証券会社CSVの建玉情報 (tate_date/tate_price) で引き当てる。同じ建日に複数の
    新規がある場合は建単価でも絞る (4258 の 02-13 に 3,200/3,100 の2本があるなど)。

    **建情報が指定されているのに一致しなければ未照合として扱う** (空リストを返す)。
    別ロットへフォールバックすると、CSVが指定した建玉ではない玉が「決済済み」になり、
    実際に残っている建玉が逆になる (PRレビュー指摘)。建情報が無い場合のみ、残っている
    建玉を古い順に返す (FIFO フォールバック)。

    broker を渡すと同じ証券会社の建玉だけを対象にする。同一銘柄を複数社で同時保有
    しているとき (実データで22エピソード)、他社の建玉と突き合わせると誤った建値の
    リターンを出し、残っている証券会社も逆になる (PRレビュー指摘)。
    他社へのフォールバックはしない — 建玉が取込範囲外の決済が別の証券会社の建玉を
    消費すると、架空の損益を出したうえでその建玉の保有株数まで消えるため。
    """
    pool = [c for c in open_pool if c["remain"] > 0
            and (not broker or c["fill"].get("broker") == broker)]
    if tate_date is None and tate_price is None:
        return pool  # 建情報なし → FIFO で古い順に充当
    return [c for c in pool
            if (not tate_date or c["fill"].get("trade_date") == tate_date)
            and (tate_price is None or c["fill"].get("price") == tate_price)]


def _consume_open_lots(open_pool: List[Dict[str, Any]], qty: float,
                       tate_date: Optional[str] = None,
                       tate_price: Optional[float] = None,
                       broker: Optional[str] = None) -> List[tuple]:
    """建玉プールから qty 株を引き当て、[(建玉fill, 引当株数), ...] を返す (issue #421)。

    1本の返済が複数ロットにまたがる場合は順に按分する。先頭ロットから全数量を
    引くと残数が負になり、決済済みのロットが「保有中」として残る。
    引き当てきれなかった分 (取込範囲外の建玉など) は返り値に含めない。
    broker を渡すと同じ証券会社の建玉を優先して引き当てる。
    """
    taken: List[tuple] = []
    remain = qty
    for cand in _match_open_lots(open_pool, tate_date, tate_price, broker):
        if remain <= 0:
            break
        take = min(remain, cand["remain"])
        cand["remain"] -= take
        remain -= take
        taken.append((cand["fill"], take))
    if remain > 0 and (tate_date is not None or tate_price is not None):
        # 建情報に一致する建玉が取込範囲内で足りない。建玉の総数は釣り合っているのに
        # CSVの建情報が実際の建玉と対応しないことがある (4377: 建1000/返済900+現引100
        # で差0なのに、一部の返済の tate_date が別の建玉を指す)。ここで消費しないと
        # 決済済みの建玉が「保有中」として残る。**建値は CSV の値を使う**ので、
        # どのロットを消費したかは表示に影響しない。
        for cand in _match_open_lots(open_pool, None, None, broker):
            if remain <= 0:
                break
            take = min(remain, cand["remain"])
            cand["remain"] -= take
            remain -= take
    return taken


def _build_shinyo_round_trips(ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """信用エピソードの往復行を作る (issue #421)。

    信用返済 fill は証券会社CSV由来の tate_date / tate_price を持つため、
    **FIFO 等で推定せずこの対応をそのまま使う**。これが証券会社が実際に決済した
    建玉の対応であり、推定するとリターンが実態とずれる (4258 の 07/06 返済は
    04/08 建 → 89日 → +54.43% だが、FIFO では 06/16 の返済に当たってしまう)。

    建情報を持たない返済 (SBI 等で 17/487 本) は建玉を特定できないため、
    推測でペアを作らず売りのみ行として出す (誤った建値でリターンを出すほうが有害)。
    ただし建玉自体は消費するので、決済済みの玉が「保有中」行として残ることはない。
    """
    is_short = ep.get("is_short", False)
    settle_side = "buy" if is_short else "sell"
    # 建玉側 fill を建日ごとに残株数付きで保持し、返済の tate_date と突き合わせる。
    # 同じ建日に複数の新規がある場合は建単価でも絞る (4258 02/13 の 3,200/3,100 など)。
    open_pool: List[Dict[str, Any]] = []
    for f in ep["fills"]:
        if f["side"] != settle_side and f.get("trade_kind", "").startswith("信用新規"):
            open_pool.append({"fill": f, "remain": f["qty"]})

    rows: List[Dict[str, Any]] = []
    for f in ep["fills"]:
        # 現引は信用建玉を現物へ振り替える取引。side="buy" なので返済側の分岐に
        # 入らないが、建玉は消える。ここで建玉を減らさないと振替済みの玉が
        # 「保有中」行として残り続ける (4258 の 2025-03-19 建玉が該当)。
        # 損益は現物側へ持ち越すため、信用側では計上しない。
        if f.get("trade_kind") == "現引":
            # 現引もCSVの建玉情報を持つ (6366 の 05-11 現引は 04-22 建玉が対象)。
            # 先頭から機械的に消費すると別の建玉を消してしまい、実際に振り替えた
            # 玉が「保有中」行として残る。建情報があればそれで引き当てる。
            taken = _consume_open_lots(open_pool, f["qty"], f.get("tate_date"),
                                       f.get("tate_price"), f.get("broker"))
            for cf, take in taken:
                row = _make_round_trip(cf, f, take,
                                       f.get("tate_date") or cf.get("trade_date"),
                                       f.get("tate_price") or cf.get("price"))
                row["genbiki"] = True  # 決済ではなく現物への振替
                rows.append(row)
            remain = f["qty"] - sum(t for _, t in taken)
            if remain > 0:
                # 対応する建玉が取込範囲に無い現引 (期首持越し玉の現引)
                row = _make_round_trip(None, f, remain, f.get("tate_date"),
                                       f.get("tate_price"))
                row["genbiki"] = True
                rows.append(row)
            continue
        if f["side"] != settle_side:
            continue
        tate_date = f.get("tate_date")
        tate_price = f.get("tate_price")
        # 建情報 (tate_date/tate_price) が無い返済は建玉を特定できない (SBI 等で
        # 17/487 本)。ただし取込範囲内に建玉が残っていれば、その玉を決済したのは
        # 明らかなので FIFO で引き当てて建玉を消費する。消費しないと決済済みの玉が
        # 「保有中」行として残り続ける (9984 売建の 2026-05-22 返済買が該当)。
        # 建値・保有日数は推測せず伏せる (誤った建値でリターンを出すほうが有害)。
        if tate_date is None and tate_price is None:
            # 建玉は消費するが建値は伏せる
            _consume_open_lots(open_pool, f["qty"], broker=f.get("broker"))
            row = _make_round_trip(None, f, f["qty"], None, None)
            if "fill_pl" in f:
                row["pl"] = f["fill_pl"]
            rows.append(row)
            continue
        # 対応する建玉 fill を建日 (+建単価) で引き当て、残株数を減らす。
        # 表示上の建日はCSVの tate_date を正とする (建玉 fill が取込範囲外でも出せる)。
        # 同じ建日・建単価の新規が複数本あり返済が先頭ロットの残数を超える場合は、
        # 候補ロットへ順に按分する。先頭から全数量を引くと残数が負になり、後続ロットが
        # 決済済みなのに「保有中」として残る (PRレビュー指摘)。
        matched = _consume_open_lots(open_pool, f["qty"], tate_date, tate_price,
                                     f.get("broker"))
        first = matched[0][0] if matched else None
        open_price = tate_price if tate_price is not None else (
            first["price"] if first else None)
        open_date = tate_date or (first.get("trade_date") if first else None)
        row = _make_round_trip(first, f, f["qty"], open_date, open_price)
        # 損益は既存の fill 単位計算をそのまま使う (計算ロジックを二重化しない)。
        if "fill_pl" in f:
            row["pl"] = f["fill_pl"]
        if "fill_return_pct" in f:
            row["return_pct"] = f["fill_return_pct"]
        if "hold_days" in f:
            row["hold_days"] = f["hold_days"]
        rows.append(row)

    # 決済されずに残った建玉は「保有中」行
    for cand in open_pool:
        if cand["remain"] > 0:
            cf = cand["fill"]
            rows.append(_make_round_trip(cf, None, cand["remain"],
                                         cf.get("trade_date"), cf.get("price")))
    return rows


def _oldest_lot(lots: List[Dict[str, Any]],
                broker: Optional[str]) -> Optional[Dict[str, Any]]:
    """FIFO キューから充当対象のロットを選ぶ (issue #421)。

    同じ証券会社の最古ロットを返す。無ければ None (他社ロットへは充当しない)。
    他社へフォールバックすると、買付が取込範囲外の売却が別の証券会社の建玉を
    消費し、架空の損益を出したうえでその建玉の保有株数まで消える
    (楽天100株保有 + SBI売却のみ → 楽天の建値で +150,000円 と誤表示、PRレビュー指摘)。
    lots は買付順に並んでいる前提 (先頭が最古)。
    """
    for lot in lots:
        if not broker or lot["fill"].get("broker") == broker:
            return lot
    return None


def _build_genbutsu_round_trips(ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """現物エピソードの往復行を FIFO (先入先出) で作る (issue #421)。

    現物の売り fill には建単価・建日が無く、どの買いに対応するかCSVに情報が無い。
    FIFO で古い買いから順に充当する (税務・証券会社の考え方に近く、ロットごとの
    リターンが実際の建値を反映するため)。

    **注意**: エピソードの実現損益 (_episode_pl_from_round) は平均取得単価法で
    計算されており、部分売却で建玉が残るラウンドでは往復行の損益合計と一致しない
    (100@100買→100@200買→100@150売100株 で FIFO +5,000 / 平均法 0)。
    全株売却されれば総額は一致する。振り返り目的では実際の建値を反映する FIFO を
    優先し、不一致は許容する方針 (issue #421)。
    """
    lots: List[Dict[str, Any]] = []  # FIFO キュー: {fill, remain}
    rows: List[Dict[str, Any]] = []
    for f in ep["fills"]:
        if f["side"] == "buy":
            lots.append({"fill": f, "remain": f["qty"]})
            continue
        # 売り: 古いロットから充当。1つの売りが複数ロットにまたがる場合は行を分ける。
        # 同一銘柄を複数社で同時保有していると、他社の買いロットと突き合わせて誤った
        # 建値のリターンを出してしまう (実データで7行該当)。同じ証券会社のロットを
        # 優先し、無ければ従来どおり最古のロットへ充当する (PRレビュー指摘)。
        remain = f["qty"]
        while remain > 0 and lots:
            lot = _oldest_lot(lots, f.get("broker"))
            if lot is None:
                break  # 同社の買いロットが無い → 建玉不明の売りとして下で処理
            take = min(remain, lot["remain"])
            bf = lot["fill"]
            row = _make_round_trip(bf, f, take, bf.get("trade_date"), bf.get("price"))
            cost = bf["price"] * take
            profit = (f["price"] - bf["price"]) * take
            row["pl"] = round(profit)
            row["return_pct"] = (profit / cost * 100) if cost else None
            rows.append(row)
            remain -= take
            lot["remain"] -= take
            if lot["remain"] <= 0:
                lots.remove(lot)
        if remain > 0:
            # 充当できる買いが無い (取込範囲外で取得した株の売却など)
            rows.append(_make_round_trip(None, f, remain, None, None))

    # 売られずに残ったロットは「保有中」行
    for lot in lots:
        if lot["remain"] > 0:
            bf = lot["fill"]
            rows.append(_make_round_trip(bf, None, lot["remain"],
                                         bf.get("trade_date"), bf.get("price")))
    return rows


def _set_unrealized(open_rows: List[Dict[str, Any]], current_price: Optional[float],
                    is_short: bool) -> None:
    """保有中ロットに含み損益を設定する (issue #421)。

    残数量 × (現在値 - 建値)。売建 (空売り) は「高く売って安く買い戻す」ので符号が逆。
    現在値が取れない、建値が不明 (取込範囲外の建玉) の行は None のままにする。
    エピソード行の含み損益 (_episode_open_pl) と定義を揃えてある。
    """
    if current_price is None:
        return
    for r in open_rows:
        if r["open_price"] is None or not r["open_price"]:
            continue
        diff = ((r["open_price"] - current_price) if is_short
                else (current_price - r["open_price"]))
        r["pl"] = round(diff * r["qty"])
        r["return_pct"] = diff / r["open_price"] * 100
        r["unrealized"] = True  # 確定損益ではなく評価額 (テンプレートで淡く出す)


def build_round_trips(ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """エピソードの fill を「買→売」の往復1行に畳む (issue #421)。

    実際に下した売買判断の単位で明細を読めるようにするのが目的。
    信用は証券会社CSVの建玉対応 (tate_date/tate_price)、現物は FIFO で対応づける。

    並び順: 保有中 (未決済) を先頭に、続いて決済済みを決済日の降順 (最新が上)。
    保有中はまだ決済日が無く「最新」の側なので、決済済みより上に置く。
    保有中どうし・決済済みどうしは買付日/決済日の降順。

    保有中ロットには現在値 (ep["current_price"]) から含み損益を付ける。
    エピソード行の含み損益と定義を揃える (残数量 × (現在値 - 建値)、売建は符号が逆)。
    """
    if ep["kind"] == "信用":
        rows = _build_shinyo_round_trips(ep)
    else:
        rows = _build_genbutsu_round_trips(ep)
    closed = [r for r in rows if r["closed"]]
    open_rows = [r for r in rows if not r["closed"]]
    _set_unrealized(open_rows, ep.get("current_price"), ep.get("is_short", False))
    closed.sort(key=lambda r: (r["close_date"] or "", r["open_date"] or ""), reverse=True)
    open_rows.sort(key=lambda r: r["open_date"] or "", reverse=True)
    return open_rows + closed


def build_stock_rollups(episodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """建玉ラウンド・エピソードを銘柄単位に集約する (issue #391)。

    build_fill_episodes() の結果を code_s でグループ化し、銘柄ごとに畳んだ
    集約 dict を返す。損益は各エピソードが持つ pl / open_pl を足し合わせるだけで、
    fill からの再計算はしない (計算ロジックの二重化を避ける)。

    pl の対象は calc_trade_summary の母数定義 (ep["closed"] and ep["pl"]) と完全に
    一致させる。これにより実現損益合計・期待値がエピソード単位と銘柄単位で厳密に
    一致する (金額加重ゆえグループ化に依存しない)。
    """
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for ep in episodes:
        by_code.setdefault(ep["code_s"], []).append(ep)

    rollups: List[Dict[str, Any]] = []
    for code_s, eps in by_code.items():
        # split_suspect (分割・併合の疑いだが未換算) は残高・損益が誤っている可能性が
        # あるため、この銘柄の全集計から一貫して除外する (issue #398)。実現損益だけ
        # 除外して含み損益は含める、といった半端な状態にすると 9252 のように銘柄行の
        # 中で基準が食い違う。エピソード単位ビューも split_suspect の数値は — にする。
        agg_eps = [ep for ep in eps if not ep.get("split_suspect")]
        priced = [ep["pl"] for ep in agg_eps if ep["closed"] and ep["pl"]]
        if priced:
            amount = sum(p["amount"] for p in priced)
            profit_amount = sum(p["profit_amount"] for p in priced)
            pl = {
                "return_pct": profit_amount / amount * 100,
                "hold_days": sum(p["hold_days"] for p in priced),
                "amount": amount,
                "profit_amount": profit_amount,
            }
        else:
            pl = None

        open_pls = [ep["open_pl"] for ep in agg_eps
                    if not ep["closed"] and ep.get("open_pl")]
        open_realized = sum(op["realized"] for op in open_pls)
        unrealized_values = [op["unrealized"] for op in open_pls if op["unrealized"] is not None]
        if not open_pls or not unrealized_values:
            open_unrealized = None
        else:
            open_unrealized = sum(unrealized_values)
        open_unrealized_partial = bool(unrealized_values) and len(unrealized_values) < len(open_pls)
        held_qty = sum(op["held_qty"] for op in open_pls)

        # 保有中も含めた通算リターン。クローズ済み (pl) と保有中 (open_pl) の損益・
        # 分母を足して1つの % にする。エピソード単位の保有中リターンと定義を揃えてあり、
        # 銘柄単位でもリターン列が「残N株」で潰れず数値で読める。
        # 保有中エピソードは全件の含みが出せるときだけ通算に混ぜる。1件でも欠けると
        # その分の取得コストだけが分母に乗って過小評価になる (9252 のように価格が
        # 取れない銘柄が該当)。保有中が無い場合は 0 == 0 で成立し確定値と一致する。
        total_amount = (pl["amount"] if pl else 0.0) + sum(
            op.get("cost_basis_total") or 0.0 for op in open_pls)
        total_profit = (pl["profit_amount"] if pl else 0.0) + open_realized + sum(
            unrealized_values)
        if total_amount > 0 and len(unrealized_values) == len(open_pls):
            total_return_pct = total_profit / total_amount * 100
        else:
            total_return_pct = None

        eps_sorted = sorted(eps, key=lambda e: e["last_trade_date"], reverse=True)
        rollups.append({
            "code_s": code_s,
            "stock_name": eps[0]["stock_name"],
            "episodes": eps_sorted,
            "episode_count": len(eps),
            "kinds": sorted({ep["kind"] for ep in eps}),
            "first_open_date": min(ep["open_date"] for ep in eps),
            "last_trade_date": max(ep["last_trade_date"] for ep in eps),
            # 銘柄の同時保有ピーク (信用買建 + 現物)。_build_code_episodes が時系列
            # 走査中に実測した値を使う。ラウンド単位の qty_peak の max では、信用と
            # 現物を同時に持つ銘柄 (6890: 現物100+信用100) でピークを取り逃す。
            "qty_peak": max([ep.get("code_qty_peak") or ep["qty_peak"] for ep in eps]),
            "has_open": any(not ep["closed"] for ep in eps),
            "has_carry_over": any(ep.get("carry_over") for ep in eps),
            "memo_count": sum(1 for ep in eps if ep.get("review_memo")),
            "pl": pl,
            # 実現損益の総額 = クローズ済み確定分 + 保有中エピソードの部分売り確定分。
            # 消費側 (銘柄単位ビュー・CLI) がこの合算を各自で書くと、片方を足し忘れて
            # 過少表示になる (4258 が確定分のみで +103,847 円、6890 が — になっていた
            # バグ)。定義をここ1箇所に置く。確定分も部分売りも無ければ None。
            "realized_total": (
                (pl["profit_amount"] if pl else 0) + open_realized
                if (pl and pl["profit_amount"] is not None) or open_realized
                else None
            ),
            "open_realized": open_realized,
            "open_unrealized": open_unrealized,
            "open_unrealized_partial": open_unrealized_partial,
            "held_qty": held_qty,
            "total_return_pct": total_return_pct,
        })

    rollups.sort(key=lambda r: r["code_s"])
    rollups.sort(key=lambda r: r["last_trade_date"], reverse=True)
    return rollups


def _finalize_round(code_s: str, kind: str, stock_name: str,
                    round_fills: List[Dict[str, Any]], qty_peak: int,
                    closed: bool = True, carry_over: bool = False,
                    open_date: Optional[str] = None,
                    is_short: bool = False) -> Dict[str, Any]:
    """建玉ラウンドの fill リストからエピソード dict を組み立てる。

    is_short=True は信用売建 (空売り) のラウンド。建玉は新規売、決済は返済買で、
    損益の符号が買建と逆になる (_episode_pl_from_round で分岐)。
    """
    dates = [f["trade_date"] for f in round_fills if f.get("trade_date")]
    # ラウンド固有のキー用に先頭 fill の seq を取る (建玉開始時に確定し不変)。
    seqs = [f.get("seq") for f in round_fills if f.get("seq") is not None]
    first_seq = min(seqs) if seqs else 0
    ep = {
        "code_s": code_s,
        "stock_name": stock_name,
        "kind": kind,
        "first_seq": first_seq,
        "open_date": open_date or (min(dates) if dates else ""),
        "close_date": max(dates) if (dates and closed) else None,
        "last_trade_date": max(dates) if dates else "",  # ラウンド内の最新約定日 (並び順の基準)
        "qty_peak": qty_peak,
        "closed": closed,
        "carry_over": carry_over,
        "is_short": is_short,
        "fills": [
            {
                "trade_date": f.get("trade_date"),
                "side": f["side"],
                "side_label": _SIDE_LABELS.get(f["side"], f["side"]),
                "qty": f["qty"],
                "price": f["price"],
                "trade_kind": f.get("trade_kind", ""),
                # 既存の楽天取込 fill は broker 追加前で未設定 (None)。未設定は「楽天」で
                # 補完する (SBI取込は必ず broker="SBI" を持つ、P2 レビュー対応)。
                "broker": f.get("broker") or "楽天",
                "tate_price": f.get("tate_price"),
                "tate_date": f.get("tate_date"),
                "settle_pl": f.get("settle_pl"),
                # 銘柄全体の保有サイクル再生 (_current_hold_cycle) の dedup キーに必要。
                "seq": f.get("seq"),
                "dedup_key": f.get("dedup_key"),
            }
            for f in round_fills
        ],
    }
    ep["pl"] = _episode_pl_from_round(ep) if closed else None
    return ep


def calc_trade_summary(episode_pls: list) -> Optional[dict]:
    """エピソード損益 dict のリストから成績サマリーを算出する。

    勝ち = return_pct > 0、負け = return_pct <= 0 (0% は負け)。
    ペイオフレシオは金額加重: 勝ち群 Σ(return_pct×amount)/Σamount ÷ |負け群同値|。
    母数0 → None (サマリー非表示)。
    """
    if not episode_pls:
        return None

    wins = [p for p in episode_pls if p["return_pct"] > 0]
    loses = [p for p in episode_pls if p["return_pct"] <= 0]
    n_total = len(episode_pls)

    def _weighted_avg_return(group):
        total_amount = sum(p["amount"] for p in group)
        if total_amount <= 0:
            return None
        return sum(p["return_pct"] * p["amount"] for p in group) / total_amount

    def _avg_hold(group):
        return sum(p["hold_days"] for p in group) / len(group) if group else None

    win_weighted = _weighted_avg_return(wins)
    lose_weighted = _weighted_avg_return(loses)
    if win_weighted is None or lose_weighted is None or lose_weighted == 0:
        payoff_ratio = None
    else:
        payoff_ratio = win_weighted / abs(lose_weighted)

    # 期待値 = 1 トレードあたりの平均リターン% (全トレードの金額加重平均)。
    # 勝率とペイオフを統合した手法の総合的な優位性。プラスならトータルで優位。
    expectancy = _weighted_avg_return(episode_pls)

    return {
        "win_rate": len(wins) / n_total * 100,
        "payoff_ratio": payoff_ratio,
        "expectancy": expectancy,
        # 勝ち/負けの金額加重平均リターン% (ペイオフレシオの分子・分母)
        "avg_return_win": win_weighted,
        "avg_return_lose": lose_weighted,
        "avg_hold_win": _avg_hold(wins),
        "avg_hold_lose": _avg_hold(loses),
        "n_total": n_total,
        "n_win": len(wins),
        "n_lose": len(loses),
    }
