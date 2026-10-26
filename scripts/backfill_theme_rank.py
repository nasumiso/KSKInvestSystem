#!/usr/bin/env python3
"""業態テーマ順位の過去分を一度だけ補完する (issue #531)。

日次バッチが記録を始める前の直近30営業日分を、日次と同じ定義で作る。
- 各日の終値は DB の price_log (銘柄・TOPIX とも直近30営業日)
- 26週前の週末終値は週足キャッシュ (銘柄 = yfinance 週足キャッシュ、TOPIX = 市場DBの株探週足)
- テーマ分類は今のもの (構成銘柄の入れ替えはさかのぼらない)
- すでに記録がある日は上書きしない。price_log や銘柄DBは書き換えない
- yfinance 週足キャッシュが無い銘柄 (株探フォールバック) は数えない

使い方:
    cd scripts && python backfill_theme_rank.py           # dry-run (書かずに件数と1日分の上位を表示)
    cd scripts && python backfill_theme_rank.py --apply   # 書き込む
"""

import argparse
import os
import sys
from datetime import date
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
import price  # noqa: E402
from ks_util import log_print  # noqa: E402
from make_market_db import get_market_db  # noqa: E402
from webapp.helpers import _bulk_get_stock_data, _group_codes_by_theme, calc_theme_ranks  # noqa: E402


def chg_26w_on(day: date, price_log: List[tuple], weekly_price_list: List[Any]) -> Optional[float]:
    """day の終値 / 26週前の週末終値。日次の price._calc_weekly_indicators と同じ定義。"""
    close = dict(price_log).get(day)
    close_26w = price._close_weeks_ago(weekly_price_list, day, 26)
    if not close or not close_26w:
        return None
    return round(float(close) / close_26w, 4)


def build_snapshots(records: List[Dict[str, Any]], stock_map: Dict[str, Dict[str, Any]],
                    weekly_by_code: Dict[str, List[Any]], topix: Dict[str, Any]) -> Dict[date, Dict[str, Any]]:
    """TOPIX の price_log の日ごとに、テーマ順位を作る (DB は読まない)。"""
    topix_weekly = topix.get("_weekly_price_list_raw") or []
    snapshots = {}
    for day, _ in topix.get("price_log") or []:
        topix_chg = chg_26w_on(day, topix["price_log"], topix_weekly)
        if topix_chg is None:
            continue
        day_map = {}
        for code_s, stock in stock_map.items():
            chg = chg_26w_on(day, stock.get("price_log") or [], weekly_by_code.get(code_s) or [])
            if chg is not None:
                day_map[code_s] = {"chg_26w": chg}
        ranks = calc_theme_ranks(records, day_map, {"chg_26w": topix_chg})
        if ranks:
            snapshots[day] = ranks
    return snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description="業態テーマ順位の過去分を補完する (issue #531)")
    parser.add_argument("--apply", action="store_true", help="実際に書き込む (未指定は dry-run)")
    args = parser.parse_args()

    records = ps.list_records(include_excluded=False)
    codes = sorted({c for cs in _group_codes_by_theme(records).values() for c in cs})
    stock_map = _bulk_get_stock_data(codes)
    weekly_by_code = {}
    for code_s in codes:
        _, rows = price._load_yfinance_cache(price.YFINANCE_WEEKLY_CACHE_FNAME % code_s)
        if rows:
            weekly_by_code[code_s] = rows
    log_print(f"backfill_theme_rank: 構成銘柄 {len(codes)} / 週足キャッシュあり {len(weekly_by_code)}")

    snapshots = build_snapshots(records, stock_map, weekly_by_code, get_market_db().get("topix", {}))
    if not snapshots:
        log_print("backfill_theme_rank: 作れた日がありません")
        return 1
    days = sorted(snapshots)
    log_print(f"backfill_theme_rank: {len(days)}日分 ({days[0]}〜{days[-1]})")
    top = sorted(snapshots[days[0]].items(), key=lambda kv: kv[1]["rank"])[:5]
    log_print(f"backfill_theme_rank: {days[0]} の上位", top)

    if not args.apply:
        log_print("backfill_theme_rank: dry-run のため書き込みませんでした (--apply で反映)")
        return 0
    written = sum(ps.save_theme_rank_snapshot(d, snapshots[d], overwrite=False) for d in days)
    log_print(f"backfill_theme_rank: {written}日分を書き込み ({len(days) - written}日は記録済みのため残した)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
