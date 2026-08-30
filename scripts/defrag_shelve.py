#!/usr/bin/env python3
"""
shelve DBのデフラグメンテーションスクリプト。

dbm.dumb はレコード更新時にファイル内に隙間（断片化）を残すため、
長期運用で .dat ファイルが肥大化する。
このスクリプトは全データを取り出し、DBファイルを再構築することで
断片化を解消する。

処理本体は db_shelve.compact_shelve() にある (issue #194)。
自動実行は行わないので、肥大化に気づいたら手動で実行する必要がある
(stocks_shelve は `make_stock_db.py compact` でも実行できる)。
実行中に読み書きされると壊れるため、WebApp と日次バッチを止めてから叩くこと。

使い方:
    cd scripts && python defrag_shelve.py --target market
"""

import os
import sys
import argparse

# scriptsディレクトリからの相対パスでインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ks_util import log_print, log_error
from db_shelve import (
    STOCKS_SHELVE,
    MARKET_SHELVE,
    SECTOR_SHELVE,
    compact_shelve,
    format_size,
)


def main():
    parser = argparse.ArgumentParser(description="shelve DBのデフラグメンテーション")
    parser.add_argument(
        "--keep-backup",
        action="store_true",
        help="デフラグ後もバックアップファイルを保持する",
    )
    parser.add_argument(
        "--target",
        choices=["stocks", "market", "sector", "all"],
        default="stocks",
        help="デフラグ対象のDB (デフォルト: stocks)",
    )
    args = parser.parse_args()

    targets = {
        "stocks": ("株式DB", STOCKS_SHELVE),
        "market": ("市場DB", MARKET_SHELVE),
        "sector": ("セクターDB", SECTOR_SHELVE),
    }

    if args.target == "all":
        db_list = list(targets.values())
    else:
        db_list = [targets[args.target]]

    total_before = 0
    total_after = 0

    for label, db_path in db_list:
        try:
            result = compact_shelve(db_path, keep_backup=args.keep_backup)
        except Exception as exc:
            log_error(f"[{label}] デフラグ失敗: {exc}")
            continue
        if result:
            total_before += result["size_before"]
            total_after += result["size_after"]

    # 単一対象なら compact_shelve のログと重複するので合計は出さない
    if len(db_list) > 1 and total_before > 0:
        ratio = (1 - total_after / total_before) * 100
        log_print(
            "合計: %s → %s (削減 %.1f%%)"
            % (format_size(total_before), format_size(total_after), ratio)
        )


if __name__ == "__main__":
    main()
