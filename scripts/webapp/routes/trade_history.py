"""売買履歴ページルート (issue #351)。

GET /trade-history : 全銘柄のアクションログから保有エピソード単位で
1行にまとめ、保有日降順で一覧表示する。
"""

from flask import Blueprint, render_template

import portfolio_shelve as ps
from webapp.helpers import resolve_stock_name

trade_history_bp = Blueprint("trade_history", __name__)


@trade_history_bp.route("/trade-history")
def trade_history():
    """売買履歴ページ — 銘柄×保有エピソード単位で1行表示。"""
    all_logs = ps.list_action_logs()  # (code_s, seq) 昇順

    episodes = []
    open_episodes = {}  # code_s -> episode dict (未売却)

    for log in all_logs:
        code_s = log["code_s"]
        if log.get("status_to") == "1保":
            # 未クローズのまま再購入（異常系）は先に確定
            if code_s in open_episodes:
                episodes.append(open_episodes.pop(code_s))
            open_episodes[code_s] = {
                "code_s": code_s,
                "stock_name": "",
                "hold_date": log["timestamp"][:10],
                "sell_date": "",
                "hold_reason": log.get("reason", ""),
                "sell_reason": "",
            }
        elif log.get("action_type") == "売却" and code_s in open_episodes:
            ep = open_episodes.pop(code_s)
            ep["sell_date"] = log["timestamp"][:10]
            ep["sell_reason"] = log.get("reason", "")
            episodes.append(ep)

    # 未売却（保有中）エピソードを追加
    episodes.extend(open_episodes.values())

    # 保有日降順
    episodes.sort(key=lambda r: r["hold_date"], reverse=True)

    # 銘柄名付与
    for ep in episodes:
        ep["stock_name"] = resolve_stock_name(ep["code_s"])

    return render_template("trade_history.html", episodes=episodes)
