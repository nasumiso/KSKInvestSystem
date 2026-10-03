"""
銘柄データ再取得ルート。

POST /stock/<code_s>/refresh : make_stock_db.refresh_stock([code_s]) を同期実行
"""

from flask import Blueprint, flash, redirect, url_for

refresh_bp = Blueprint("refresh", __name__)


@refresh_bp.route("/stock/<code_s>/refresh", methods=["POST"])
def post_refresh(code_s: str):
    """master/price/shihyo/gyoseki/rironkabuka + research_shelve を強制再取得して flash 後にリダイレクト。"""
    try:
        # make_stock_db は scipy/yfinance 等の重依存を持つため、WebApp 起動時に
        # 巻き添えで落ちないようルート呼び出し時に遅延 import する
        from make_stock_db import refresh_stock

        # スナップショットはウォッチ・保有銘柄だけが対象。対象外のときは株式 DB しか
        # 更新されないので、画面に「取れていない」と見える理由を添える
        if code_s in refresh_stock([code_s]):
            flash(f"再取得しました ({code_s})", "info")
        else:
            flash(f"再取得しました ({code_s})。保有銘柄一覧に無いため、スナップショットは更新していません", "info")
    except Exception as e:
        flash(f"再取得に失敗しました ({code_s}): {e}", "error")
    return redirect(url_for("detail.stock_detail", code_s=code_s))
