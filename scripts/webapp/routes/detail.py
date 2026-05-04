"""
銘柄詳細ビュールート。

GET /stock/<code_s> : 銘柄の全セクション表示
"""

from flask import Blueprint, render_template, abort

import portfolio
import portfolio_shelve as ps
from webapp.helpers import (
    get_research_detail,
    get_stock_data,
    get_disclosures,
    has_recent_disclosure,
)
from webapp.routes.portfolio import STATUS_VALUE_TO_LABEL, STATUS_VALUE_TO_QUERY
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

    snapshots = record.get("snapshots") or []
    indicator_snaps = [
        s for s in snapshots
        if s.get("quality_indicators") or s.get("rironkabuka_kairi")
    ]

    portfolio_record = ps.get_record(code_s)
    portfolio_status = portfolio_record.get("status") if portfolio_record else None
    if portfolio_status is None:
        # shelve 未移行環境のフォールバック: my_watch_list.txt 経由の所属を見る
        # (portfolio.parse_my_portforio は shelve 空時に txt フォールバックする)
        try:
            watch, possess = portfolio.parse_my_portforio()
        except Exception:  # noqa: BLE001
            watch, possess = ([], [])
        if code_s in possess:
            portfolio_status = "1保"
        elif code_s in watch:
            portfolio_status = "3監"
    portfolio_status_label = STATUS_VALUE_TO_LABEL.get(portfolio_status) if portfolio_status else None
    portfolio_status_query = STATUS_VALUE_TO_QUERY.get(portfolio_status) if portfolio_status else None

    return render_template(
        "detail.html",
        record=record,
        stock=stock,
        disclosures=disclosures,
        disclosures_has_recent=disclosures_has_recent,
        indicator_snaps=indicator_snaps,
        valid_ratings=sorted(VALID_RATINGS - {""}),
        portfolio_status=portfolio_status,
        portfolio_status_label=portfolio_status_label,
        portfolio_status_query=portfolio_status_query,
    )
