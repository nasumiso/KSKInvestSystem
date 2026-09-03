"""売買履歴ページルート (issue #351, #357, #387)。

GET  /trade-history                          : 売買履歴/アクションログの2タブ表示
POST /trade-history/import                    : 楽天/SBI CSV をアップロード取込 (issue #387 4a)
POST /trade-history/<code_s>/<int:seq>/review-memo : 振り返りメモを保存
     seq は売却ログまたは1保遷移ログの seq。どちらも review_memo に保存可能。
"""

import datetime
import os
import shutil
import tempfile
import uuid

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
    build_round_trips,
    build_stock_rollups,
    calc_trade_summary,
    count_orphan_strategies,
    episode_hold_days,
    fill_date_range_by_broker,
    resolve_stock_name,
    summarize_by_strategy,
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


def _split_current_and_past_years(items, keep_open, current_year):
    """売買履歴の一覧を「初期表示分」と「年ごとの過去分」に分ける (issue #406)。

    エピソード数は増え続けるため、全件を初期HTMLに出すと描画コストが嵩む。
    今年の分と、`keep_open` が真のもの (保有中) を初期表示に残し、それ以外を
    最終約定年ごとにまとめて折りたたむ。保有中を年で畳まないのは、古い建玉が
    過去年に埋もれて見落とされるのを防ぐため。

    items は last_trade_date の降順に並んでいる前提 (build_fill_episodes /
    build_stock_rollups と同じ順序)。返り値もその順序を保つ。

    current_year は呼び出し側で1回だけ求めた値を渡す (1リクエスト内で年の基準が
    ずれると、年境界でアコーディオンとサマリーの年が食い違いうる)。

    Returns: (初期表示分, [(年, 件数分のリスト), ...] 年降順)
    """
    current, past_by_year = [], {}
    for item in items:
        year = (item["last_trade_date"] or "")[:4]
        if year >= current_year or keep_open(item):
            current.append(item)
        else:
            past_by_year.setdefault(year, []).append(item)
    past_years = sorted(past_by_year.items(), key=lambda x: x[0], reverse=True)
    return current, past_years


def _summarize_fill_episodes(episodes):
    """エピソード群から (エピソード単位サマリー, 銘柄単位サマリー) を返す。

    issue #398: split_suspect (分割・併合の疑いだが未換算) は残高・損益が誤っている
    可能性があるため、エピソード単位・銘柄単位のどちらの集計からも一貫して除外する。
    build_stock_rollups() 側も同じ方針で除外するが、ここでは母数のカウント
    (closed_count) も除外後で数えたいので先に絞ってから渡す。除外後に集約すれば
    実現損益・期待値はエピソード単位と厳密に一致する (calc_trade_summary が
    金額加重のためグループ化に依存しない)。
    """
    valid = [ep for ep in episodes if not ep.get("split_suspect")]
    pls = [ep["pl"] for ep in valid if ep["closed"] and ep["pl"]]
    total_pl = sum(p["profit_amount"] for p in pls if p["profit_amount"] is not None)
    episode_part = {
        "summary": calc_trade_summary(pls),
        "total_pl": total_pl,
        "priced_count": len(pls),
        "closed_count": sum(1 for ep in valid if ep["closed"]),
    }

    rollups = build_stock_rollups(valid)
    stock_pls = [r["pl"] for r in rollups if r["pl"]]
    # クローズ済みエピソードを1件以上持つ銘柄数。has_open (全エピソードのうち
    # 1件でも保有中があるか) とは独立: 6227 のように closed 12 + open 1 の
    # 混在銘柄は has_open=True だが、クローズ済みエピソードを持つのでここに含める。
    stock_part = {
        "summary": calc_trade_summary(stock_pls),
        "total_pl": total_pl,  # 実現損益はエピソード単位と一致する
        "priced_count": len(stock_pls),
        "closed_count": sum(
            1 for r in rollups if any(ep["closed"] for ep in r["episodes"])
        ),
    }
    return episode_part, stock_part


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

    # 往復行 (issue #421) は明細表示専用のため、共有の build_fill_episodes() ではなく
    # 描画側で付与する。ポートフォリオ画面や CLI・移行スクリプトも
    # build_fill_episodes() を呼ぶが、往復行は使わない。
    for ep in fill_episodes:
        ep["round_trips"] = build_round_trips(ep)

    # 年の基準はこの1箇所で決め、サマリーとアコーディオンで共有する。
    current_year = datetime.datetime.now(ps.JST).strftime("%Y")

    # 成績サマリーを年単位で出す。年は last_trade_date の年 = 手仕舞い年
    # (クローズ済みエピソードでは最終約定日が決済日)。「その年に決済した取引の成績」
    # になる。母数はクローズ済みのみなので保有中エピソードはどの年にも入らない。
    eps_by_year: dict[str, list] = {}
    for ep in fill_episodes:
        if not ep["closed"]:
            continue
        year = (ep["last_trade_date"] or "")[:4]
        if year:
            eps_by_year.setdefault(year, []).append(ep)

    # 粒度ごとに [(年, サマリー), ...] 年降順。テンプレート側でどちらを描くか
    # 分岐させず、そのまま同じマクロに渡せる形にしておく。
    episode_summary_years, stock_summary_years = [], []
    for year in sorted(eps_by_year, reverse=True):
        ep_part, stock_part = _summarize_fill_episodes(eps_by_year[year])
        episode_summary_years.append((year, ep_part))
        stock_summary_years.append((year, stock_part))

    # 初期選択年は今年。今年の決済がまだ無ければ最新年にフォールバックする
    # (年初など、今年の決済ゼロで空サマリーが出るのを防ぐ)。
    selected_summary_year = current_year if current_year in eps_by_year else (
        episode_summary_years[0][0] if episode_summary_years else "")

    # issue #391: 銘柄単位の集約ビュー (一覧テーブル用、全期間)。
    stock_rollups = build_stock_rollups(fill_episodes)

    # issue #406: 売買履歴の2ビューを年単位アコーディオン化して初期HTML量を減らす。
    # 保有中は年に関係なく常に初期表示する (古い建玉が畳まれて見落とされるのを防ぐ)。
    fill_current, fill_past_years = _split_current_and_past_years(
        fill_episodes, lambda ep: not ep["closed"], current_year
    )
    stock_current, stock_past_years = _split_current_and_past_years(
        stock_rollups, lambda r: r["has_open"], current_year
    )

    # 証券会社別の取込済み約定日レンジ (次回インポートの参考、取込のたびに更新される)
    broker_ranges = fill_date_range_by_broker()

    # issue #419: 戦略別の成績 (全期間)。未分類・要再確認は比較の母数から外して下部に出す。
    strategy_summary = summarize_by_strategy(fill_episodes)

    trade_idea_options = [t["name"] for t in ps.list_trade_ideas()]
    orphan_strategies = count_orphan_strategies(fill_episodes)

    return render_template(
        "trade_history.html",
        strategy_summary=strategy_summary,
        trade_idea_options=trade_idea_options,
        orphan_strategies=orphan_strategies,
        recent=recent,
        past_years=past_years,
        fill_episodes=fill_episodes,
        episode_summary_years=episode_summary_years,
        stock_summary_years=stock_summary_years,
        selected_summary_year=selected_summary_year,
        stock_rollups=stock_rollups,
        fill_current=fill_current,
        fill_past_years=fill_past_years,
        stock_current=stock_current,
        stock_past_years=stock_past_years,
        broker_ranges=broker_ranges,
    )


@trade_history_bp.route("/trade-history/import", methods=["POST"])
def import_trade_csv():
    """楽天/SBI/マネックス の約定CSVをアップロード取込する (issue #387 4a)。

    複数ファイルをまとめて選択できる。楽天の期間分割CSV (上期/下期など) のように
    同じ証券会社のファイルが複数あっても構わない — 残高CSVと違い約定履歴は累積の
    ため、1証券会社1ファイルに定まらない。dedup があるので重複はスキップされる。

    ファイルごとに独立して処理し、1つが失敗しても残りの取込は続行する
    (失敗したファイルだけ直して再アップロードすれば済むため)。ヘッダで証券会社を
    自動判定 → 該当パーサーで取込 → 成功したものだけ原本を TRADE_HISTORY_DIR へ
    同名上書きコピー。結果をファイルごとに flash して /trade-history へ戻る。
    """
    files = [f for f in request.files.getlist("csv_files") if f and f.filename]
    if not files:
        flash("CSVファイルが選択されていません。", "error")
        return redirect(url_for("trade_history.trade_history"))

    used_names = set()
    for file in files:
        filename = _safe_csv_filename(file.filename)
        # 同一リクエスト内で原本の保存名が衝突すると後勝ちで上書きされ、
        # 先のファイルの原本が失われる (SBIは同名 SaveFile.csv で降ってくる)。
        filename = _dedupe_filename(filename, used_names)
        used_names.add(filename)
        try:
            _import_one_csv(file, filename)
        except Exception as e:  # noqa: BLE001 - 1ファイルの失敗で他を巻き込まない
            flash(f"取込中にエラーが発生しました ({filename}): {e}", "error")

    return redirect(url_for("trade_history.trade_history"))


def _safe_csv_filename(raw: str) -> str:
    """アップロード名から保存用の安全なファイル名を作る。

    原本は TRADE_HISTORY_DIR へこの名前で保存するため、basename だけでは
    ".." のような名前でディレクトリ外へ逃げられる。区切り文字を除いた上で
    危険な名前を弾き、空になったら既定名にフォールバックする。
    """
    name = os.path.basename((raw or "").replace("\\", "/").strip())
    if name in ("", ".", "..") or "/" in name:
        return "uploaded.csv"
    return name


def _dedupe_filename(filename: str, used: set) -> str:
    """同一リクエスト内で既に使った保存名なら連番を付けて衝突を避ける。"""
    if filename not in used:
        return filename
    stem, ext = os.path.splitext(filename)
    for i in range(2, 100):
        candidate = f"{stem}_{i}{ext}"
        if candidate not in used:
            return candidate
    return f"{stem}_{uuid.uuid4().hex[:8]}{ext}"


def _import_one_csv(file, filename: str) -> None:
    """CSV 1ファイルを判別・取込し、成功なら原本を保存して flash する。

    呼び出し側が例外を握って次のファイルへ進むため、ここでは失敗を隠さない。
    """
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
            return

        stats = module.import_csv_to_fills(tmp_path)

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


def _episode_by_key(episode_key):
    """episode_key からエピソードを引く。指紋・保有日数の焼き付けに使う。"""
    for ep in build_fill_episodes():
        if ep["episode_key"] == episode_key:
            return ep
    return None


@trade_history_bp.route("/trade-history/episode-strategy", methods=["POST"])
def save_episode_strategy():
    """エピソードに売買戦略を焼き付ける (issue #419 レイヤー3)。

    空文字は未分類に戻す (キー削除)。クローズ済みなら指紋と保有日数を一緒に
    焼き付ける (保有中は姿が未確定なので None。クローズ確定時に seal が焼く)。
    """
    episode_key = request.form.get("episode_key", "")
    trade_idea = request.form.get("trade_idea", "")
    if not episode_key:
        abort(400)
    ep = _episode_by_key(episode_key)
    if ep is None:
        abort(404)
    try:
        ps.set_episode_strategy(
            episode_key, trade_idea,
            source="manual",
            fingerprint=(ps.episode_fingerprint(ep["fills"]) if ep["closed"] else None),
            hold_days=episode_hold_days(ep),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})
