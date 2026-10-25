"""テーマ別のブレイク集計 (issue #509)。

日次発掘レポート (shintakane_result_YYMMDD.csv) の本日リスト (新高値・出来高急増・PTS) を
株探テーマで数え、平常より急に増えたテーマを /market に出す。
集計はリクエストごとに CSV から作り、ファイルや DB には書かない。
"""

import csv
import glob
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import portfolio_shelve as ps
from ks_util import DATA_DIR

RESULT_DIR = os.path.join(DATA_DIR, "shintakane_result_data")

MIN_COUNT = 5  # 該当テーマとみなす最小の銘柄数
RATIO = 2.5  # 前 WINDOW 日の平均の何倍以上で該当とするか
WINDOW = 20  # 平常の平均と掲載日数を数える営業日数
MERGE_OVERLAP = 0.6  # 構成銘柄がこの割合以上重なるテーマは1行に畳む
MAX_ROWS = 8
MAX_UNFOLLOWED_PER_THEME = 2
# 未フォロー銘柄を監視に追加したときの action_log の reason。月次の振り返り (trade_review) が前方一致で拾う
DISCOVERY_PREFIX = "発掘元:日次レポート"

# 本日リストに当たる節 (【過去銘柄】は数えない)
_TODAY_SECTIONS = {"【本日銘柄】": True, "【本日既出銘柄】": False}
_STATUS_ORDER = {"1保": 0, "2準": 1, "3監": 2}
_FILE_RE = re.compile(r"shintakane_result_(\d{6})\.csv$")


def _hyperlink_text(cell: str) -> str:
    """=HYPERLINK("url","表示") の表示文字列を返す。式でなければそのまま。"""
    m = re.match(r'=HYPERLINK\(".*?",\s*"(.*)"\)$', cell)
    return m.group(1).replace('""', '"') if m else cell


def _to_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def load_result_csv(path: str) -> List[Dict[str, Any]]:
    """日次レポート CSV から本日リストの行を読む。列は見出しの名前で引く (2026-03 に並びが変わった)。"""
    rows: List[Dict[str, Any]] = []
    col: Dict[str, int] = {}
    first: Optional[bool] = None
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0] == "コード":
                col = {name: i for i, name in enumerate(row)}
                continue
            if row[0].startswith("【"):
                first = _TODAY_SECTIONS.get(row[0])
                continue
            if first is None or not col:
                continue

            def cell(name: str) -> str:
                i = col.get(name)
                return row[i] if i is not None and i < len(row) else ""

            rows.append({
                "code_s": _hyperlink_text(cell("コード")),
                "name": _hyperlink_text(cell("銘柄名")),
                "kind": cell("種類"),
                "first": first,
                "score": _to_float(cell("業績スコア")),
                "themes": [t for t in cell("テーマ").split(",") if t],
                "change": cell("前日比(%)"),
                "overview": cell("概要"),
                "news": _hyperlink_text(cell("ニュース1")),
            })
    return rows


def recent_result_csvs(n: int = WINDOW + 1) -> List[Tuple[str, str]]:
    """日次レポート CSV を新しい方から n 日分、日付昇順の [(YYYY-MM-DD, path)] で返す。

    30日を過ぎた CSV は history/ に移るので、両方を見る。
    """
    by_date: Dict[str, str] = {}
    for d in (os.path.join(RESULT_DIR, "history"), RESULT_DIR):  # 同じ日付は本体側を優先
        for path in glob.glob(os.path.join(d, "shintakane_result_*.csv")):
            m = _FILE_RE.search(os.path.basename(path))
            if m:
                yymmdd = m.group(1)
                by_date["20%s-%s-%s" % (yymmdd[:2], yymmdd[2:4], yymmdd[4:])] = path
    return sorted(by_date.items())[-n:]


def _theme_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in rows:
        for t in r["themes"]:
            counts[t] = counts.get(t, 0) + 1
    return counts


def build_breakout(days: List[Tuple[str, List[Dict[str, Any]]]],
                   managed: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """最新日に平常より急に増えたテーマを、表示用の行にして返す。

    Args:
        days: 日付昇順の [(YYYY-MM-DD, load_result_csv の戻り値)]。最後が最新日
        managed: 管理対象 {code_s: "1保"/"2準"/"3監"}
    Returns:
        {"date", "rows": [...], "more": 上限で省いた行数}。days が空なら None
    """
    if not days:
        return None
    date, rows = days[-1]
    prev_counts = [_theme_counts(r) for _, r in days[-1 - WINDOW:-1]]
    today_counts = _theme_counts(rows)
    listed = [{r["code_s"] for r in day_rows} for _, day_rows in days[-WINDOW:]]

    hits: Dict[str, float] = {}
    for theme, n in today_counts.items():
        avg = sum(c.get(theme, 0) for c in prev_counts) / len(prev_counts) if prev_counts else 0.0
        if n >= MIN_COUNT and n >= RATIO * avg:
            hits[theme] = avg

    # 該当数の多い順に見て、構成銘柄の大半が先の行と重なるテーマはその行の別名にする
    groups: List[Dict[str, Any]] = []
    for theme in sorted(hits, key=lambda t: (-today_counts[t], t)):
        members = [r for r in rows if theme in r["themes"]]
        codes = {r["code_s"] for r in members}
        for g in groups:
            if len(codes & g["codes"]) >= MERGE_OVERLAP * len(codes):
                g["aliases"].append(theme)
                g["members"] += [r for r in members if r["code_s"] not in g["codes"]]
                g["codes"] |= codes
                break
        else:
            groups.append({"theme": theme, "aliases": [], "members": members, "codes": codes})

    shown_unfollowed = set()
    out = []
    for g in groups[:MAX_ROWS]:
        members = g["members"]
        managed_rows = sorted(
            ({"code_s": r["code_s"], "name": r["name"], "status": managed[r["code_s"]],
              "days_listed": sum(r["code_s"] in s for s in listed)}
             for r in members if r["code_s"] in managed),
            key=lambda m: (_STATUS_ORDER.get(m["status"], 9), -m["days_listed"], m["code_s"]))
        candidates = sorted(
            (r for r in members
             if r["code_s"] not in managed and r["first"] and "高" in r["kind"]
             and r["code_s"] not in shown_unfollowed),
            key=lambda r: (-r["score"], r["code_s"]))
        unfollowed = candidates[:MAX_UNFOLLOWED_PER_THEME]
        shown_unfollowed |= {r["code_s"] for r in unfollowed}
        out.append({
            "theme": g["theme"],
            "aliases": g["aliases"],
            "count": len(members),
            "kinds": {k: sum(k in r["kind"] for r in members) for k in ("高", "出", "P")},
            "avg": hits[g["theme"]],
            "history": [c.get(g["theme"], 0) for c in prev_counts],
            "managed": managed_rows,
            "unfollowed": unfollowed,
            "others": len(members) - len(managed_rows) - len(unfollowed),
            "discovery_reason": "%s テーマ=%s 日付=%s" % (DISCOVERY_PREFIX, g["theme"], date),
        })
    return {"date": date, "rows": out, "more": max(len(groups) - MAX_ROWS, 0)}


def load_for_display() -> Optional[Dict[str, Any]]:
    """/market 用。CSV が無ければ None。"""
    paths = recent_result_csvs()
    if not paths:
        return None
    days = [(d, load_result_csv(p)) for d, p in paths]
    managed = {r["code_s"]: r["status"] for r in ps.list_records()}
    return build_breakout(days, managed)
