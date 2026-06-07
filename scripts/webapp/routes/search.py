"""
検索・ホーム画面ルート。

GET / : 銘柄コード/名前/評価でフィルタし一覧表示
"""

import os
import unicodedata
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

from ks_util import DATA_DIR
from research_shelve import CODE_S_PATTERN
from webapp.helpers import search_records, add_stock

search_bp = Blueprint("search", __name__)

# issue #98: トップページに統合する Google Spreadsheet 一覧
# 最終更新日はローカル CSV の mtime (スプシ upload 直前に書かれる) を流用する
PORTAL_SPREADSHEETS = (
    {
        "title": "Shintakane Result",
        "url": "https://docs.google.com/spreadsheets/d/1KxOFvfgT7o_XGDASGylxA0Rn9yEqPLSv6Yweb_jGsHk/edit?usp=sharing",
        "mtime_csv": "shintakane_result_data/shintakane_result.csv",
    },
    {
        "title": "Code Rank",
        "url": "https://docs.google.com/spreadsheets/d/1zto-8-fZ5hTZfXY6k2C49HZHbyA3OE8BgkReAViLSNU/edit?usp=sharing",
        "mtime_csv": "code_rank_data/code_rank.csv",
    },
    {
        "title": "投資メモ",
        "url": "https://docs.google.com/document/d/1_O8GdD7SwA6mCMJR5SC362oX1UouJCGworj9z1rZ2I4/edit?usp=sharing",
    },
)

# GitHub の PR / issue 一覧へのリンク (最終更新は持たない)
PORTAL_LINKS = (
    {"title": "PR", "url": "https://github.com/nasumiso/KSKInvestSystem/pulls"},
    {"title": "issue", "url": "https://github.com/nasumiso/KSKInvestSystem/issues"},
)


def _portal_spreadsheets():
    """PORTAL_SPREADSHEETS に最終更新日を付与して返す"""
    cards = []
    for sp in PORTAL_SPREADSHEETS:
        mtime_csv = sp.get("mtime_csv")
        if mtime_csv:
            csv_path = os.path.join(DATA_DIR, mtime_csv)
            try:
                ts = os.path.getmtime(csv_path)
                updated_at = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            except OSError:
                updated_at = "—"
        else:
            updated_at = "—"
        cards.append({
            "title": sp["title"],
            "url": sp["url"],
            "updated_at": updated_at,
        })
    return cards


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
        # NFKC で全角コード入力 (例: '６９９９') も半角登録の code_s にマッチさせる
        q_upper = unicodedata.normalize("NFKC", q).upper()
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
            code_s_norm = unicodedata.normalize("NFKC", code_s).upper()
            records = [r for r in records if code_s_norm in r.get("code_s", "")]

    # コード/銘柄名検索で1件のみヒットした場合は詳細ページへ直接ジャンプ
    if (q or code_s) and len(records) == 1:
        return redirect(url_for("detail.stock_detail", code_s=records[0]["code_s"]))

    # issue #216: q がコード形式 (4桁数字 or 3桁数字+大文字) で 0 件ヒット時のみ
    # 「この銘柄を追加」フォームを出す。銘柄名検索や不正フォーマットでは出さない。
    q_normalized = unicodedata.normalize("NFKC", q).upper()
    can_add = (
        bool(q)
        and not records
        and bool(CODE_S_PATTERN.match(q_normalized))
    )

    # issue #244: クエリが何も無いトップアクセスでは銘柄一覧を出さず
    # What's new プレースホルダーを描画する。一覧は検索クエリありのときだけ。
    has_query = bool(q or code_s or rating or keyword)

    return render_template(
        "search.html",
        records=records,
        filter_rating=rating or "",
        filter_keyword=keyword or "",
        filter_code_s=code_s,
        filter_q=q,
        can_add=can_add,
        q_normalized=q_normalized,
        has_query=has_query,
        portal_spreadsheets=_portal_spreadsheets() if not has_query else None,
        portal_links=PORTAL_LINKS if not has_query else None,
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
