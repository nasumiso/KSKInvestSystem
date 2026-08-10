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
    cd scripts && python show_fill_episodes.py --check-splits          # 分割・併合の疑いを診断 (issue #398)
    cd scripts && python show_fill_episodes.py --register-split 1491 2025-09-29 0.05  # 換算比率を登録

--check-splits は fill 本体・split_adj を更新しないが、未登録の発見を
split_pending_review (拒否リスト) に記録する。これにより webapp 表示 (yfinance を
呼ばない) でも同じ疑いを検知できる。--register-split で登録すれば解除される。
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
from ks_util import log_warning  # noqa: E402
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

    (a) 単価ジャンプ検知・(b) 保有中総当たりチェックの両方から呼ぶ共通処理。
    """
    ps.mark_split_pending_review(code_s, reason=reason, db_path=db_path)
    if splits is None or splits.empty:
        print("      split_adj 未登録。yfinance に該当データなし (要手動判断)")
        return
    for ex_date, ratio in splits.items():
        print(f"      split_adj 未登録。yfinance suggests: {ex_date.date()} ratio={ratio}")
    print(f"      登録: python show_fill_episodes.py --register-split {code_s} "
          f"<ex_date> <ratio>")


def _check_splits(db_path: Optional[str]) -> int:
    """分割・併合の疑いを診断する。issue #398。

    (a) 単価ジャンプ検知: 全銘柄の fill を約定日順に見て単価が急変する箇所を検出。
    (b) 保有中現物の総当たり: 単価ジャンプが無くても、保有継続中で売買が発生していない
        銘柄は yfinance で open_date 以降の split イベントの有無を直接確認する。

    fill 本体・split_adj は更新しないが、未登録の発見は split_pending_review
    (拒否リスト) に記録する。build_fill_episodes は yfinance を呼ばないため、
    (b) でのみ見つかるケース (9252相当) を webapp 表示でも検知できるようにするため。
    """
    import make_stock_db

    all_fills = ps.list_fills(db_path=db_path)
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for f in all_fills:
        by_code.setdefault(f["code_s"], []).append(f)

    names = helpers._bulk_resolve_stock_names(list(by_code.keys()))
    registered = ps.list_all_split_adjustments(db_path=db_path)
    stock_db = make_stock_db.load_stock_db()  # ticker symbol の市場区分解決用 (1回だけロード)
    found = 0

    for code_s, fills in sorted(by_code.items()):
        jumps = helpers._detect_price_jumps(fills)
        for jump in jumps:
            found += 1
            print(f" {code_s:>5} {names.get(code_s, ''):<12} 単価ジャンプ検出: "
                  f"{jump['before_date']} @{jump['before_price']:,.0f} -> "
                  f"{jump['after_date']} @{jump['after_price']:,.0f} "
                  f"(x{jump['after_price'] / jump['before_price']:.1f})")
            if code_s in registered:
                print(f"      split_adj 登録済み: {registered[code_s]}")
                continue
            _report_split_candidate(
                code_s, _yfinance_splits(code_s, stock_db), "単価ジャンプ検出", db_path)

    open_genbutsu_dates = {
        ep["code_s"]: ep["open_date"] for ep in helpers.build_fill_episodes(db_path=db_path)
        if not ep["closed"] and ep["kind"] == "現物"
    }
    for code_s, open_date in sorted(open_genbutsu_dates.items()):
        if code_s in registered:
            continue
        splits = _yfinance_splits(code_s, stock_db)
        if splits is None or splits.empty:
            continue
        # open_date より後に権利落ちしたイベントのみが今の保有に影響する
        relevant_dates = [ex_date for ex_date in splits.index
                          if ex_date.strftime("%Y-%m-%d") >= open_date]
        if not relevant_dates:
            continue
        found += 1
        print(f" {code_s:>5} {names.get(code_s, ''):<12} 保有中チェック (open_date={open_date}): "
              f"split イベントあり (未登録)")
        _report_split_candidate(
            code_s, splits.loc[relevant_dates], "保有中総当たりチェック", db_path)

    if found == 0:
        print("分割・併合の疑いは検出されませんでした。")
    return 0


def _register_split(code_s: str, ex_date: str, ratio: str, db_path: Optional[str]) -> int:
    """split_adj イベントを1件登録する (issue #398)。"""
    stored = ps.add_split_adjustment(code_s, ex_date, float(ratio), db_path=db_path)
    print(f"登録しました: {code_s} {stored['events']}")
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
    parser.add_argument("--check-splits", action="store_true",
                        help="分割・併合の疑いを診断する (issue #398、DB非更新)")
    parser.add_argument("--register-split", nargs=3, metavar=("CODE", "EX_DATE", "RATIO"),
                        help="分割・併合の換算比率を登録する (例: 1491 2025-09-29 0.05)")
    args = parser.parse_args()

    if args.register_split:
        return _register_split(*args.register_split, db_path=args.db_path)
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
