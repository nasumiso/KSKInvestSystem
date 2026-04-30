#!/usr/bin/env python3
"""PTS (夜間取引) CSV のロード用ユーティリティ。

shintakane.py の `search_fromcsv_pts()` から CSV パース部を抽出し、
make_stock_db / research 系から呼べる純粋関数として提供する。
"""

import csv
import datetime as _datetime
import os
from typing import Dict, Optional

from ks_util import DATA_DIR, log_warning


def get_pts_csv_path_for_date(date_obj):
    """指定日 (datetime.date) の pts_YYMMDD.csv パスを返す。

    存在チェックはしない。命名規則は `shintakane.py:208` の
    `get_pts_day_txtname()` と一致させる: `pts_YYMMDD.csv`。
    """
    if not isinstance(date_obj, _datetime.date):
        raise TypeError(f"date_obj は datetime.date 型: got {type(date_obj).__name__}")
    fname = "pts_%02d%02d%02d.csv" % (
        date_obj.year - 2000,
        date_obj.month,
        date_obj.day,
    )
    return os.path.join(DATA_DIR, "today_stocks", fname)


def load_pts_changes_for_date(date_obj) -> Dict[str, str]:
    """指定日の PTS CSV を読み、`{code_s: zenjitsuhi_per_str}` の dict を返す。

    - 引数: 必須の `date_obj` (datetime.date)。日付一致を必須化
    - 戻り値の値は ``"+2.5"`` 形式 (% 記号を除去、符号は保持)
    - 指定日 CSV 不在時は空 dict
    - 「最新CSVフォールバック」は実装しない (古い CSV を当日扱いする誤適用防止)
    """
    csv_path = get_pts_csv_path_for_date(date_obj)
    if not os.path.exists(csv_path):
        return {}

    result: Dict[str, str] = {}
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or len(row) < 7:
                    continue
                code_field = row[1].split()
                if not code_field:
                    continue
                code_s = code_field[0]
                raw = row[6] or ""
                normalized = _normalize_pts_change_str(raw)
                if normalized:
                    result[code_s] = normalized
    except (OSError, csv.Error) as e:
        log_warning(f"PTS CSV 読み込み失敗: {csv_path}: {e}")
        return {}
    return result


def _normalize_pts_change_str(raw: str) -> Optional[str]:
    """CSV の zenjitsuhi_per ("+2.5%") を保存形式 ("+2.5") に正規化。

    - 末尾の "%" を除去
    - 符号 ("+" / "-") は保持
    - 空文字や数値変換不能なら None
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("%"):
        s = s[:-1]
    s = s.strip()
    if not s:
        return None
    body = s.lstrip("+-")
    if not body:
        return None
    try:
        float(body)
    except ValueError:
        return None
    return s
