"""
市場データ・決算カレンダー ルート。

GET  /market                         : 市場データページ (market_data.html を取り込み + 動的決算セクション)
GET  /api/kessan_comment/<code_s>    : 指定銘柄・決算日のコメントを JSON で返す
POST /api/kessan_comment/<code_s>    : 決算コメントを保存 (新規 or 上書き)
"""

from flask import Blueprint, jsonify, render_template, request

from webapp.helpers import (
    get_kessan_comment,
    get_market_html_parts,
    get_market_kessan_data,
    save_kessan_comment,
)
from research_shelve import VALID_EXPECTATIONS

market_bp = Blueprint("market", __name__)


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
            "post_price_change": "",
            "post_comment": "",
            "kessan_matagi": False,
        }
    return jsonify(entry)


@market_bp.route("/api/kessan_comment/<code_s>", methods=["POST"])
def post_kessan_comment_api(code_s: str):
    """決算コメントを保存。成功時は保存後エントリを返す。"""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    try:
        entry = save_kessan_comment(code_s, form)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(entry)
