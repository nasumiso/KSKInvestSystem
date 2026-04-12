"""
銘柄詳細ビュールート。

GET /stock/<code_s> : 銘柄の全セクション表示
"""

from flask import Blueprint, render_template, abort

from webapp.helpers import get_research_detail
from research_shelve import VALID_RATINGS

detail_bp = Blueprint("detail", __name__)


@detail_bp.route("/stock/<code_s>")
def stock_detail(code_s: str):
    """銘柄詳細ビュー。"""
    record = get_research_detail(code_s)
    if record is None:
        abort(404)

    return render_template(
        "detail.html",
        record=record,
        valid_ratings=sorted(VALID_RATINGS - {""}),
    )
