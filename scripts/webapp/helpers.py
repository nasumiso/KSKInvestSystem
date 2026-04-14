"""
DB読み書きヘルパー。

research_shelve のデータ取得・更新をWebアプリ用にラップする。
排他制御は research_shelve._flock() を共用し、Web側とバッチ側で
同じロックファイルを取ることでプロセス間の安全な共存を保証する。
"""

from typing import Any, Dict, List, Optional

from html_sanitizer import sanitize_html
from research_shelve import (
    get_research_record,
    upsert_research_record,
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


def search_records(
    *,
    rating: Optional[str] = None,
    keyword: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """銘柄調査レコードをフィルタ検索する。"""
    return list_research_records(rating=rating, keyword=keyword)


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
        record["memo"] = sanitize_html(form_data.get("memo", ""))
        record["openwork"] = sanitize_html(form_data.get("openwork", ""))
        record["cramer"] = form_data.get("cramer", "")

        upsert_research_record(record)


def save_shikiho(code_s: str, form_data: dict) -> None:
    """四季報フィールドを更新する。

    対象: overview, shikiho_comments (最大5件)
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)

    with _flock():
        record = get_research_record(normalized)
        if record is None:
            raise ValueError(f"レコード未登録: {normalized}")

        record["overview"] = form_data.get("overview", "")

        comments: List[str] = []
        for i in range(5):
            key = f"shikiho_comments_{i}"
            val = form_data.get(key)
            if val is not None:
                stripped = val.strip()
                if stripped:
                    comments.append(stripped)
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
                snap["ir_comment"] = sanitize_html(form_data[form_key])

        record["snapshots"] = snapshots
        upsert_research_record(record)
