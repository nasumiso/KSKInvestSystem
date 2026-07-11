#!/usr/bin/env python3
"""action_log の売買日イベントに終値プロキシを一括付与する CLI (issue #361)。

既存 DB の price_log (直近30営業日) の範囲のみ終値を埋める。窓外は None のまま。
同時に土日 timestamp を直前営業日に正規化する。

使い方:
    cd scripts && python backfill_price_proxy.py             # None のみ埋める (冪等)
    cd scripts && python backfill_price_proxy.py --overwrite # actual 以外を再取得

夜間の price 更新後に叩くと、記録時に終値未確定だった当日イベントの None が埋まる。
"""

import argparse
import os
import sys

# scripts/ を sys.path に追加(直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402

try:
    from ks_util import log_print
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="price_source != 'actual' のイベントを全て再取得する (既存プロキシも上書き)",
    )
    args = parser.parse_args()

    stats = ps.backfill_price_proxies(overwrite=args.overwrite)
    log_print(
        "backfill_price_proxy 完了:",
        f"updated={stats['updated']}",
        f"skipped={stats['skipped']}",
        f"no_price={stats['no_price']}",
        f"date_fixed={stats['date_fixed']}",
    )


if __name__ == "__main__":
    main()
