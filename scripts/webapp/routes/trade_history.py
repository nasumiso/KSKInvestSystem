"""売買履歴ページルート (issue #351)。

GET  /trade-history                          : 保有エピソード単位で一覧表示
POST /trade-history/<code_s>/<int:seq>/review-memo : 振り返りメモを保存
"""

from flask import Blueprint, abort, jsonify, render_template, request

import portfolio_shelve as ps
from webapp.helpers import resolve_stock_name

trade_history_bp = Blueprint("trade_history", __name__)


def _extract_initial_qty(qty_changes: list) -> str:
    """qty_changes の最初のエントリの reason から IN 時の株数を取り出す。

    reason 形式: "0 → 100" or "0 → 100 (買い増し)" → "100"
    取り出せない場合は空文字。
    """
    if not qty_changes:
        return ""
    reason = qty_changes[0].get("reason", "")
    # "→" の右側を取り、末尾の括弧注釈を除去
    if "→" not in reason:
        return ""
    before = reason.split("→", 1)[0].strip()
    return before


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
                "sell_seq": None,
                "review_memo": "",
                "qty_changes": [],
            }
        elif log.get("action_type") == "株数変更" and code_s in open_episodes:
            open_episodes[code_s]["qty_changes"].append({
                "date": log["timestamp"][:10],
                "reason": log.get("reason", ""),
            })
        elif log.get("action_type") == "売却" and code_s in open_episodes:
            ep = open_episodes.pop(code_s)
            ep["sell_date"] = log["timestamp"][:10]
            ep["sell_reason"] = log.get("reason", "")
            ep["sell_seq"] = log["seq"]
            ep["review_memo"] = log.get("review_memo", "")
            episodes.append(ep)

    # 未売却（保有中）エピソードを追加
    episodes.extend(open_episodes.values())

    # 保有日降順
    episodes.sort(key=lambda r: r["hold_date"], reverse=True)

    # 銘柄名付与・初期株数抽出
    for ep in episodes:
        ep["stock_name"] = resolve_stock_name(ep["code_s"])
        ep["initial_qty"] = _extract_initial_qty(ep["qty_changes"])

    return render_template("trade_history.html", episodes=episodes)


@trade_history_bp.route(
    "/trade-history/<code_s>/<int:seq>/review-memo", methods=["POST"]
)
def save_review_memo(code_s: str, seq: int):
    """売却ログの振り返りメモを上書き保存する (fetch POST / JSON レスポンス)。"""
    review_memo = request.form.get("review_memo", "")
    try:
        logs = ps.list_action_logs(code_s)
        target = next((l for l in logs if l["seq"] == seq), None)
        if target is None or target.get("action_type") != "売却":
            abort(404)
        ps.update_action_log_review_memo(code_s, seq, review_memo)
    except KeyError:
        abort(404)
    return jsonify({"ok": True})
