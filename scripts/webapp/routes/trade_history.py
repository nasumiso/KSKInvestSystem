"""売買履歴ページルート (issue #351, #357)。

GET  /trade-history                          : 保有エピソード単位でサブ行展開表示
POST /trade-history/<code_s>/<int:seq>/review-memo : 振り返りメモを保存
"""

from flask import Blueprint, abort, jsonify, render_template, request

import portfolio_shelve as ps
from webapp.helpers import resolve_stock_name

trade_history_bp = Blueprint("trade_history", __name__)


def _build_rows(ep: dict) -> list:
    """エピソードからサブ行リストを組み立てる。

    保有 → 株数変更（0個以上）→ 売却 の順に並べる。
    株数列: 保有=IN株数（"0 → N" 形式のログがあれば N、なければ空）、
            株数変更=変更後株数（→右辺）、売却=空。
    株数変更の理由列は issue #356 対応まで空欄。
    """
    rows = []

    # IN時株数: "0 → N" 形式（左辺が "0"）の株数変更ログがあれば N を使う
    qty_changes = ep.get("qty_changes", [])
    in_qty = ""
    if qty_changes:
        first_reason = qty_changes[0].get("reason", "")
        if "→" in first_reason:
            left, right = first_reason.split("→", 1)
            if left.strip() == "0":
                in_qty = right.strip()

    rows.append({
        "kind":   "保有",
        "date":   ep["hold_date"],
        "qty":    in_qty,
        "reason": ep["hold_reason"],
    })

    for qc in qty_changes:
        reason = qc.get("reason", "")
        after = reason.split("→", 1)[1].strip() if "→" in reason else ""
        rows.append({
            "kind":   "株数変更",
            "date":   qc["date"],
            "qty":    after,
            "reason": "",  # issue #356 対応後に表示
        })

    if ep["sell_date"]:
        rows.append({
            "kind":   "売却",
            "date":   ep["sell_date"],
            "qty":    "",
            "reason": ep["sell_reason"],
        })

    return rows


@trade_history_bp.route("/trade-history")
def trade_history():
    """売買履歴ページ — 銘柄×保有エピソードをサブ行展開で表示。"""
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
            # reason 形式 "500 → 700 (保有理由の流用)" の括弧内は除去して差分のみ表示
            raw_reason = log.get("reason", "")
            diff = raw_reason.split("(")[0].strip()
            open_episodes[code_s]["qty_changes"].append({
                "date": log["timestamp"][:10],
                "reason": diff,
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

    # 銘柄名付与・サブ行組み立て
    for ep in episodes:
        ep["stock_name"] = resolve_stock_name(ep["code_s"])
        ep["rows"] = _build_rows(ep)
        ep["rowspan"] = len(ep["rows"])

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
