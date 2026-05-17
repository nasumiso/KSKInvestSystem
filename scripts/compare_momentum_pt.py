# -*- coding: utf-8 -*-
"""新旧モデル比較スクリプト (issue #104 Phase 2)。

既存DBの全銘柄について、Phase 2 切替前の正規分布モデル (scale=0.3) と、
切替後の対数正規モデル (calib loc/scale) で算出した momentum_pt を並べた
CSV を出力する。code_rank への波及度の見当をつけるために使う。

CSV: data/momentum_pt_compare.csv
集計サマリー: stderr

実行:
    cd scripts && python compare_momentum_pt.py
"""

import csv
import math
import os
import statistics
import sys

from scipy.stats import norm

import make_market_db
import make_stock_db
import price
from ks_util import DATA_DIR, log_print


def calc_momentum_pt_old(rs_raw, topix_rs_raw):
    """Phase 2 切替前の正規分布モデル (scale=0.3 ハードコード) を再現する。"""
    if topix_rs_raw <= 0 or rs_raw <= 0:
        return 0
    rs_rel = rs_raw / topix_rs_raw
    return int(100 * (1 - norm.sf(x=rs_rel, loc=1.0, scale=0.3)))


def main():
    stocks = make_stock_db.load_stock_db()
    market_db = make_market_db.get_market_db()
    topix_rs_raw = market_db.get("topix", {}).get("rs_raw", 0)

    loc, scale, source = price.get_momentum_calib(market_db)
    print(
        "[compare] topix_rs_raw=%.4f, calib_loc=%.4f, calib_scale=%.4f, source=%s"
        % (topix_rs_raw, loc, scale, source),
        file=sys.stderr,
    )

    rows = []
    for code_s, stock in stocks.items():
        rs_raw = stock.get("rs_raw", 0)
        if not rs_raw or rs_raw <= 0:
            continue
        old_pt = calc_momentum_pt_old(rs_raw, topix_rs_raw)
        new_pt = price.calc_momentum_pt_value(rs_raw, market_db)
        rows.append({
            "code_s": code_s,
            "name": stock.get("name", ""),
            "rs_raw": rs_raw,
            "momentum_pt_old": old_pt,
            "momentum_pt_new": new_pt,
            "diff": new_pt - old_pt,
        })

    out_dir = os.path.join(DATA_DIR, "code_rank_data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "momentum_pt_compare.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["code_s", "name", "rs_raw", "momentum_pt_old", "momentum_pt_new", "diff"],
        )
        writer.writeheader()
        writer.writerows(rows)
    log_print("[compare] %d 件を %s に出力しました" % (len(rows), out_path))

    if not rows:
        print("[compare] 比較対象なし", file=sys.stderr)
        return

    # |diff| 分布
    diffs = sorted(abs(r["diff"]) for r in rows)
    n = len(diffs)
    avg_abs_diff = statistics.mean(diffs)
    median_abs_diff = statistics.median(diffs)
    p90_abs_diff = diffs[int(n * 0.9)]
    max_abs_diff = diffs[-1]
    print(
        "[compare] |diff| 分布: 平均=%.2f, 中央値=%.2f, p90=%.2f, 最大=%d (n=%d)"
        % (avg_abs_diff, median_abs_diff, p90_abs_diff, max_abs_diff, n),
        file=sys.stderr,
    )

    # momentum_pt 単体での上位N入れ替わり
    for top_n in (50, 100):
        old_top = {r["code_s"] for r in sorted(rows, key=lambda r: -r["momentum_pt_old"])[:top_n]}
        new_top = {r["code_s"] for r in sorted(rows, key=lambda r: -r["momentum_pt_new"])[:top_n]}
        in_only_old = old_top - new_top
        in_only_new = new_top - old_top
        print(
            "[compare] 上位%d 入れ替わり: 旧のみ=%d / 新のみ=%d (差集合サイズが同じはずだが念のため両方表示)"
            % (top_n, len(in_only_old), len(in_only_new)),
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
