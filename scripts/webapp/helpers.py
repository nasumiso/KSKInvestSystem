"""
DB読み書きヘルパー。

research_shelve のデータ取得・更新をWebアプリ用にラップする。
排他制御は research_shelve._flock() を共用し、Web側とバッチ側で
同じロックファイルを取ることでプロセス間の安全な共存を保証する。
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional

from db_shelve import STOCKS_SHELVE, ShelveDB
from html_sanitizer import sanitize_html
from ks_util import get_price_day, log_warning
from research_shelve import (
    get_research_record,
    upsert_research_record,
    create_research_record,
    create_snapshot,
    list_research_records,
    validate_code_s,
    normalize_code_s,
    validate_rating,
    _flock,
    VALID_RATINGS,
)


def get_research_detail(code_s: str) -> Optional[Dict[str, Any]]:
    """1銘柄の調査レコードを取得する。"""
    validate_code_s(code_s)
    return get_research_record(code_s)


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
        for i in range(8):
            comment = form_data.get(f"shikiho_comments_{i}", "").strip()
            period = form_data.get(f"shikiho_periods_{i}", "").strip()
            if comment:
                comments.append({"period": period, "comment": comment})
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
