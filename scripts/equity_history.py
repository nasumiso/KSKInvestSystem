#!/usr/bin/env python3
"""約定と終値から、日々の運用総額と建玉1円あたりの日次リターンを復元する (issue #594 段階1)。

結果: doc/投資メモ分析/レビュー/資金管理の検証_2026-11.md (検証の前提)

約定を「銘柄 × 向き (買い玉・売り玉)」ごとの符号つき株数で積み上げ、毎日終値で値洗いする。
往復行を使わないので、建値の分からない期首持ち越しの玉も扱える。自己資本は出さない (段階2)。
証券会社では分けない。株を証券会社間で移す (入庫・出庫) と約定が出ず、分けると移した先と元で二重に数えるため。

使い方 (scripts/ から):
    python equity_history.py build   # yfinance から終値を取り、復元して保存し、検証を出す

保存先は $KS_DATA_DIR/analysis/equity_history.json (金額を含むのでリポジトリに置かない)。
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import ks_util  # noqa: E402
from ks_util import log_print, log_warning  # noqa: E402

START_DATE = "2025-01-06"  # 楽天の約定の取込が始まった日。これより前の損益は数えない
# 終値を持ち越した日数がこれを超えたら一覧に出す (売買停止・上場廃止など)
STALE_DAYS = 7
OUTPUT_PATH = os.path.join(ks_util.DATA_DIR, "analysis", "equity_history.json")
TRADE_HISTORY_DIR = os.path.join(ks_util.DATA_DIR, "trade_history")

LONG, SHORT = "long", "short"

# (side, trade_kind) → (向き, 株数の符号)。現引は信用から現物への振り替えで株数が変わらないので 0
_MOVES = {
    ("buy", "現物"): (LONG, 1), ("sell", "現物"): (LONG, -1),
    ("buy", "現物(単元未満)"): (LONG, 1), ("sell", "現物(単元未満)"): (LONG, -1),
    ("buy", "信用新規"): (LONG, 1), ("sell", "信用返済"): (LONG, -1),
    ("sell", "信用新規"): (SHORT, -1), ("buy", "信用返済"): (SHORT, 1),
    ("buy", "現引"): (LONG, 0),
}

Key = Tuple[str, str]  # (銘柄コード, 向き)


def to_moves(fills: Iterable[Dict[str, Any]],
             splits: Dict[str, List[Tuple[str, float]]]) -> List[Dict[str, Any]]:
    """約定を符号つきの株数の動きにする。株数・単価は分割でいまの株数の基準に直す。

    splits: {code_s: [(分割日, 比率)]}。比率は yfinance の Stock Splits (1株→2株なら 2.0)。
    分割日より前の約定に掛ける (信用・現引も。yfinance の終値は全部さかのぼって調整されるため)。
    """
    moves = []
    for f in fills:
        mapped = _MOVES.get((f["side"], f["trade_kind"]))
        if mapped is None:
            raise ValueError(f"想定外の約定の区分: {f['code_s']} {f['trade_date']} "
                             f"{f['side']}/{f['trade_kind']}")
        direction, sign = mapped
        if sign == 0:
            continue
        ratio = 1.0
        for d, r in splits.get(f["code_s"], []):
            if f["trade_date"] < d:
                ratio *= r
        moves.append({"key": (f["code_s"], direction), "date": f["trade_date"],
                      "dq": sign * f["qty"] * ratio, "price": f["price"] / ratio})
    moves.sort(key=lambda m: m["date"])
    return moves


def carry_in(moves: List[Dict[str, Any]]) -> Dict[Key, float]:
    """取込より前から持っていた買い玉の株数。引け時点の株数の最小値がマイナスなら、その分を持っていたとみなす。

    同じ日の約定は全部足してから見る (CSV の並びは約定の順とは限らず、日計りで返済が先に来ると
    ありもしない持ち越しが生まれるため)。
    売り玉は期首から持ち越さない前提 (空売りは 2026-02 以降だけ)。返済買いが新規売りより多ければ止める。
    """
    cum: Dict[Key, float] = {}
    low: Dict[Key, float] = {}
    for i, m in enumerate(moves):
        k = m["key"]
        cum[k] = cum.get(k, 0.0) + m["dq"]
        if i + 1 == len(moves) or moves[i + 1]["date"] != m["date"]:
            for kk, v in cum.items():
                low[kk] = min(low.get(kk, 0.0), v)
    result = {}
    for k, v in low.items():
        if k[1] == LONG and v < -1e-9:
            result[k] = -v
    over = [k for k in cum if k[1] == SHORT and cum[k] > 1e-9]
    if over:
        raise ValueError(f"返済買いが新規売りより多い売り玉があります: {over}")
    return result


def build_series(moves: List[Dict[str, Any]], closes: Dict[str, Dict[str, float]],
                 days: List[str]) -> Dict[str, Any]:
    """日ごとの運用総額 P_t・売買損益 PnL_t・建玉1円あたりの日次リターン r_t を出す。

    PnL_t = Σ_玉 [ q_{t−1} × (c_t − c_{t−1}) + Σ_その日の約定 Δq × (c_t − 約定単価) ]
    r_t = PnL_t ÷ (P_{t−1} + max(0, その日に建てた約定額 − 手じまった約定額))
    days の初日より前の約定と期首持ち越しは、初日の終値から値洗いを始める (初日より前の損益は数えない)。
    終値が無い日は直前の終値を持ち越す。

    Args:
        moves: to_moves() の戻り値
        closes: {code_s: {"YYYY-MM-DD": 終値}}
        days: 営業日 (昇順)
    """
    start = days[0]
    q: Dict[Key, float] = dict(carry_in(moves))
    i = 0
    while i < len(moves) and moves[i]["date"] < start:
        q[moves[i]["key"]] = q.get(moves[i]["key"], 0.0) + moves[i]["dq"]
        i += 1

    last: Dict[str, Tuple[str, float]] = {}  # code_s → (終値の日付, 終値)
    stale: Dict[str, Dict[str, str]] = {}    # 持ち越しが STALE_DAYS を超えた銘柄 → 期間
    backfilled: set = set()                   # 最初の終値より前の日を、その終値で埋めた銘柄

    def close_at(code_s: str, day: str) -> Optional[float]:
        c = (closes.get(code_s) or {}).get(day)
        if c is not None:
            last[code_s] = (day, c)
            return c
        if code_s not in last:
            # まだ終値の無い日 (期首持ち越しの初日など) は、後で最初に取れた終値を使う (その間の値動きは 0)
            later = sorted(d for d in (closes.get(code_s) or {}) if d > day)
            if not later:
                return None
            backfilled.add(code_s)
            return closes[code_s][later[0]]
        d, c = last[code_s]
        if (date.fromisoformat(day) - date.fromisoformat(d)).days > STALE_DAYS:
            span = stale.setdefault(code_s, {"from": d, "to": day})
            span["to"] = day
        return c

    rows = []
    prev_close: Dict[str, float] = {}
    prev_p = 0.0
    for day in days:
        todays = []
        while i < len(moves) and moves[i]["date"] <= day:
            todays.append(moves[i])
            i += 1
        codes = {k[0] for k, v in q.items() if abs(v) > 1e-9} | {m["key"][0] for m in todays}
        c_now = {}
        for code_s in codes:
            c = close_at(code_s, day)
            if c is None:
                raise ValueError(f"保有中に終値が一度も取れていない銘柄があります: {code_s} ({day})")
            c_now[code_s] = c
        pnl = 0.0
        for k, v in q.items():
            if abs(v) > 1e-9:
                pnl += v * (c_now[k[0]] - prev_close.get(k[0], c_now[k[0]]))
        opened = closed = 0.0
        for m in todays:
            k = m["key"]
            pnl += m["dq"] * (c_now[k[0]] - m["price"])
            before = q.get(k, 0.0)
            after = before + m["dq"]
            amount = abs(m["dq"]) * m["price"]
            if abs(after) > abs(before):
                opened += amount
            else:
                closed += amount
            q[k] = after
        p = sum(abs(v) * c_now[k[0]] for k, v in q.items() if abs(v) > 1e-9)
        denom = prev_p + max(0.0, opened - closed)
        rows.append({"date": day, "P": p, "pnl": pnl, "r": pnl / denom if denom > 0 else 0.0})
        prev_close.update(c_now)
        prev_p = p
    return {
        "days": rows,
        "carry_in": [{"code_s": k[0], "qty": v} for k, v in sorted(carry_in(moves).items())],
        "stale": [{"code_s": c, **span} for c, span in sorted(stale.items())],
        "backfilled": sorted(backfilled),
        "final_qty": {f"{k[0]}|{k[1]}": v for k, v in sorted(q.items()) if abs(v) > 1e-9},
    }


# ==================================================
# 入力の読み込み (DB・CSV・yfinance)
# ==================================================

def _broker_of(path: str) -> Optional[str]:
    """trade_history の CSV のファイル名から証券会社を決める。"""
    name = os.path.basename(path)
    if name.startswith("tradehistory"):
        return "楽天"
    if name.startswith("SaveFile"):
        return "SBI"
    if name[:8].isdigit() and name[8:9] == "-":
        return "マネックス"
    return None


def load_etf_fills(trade_history_dir: str = TRADE_HISTORY_DIR) -> List[Dict[str, Any]]:
    """保存済みの約定 CSV から ETF の約定だけを読む (取込では飛ばしているため)。

    CSV は期間が重なるので、同じ約定はファイルをまたいで1回だけ数える
    (1つのファイルの中で同じ約定が複数あれば、別々の約定として数える)。
    同じ約定かは約定の中身 (約定日・銘柄・売買・区分・株数・単価) で見る。楽天の CSV は同じ約定でも
    後日の出力で受渡金額が変わることがあり、行全体で見ると二重に数えるため (取込の dedup_key と同じ考え方)。
    """
    import import_monex_fills
    import import_rakuten_fills
    import import_sbi_fills
    import portfolio_shelve as ps

    mods = {"楽天": import_rakuten_fills, "SBI": import_sbi_fills, "マネックス": import_monex_fills}
    best: Dict[Tuple[Any, ...], int] = {}
    parsed: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(trade_history_dir, "*.csv"))):
        broker = _broker_of(path)
        if broker is None:
            continue
        mod = mods[broker]
        counts: Counter = Counter()
        for row in mod.read_csv_rows(path):
            try:
                f = mod.parse_fill_row(row, allow_etf=True)
            except mod.RowSkip:
                continue
            if not ps.is_etf_code(f["code_s"]):
                continue
            key = (broker, f["trade_date"], f["code_s"], f["side"], f["trade_kind"], f["qty"], f["price"])
            counts[key] += 1
            parsed[key] = {**f, "broker": broker}
        for key, n in counts.items():
            best[key] = max(best.get(key, 0), n)
    return [parsed[k] for k, n in best.items() for _ in range(n)]


def fetch_closes(codes: List[str], start: str, end: str) -> Dict[str, Any]:
    """配当調整なしの終値 (分割は調整済み) と分割を yfinance から取る。"""
    import yfinance as yf

    import price
    from webapp import helpers

    stock_map = helpers._bulk_get_stock_data(codes)
    first = (date.fromisoformat(start) - timedelta(days=STALE_DAYS)).isoformat()
    last = (date.fromisoformat(end) + timedelta(days=1)).isoformat()  # end は当日を含まない
    closes: Dict[str, Dict[str, float]] = {}
    splits: Dict[str, List[Tuple[str, float]]] = {}
    log_print(f"終値を yfinance から取得します: {len(codes)}銘柄 {first}〜{end}")
    for code_s in codes:
        try:
            ticker = yf.Ticker(price._get_ticker_symbol(code_s, stock_map.get(code_s) or {}))
            df = ticker.history(start=first, end=last, auto_adjust=False, actions=True)
        except Exception as e:  # noqa: BLE001 - 取れない銘柄は「終値なし」として後で止める
            log_warning(f"終値の取得に失敗: {code_s} {e!r}")
            continue
        if df.empty:
            continue
        closes[code_s] = {idx.date().isoformat(): float(v) for idx, v in df["Close"].items()
                          if v == v}
        if "Stock Splits" in df:
            splits[code_s] = [(idx.date().isoformat(), float(v))
                              for idx, v in df["Stock Splits"].items() if v and v > 0]
    return {"closes": closes, "splits": splits}


def _business_days(closes: Dict[str, Dict[str, float]], start: str, end: str) -> List[str]:
    """どれかの銘柄に終値がある日を営業日とする。"""
    return sorted({d for c in closes.values() for d in c if start <= d <= end})


def _checks(result: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """段階1の検証に使う数字 (月ごとの売買損益、最終日の株数と 1保 の食い違い) を出す。"""
    monthly: Dict[str, float] = {}
    for row in result["days"]:
        monthly[row["date"][:7]] = monthly.get(row["date"][:7], 0.0) + row["pnl"]

    restored: Dict[str, float] = {}
    for k, v in result["final_qty"].items():
        code_s, direction = k.split("|")
        if direction == LONG:
            restored[code_s] = restored.get(code_s, 0.0) + v
    held = {r["code_s"]: float(r.get("qty") or 0) for r in records}
    qty = [{"code_s": c, "restored": restored.get(c, 0.0), "held": held.get(c, 0.0)}
           for c in sorted(set(restored) | set(held))
           if abs(restored.get(c, 0.0) - held.get(c, 0.0)) > 1e-6]
    return {"monthly_pnl": monthly, "qty_mismatch": qty}


def build() -> Dict[str, Any]:
    """DB・CSV・yfinance から読んで復元し、検証の数字を添えて返す (DB は書かない)。"""
    import portfolio_shelve as ps

    fills = ps.list_fills() + load_etf_fills()
    end = ks_util.get_price_day(datetime.now()).isoformat()
    data = fetch_closes(sorted({f["code_s"] for f in fills}), START_DATE, end)
    # 約定の日に yfinance の終値が無ければ (上場廃止・終値の欠けなど)、約定の単価をその日の終値の代わりにする。
    # 約定の間の値動きは次の約定の日にまとめて出る。終値が無いまま遠い日の値で埋めると偽の損益が出る
    # (実例: 1948 は 2026-09-24 の値が1つだけあり、2025-01 の約定がそれで値洗いされた)
    moves = to_moves(fills, data["splits"])
    missing = sorted({m["key"][0] for m in moves if m["date"] not in data["closes"].get(m["key"][0], {})})
    for m in moves:  # 単価は分割を直した後の値 (終値と同じ基準)
        data["closes"].setdefault(m["key"][0], {}).setdefault(m["date"], m["price"])
    result = build_series(moves, data["closes"], _business_days(data["closes"], START_DATE, end))
    result["fill_price_codes"] = missing
    result["etf_fill_count"] = sum(1 for f in fills if ps.is_etf_code(f["code_s"]))
    result["checks"] = _checks(result, ps.list_records(status="1保"))
    return result


def _report(result: Dict[str, Any]) -> None:
    c = result["checks"]
    log_print(f"復元: {result['days'][0]['date']}〜{result['days'][-1]['date']} "
              f"{len(result['days'])}営業日、ETF の約定 {result['etf_fill_count']}件、"
              f"期首持ち越し {len(result['carry_in'])}件")
    log_print("検証1 月ごとの売買損益 (万円): " + " / ".join(
        f"{m} {v / 1e4:+,.0f}" for m, v in sorted(c["monthly_pnl"].items())))
    log_print(f"検証2 最終日の株数と1保の保有株数の食い違い: {len(c['qty_mismatch'])}銘柄")
    for q in c["qty_mismatch"]:
        log_print(f"  {q['code_s']} 復元 {q['restored']:g} 1保 {q['held']:g}")
    if result["fill_price_codes"]:
        log_print(f"約定の日の終値が無く約定の単価で代えた銘柄: {', '.join(result['fill_price_codes'])}")
    if result["backfilled"]:
        log_print(f"最初の終値より前を、その終値で埋めた銘柄: {', '.join(result['backfilled'])}")
    for s in result["stale"]:
        log_print(f"終値を{STALE_DAYS}日超持ち越した: {s['code_s']} {s['from']}〜{s['to']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="約定と終値から日々の運用総額と日次リターンを復元する (issue #594)")
    parser.add_argument("command", choices=["build"], help="build: 復元して保存し、検証を出す")
    parser.parse_args()
    result = build()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    _report(result)
    log_print(f"保存しました: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
