"""
決算・開示ページ ルート。

GET  /disclosure                     : 決算日カード + 適時開示 (disclosure_data.html) ページ
GET  /api/kessan_comment/<code_s>    : 指定銘柄・決算日のコメントを JSON で返す
POST /api/kessan_comment/<code_s>    : 決算コメントを保存 (新規 or 上書き)
"""

from typing import Any, Dict

from flask import Blueprint, jsonify, render_template, request

import portfolio_shelve as ps
from ks_util import log_warning
from webapp.helpers import (
    get_disclosure_html_parts,
    get_kessan_comment,
    get_market_kessan_data,
    save_kessan_comment,
)
from research_shelve import (
    KESSAN_REACTION_PERIODS,
    VALID_EXPECTATIONS,
    normalize_kessan_post_price_changes,
)


disclosure_bp = Blueprint("disclosure", __name__)


# API レスポンスに含める決算コメントエントリのキー集合
# 旧 post_price_change は API 契約上含めない（_serialize_kessan_entry_for_api で除外）
_API_ENTRY_KEYS = (
    "kessanbi",
    "quarter",
    "pre_expectation",
    "pre_outlook",
    "post_comment",
    "kessan_matagi",
    "held_before_kessan",
    "held_after_kessan",
)


def _serialize_kessan_entry_for_api(entry: Dict[str, Any]) -> Dict[str, Any]:
    """決算コメントエントリを API 用に整形する。

    新スキーマ (post_price_changes) のみを返し、旧 post_price_change キーは含めない。
    旧形式のみのデータを受け取った場合は post_price_changes に正規化して返す。
    """
    result: Dict[str, Any] = {}
    for key in _API_ENTRY_KEYS:
        if key in entry:
            result[key] = entry[key]
    result["post_price_changes"] = normalize_kessan_post_price_changes(entry)
    return result


# バッジを付けるステータス。このページの銘柄は全部が保有・準保有・監視のどれかなので、
# 監視にも付けると全行に並んで区別にならない。
_BADGE_STATUSES = {"1保": ("保有", "hold"), "2準": ("準保有", "semi")}


def _status_badges() -> Dict[str, Dict[str, str]]:
    """保有・準保有の銘柄を {code_s: {"label", "query"}} で返す。読めなければ空。"""
    try:
        records = ps.list_records()
    except Exception as e:  # noqa: BLE001
        log_warning(f"[disclosure] 保有ステータスを読めませんでした: {e}")
        return {}
    badges = {}
    for r in records:
        if r.get("status") in _BADGE_STATUSES:
            label, query = _BADGE_STATUSES[r["status"]]
            badges[r["code_s"]] = {"label": label, "query": query}
    return badges


@disclosure_bp.route("/disclosure", methods=["GET"])
def disclosure_page():
    """決算・開示ページ。決算日カード + 静的 disclosure_data.html を取り込んで表示する。"""
    disclosure_parts = get_disclosure_html_parts()
    kessan_data = get_market_kessan_data()
    expectation_options = sorted(VALID_EXPECTATIONS, key=lambda x: ("", "◎", "○", "▲", "△", "×").index(x))
    return render_template(
        "disclosure.html",
        disclosure_parts=disclosure_parts,
        kessan_data=kessan_data,
        expectation_options=expectation_options,
        status_badges=_status_badges(),
    )


@disclosure_bp.route("/api/kessan_comment/<code_s>", methods=["GET"])
def get_kessan_comment_api(code_s: str):
    """指定銘柄・決算日のコメントを JSON で返す。未登録時は空エントリ。"""
    kessanbi = request.args.get("kessanbi", "").strip()
    if not kessanbi:
        return jsonify({"error": "kessanbi required"}), 400
    entry = get_kessan_comment(code_s, kessanbi)
    if entry is None:
        entry = {
            "kessanbi": kessanbi,
            "quarter": 0,
            "pre_expectation": "",
            "pre_outlook": "",
            "post_price_changes": {key: "" for key, _ in KESSAN_REACTION_PERIODS},
            "post_comment": "",
            "kessan_matagi": False,
        }
        return jsonify(entry)
    return jsonify(_serialize_kessan_entry_for_api(entry))


@disclosure_bp.route("/api/kessan_comment/<code_s>", methods=["POST"])
def post_kessan_comment_api(code_s: str):
    """決算コメントを保存。成功時は保存後エントリを返す。"""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    try:
        entry = save_kessan_comment(code_s, form)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_serialize_kessan_entry_for_api(entry))
