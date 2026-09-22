#!/usr/bin/env python3
"""四季報コメント・IR問い合わせ回答を読み取り専用で提供する stdio MCP サーバー。"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
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
    sort_ir_qa_desc,
    sort_shikiho_comments_desc,
)
from db_shelve import RESEARCH_SHELVE
from ks_util import DATA_DIR

logger = logging.getLogger(__name__)

# ir_docs.py は収集用に requests / pypdf を読み込むため import せず、
# 読み取り専用の本サーバーでは保存先だけを同じ規則で組み立てる。
IR_DOCS_DIR = Path(DATA_DIR) / "ir_docs"

mcp = MCPServer(
    "shintakane-shikiho",
    instructions=(
        "Shintakane の四季報コメントと業績予想を読み取り専用で返します。"
        "period は四季報の版情報であり時点情報ではありません。"
        "四季報コメントの as_of は常に null です。"
        "gyoseki は四季報の業績予想 (単位: 百万円) で、特に next_year (2期先) は"
        "不確実性が高く、定量的な評価指標としてではなく、会社の成長シナリオを読む"
        "ための定性的な材料として扱ってください。"
        "updated_at は入力日なので、古い場合は予想が陳腐化している可能性があります。"
        "IR問い合わせ回答 (get_ir_qa) は非公開の一次情報で、answered_at は実際の回答日です。"
        "決算資料 (list_earnings_documents / get_earnings_document) は収集済みのものに"
        "限られます。coverage_status が not_collected なら未収集であり、資料が存在しない"
        "ことを意味しません。partial_coverage が true のときは coverage_through 以降が"
        "未収集で、最新の資料が欠けている可能性があります。"
        "決算説明資料はスライド形式で図表が主体のため、返されるテキストには"
        "グラフや表の中の数値が含まれないことがあります。テキストに項目名だけがあり"
        "対応する数値が見当たらない場合、その数値は資料に存在しないのではなく"
        "抽出できていないと考えてください。数値の裏取りが必要な分析では、"
        "テキストから読み取れた範囲を明示し、local_path の PDF をユーザーに添付"
        "してもらうよう促してください。local_path はこのサーバーを動かしている端末の"
        "パスであり、あなたが直接開くことはできません。"
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


def _format_gyoseki(gyoseki: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """四季報業績予想を MCP の返却形式へ整形する。未入力なら None。

    raw_text (貼り付け原文) は AI に渡す価値が薄くトークンを食うため返さない。
    """
    if not isinstance(gyoseki, dict):
        return None
    return {
        "unit": "百万円",
        "prev_year": gyoseki.get("prev_year"),
        "this_year": gyoseki.get("this_year"),
        "next_year": gyoseki.get("next_year"),
        "updated_at": gyoseki.get("updated_at"),
    }


def get_shikiho_data(code_s: str, limit: int = 8) -> Dict[str, Any]:
    """指定銘柄の四季報コメントと業績予想を MCP の返却形式へ整形する。"""
    record = get_research_record_locked(code_s)
    if record is None:
        return {
            "code_s": code_s.strip().upper(),
            "found": False,
            "source": "research_shelve",
            "total_comments": 0,
            "shikiho_comments": [],
            "gyoseki": None,
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
        "gyoseki": _format_gyoseki(record.get("shikiho_gyoseki")),
        "source": "research_shelve",
        "total_comments": len(comments),
    }


def get_ir_qa_data(code_s: str, limit: int = 10) -> Dict[str, Any]:
    """指定銘柄のIR問い合わせ回答を MCP の返却形式へ整形する。"""
    record = get_research_record_locked(code_s)
    if record is None:
        return {
            "code_s": code_s.strip().upper(),
            "found": False,
            "source": "research_shelve",
            "total_entries": 0,
            "ir_qa": [],
        }

    entries = sort_ir_qa_desc(record.get("ir_qa", []))
    formatted = []
    for item in entries[:_limit(limit, 10)]:
        answered_at = (item.get("answered_at") or "").strip()
        formatted.append(
            {
                "answered_at": answered_at,
                # 四季報の period と違い実日付なので as_of に入れてよい
                "as_of": answered_at or None,
                "body": strip_html_tags(item.get("body", "")),
            }
        )
    return {
        "code_s": record["code_s"],
        "found": True,
        "stock_name": record.get("stock_name", ""),
        "ir_qa": formatted,
        "source": "research_shelve",
        "total_entries": len(entries),
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


def _ir_docs_dir(code_s: str) -> Path:
    """指定銘柄のIR資料ディレクトリを返す。"""
    return IR_DOCS_DIR / code_s.strip().upper()


def _load_ir_index(code_s: str) -> Optional[Dict[str, Any]]:
    """index.json を読む。未収集なら None。

    壊れている・読めない場合も None を返すが、「未収集」と混同させないため
    呼び出し側が区別できるよう空 dict ではなく None に倒し、警告を残す。
    """
    index_path = _ir_docs_dir(code_s) / "index.json"
    if not index_path.exists():
        return None
    try:
        with open(index_path, encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except (OSError, ValueError) as exc:
        logger.warning("IR資料の index.json を読めません: %s (%s)", code_s, exc)
        return None


def _iso_date(value: str) -> Optional[str]:
    """ir_docs の YYYYMMDD を YYYY-MM-DD へ正規化する。"""
    text = (value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _coverage_window(index: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """収集済み区間 [from, through] を求める。

    collected_months は「最後に収集した時点」からの深度であり、現在時点の
    カバレッジではない。収集後に経過した分は最新側が穴になるため、区間で返す。
    """
    # last_collected_at は documents[].date (YYYYMMDD) と違い
    # ISO 8601 のタイムスタンプ ("2026-09-22T18:50:31+09:00")。
    raw = (index.get("last_collected_at") or "").strip()
    months = index.get("collected_months") or 0
    try:
        through = datetime.fromisoformat(raw).date()
    except ValueError:
        return {"from": None, "through": None, "at": None, "discontinuous": False}

    # ir_docs.py は latest 実行でも last_collected_at を現在へ更新する一方、
    # collected_depth はランクが上がるときしか変えない (1y 済みに latest を
    # かけても 1y のまま)。latest が足すのは直近1件だけなので、前回の深い
    # 収集から今日までの間に出た開示には穴が残る。last_collected_at を連続
    # 収集の終端として扱うと、その穴を「収集済み」と偽ってしまう。
    # 収集日そのものは事実なので at に残し、区間の主張だけを取り下げる。
    if index.get("last_requested_depth") == "latest" and months:
        return {
            "from": None, "through": None,
            "at": through.isoformat(), "discontinuous": True,
        }

    return {
        # depth=latest (months=0) は直近1件のみで期間を張らないため from は持たない
        "from": (through - timedelta(days=months * 30)).isoformat() if months else None,
        "through": through.isoformat(),
        "at": through.isoformat(),
        "discontinuous": False,
    }


def list_earnings_documents_data(
    code_s: str, months: int = 12, include_superseded: bool = False,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """指定銘柄の収集済みIR資料一覧を MCP の返却形式へ整形する。"""
    code = code_s.strip().upper()
    # LLM は任意の値を渡せる。大きすぎると日付計算が OverflowError を投げ、
    # 0 以下だと「0ヶ月を要求したので全部収まっている」という無意味な
    # partial_coverage: False になるため、実用的な範囲へ丸める。
    months = min(max(months if isinstance(months, int) else 12, 1), 120)
    index = _load_ir_index(code)
    if index is None:
        return {
            "code_s": code,
            "coverage_status": "not_collected",
            "collected_months": 0,
            "total_documents": 0,
            "documents": [],
            "note": "この銘柄は未収集です。資料の有無は判定できません。",
        }

    documents = index.get("documents") or []
    if not include_superseded:
        documents = [d for d in documents if d.get("is_latest", True)]

    # 期間フィルタ。収集済み総数 (months 無視) は別に返し、
    # 「未収集」「期間内に無いだけ」「本当に0件」を LLM が区別できるようにする。
    total_documents = len(documents)
    cutoff = ((today or date.today()) - timedelta(days=months * 30)).strftime("%Y%m%d")
    in_range = [d for d in documents if (d.get("date") or "") >= cutoff]

    window = _coverage_window(index)
    # 要求区間 [今日-months, 今日] が収集区間に収まらなければ partial。
    # 穴は最新側 (収集日〜今日) に空くため、月数の引き算では判定できない。
    requested_from = ((today or date.today()) - timedelta(days=months * 30)).isoformat()
    requested_through = (today or date.today()).isoformat()
    partial = (
        window["through"] is None
        or window["from"] is None  # depth=latest は期間を張らない
        or window["through"] < requested_through
        or window["from"] > requested_from
    )

    errors = index.get("collection_errors") or {}
    error_count = sum(len(v) for v in errors.values() if isinstance(v, list))

    notes: List[str] = []
    if not partial:
        pass
    elif window.get("discontinuous"):
        notes.append(
            f"過去に期間を遡って収集した後、{window['at']}に直近の資料のみを"
            "追加で収集しています。その間に開示された資料が抜けている可能性があり、"
            "連続した期間の網羅は保証できません。"
        )
    elif not window["through"]:
        notes.append("収集範囲が不明です。資料の網羅性は保証できません。")
    elif not index.get("collected_months"):
        # depth=latest は直近1件のみの取得で、期間の網羅を保証しない
        notes.append(
            f"{window['at']}に直近の資料のみを収集しました。"
            "期間を遡った収集をしていないため、網羅性は保証できません。"
        )
    else:
        # 要求区間のどちら側がはみ出しているかで理由が異なる。
        # 新しい側 = 収集後に開示された分、古い側 = 収集深度より前。
        shortfalls = []
        if window["through"] < requested_through:
            shortfalls.append(
                f"{window['through']}以降に開示された資料は未収集です。"
            )
        if window["from"] > requested_from:
            shortfalls.append(
                f"{window['from']}より前は収集していません "
                f"(収集深度は{index.get('collected_months')}ヶ月)。"
            )
        notes.append(
            f"収集済みの範囲は{window['from']}〜{window['through']}です。"
            + "".join(shortfalls)
            + "この期間の資料の有無は判定できません。"
        )
    if not in_range and total_documents:
        notes.append(
            f"収集済みですが、指定期間 (months={months}) に該当する資料は"
            f"ありません。期間外に{total_documents}件あります。"
        )
    if error_count:
        notes.append(
            f"収集時に{error_count}件の資料でエラーが発生しており、"
            "一覧は不完全な可能性があります。"
        )

    return {
        "code_s": code,
        "coverage_status": "collected",
        "collected_months": index.get("collected_months") or 0,
        "last_collected_at": window["at"],
        "coverage_from": window["from"],
        "coverage_through": window["through"],
        "requested_months": months,
        "partial_coverage": partial,
        "coverage_discontinuous": bool(window.get("discontinuous")),
        "has_collection_errors": bool(error_count),
        "collection_error_count": error_count,
        "total_documents": total_documents,
        "documents": [_format_ir_document(code, d) for d in in_range],
        "note": " ".join(notes) or None,
    }


def _format_ir_document(code_s: str, document: Dict[str, Any]) -> Dict[str, Any]:
    """一覧用に資料メタデータを整形する。テキスト本体は含めない。"""
    iso = _iso_date(document.get("date", ""))
    return {
        "doc_id": document.get("doc_id"),
        "date": iso,
        "as_of": iso,
        "heading": document.get("heading", ""),
        "doc_type": document.get("doc_type"),
        "fiscal_period": document.get("fiscal_period"),
        "quarter": document.get("quarter"),
        "pages": document.get("pages"),
        "total_chars": document.get("total_chars"),
        "text_quality": document.get("text_quality"),
        "is_latest": document.get("is_latest", True),
        "superseded_by": document.get("superseded_by"),
        "local_path": str(_ir_docs_dir(code_s) / document.get("pdf_path", "")),
    }


def get_earnings_document_data(
    code_s: str, doc_id: str, page_from: int = 1,
    page_to: Optional[int] = None, max_chars: int = 15000,
) -> Dict[str, Any]:
    """抽出済みテキストをページ範囲で返す。切り出しは必ずページ境界で行う。"""
    code = code_s.strip().upper()
    index = _load_ir_index(code)
    documents = (index or {}).get("documents") or []
    document = next((d for d in documents if d.get("doc_id") == doc_id), None)
    if document is None:
        return {
            "code_s": code, "doc_id": doc_id, "found": False,
            "text": None,
            "note": "指定された doc_id の資料は収集されていません。",
        }

    local_path = str(_ir_docs_dir(code) / document.get("pdf_path", ""))
    quality = document.get("text_quality")
    # 品質が悪い資料で空文字や文字化けを返すと、LLM が推測で分析を進める。
    # 必ず理由とローカルパスを示し、PDF添付へ誘導する。
    if quality != "ok":
        reason = (
            "画像主体でテキストを抽出できません"
            if quality == "image_based"
            else "テキストが文字化けしており内容を読み取れません"
        )
        return {
            "code_s": code, "doc_id": doc_id, "found": True,
            "text_quality": quality, "text": None,
            "local_path": local_path,
            "note": (
                f"この資料は{reason}。local_path の PDF をユーザーに"
                "添付してもらってください (このパスをあなたが開くことはできません)。"
            ),
        }

    # index.json に載っていてもテキストJSONが無いことはある (保持期間の棚卸しで
    # ファイルだけ消えた、同期途中で欠けている等)。例外を MCP の外へ漏らすと
    # ChatGPT 側は原因不明のエラーになるため、PDF添付へ誘導して返す。
    text_path = _ir_docs_dir(code) / document.get("text_path", "")
    try:
        with open(text_path, encoding="utf-8") as file_obj:
            pages = json.load(file_obj).get("pages") or []
    except (OSError, ValueError) as exc:
        logger.warning("IR資料のテキストを読めません: %s %s (%s)", code, doc_id, exc)
        return {
            "code_s": code, "doc_id": doc_id, "found": True,
            "text_quality": quality, "text": None,
            "local_path": local_path,
            "note": (
                "この資料の抽出済みテキストが見つかりません。"
                "local_path の PDF をユーザーに添付してもらってください "
                "(このパスをあなたが開くことはできません)。"
            ),
        }

    selected, used = [], 0
    for page in pages:
        number = page.get("page")
        if number < page_from or (page_to is not None and number > page_to):
            continue
        text = page.get("text") or ""
        # 1ページが単独で max_chars を超える場合はそのページだけを返す。
        # 超過を許容しないと同じページを返し続けて無限ループする。
        if selected and used + len(text) > max_chars:
            break
        selected.append(page)
        used += len(text)
        if used >= max_chars:
            break

    last_number = selected[-1]["page"] if selected else page_from
    remaining = [
        p for p in pages
        if p.get("page") > last_number
        and (page_to is None or p.get("page") <= page_to)
    ]
    iso = _iso_date(document.get("date", ""))
    return {
        "code_s": code, "doc_id": doc_id, "found": True,
        "text_quality": quality, "as_of": iso,
        "heading": document.get("heading", ""),
        "pages": document.get("pages"),
        "page_from": selected[0]["page"] if selected else page_from,
        "page_to": last_number,
        "truncated": bool(remaining),
        "next_page_from": last_number + 1 if remaining else None,
        "text": "\n".join(p.get("text") or "" for p in selected),
        "local_path": local_path,
    }


@mcp.tool()
def list_earnings_documents(
    code_s: str, months: int = 12, include_superseded: bool = False,
) -> Dict[str, Any]:
    """銘柄コードから収集済みの決算説明資料・決算短信の一覧を返す。

    テキスト本体は含みません。本文は get_earnings_document で取得します。

    coverage_status が not_collected の場合、その銘柄は未収集であり
    「資料が存在しない」ことを意味しません。partial_coverage が true の
    ときは coverage_through 以降が未収集のため、最新の資料が欠けている
    可能性があります。既定では訂正版に置き換えられた旧版を返しません。
    """
    return list_earnings_documents_data(code_s, months, include_superseded)


@mcp.tool()
def get_earnings_document(
    code_s: str, doc_id: str, page_from: int = 1,
    page_to: Optional[int] = None, max_chars: int = 15000,
) -> Dict[str, Any]:
    """決算資料の抽出済みテキストをページ範囲を指定して返す。

    doc_id は list_earnings_documents が返す TDnet ID です。
    truncated が true のとき、続きは next_page_from を page_from に渡して
    取得します。

    返すのは PDF から抽出したテキストのみです。決算説明資料はスライド形式で
    図表が主体のため、グラフや表の中の数値は含まれないことがあります。
    項目名 (例「売上高 営業利益 (単位:百万円)」) だけがあって数値が続かない
    場合、その数値は抽出できていないだけで資料には存在します。

    text が null の場合はテキストを利用できない資料です。いずれの場合も
    local_path はこのサーバーを動かしている端末のパスで、あなたが直接
    開くことはできません。PDF が必要なときはユーザーに添付を依頼してください。
    """
    return get_earnings_document_data(code_s, doc_id, page_from, page_to, max_chars)


@mcp.tool()
def get_shikiho(code_s: str, limit: int = 8) -> Dict[str, Any]:
    """銘柄コードから四季報コメント履歴・事業概要・業績予想を返す。

    period は四季報の版情報であり、正確な時点は不明です。as_of は常に null
    として返します。データは読み取り専用です。

    gyoseki は四季報の業績予想 (単位: 百万円、未入力なら null)。
    prev_year (直前実績) / this_year (今季予想) / next_year (来季予想) を持ち、
    予想には前期比の成長率 (sales_growth / op_growth、%) が付きます。
    特に next_year は2期先であり不確実性が高いため、定量的な評価指標としてではなく、
    会社の成長シナリオを読むための定性的な材料として扱ってください。
    """
    return get_shikiho_data(code_s, limit)


@mcp.tool()
def get_ir_qa(code_s: str, limit: int = 10) -> Dict[str, Any]:
    """銘柄コードからIR部門への問い合わせ回答履歴を新しい順に返す。

    公開情報として流通しない非公開の一次情報です。answered_at は実際の回答日で、
    as_of にも同じ値が入ります。データは読み取り専用です。
    """
    return get_ir_qa_data(code_s, limit)


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
