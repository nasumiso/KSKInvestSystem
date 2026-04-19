"""
銘柄詳細ビュールート。

GET /stock/<code_s> : 銘柄の全セクション表示
"""

from flask import Blueprint, render_template, abort

from webapp.helpers import (
    get_research_detail,
    get_stock_data,
    get_disclosures,
    has_recent_disclosure,
)
from research_shelve import VALID_RATINGS

detail_bp = Blueprint("detail", __name__)


@detail_bp.route("/stock/<code_s>")
def stock_detail(code_s: str):
    """銘柄詳細ビュー。"""
    record = get_research_detail(code_s)
    if record is None:
        abort(404)

    stock = get_stock_data(code_s)
    disclosures = get_disclosures(code_s)
    disclosures_has_recent = has_recent_disclosure(disclosures, days=7)

    return render_template(
        "detail.html",
        record=record,
        stock=stock,
        disclosures=disclosures,
        disclosures_has_recent=disclosures_has_recent,
        valid_ratings=sorted(VALID_RATINGS - {""}),
    )
