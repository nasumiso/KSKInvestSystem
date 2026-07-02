"""売買履歴ページルート (issue #351)。

GET /trade-history : 全銘柄のアクションログから「実保有」「売却」のみを
時系列降順で一覧表示する。
"""

from flask import Blueprint, render_template

import portfolio_shelve as ps
from webapp.helpers import resolve_stock_name

trade_history_bp = Blueprint("trade_history", __name__)


@trade_history_bp.route("/trade-history")
def trade_history():
    """売買履歴ページ — 全銘柄の実保有・売却ログを時系列降順で表示する。"""
    all_logs = ps.list_action_logs()

    # 実保有になったログ (status_to == "1保") と売却ログのみ抽出
    entries = [
        log for log in all_logs
        if log.get("status_to") == "1保" or log.get("action_type") == "売却"
    ]

    # 時系列降順
    entries.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # 銘柄名を付与 (resolve_stock_name は見つからない場合 code_s を返す)
    for entry in entries:
        entry["stock_name"] = resolve_stock_name(entry["code_s"])

    return render_template("trade_history.html", entries=entries)
