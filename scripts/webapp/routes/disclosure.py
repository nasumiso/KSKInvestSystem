"""
適宜開示ページ ルート。

GET /disclosure : 適宜開示ページ (disclosure_data.html を取り込み表示)
"""

from flask import Blueprint, render_template

from webapp.helpers import get_disclosure_html_parts


disclosure_bp = Blueprint("disclosure", __name__)


@disclosure_bp.route("/disclosure", methods=["GET"])
def disclosure_page():
    """適宜開示ページ。静的 disclosure_data.html を取り込んで表示する。"""
    disclosure_parts = get_disclosure_html_parts()
    return render_template("disclosure.html", disclosure_parts=disclosure_parts)
