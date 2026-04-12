"""
検索・ホーム画面ルート。

GET / : 銘柄コード/名前/評価でフィルタし一覧表示
"""

from flask import Blueprint, render_template, request

from webapp.helpers import search_records

search_bp = Blueprint("search", __name__)


@search_bp.route("/")
def index():
    """検索・ホーム画面。"""
    rating = request.args.get("rating", "").strip() or None
    keyword = request.args.get("keyword", "").strip() or None
    code_s = request.args.get("code_s", "").strip()

    records = search_records(rating=rating, keyword=keyword)

    # 銘柄コードフィルタ (部分一致)
    if code_s:
        records = [
            r for r in records if code_s.upper() in r.get("code_s", "")
        ]

    return render_template(
        "search.html",
        records=records,
        filter_rating=rating or "",
        filter_keyword=keyword or "",
        filter_code_s=code_s,
    )
