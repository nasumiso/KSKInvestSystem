"""売買履歴ページルート (issue #351, #357, #387)。

GET  /trade-history                          : 売買履歴/アクションログの2タブ表示
POST /trade-history/import                    : 楽天/SBI CSV をアップロード取込 (issue #387 4a)
POST /trade-history/<code_s>/<int:seq>/review-memo : 振り返りメモを保存
     seq は売却ログまたは1保遷移ログの seq。どちらも review_memo に保存可能。
"""

import os
import shutil
import tempfile

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import import_monex_fills as monex
import import_rakuten_fills as rakuten
import import_sbi_fills as sbi
import portfolio_shelve as ps
from ks_util import DATA_DIR
from webapp.helpers import (
    build_fill_episodes,
    build_stock_rollups,
    calc_trade_summary,
    fill_date_range_by_broker,
    resolve_stock_name,
)

trade_history_bp = Blueprint("trade_history", __name__)

# 取込成功CSVの保存先 (取引履歴の原本置き場)。issue #387 4a
TRADE_HISTORY_DIR = os.path.join(DATA_DIR, "trade_history")


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
    """売買履歴ページ — 2タブ構成 (issue #387)。

    - 売買履歴タブ: 楽天CSVの実約定 (fill) を約定日降順で一覧
    - アクションログタブ: 手動記録の保有エピソードをサブ行展開で表示
    """
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
                "hold_seq": log["seq"],           # 1保ログの seq（fill マッチ解決用、issue #360）
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

    # 銘柄名付与・サブ行組み立て (issue #361)
    # 成績サマリー・概算損益は fill 側 (売買履歴タブ) に一本化したため、
    # アクションログ側では計算しない (issue #387 Phase4b)。
    for ep in episodes:
        ep["stock_name"] = resolve_stock_name(ep["code_s"])
        ep["rows"] = _build_rows(ep)
        ep["rowspan"] = len(ep["rows"])

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

    # issue #387 Phase4b: 売買履歴タブ = fill 基準の建玉ラウンド・エピソード。
    # 成績サマリー (勝率/ペイオフ) はクローズ済みで損益算出できたエピソードから算出。
    fill_episodes = build_fill_episodes()
    fill_pls = [ep["pl"] for ep in fill_episodes if ep["closed"] and ep["pl"]]
    fill_summary = calc_trade_summary(fill_pls)
    fill_closed_count = sum(1 for ep in fill_episodes if ep["closed"])
    fill_priced_count = len(fill_pls)
    fill_total_pl = sum(
        p["profit_amount"] for p in fill_pls if p["profit_amount"] is not None
    )

    # issue #391: 銘柄単位の集約ビュー。実現損益合計・期待値はエピソード単位と
    # 完全一致する (calc_trade_summary が金額加重のためグループ化に依存しない)。
    stock_rollups = build_stock_rollups(fill_episodes)
    stock_pls = [r["pl"] for r in stock_rollups if r["pl"]]
    stock_summary = calc_trade_summary(stock_pls)
    stock_priced_count = len(stock_pls)
    # クローズ済みエピソードを1件以上持つ銘柄数。has_open (全エピソードのうち
    # 1件でも保有中があるか) とは独立: 6227 のように closed 12 + open 1 の
    # 混在銘柄は has_open=True だが、クローズ済みエピソードを持つのでここに含める。
    stock_closed_count = sum(
        1 for r in stock_rollups if any(ep["closed"] for ep in r["episodes"])
    )

    # 証券会社別の取込済み約定日レンジ (次回インポートの参考、取込のたびに更新される)
    broker_ranges = fill_date_range_by_broker()

    return render_template(
        "trade_history.html",
        recent=recent,
        past_years=past_years,
        fill_episodes=fill_episodes,
        fill_summary=fill_summary,
        fill_closed_count=fill_closed_count,
        fill_priced_count=fill_priced_count,
        fill_total_pl=fill_total_pl,
        stock_rollups=stock_rollups,
        stock_summary=stock_summary,
        stock_priced_count=stock_priced_count,
        stock_closed_count=stock_closed_count,
        broker_ranges=broker_ranges,
    )


@trade_history_bp.route("/trade-history/import", methods=["POST"])
def import_trade_csv():
    """楽天/SBI の約定CSVをアップロード取込する (issue #387 4a)。

    ヘッダで証券会社を自動判定 → 該当パーサーで取込 → 成功時のみ原本を
    TRADE_HISTORY_DIR へ同名上書きコピー。結果を flash して /trade-history へ戻る。
    dedup があるため再取込は冪等 (重複はスキップされる)。
    """
    file = request.files.get("csv_file")
    if file is None or not file.filename:
        flash("CSVファイルが選択されていません。", "error")
        return redirect(url_for("trade_history.trade_history"))

    filename = os.path.basename(file.filename)
    # 一時ファイルに保存 (パーサーはパス受け取りのため)
    fd, tmp_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        file.save(tmp_path)

        # ヘッダ自動判定 (楽天=先頭行が約定日ヘッダ28列 / SBI=メタ行+14列 /
        # マネックス=メタ行+25列+建単価列)。列数が異なるので3者は排他。
        if rakuten.is_rakuten_csv(tmp_path):
            module = rakuten
        elif sbi.is_sbi_csv(tmp_path):
            module = sbi
        elif monex.is_monex_csv(tmp_path):
            module = monex
        else:
            flash(
                f"楽天/SBI/マネックス の取引履歴CSVとして認識できませんでした: {filename}",
                "error",
            )
            return redirect(url_for("trade_history.trade_history"))

        try:
            stats = module.import_csv_to_fills(tmp_path)
        except Exception as e:  # noqa: BLE001 - パース例外はユーザーへ提示
            flash(f"取込中にエラーが発生しました ({filename}): {e}", "error")
            return redirect(url_for("trade_history.trade_history"))

        # 成功: 原本を正式置き場へ同名上書きコピー
        os.makedirs(TRADE_HISTORY_DIR, exist_ok=True)
        shutil.copy2(tmp_path, os.path.join(TRADE_HISTORY_DIR, filename))

        flash(
            f"{module.BROKER} CSV 取込完了: {filename} — "
            f"新規 {stats['imported']} 件 / 重複スキップ {stats['skipped_dup']} 件 / "
            f"対象外 {stats['skipped_invalid']} 件",
            "success",
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return redirect(url_for("trade_history.trade_history"))


@trade_history_bp.route("/trade-history/fill-memo", methods=["POST"])
def save_fill_memo():
    """fill 建玉ラウンド (エピソード) の振り返りメモを保存する (issue #387 Phase2)。

    エピソードキー (code_s|kind|open_date|close_date) を受け取り上書き保存する。
    空文字は削除扱い。fetch POST / JSON レスポンス。
    振り返りメモはアクションログ側から売買履歴 (fill=真実源) 側へ一本化した。
    """
    episode_key = request.form.get("episode_key", "")
    review_memo = request.form.get("review_memo", "")
    if not episode_key:
        abort(400)
    ps.set_fill_memo(episode_key, review_memo)
    return jsonify({"ok": True})
