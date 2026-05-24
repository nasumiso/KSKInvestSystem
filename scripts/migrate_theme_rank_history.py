#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""テーマランク履歴を過去のtheme_rank_YYMMDD.htmlから遡及再構築する。

- theme_rank/ 直下 + theme_rank/history/ の全 HTML から営業日日付を導出
- mtime を get_price_day() に通して本番ロジック (_theme_rank_history_date) と整合
- 同一営業日に複数ファイルがある場合は mtime が新しい方を採用
- 末尾 THEME_STRENGTH_WINDOW_DAYS 営業日ぶんを使って history を構築
- 既存 market_db.theme_rank_history / theme_strength / theme_strength_days を上書き
- --dry-run: DB を更新せず構築予定の件数・上位10テーマを表示
"""

import argparse
import os
import re
from datetime import datetime

from db_shelve import MARKET_SHELVE, ShelveDB
from ks_util import DATA_DIR, file_read, get_price_day, log_print, log_warning
from make_market_db import (
    THEME_STRENGTH_WINDOW_DAYS,
    parse_theme_html,
    update_theme_rank_history,
)


THEME_RANK_FNAME_RE = re.compile(r"^theme_rank_\d{6}\.html$")


def collect_theme_rank_files(theme_rank_dir):
    """theme_rank/ と history/ から theme_rank_YYMMDD.html を全て収集する。

    Returns:
        list of (mtime_datetime, fpath) — mtime 昇順ソート済み
    """
    candidates = []
    for sub in (theme_rank_dir, os.path.join(theme_rank_dir, "history")):
        if not os.path.isdir(sub):
            continue
        for fname in os.listdir(sub):
            if not THEME_RANK_FNAME_RE.match(fname):
                continue
            fpath = os.path.join(sub, fname)
            mtime = datetime.fromtimestamp(os.stat(fpath).st_mtime)
            candidates.append((mtime, fpath))
    candidates.sort(key=lambda x: x[0])
    return candidates


def select_latest_per_business_day(candidates):
    """営業日ごとに mtime が最新の1ファイルを採用する。

    Args:
        candidates: list of (mtime, fpath) — mtime 昇順想定
    Returns:
        list of (date_str, fpath) — date_str 昇順
    """
    by_day = {}
    for mtime, fpath in candidates:
        date_str = get_price_day(mtime).isoformat()
        by_day[date_str] = fpath  # 後勝ち=mtime新しい方
    return sorted(by_day.items(), key=lambda x: x[0])


def build_history(theme_rank_dir, window_days=THEME_STRENGTH_WINDOW_DAYS):
    """過去ファイル群から history / strength / strength_days を再構築する。"""
    candidates = collect_theme_rank_files(theme_rank_dir)
    if not candidates:
        log_warning("theme_rank_*.html が見つかりません:", theme_rank_dir)
        return [], {}, {}
    per_day = select_latest_per_business_day(candidates)
    per_day = per_day[-window_days:]

    history = []
    strength = {}
    strength_days = {}
    for date_str, fpath in per_day:
        html = file_read(fpath)
        theme_list = parse_theme_html(html)
        if not theme_list:
            log_warning("空のtheme_rank:", fpath)
            continue
        history, strength, strength_days = update_theme_rank_history(
            history, date_str, theme_list, window_days=window_days,
        )
    return history, strength, strength_days


def report(history, strength, strength_days):
    """構築結果を log_print で出力する。"""
    log_print("再構築history件数:", len(history))
    if history:
        log_print("日付範囲:", history[0][0], "〜", history[-1][0])
    log_print("上位10テーマ (strength desc):")
    top = sorted(strength.items(), key=lambda x: -x[1])[:10]
    for theme, pt in top:
        log_print("  %-30s pt=%4d days=%2d" % (theme, pt, strength_days.get(theme, 0)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="DB を更新せず構築結果のみ表示")
    args = parser.parse_args()

    theme_rank_dir = os.path.join(DATA_DIR, "market_data", "theme_rank")
    history, strength, strength_days = build_history(theme_rank_dir)
    report(history, strength, strength_days)

    if args.dry_run:
        log_print("--dry-run: DB は更新しません")
        return
    if not history:
        log_print("再構築結果が空のため DB 更新をスキップします")
        return

    with ShelveDB(MARKET_SHELVE) as db:
        db["theme_rank_history"] = history
        db["theme_strength"] = strength
        db["theme_strength_days"] = strength_days
    log_print("market_db の theme_rank_history / theme_strength / theme_strength_days を更新しました")


if __name__ == "__main__":
    main()
