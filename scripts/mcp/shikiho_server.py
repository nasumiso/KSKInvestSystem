#!/usr/bin/env python3
"""四季報コメントを読み取り専用で提供する stdio MCP サーバー。"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from mcp.server import MCPServer

from html_sanitizer import strip_html_tags
from research_shelve import (
    get_research_record_locked,
    list_research_records_locked,
    normalize_for_search,
    sort_shikiho_comments_desc,
)
from db_shelve import RESEARCH_SHELVE

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "shintakane-shikiho",
    instructions=(
        "Shintakane の四季報コメントを読み取り専用で返します。"
        "period は四季報の版情報であり時点情報ではありません。"
        "as_of は常に null です。"
    ),
)


def _limit(value: int, default: int) -> int:
    """取得件数を安全な範囲に丸める。"""
    if not isinstance(value, int):
        return default
    return min(max(value, 1), 50)


def _period_label(period: str) -> Optional[str]:
    """四季報の版情報を人間可読な表示へ変換する。"""
    parts = period.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        return None
    return f"四季報 20{year:02d}年{month}月号"


def get_shikiho_data(code_s: str, limit: int = 8) -> Dict[str, Any]:
    """指定銘柄の四季報コメントを MCP の返却形式へ整形する。"""
    record = get_research_record_locked(code_s)
    if record is None:
        return {
            "code_s": code_s.strip().upper(),
            "found": False,
            "source": "research_shelve",
            "total_comments": 0,
            "shikiho_comments": [],
        }

    comments = sort_shikiho_comments_desc(record.get("shikiho_comments", []))
    formatted = []
    for item in comments[:_limit(limit, 8)]:
        period = (item.get("period") or "").strip()
        formatted.append(
            {
                "period": period,
                "period_label": _period_label(period),
                "as_of": None,
                "comment": strip_html_tags(item.get("comment", "")),
            }
        )
    return {
        "code_s": record["code_s"],
        "found": True,
        "stock_name": record.get("stock_name", ""),
        "overview": strip_html_tags(record.get("overview", "")),
        "shikiho_comments": formatted,
        "source": "research_shelve",
        "total_comments": len(comments),
    }


def search_stocks_data(query: str, limit: int = 10) -> Dict[str, List[Dict[str, Any]]]:
    """社名またはコードで銘柄を検索し、コード完全一致を最優先する。"""
    query_norm = normalize_for_search(query.strip())
    if not query_norm:
        return {"results": []}

    exact, partial = [], []
    for record in list_research_records_locked():
        code_s = record.get("code_s", "")
        stock_name = record.get("stock_name", "")
        if query_norm == normalize_for_search(code_s):
            target = exact
        elif query_norm in normalize_for_search(stock_name):
            target = partial
        else:
            continue
        comments = record.get("shikiho_comments", [])
        target.append(
            {
                "code_s": code_s,
                "stock_name": stock_name,
                "has_shikiho": bool(comments),
                "comment_count": len(comments),
            }
        )
    return {"results": (exact + partial)[:_limit(limit, 10)]}


@mcp.tool()
def get_shikiho(code_s: str, limit: int = 8) -> Dict[str, Any]:
    """銘柄コードから四季報コメント履歴と事業概要を返す。

    period は四季報の版情報であり、正確な時点は不明です。as_of は常に null
    として返します。データは読み取り専用です。
    """
    return get_shikiho_data(code_s, limit)


@mcp.tool()
def search_stocks(query: str, limit: int = 10) -> Dict[str, List[Dict[str, Any]]]:
    """社名の一部または銘柄コードで、四季報データを持つ銘柄候補を検索する。"""
    return search_stocks_data(query, limit)


def _check_runtime_database() -> None:
    """意図しないリポジトリ内 data/ へのフォールバックを起動時に検出する。"""
    data_dir = os.environ.get("KS_DATA_DIR")
    repository_data = REPOSITORY_ROOT / "data"
    resolved_path = Path(RESEARCH_SHELVE).resolve()
    if not data_dir or resolved_path.is_relative_to(repository_data):
        raise RuntimeError(
            "KS_DATA_DIR が未設定、またはリポジトリ内 data/ を参照しています: "
            f"{resolved_path}"
        )
    count = len(list_research_records_locked())
    if count == 0:
        raise RuntimeError(f"research_shelve が空です: {resolved_path}")
    logger.info("research_shelve: %s (%d records)", resolved_path, count)


if __name__ == "__main__":
    _check_runtime_database()
    mcp.run()
