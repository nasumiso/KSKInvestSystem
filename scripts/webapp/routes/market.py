"""
市場データ・決算カレンダー ルート。

GET  /market                         : 市場データページ (market_data.html を取り込み + 動的決算セクション)
GET  /api/kessan_comment/<code_s>    : 指定銘柄・決算日のコメントを JSON で返す
POST /api/kessan_comment/<code_s>    : 決算コメントを保存 (新規 or 上書き)
"""

from typing import Any, Dict

from flask import Blueprint, jsonify, render_template, request

from webapp.helpers import (
    get_kessan_comment,
    get_market_html_parts,
    get_market_kessan_data,
    save_kessan_comment,
)
from research_shelve import (
    KESSAN_REACTION_PERIODS,
    VALID_EXPECTATIONS,
    _normalize_kessan_post_price_changes,
)

market_bp = Blueprint("market", __name__)


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
    result["post_price_changes"] = _normalize_kessan_post_price_changes(entry)
    return result


@market_bp.route("/market", methods=["GET"])
def market_page():
    """市場データページ。静的 market_data.html を取り込み、決算セクションを動的版に差し替える。"""
    market_parts = get_market_html_parts()
    kessan_data = get_market_kessan_data()

    expectation_options = sorted(VALID_EXPECTATIONS, key=lambda x: ("", "◎", "○", "▲", "△", "×").index(x))

    return render_template(
        "market.html",
        market_parts=market_parts,
        kessan_data=kessan_data,
        expectation_options=expectation_options,
    )


@market_bp.route("/api/kessan_comment/<code_s>", methods=["GET"])
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


@market_bp.route("/api/kessan_comment/<code_s>", methods=["POST"])
def post_kessan_comment_api(code_s: str):
    """決算コメントを保存。成功時は保存後エントリを返す。"""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    try:
        entry = save_kessan_comment(code_s, form)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_serialize_kessan_entry_for_api(entry))
