#!/usr/bin/env python3
"""fill 建玉ラウンド (エピソード) を確認する開発補助CLI (issue #387)。

build_fill_episodes (webapp.helpers) を呼んで、取込後の検算・保有中確認・
現引や信用の損益・振り返りメモの紐付けをターミナルで確認する。

使い方:
    cd scripts && python show_fill_episodes.py            # 全エピソード (最新約定日降順、DB非更新)
    cd scripts && python show_fill_episodes.py 6324       # 特定銘柄のみ (DB非更新)
    cd scripts && python show_fill_episodes.py --open     # 保有中のみ (DB非更新)
    cd scripts && python show_fill_episodes.py --memo     # 振り返りメモ付きのみ (DB非更新)
    cd scripts && python show_fill_episodes.py --fills 6324  # 内訳 fill も表示 (DB非更新)
    cd scripts && python show_fill_episodes.py --check-dups            # 未確定CSV由来の重複約定を検出
    cd scripts && python show_fill_episodes.py --check-splits          # 分割・併合の疑いを診断 (issue #398)
    cd scripts && python show_fill_episodes.py --register-split 1491 2025-09-29 0.05  # 換算比率を登録
    cd scripts && python show_fill_episodes.py --reject-split 1491 2025-09-29  # 誤検知を解除

--check-splits は fill 本体・split_adj を更新しないが、未登録の発見を
split_pending_review (拒否リスト) に記録する。これにより webapp 表示 (yfinance を
呼ばない) でも同じ疑いを検知できる。--register-split で登録、または
--reject-split で誤検知として却下すれば解除される。
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
from ks_util import log_print, log_warning  # noqa: E402
from webapp import helpers  # noqa: E402


def _fmt_pl(ep: Dict[str, Any]) -> str:
    """損益列の文字列を組み立てる (クローズ=実現、保有中=実現+含み)。

    split_suspect (issue #398、分割・併合の疑いだが未換算) は残高・損益が
    誤っている可能性があるため、webapp (trade_history.html) と同様に隠す。
    """
    if ep.get("split_suspect"):
        return "⚠ 分割・併合の疑い (要確認、--check-splits参照)"
    if ep["closed"]:
        pl = ep.get("pl")
        if not pl or pl.get("profit_amount") is None:
            return "損益 —"
        return f"実現 {pl['profit_amount']:+,}円 ({pl['return_pct']:+.1f}%)"
    op = ep.get("open_pl") or {}
    parts = []
    if op.get("held_qty"):
        parts.append(f"残 {op['held_qty']}株")
    if op.get("realized"):
        parts.append(f"実現 {op['realized']:+,}円")
    if op.get("unrealized") is not None:
        parts.append(f"含み {op['unrealized']:+,}円")
    return " / ".join(parts) if parts else "—"


def _print_episode(ep: Dict[str, Any], show_fills: bool) -> None:
    held = " [保有中]" if not ep["closed"] else ""
    carry_over = " [期首持越し]" if ep.get("carry_over") else ""
    period = ep["open_date"]
    if ep.get("close_date") and ep["close_date"] != ep["open_date"]:
        period += f"〜{ep['close_date']}"
    print(f"{ep['code_s']:>5} {ep['kind']}{held}{carry_over} {ep['stock_name']}")
    print(f"      期間 {period}  最大建玉 {ep['qty_peak']}株  {_fmt_pl(ep)}")
    if ep.get("review_memo"):
        first_line = ep["review_memo"].splitlines()[0]
        print(f"      📝 {first_line}")
    if show_fills:
        for f in ep["fills"]:
            tate = f" 建{f['tate_price']:,.0f}" if f.get("tate_price") else ""
            print(f"        {f['trade_date']} {f['side_label']:>4} {f['trade_kind']:<6}"
                  f" {f['qty']:>5}株 @{f['price']:,.0f}{tate} [{f['broker']}]")


def _yfinance_splits(code_s: str, stock_db: Dict[str, Any]):
    """yfinance の corporate actions (splits) を取得する。失敗時は None。issue #398。

    ticker symbol は price._get_ticker_symbol で市場区分別のサフィックス
    (.T/.S/.N/.F) を解決する (東証固定の .T だと札証・名証・福証銘柄で誤る)。
    """
    import price as price_module

    try:
        import yfinance as yf
        ticker_symbol = price_module._get_ticker_symbol(code_s, stock_db.get(code_s, {}))
        ticker = yf.Ticker(ticker_symbol)
        return ticker.splits
    except Exception as e:  # yfinance 側の例外種別は不定
        log_warning(f"yfinance 取得失敗 ({code_s}): {e}")
        return None


def _report_split_candidate(code_s: str, splits, reason: str, db_path: Optional[str]) -> None:
    """未登録の分割・併合イベントを pending_review に記録し、登録コマンドを案内する。

    (a) 単価ジャンプ検知・(b) エピソード期間総当たりチェックの両方から呼ぶ共通処理。
    ex_date が分かるイベントごとに pending へ積む (PRレビュー #405 P1 対応:
    複数の未登録イベントがあっても登録済みの日付だけが解除されるようにするため)。
    """
    if splits is None or splits.empty:
        ps.mark_split_pending_review(code_s, reason=reason, db_path=db_path)
        log_warning("      split_adj 未登録。yfinance に該当データなし (要手動判断)")
        log_print(f"      却下: python show_fill_episodes.py --reject-split {code_s}")
        return
    for ex_date, ratio in splits.items():
        ps.mark_split_pending_review(
            code_s, reason=reason, ex_date=str(ex_date.date()), db_path=db_path)
        log_warning(f"      split_adj 未登録。yfinance suggests: {ex_date.date()} ratio={ratio}")
    log_print(f"      登録: python show_fill_episodes.py --register-split {code_s} "
              f"<ex_date> <ratio>")
    log_print(f"      却下: python show_fill_episodes.py --reject-split {code_s} <ex_date>")


def _filter_rejected_splits(code_s: str, splits, rejected_dates: set):
    """却下済み ex_date を yfinance splits から除外する。"""
    if splits is None or splits.empty:
        return splits
    if not rejected_dates:
        return splits
    mask = [ex_date.strftime("%Y-%m-%d") not in rejected_dates for ex_date in splits.index]
    return splits[mask]


def _check_splits(db_path: Optional[str]) -> int:
    """分割・併合の疑いを診断する。issue #398。

    (a) 単価ジャンプ検知: 全銘柄の fill を約定日順に見て単価が急変する箇所を検出。
    (b) 現物エピソード期間の総当たり: 単価ジャンプが無くても、エピソード期間中の
        split イベントの有無を yfinance で直接確認する。

    fill 本体・split_adj は更新しないが、未登録の発見は split_pending_review
    (拒否リスト) に記録する。build_fill_episodes は yfinance を呼ばないため、
    (b) でのみ見つかるケース (9252相当、2:1分割など) を webapp 表示でも
    検知できるようにするため。
    """
    import make_stock_db

    all_fills = ps.list_fills(db_path=db_path)
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for f in all_fills:
        by_code.setdefault(f["code_s"], []).append(f)

    names = helpers._bulk_resolve_stock_names(list(by_code.keys()))
    registered = ps.list_all_split_adjustments(db_path=db_path)
    rejected = ps.list_rejected_review_events(db_path=db_path)
    stock_db = make_stock_db.load_stock_db()  # ticker symbol の市場区分解決用 (1回だけロード)
    found = 0

    for code_s, fills in sorted(by_code.items()):
        events = registered.get(code_s, [])
        rejected_dates = set(rejected.get(code_s, []))
        # ジャンプ検知は換算後の fills に対して行う (build_fill_episodes と同じ理由:
        # 未換算のまま検知すると、登録済みイベントで残高の基準が変わった後の
        # 残高追跡が崩れ、別の未登録イベントのジャンプを見逃す)。
        adjusted_fills = helpers._apply_split_adjustments(fills, events) if events else fills
        jumps = helpers._detect_price_jumps(adjusted_fills)
        for jump in jumps:
            # このジャンプの日付範囲をカバーする登録済みイベントがあるかで判定する
            # (銘柄単位の in registered だけだと、後日発生した別イベントを見逃す)
            covering = [ev for ev in events
                       if jump["before_date"] < ev["ex_date"] <= jump["after_date"]]
            if covering:
                log_print(f"      split_adj 登録済み: {covering}")
                continue
            if "unknown" in rejected_dates:
                continue
            splits = _yfinance_splits(code_s, stock_db)
            # yfinance の全履歴をそのまま渡すと、登録済みの古い日付まで再度
            # pending に積んでしまう (PRレビュー対応)。このジャンプの日付範囲を
            # カバーする未登録イベントだけに絞り込む。
            if splits is not None and not splits.empty:
                registered_dates = {ev["ex_date"] for ev in events}
                mask = [
                    jump["before_date"] < ex_date.strftime("%Y-%m-%d") <= jump["after_date"]
                    and ex_date.strftime("%Y-%m-%d") not in registered_dates
                    and ex_date.strftime("%Y-%m-%d") not in rejected_dates
                    for ex_date in splits.index
                ]
                splits = splits[mask]
                if splits.empty:
                    continue
            found += 1
            log_warning(f" {code_s:>5} {names.get(code_s, ''):<12} 単価ジャンプ検出: "
                        f"{jump['before_date']} @{jump['before_price']:,.0f} -> "
                        f"{jump['after_date']} @{jump['after_price']:,.0f} "
                        f"(x{jump['after_price'] / jump['before_price']:.1f})")
            _report_split_candidate(code_s, splits, "単価ジャンプ検出", db_path)

    genbutsu_ranges: Dict[str, List[Dict[str, str]]] = {}
    for ep in helpers.build_fill_episodes(db_path=db_path):
        if ep["kind"] != "現物":
            continue
        genbutsu_ranges.setdefault(ep["code_s"], []).append({
            "open_date": ep["open_date"],
            "close_date": ep.get("close_date") or "9999-12-31",
        })
    for code_s, ranges in sorted(genbutsu_ranges.items()):
        splits = _yfinance_splits(code_s, stock_db)
        if splits is None or splits.empty:
            continue
        registered_dates = {ev["ex_date"] for ev in registered.get(code_s, [])}
        rejected_dates = set(rejected.get(code_s, []))
        splits = _filter_rejected_splits(code_s, splits, rejected_dates)
        if splits.empty:
            continue
        # エピソード期間中に権利落ちし、かつ未登録のイベントのみが当該ラウンドに影響する。
        # 保有中だけに限定すると、pending 検出後に売却でクローズした瞬間に警告が外れ、
        # 2:1 分割など単価ジャンプ閾値未満の誤損益が集計へ戻ってしまう。
        relevant_dates = []
        for ex_date in splits.index:
            ex_date_s = ex_date.strftime("%Y-%m-%d")
            if ex_date_s in registered_dates:
                continue
            if any(r["open_date"] < ex_date_s <= r["close_date"] for r in ranges):
                relevant_dates.append(ex_date)
        if not relevant_dates:
            continue
        found += 1
        log_warning(f" {code_s:>5} {names.get(code_s, ''):<12} エピソード期間チェック: "
                    f"split イベントあり (未登録)")
        _report_split_candidate(
            code_s, splits.loc[relevant_dates], "エピソード期間総当たりチェック", db_path)

    if found == 0:
        log_print("分割・併合の疑いは検出されませんでした。")
    return 0


def _check_dups(db_path: Optional[str]) -> int:
    """未確定CSV由来の重複約定を検出する (DB非更新)。

    証券会社CSVは約定直後だと受渡金額が 0 で出力されることがあり、後日その約定が
    確定額つきで再取込されると **同じ約定が2件の fill として残る**。dedup_key は
    amount を含む (make_dedup_key) ため、0円行と確定額行は別ハッシュになり
    冪等取込では弾けない。

    検出方法: 約定本体 (約定日・銘柄・売買・区分・数量・単価) が同一で、
    受渡金額 0 の行と 0 でない行が併存する組を重複候補として報告する。

    信用新規は受渡金額が元から 0 なので **対象外** (495件が該当し、全件が正常)。
    誤検知を避けるためここを除外するのが肝。
    """
    fills = ps.list_fills(db_path=db_path)
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for f in fills:
        if f.get("trade_kind") == "信用新規":
            continue  # 建玉を作る側は受渡金額を持たないのが正常
        # broker をキーに含める: 別会社で同日・同銘柄・同条件の約定をした場合、
        # 片方が未確定 (受渡金額0) だと正当な2件を重複候補として報告してしまう。
        # 出力は未確定側の削除を勧めるため、正常な fill を消す事故につながる
        key = (f.get("trade_date"), f.get("code_s"), f.get("side"),
               f.get("trade_kind"), f.get("qty"), f.get("price"), f.get("broker"))
        groups.setdefault(key, []).append(f)

    found = 0
    for key, rows in sorted(groups.items()):
        zero = [r for r in rows if (r.get("amount") or 0) == 0]
        fixed = [r for r in rows if (r.get("amount") or 0) != 0]
        if not (zero and fixed):
            continue
        found += 1
        trade_date, code_s, side, trade_kind, qty, price, broker = key
        log_warning(f"[重複候補] {code_s} {trade_date} {side} {trade_kind} "
                    f"{qty}株 @{price} ({broker}) — 未確定 {len(zero)}件 / 確定 {len(fixed)}件")
        for r in zero + fixed:
            log_print(f"    seq={r.get('seq')} amount={r.get('amount')} "
                      f"broker={r.get('broker')} dedup_key={r.get('dedup_key')}")

    if found:
        log_warning(f"--- 重複候補 {found} 件。未確定 (受渡金額0) 側の削除を検討してください")
    else:
        log_print("--- 未確定CSV由来の重複はありません")
    return 1 if found else 0


def _register_split(code_s: str, ex_date: str, ratio: str, db_path: Optional[str]) -> int:
    """split_adj イベントを1件登録する (issue #398)。"""
    stored = ps.add_split_adjustment(code_s, ex_date, float(ratio), db_path=db_path)
    log_print(f"登録しました: {code_s} {stored['events']}")
    return 0


def _reject_split(args: List[str], db_path: Optional[str]) -> int:
    """分割・併合ではないと判断した pending_review を解除する。"""
    if len(args) not in (1, 2):
        log_warning("--reject-split は CODE または CODE EX_DATE を指定してください。")
        return 2
    code_s = args[0]
    ex_date = args[1] if len(args) == 2 else None
    changed = ps.reject_split_pending_review(code_s, ex_date=ex_date, db_path=db_path)
    if changed:
        target = ex_date or "全pending"
        log_print(f"却下しました: {code_s} {target}")
    else:
        target = ex_date or "pending"
        log_print(f"該当する pending はありません: {code_s} {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="fill 建玉ラウンド (エピソード) を確認する (DB非更新)"
    )
    parser.add_argument("code_s", nargs="?", default=None,
                        help="銘柄コード (省略時は全銘柄)")
    parser.add_argument("--open", action="store_true", help="保有中のみ表示")
    parser.add_argument("--memo", action="store_true", help="振り返りメモ付きのみ表示")
    parser.add_argument("--fills", action="store_true", help="内訳の個別 fill も表示")
    parser.add_argument("--db-path", default=None, help="portfolio DB パス")
    parser.add_argument("--check-dups", action="store_true",
                        help="未確定CSV由来の重複約定を検出する (DB非更新)")
    parser.add_argument("--check-splits", action="store_true",
                        help="分割・併合の疑いを診断する (issue #398、DB非更新)")
    parser.add_argument("--register-split", nargs=3, metavar=("CODE", "EX_DATE", "RATIO"),
                        help="分割・併合の換算比率を登録する (例: 1491 2025-09-29 0.05)")
    parser.add_argument("--reject-split", nargs="+", metavar=("CODE_OR_EX_DATE"),
                        help="分割・併合ではない pending を解除する (例: 1491 [2025-09-29])")
    args = parser.parse_args()

    if args.register_split:
        return _register_split(*args.register_split, db_path=args.db_path)
    if args.reject_split:
        return _reject_split(args.reject_split, db_path=args.db_path)
    if args.check_dups:
        return _check_dups(args.db_path)
    if args.check_splits:
        return _check_splits(args.db_path)

    episodes: List[Dict[str, Any]] = helpers.build_fill_episodes(db_path=args.db_path)

    if args.code_s:
        code = args.code_s.upper()
        episodes = [e for e in episodes if e["code_s"] == code]
    if args.open:
        episodes = [e for e in episodes if not e["closed"]]
    if args.memo:
        episodes = [e for e in episodes if e.get("review_memo")]

    if not episodes:
        print("該当するエピソードがありません。")
        return 0

    for ep in episodes:
        _print_episode(ep, show_fills=args.fills or bool(args.code_s))
        print()

    print(f"--- {len(episodes)} エピソード "
          f"(クローズ済 {sum(1 for e in episodes if e['closed'])} / "
          f"保有中 {sum(1 for e in episodes if not e['closed'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
