"""
検索・ホーム画面ルート。

GET / : 銘柄コード/名前/評価でフィルタし一覧表示
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash

from webapp.helpers import search_records, add_stock

search_bp = Blueprint("search", __name__)


@search_bp.route("/")
def index():
    """検索・ホーム画面。"""
    rating = request.args.get("rating", "").strip() or None
    keyword = request.args.get("keyword", "").strip() or None
    code_s = request.args.get("code_s", "").strip()
    # ナビの汎用検索欄: code_s 部分一致 OR 銘柄名(他テキスト)部分一致
    q = request.args.get("q", "").strip()

    if q:
        # q は code_s 部分一致 OR keyword 一致 (search_records が見る複数フィールド)
        q_upper = q.upper()
        code_match = [
            r for r in search_records(rating=rating)
            if q_upper in r.get("code_s", "")
        ]
        keyword_match = search_records(rating=rating, keyword=q)
        seen = set()
        records = []
        for r in code_match + keyword_match:
            cs = r.get("code_s", "")
            if cs and cs not in seen:
                seen.add(cs)
                records.append(r)
        records.sort(key=lambda r: r.get("code_s", ""))
    else:
        records = search_records(rating=rating, keyword=keyword)
        if code_s:
            records = [r for r in records if code_s.upper() in r.get("code_s", "")]

    return render_template(
        "search.html",
        records=records,
        filter_rating=rating or "",
        filter_keyword=keyword or "",
        filter_code_s=code_s,
        filter_q=q,
    )


@search_bp.route("/stock/add", methods=["POST"])
def add_stock_route():
    """銘柄追加。"""
    code_s = request.form.get("add_code_s", "").strip()
    try:
        code_s = add_stock(code_s)
        return redirect(url_for("detail.stock_detail", code_s=code_s))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("search.index"))
