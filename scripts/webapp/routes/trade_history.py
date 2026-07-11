"""売買履歴ページルート (issue #351, #357)。

GET  /trade-history                          : 保有エピソード単位でサブ行展開表示
POST /trade-history/<code_s>/<int:seq>/review-memo : 振り返りメモを保存
     seq は売却ログまたは1保遷移ログの seq。どちらも review_memo に保存可能。
"""

from flask import Blueprint, abort, jsonify, render_template, request

import portfolio_shelve as ps
from webapp.helpers import calc_episode_pl, calc_trade_summary, resolve_stock_name

trade_history_bp = Blueprint("trade_history", __name__)


def _build_rows(ep: dict) -> list:
    """エピソードからサブ行リストを組み立てる。

    保有 → 株数変更（0個以上）→ 売却 の順に並べる。
    株数列: 保有=IN株数（"0 → N" 形式のログがあれば N、なければ空）、
            株数変更=変更後株数（→右辺）、売却=空。
    株数変更の理由列は issue #356 対応まで空欄。
    """
    rows = []

    qty_changes = ep.get("qty_changes", [])

    # IN時株数: 1保ログの qty を優先 (issue #357)、なければ株数変更ログの左辺で補填
    if ep.get("hold_qty") is not None:
        in_qty = str(ep["hold_qty"])
    elif qty_changes:
        first_reason = qty_changes[0].get("reason", "")
        in_qty = first_reason.split("→", 1)[0].strip() if "→" in first_reason else ""
    else:
        in_qty = ""

    rows.append({
        "kind":   "保有",
        "date":   ep["hold_date"],
        "qty":    in_qty,
        "price":  ep.get("hold_price"),
        "reason": ep["hold_reason"],
    })

    for qc in qty_changes:
        reason = qc.get("reason", "")
        before = ""
        after = ""
        if "→" in reason:
            before, after = [part.strip() for part in reason.split("→", 1)]
        before_qty = int(before) if before.isdigit() else None
        after_qty = qc.get("after_qty")
        if after_qty is None and after.isdigit():
            after_qty = int(after)
        if before_qty is None or after_qty is None:
            kind = "株数修正"
        elif after_qty > before_qty:
            kind = "買増"
        elif after_qty < before_qty:
            kind = "一部売却"
        else:
            kind = "株数修正"
        memo = qc.get("memo", "")
        rows.append({
            "kind":   kind,
            "date":   qc["date"],
            "qty":    after,
            "price":  qc.get("price"),
            "reason": memo,  # issue #356: 株数変更メモ（なければ空欄）
        })

    if ep["sell_date"]:
        sell_qty = ep.get("sell_qty")
        rows.append({
            "kind":   "売却",
            "date":   ep["sell_date"],
            "qty":    str(sell_qty) if sell_qty is not None else "",
            "price":  ep.get("sell_price"),
            "reason": ep["sell_reason"],
        })

    return rows


def _last_action_date(ep: dict) -> str:
    """エピソード内の最新アクション日 (保有/株数変更/売却の最大日付)。

    timestamp は売買日であり遡り入力があり得るため、末尾要素ではなく max で取る。
    """
    dates = [ep["hold_date"]]
    dates.extend(qc["date"] for qc in ep["qty_changes"])
    if ep["sell_date"]:
        dates.append(ep["sell_date"])
    return max(dates)


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
                "hold_qty": log.get("qty"),       # 1保遷移時のIN株数 (issue #357)
                "hold_price": log.get("price_proxy"),  # 保有開始日の終値プロキシ (issue #361)
                "sell_reason": "",
                "sell_qty": None,
                "sell_price": None,
                "memo_seq": log["seq"],           # 1保ログの seq（未売却時のメモ保存先）
                "sell_seq": None,
                "review_memo": log.get("review_memo", ""),
                "qty_changes": [],
            }
        elif log.get("action_type") == "株数変更" and code_s in open_episodes:
            # reason 形式 "500 → 700 (メモ)" → 差分と括弧内メモを分離
            raw_reason = log.get("reason", "")
            diff = raw_reason.split("(")[0].strip()
            memo = ""
            if "(" in raw_reason and raw_reason.endswith(")"):
                memo = raw_reason[raw_reason.index("(") + 1:-1].strip()
            # 変更後株数 (右辺) を int パース。加重平均取得単価の計算に使う (issue #361)
            after_qty = None
            if "→" in diff:
                right = diff.split("→", 1)[1].strip()
                if right.isdigit():
                    after_qty = int(right)
            open_episodes[code_s]["qty_changes"].append({
                "date": log["timestamp"][:10],
                "reason": diff,
                "memo": memo,
                "price": log.get("price_proxy"),  # 株数変更日の終値プロキシ (issue #361)
                "after_qty": after_qty,
            })
        elif log.get("action_type") == "売却" and code_s in open_episodes:
            ep = open_episodes.pop(code_s)
            ep["sell_date"] = log["timestamp"][:10]
            ep["sell_reason"] = log.get("reason", "")
            ep["sell_qty"] = log.get("qty")       # 売却時の保有株数（旧ログは None）
            ep["sell_price"] = log.get("price_proxy")  # 売却日の終値プロキシ (issue #361)
            ep["sell_seq"] = log["seq"]
            ep["memo_seq"] = log["seq"]           # 売却済みはこちらがメモ保存先
            # 保有中に入力したメモを引き継ぐ
            # 売却ログの review_memo が None（未設定）のときのみ1保ログのメモを使う
            # 空文字（明示削除）はそのまま優先する
            sell_memo = log.get("review_memo")
            ep["review_memo"] = sell_memo if sell_memo is not None else ep.get("review_memo", "")
            episodes.append(ep)

    # 未売却（保有中）エピソードを追加
    episodes.extend(open_episodes.values())

    # エピソード内の最新アクション日 (売却 > 株数変更 > 保有) 降順
    episodes.sort(key=_last_action_date, reverse=True)

    # 銘柄名付与・サブ行組み立て・概算損益 (issue #361)
    episode_pls = []
    for ep in episodes:
        ep["stock_name"] = resolve_stock_name(ep["code_s"])
        ep["rows"] = _build_rows(ep)
        ep["rowspan"] = len(ep["rows"])
        ep["pl"] = calc_episode_pl(ep)  # 算出不可なら None (行は「—」表示)
        if ep["pl"] is not None:
            episode_pls.append(ep["pl"])

    # 売却済み全体の成績サマリー (issue #361)。母数0/算出不可のみなら None
    closed_count = sum(1 for ep in episodes if ep["sell_date"])
    summary = calc_trade_summary(episode_pls)

    # 直近30件と過去ログに分割
    recent = episodes[:30]
    past = episodes[30:]

    # 過去ログを最新アクション年でグルーピング (ソート基準と揃える)
    past_by_year: dict[str, list] = {}
    for ep in past:
        year = _last_action_date(ep)[:4]
        past_by_year.setdefault(year, []).append(ep)
    # 年降順のリスト [(year, episodes), ...]
    past_years = sorted(past_by_year.items(), key=lambda x: x[0], reverse=True)

    return render_template(
        "trade_history.html",
        recent=recent,
        past_years=past_years,
        summary=summary,
        closed_count=closed_count,
    )


@trade_history_bp.route(
    "/trade-history/<code_s>/<int:seq>/review-memo", methods=["POST"]
)
def save_review_memo(code_s: str, seq: int):
    """振り返りメモを上書き保存する (fetch POST / JSON レスポンス)。

    seq は売却ログまたは1保遷移ログの seq。
    どちらも review_memo を持つため同じエンドポイントで処理する。
    """
    review_memo = request.form.get("review_memo", "")
    try:
        logs = ps.list_action_logs(code_s)
        target = next((l for l in logs if l["seq"] == seq), None)
        if target is None:
            abort(404)
        action_type = target.get("action_type")
        status_to = target.get("status_to")
        if not (action_type == "売却" or (action_type == "ステータス変更" and status_to == "1保")):
            abort(404)
        ps.update_action_log_review_memo(code_s, seq, review_memo)
    except KeyError:
        abort(404)
    return jsonify({"ok": True})
