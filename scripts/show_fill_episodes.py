#!/usr/bin/env python3
"""fill 建玉ラウンド (エピソード) を確認する開発補助CLI (issue #387)。

build_fill_episodes (webapp.helpers) を呼んで、取込後の検算・保有中確認・
現引や信用の損益・振り返りメモの紐付けをターミナルで確認する。DB は更新しない。

使い方:
    cd scripts && python show_fill_episodes.py            # 全エピソード (最新約定日降順)
    cd scripts && python show_fill_episodes.py 6324       # 特定銘柄のみ
    cd scripts && python show_fill_episodes.py --open     # 保有中のみ
    cd scripts && python show_fill_episodes.py --memo     # 振り返りメモ付きのみ
    cd scripts && python show_fill_episodes.py --fills 6324  # 内訳 fill も表示
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from webapp import helpers  # noqa: E402


def _fmt_pl(ep: Dict[str, Any]) -> str:
    """損益列の文字列を組み立てる (クローズ=実現、保有中=実現+含み)。"""
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
    args = parser.parse_args()

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
