#!/usr/bin/env python3
"""主要データの件数を出す CLI (運用機と開発機の突き合わせ用)。

    python data_counts.py

`deploy/macmini.sh counts` が運用機と開発機の両方で実行し、横に並べる。
移行・`pull-data` の後、運用機の復旧時に、データが揃っているかを確かめる。
stdout には「名前 件数」の行だけを出す (ログは混ぜない)。
"""

import os
import sys

import portfolio_shelve as ps
import research_shelve as rs
from db_shelve import ShelveDB
from ks_util import DATA_DIR


def main():
    # 読み取り専用でも shelve は無ければ空で作られる。パス違いを 0件と誤読しないよう止める
    if not os.path.isdir(os.path.join(DATA_DIR, "stock_data")):
        sys.exit("stock_data がありません: %s" % DATA_DIR)
    with ShelveDB(os.path.join(DATA_DIR, "stock_data", "stocks_shelve"), read_only=True) as db:
        n_stocks = len(list(db.keys()))
    counts = [
        ("stocks", n_stocks),
        ("research", len(rs.list_research_records_locked())),
        ("records", len(ps.list_records())),
        ("positions", len(ps.list_positions())),
        ("fills", len(ps.list_fills())),
        ("action_logs", len(ps.list_action_logs())),
    ]
    for name, n in counts:
        print(name, n)


if __name__ == "__main__":
    main()
