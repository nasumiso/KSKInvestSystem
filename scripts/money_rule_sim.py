#!/usr/bin/env python3
"""資金管理のルールを、復元した日次リターンに重ねて比べる (issue #594 の 2. 比較)。

結果と判断: doc/投資メモ分析/レビュー/資金管理の検証_2026-11.md

実際の保有に倍率を重ねたら、という評価で、銘柄の入れ替えは再現しない。元手1の比率だけを出す。

使い方 (scripts/ から):
    python money_rule_sim.py run      # 保有の日次リターンで全版を計算し、表を出して保存する
    python money_rule_sim.py nikkei   # 日経平均の長期で、暴落の合図のあとの値動きと最終案を確かめる (保存しない)

入力は equity_history.json (equity_history.py build で作る)。F&G・日経VI・信用評価損益率・
10年金利・日経平均は取り直す。保存先は $KS_DATA_DIR/analysis/money_rule_sim.json。
"""

import argparse
import json
import math
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import ks_util  # noqa: E402
from ks_util import log_print  # noqa: E402

EQUITY_PATH = os.path.join(ks_util.DATA_DIR, "analysis", "equity_history.json")
OUTPUT_PATH = os.path.join(ks_util.DATA_DIR, "analysis", "money_rule_sim.json")

LADDER = [0.8, 1.0, 1.1, 1.2, 1.4, 1.6, 2.0]  # 倍率の段 (倍率の順)
START_MULTIPLE = 1.2                          # 初日は相場温度 0 の段
MARGIN_CALL_RATE = 0.2                        # 追証の線 (保証金率 20%)
FULL_FNG_FROM = "2025-10-28"                  # F&G の5成分がそろう日。それより前は3成分
CREDIT_BOTTOM = -15.0                         # 信用評価損益率の底値圏
CREDIT_LAG_DAYS = 7                           # 信用の公表値は約1週遅れ
VOL_WINDOW, VOL_WARMUP, VOL_PCTL = 20, 60, 0.8  # B5: 下方ボラの窓・判定しない初めの日数・閾値
VI_K, BETA_DEFAULT, BETA_WARMUP = 3.0, 1.5, 60  # B6: k・β の初期値・β を出さない初めの日数
MA_DAYS = 200                                 # 200日線の版の移動平均の日数
BOOST_DAYS = 63                               # 最終案: 信用の底から買い向かう期間 (約3か月)

# 相場温度 → 張りたい量の倍率 (資金管理の検証_2026-11.md の版ごとの表)。−3・+3 を入れない版は −2・+2 として扱う
_TREND_DOWN = {-2: 1.0, -1: 1.1, 0: 1.2, 1: 1.2, 2: 1.2}
_TREND_BOTH = {-2: 1.0, -1: 1.1, 0: 1.2, 1: 1.4, 2: 1.6}
TABLES = {
    "flat": {t: 1.2 for t in range(-3, 4)},
    "flat14": {t: 1.4 for t in range(-3, 4)},
    "flat16": {t: 1.6 for t in range(-3, 4)},
    "A2": {**_TREND_DOWN, -3: 1.0, 3: 1.2},
    "A3": {**_TREND_BOTH, -3: 1.0, 3: 1.6},
    "A4": {**_TREND_BOTH, -3: 1.0, 3: 0.8},
    "A5": {**_TREND_BOTH, -3: 2.0, 3: 0.8},
    "A5p": {**_TREND_BOTH, -3: 1.2, 3: 0.8},
    # 旧方式 (推定維持率仕様の #480・#535): 全域の逆張り。目標維持率 50〜120% に当たる倍率
    # 文献の時系列モメンタムに近い遅い物差し: 日経平均が200日線の上なら 1.4倍、下なら 1.0倍 (試す前に決め打ち)
    "MA200": {1: 1.4, -1: 1.0},
    # 最終案 (2026-11-26 決定): ふだん 1.2倍、信用の底 (−15% 以下で改善) から3か月は 1.6倍
    "FINAL": {0: 1.2, 1: 1.6},
    "OLD": {-3: 2.0, -2: 1.6, -1: 1.4, 0: 1.2, 1: 1.1, 2: 1.0, 3: 0.8},
}
CREDIT_TOP = -3.0  # 旧方式: 信用評価損益率の天井圏

PERIODS = [
    ("全期間", None, None),
    ("2025-04 関税ショック", "2025-03-27", "2025-05-16"),
    ("5成分の期間", FULL_FNG_FROM, None),
    ("2026-10〜11 半導体の暴落", "2026-09-30", None),
]


def ladder_floor(x: float) -> float:
    """x 以下で一番高い段。x が一番下の段より小さければ一番下の段。"""
    below = [v for v in LADDER if v <= x + 1e-9]
    return below[-1] if below else LADDER[0]


def step_toward(current: float, target: float) -> float:
    """いまの段から目標の段へ、隣の段を1つだけ動く。"""
    i, j = LADDER.index(current), LADDER.index(ladder_floor(target))
    return LADDER[i + (j > i) - (j < i)]


def room_cap(drawdown: float, width: float) -> float:
    """張れる量: 2.0 × (1 − 口座の高値からの下落率 ÷ 幅)、下限 0.8倍。"""
    return max(0.8, 2.0 * (1 - drawdown / width))


def temperature(rating: Optional[str], credit_confirms: bool) -> int:
    """F&G の5区分 → 相場温度。Extreme Greed は確認なしで +3、Extreme Fear は確認があれば −3。"""
    base = {"Extreme Fear": -2, "Fear": -1, "Neutral": 0, "Greed": 1, "Extreme Greed": 2}.get(rating, 0)
    if base == 2:
        return 3
    if base == -2 and credit_confirms:
        return -3
    return base


def old_score(rating: Optional[str], credit: Optional[float], improving: bool, emotion: int) -> int:
    """旧方式のスコア: 感情 + 逆張りのずらし (F&G の Extreme と信用の天井圏・底値圏かつ改善)。

    ずらしは重ねない (同じ向きなら1段、逆向きなら打ち消す)。±3 で止める。市場ステートの上限は、
    感情を 0 とみなす版では攻める側 (マイナス) にしか効かず、ずらしが無い日は 0 のままなので入れない。
    """
    fng = {"Extreme Fear": -1, "Extreme Greed": 1}.get(rating, 0)
    cr = 1 if credit is not None and credit >= CREDIT_TOP else (
        -1 if credit is not None and credit <= CREDIT_BOTTOM and improving else 0)
    shift = fng if fng == cr or cr == 0 else (cr if fng == 0 else 0)
    return max(-3, min(3, emotion + shift))


def simulate(days: List[Dict[str, Any]], weekly: Dict[str, Dict[str, Any]],
             version: Dict[str, Any]) -> Dict[str, Any]:
    """1つの版を全期間で動かす。

    days: [{"date", "r"}] (r は建玉1円あたりの日次リターン)
    weekly: {判定日 (週の最終営業日): {"temp", "vol_high", "vi_cap"}}。翌営業日の r から当てる
    version: {"table", "cap", "width", "reset", "b3", "b4", "b5", "b6"}
    """
    table = TABLES[version["table"]]
    a, peak = 1.0, 1.0
    level = START_MULTIPLE
    p = level * a
    week_start_value, week_low = 1.0, 1.0
    rows, weeks = [], []
    halted = None
    for i, d in enumerate(days):
        a += p * d["r"]
        p *= 1 + d["r"]
        peak = max(peak, a)
        week_start_value *= 1 + d["r"]
        week_low = min(week_low, week_start_value)
        if 1 - week_low > 1 / level - MARGIN_CALL_RATE:  # 追証に届いた
            halted = d["date"]
            rows.append({"date": d["date"], "A": a, "L": level})
            break
        rows.append({"date": d["date"], "A": a, "L": level})
        nxt = days[i + 1]["date"] if i + 1 < len(days) else None
        if nxt and version["reset"] and (int(nxt[5:7]) - 1) // 3 != (int(d["date"][5:7]) - 1) // 3:
            peak = a  # 四半期末に高値をその時点の資産へ置き換える
        w = weekly.get(d["date"])
        if w is None or nxt is None:
            continue
        desire = table[w["temp"]]
        cap = room_cap(1 - a / peak, version["width"]) if version["cap"] else math.inf
        if version.get("b5") and w["vol_high"]:
            cap = min(cap, 1.0)
        if version.get("b6"):
            cap = min(cap, w["vi_cap"])
        before = level
        if version.get("direct"):
            level = ladder_floor(min(desire, cap))  # 旧方式: 毎週その目標へ直接合わせる
        elif version.get("b3") and level > cap:
            level = ladder_floor(cap)  # 上限を超えた分は一気に戻す
        elif version.get("b4") and w["temp"] == 3 and desire < level:
            level = ladder_floor(min(desire, cap))  # +3 に入った週は直接移る
        else:
            level = step_toward(level, min(desire, cap))
        weeks.append({"date": d["date"], "temp": w["temp"], "desire": desire, "cap": cap,
                      "before": before, "level": level})
        p = level * a
        week_start_value, week_low = 1.0, 1.0
    return {"rows": rows, "weeks": weeks, "halted": halted}


def metrics(result: Dict[str, Any], start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    """期間の最大ドローダウン・リターン・往復の回数・上限に当たった週数・相場温度の週数。"""
    rows = [r for r in result["rows"] if (start is None or r["date"] >= start)
            and (end is None or r["date"] <= end)]
    weeks = [w for w in result["weeks"] if (start is None or w["date"] >= start)
             and (end is None or w["date"] <= end)]
    if not rows:
        return {}
    i0 = result["rows"].index(rows[0])
    base = result["rows"][i0 - 1]["A"] if i0 > 0 else 1.0
    hi, mdd = base, 0.0
    for r in rows:
        hi = max(hi, r["A"])
        mdd = max(mdd, 1 - r["A"] / hi)
    ret = rows[-1]["A"] / base - 1
    years = (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days / 365.25
    round_trips = 0
    for k, w in enumerate(weeks):
        if w["level"] < w["before"] and any(x["level"] >= w["before"] for x in weeks[k + 1:k + 5]):
            round_trips += 1
    temps: Dict[int, int] = {}
    for w in weeks:
        temps[w["temp"]] = temps.get(w["temp"], 0) + 1
    return {
        "max_dd": mdd, "return": ret,
        "cagr": (1 + ret) ** (1 / years) - 1 if years > 0.5 and ret > -1 else None,
        "round_trips": round_trips,
        "cap_weeks": sum(1 for w in weeks if w["cap"] < w["desire"]),
        "temp_weeks": temps,
        "avg_level": sum(w["level"] for w in weeks) / len(weeks) if weeks else None,
    }


VERSIONS: List[Tuple[str, Dict[str, Any]]] = [
    ("A0 一定 1.2倍", {"table": "flat", "cap": False}),
    ("A1 口座の上限だけ", {"table": "flat"}),
    ("A2 +順張りの下げる側", {"table": "A2"}),
    ("A3 +順張りの両側", {"table": "A3"}),
    ("A4 +過熱で減らす", {"table": "A4"}),
    ("A5 +恐怖で2.0倍", {"table": "A5", "fear_only": True}),
    ("A5' +恐怖で標準に戻す", {"table": "A5p", "fear_only": True}),
    ("旧方式 (感情 0)", {"table": "OLD", "cap": False, "direct": True, "temp": "temp_old0"}),
    ("旧方式 (感情 = 市場に沿う)", {"table": "OLD", "cap": False, "direct": True, "temp": "temp_old_fng"}),
    ("上限 + 200日線 (上1.4・下1.0)", {"table": "MA200", "temp": "temp_ma200"}),
    ("最終案 (1.2倍・上限・信用の底で1.6倍)", {"table": "FINAL", "temp": "temp_final"}),
    ("上限 + 一定1.4倍", {"table": "flat14"}),
    ("上限 + 一定1.6倍", {"table": "flat16"}),
]
B_CHANGES: List[Tuple[str, Dict[str, Any]]] = [
    ("B1a 幅15%", {"width": 0.15}),
    ("B1b 幅25%", {"width": 0.25}),
    ("B2 四半期の置き換えなし", {"reset": False}),
    ("B3 上限超えは一気に戻す", {"b3": True}),
    ("B4 +3 で直接移る", {"b4": True}),
    ("B5 下方ボラで上限", {"b5": True}),
    ("B6 日経VIで上限", {"b6": True}),
]
_DEFAULTS = {"cap": True, "width": 0.20, "reset": True}


# ==================================================
# 入力 (F&G・信用・VI・β) を判定日ごとに作る
# ==================================================

def _weekly_dates(days: List[str]) -> List[str]:
    """週の最終営業日 (日曜の判定に使う金曜の引け)。"""
    out = []
    for d, nxt in zip(days, days[1:] + [None]):
        if nxt is None or date.fromisoformat(nxt).isocalendar()[:2] != date.fromisoformat(d).isocalendar()[:2]:
            out.append(d)
    return out


def _fng_on(day: str, breadth: List[Dict[str, Any]], vi: List[Dict[str, Any]],
            jgb: List[Dict[str, Any]], full: bool) -> Optional[Dict[str, Any]]:
    """day までのデータだけで F&G を出す。full=False なら騰落・新高値を外した3成分。"""
    import fear_greed_jp as fg

    rows = [r for r in breadth if r["date"] <= day]
    if not full:
        rows = [{"date": r["date"], "nikkei_close": r["nikkei_close"]} for r in rows]
    return fg.compute_fear_greed_jp(rows, [v for v in vi if v["date"] <= day],
                                    [j for j in jgb if j["date"] <= day])


def _credit_confirms(day: str, credit: List[Dict[str, Any]]) -> Tuple[bool, Optional[float]]:
    """day の時点で公表済みの信用評価損益率が、底値圏かつ前週より改善しているか。"""
    limit = (date.fromisoformat(day) - timedelta(days=CREDIT_LAG_DAYS)).isoformat()
    known = [c for c in credit if c["date"] <= limit]
    if len(known) < 2:
        return False, None, False
    now, prev = known[-1]["credit_eval_rate"], known[-2]["credit_eval_rate"]
    return now <= CREDIT_BOTTOM and now > prev, now, now > prev


def build_weekly(days: List[Dict[str, Any]], breadth, vi, jgb, credit,
                 nikkei: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
    """判定日ごとに、F&G・相場温度 (信用の確認あり / Extreme Fear だけ)・B5・B6 の材料を作る。"""
    dates = [d["date"] for d in days]
    vi_by = {v["date"]: v["nikkei_vi"] for v in vi}
    r_by = {d["date"]: d["r"] for d in days}
    out: Dict[str, Dict[str, Any]] = {}
    downs: List[float] = []  # B5: 過去の 20日下方ボラ (保有の r、倍率をかける前)
    last_bottom = -10 ** 9   # 最終案: 信用の底の合図が出た営業日の位置
    for d in _weekly_dates(dates):
        idx = dates.index(d)
        fng = _fng_on(d, breadth, vi, jgb, full=d >= FULL_FNG_FROM)
        rating = fng["rating"] if fng else None
        confirms, credit_now, improving = _credit_confirms(d, credit)
        if confirms:
            last_bottom = idx
        emotion_fng = {"Extreme Fear": -2, "Fear": -1, "Greed": 1, "Extreme Greed": 2}.get(rating, 0)
        # B5: 20日の下方ボラ (年率) と、それまでの全期間の 80パーセンタイル
        window = [r_by[x] for x in dates[max(0, idx - VOL_WINDOW + 1):idx + 1]]
        down = math.sqrt(sum(min(r, 0) ** 2 for r in window) / len(window) * 250)
        vol_high = idx >= VOL_WARMUP and down > sorted(downs)[int(len(downs) * VOL_PCTL)] if downs else False
        downs.append(down)
        # B6: β は d までの保有と日経平均の日次リターンの回帰
        pairs = []
        for x, y in zip(dates[:idx], dates[1:idx + 1]):
            if x in nikkei and y in nikkei:
                pairs.append((nikkei[y] / nikkei[x] - 1, r_by[y]))
        beta = BETA_DEFAULT
        if len(pairs) >= BETA_WARMUP:
            mx = sum(p[0] for p in pairs) / len(pairs)
            my = sum(p[1] for p in pairs) / len(pairs)
            var = sum((p[0] - mx) ** 2 for p in pairs)
            beta = sum((p[0] - mx) * (p[1] - my) for p in pairs) / var if var else BETA_DEFAULT
        closes = [nikkei[x] for x in sorted(nikkei) if x <= d][-MA_DAYS:]
        above = len(closes) == MA_DAYS and closes[-1] > sum(closes) / MA_DAYS
        vi_now = vi_by.get(d) or next((vi_by[x] for x in reversed(dates[:idx]) if x in vi_by), None)
        sigma_w = (vi_now or 0) / 100 / math.sqrt(52)
        out[d] = {
            "fng": fng["score"] if fng else None, "rating": rating,
            "temp_strict": temperature(rating, confirms),
            "temp_fear_only": temperature(rating, True),
            "temp_ma200": 1 if above else -1,
            "temp_final": 1 if idx - last_bottom <= BOOST_DAYS else 0,
            "temp_old0": old_score(rating, credit_now, improving, 0),
            "temp_old_fng": old_score(rating, credit_now, improving, emotion_fng),
            "credit": credit_now, "vol_high": vol_high, "downside_vol": down,
            "beta": beta, "vi": vi_now, "vi_cap": 1 / (MARGIN_CALL_RATE + VI_K * beta * sigma_w),
        }
    return out


def _fng_agreement(weekly_dates: List[str], breadth, vi, jgb) -> Dict[str, Any]:
    """5成分がそろう期間で、3成分と5成分の5区分の一致率。"""
    same = total = 0
    for d in weekly_dates:
        if d < FULL_FNG_FROM:
            continue
        full, part = _fng_on(d, breadth, vi, jgb, True), _fng_on(d, breadth, vi, jgb, False)
        if full and part:
            total += 1
            same += full["rating"] == part["rating"]
    return {"weeks": total, "same": same, "rate": same / total if total else None}


def _load_inputs() -> Dict[str, Any]:
    import yfinance as yf

    import market_breadth as mb

    with open(EQUITY_PATH, encoding="utf-8") as f:
        days = [{"date": d["date"], "r": d["r"]} for d in json.load(f)["days"]]
    df = yf.Ticker("^N225").history(start="2022-06-01", auto_adjust=False)
    nikkei = {i.date().isoformat(): float(v) for i, v in df["Close"].items()}
    # 日経平均は daily2year.json の終値を優先し、それより前を yfinance で補う (3成分の正規化に約2年要る)
    breadth = mb.fetch_market_breadth_daily()
    first = breadth[0]["date"]
    early = [{"date": d, "nikkei_close": c} for d, c in sorted(nikkei.items()) if d < first]
    return {"days": days, "breadth": early + breadth, "vi": mb.fetch_nikkei_vi(),
            "jgb": mb.fetch_jgb10(), "credit": mb.fetch_credit_balance_weekly(), "nikkei": nikkei}


def run() -> Dict[str, Any]:
    inp = _load_inputs()
    days = inp["days"]
    weekly = build_weekly(days, inp["breadth"], inp["vi"], inp["jgb"], inp["credit"], inp["nikkei"])
    results = {}

    def one(name: str, v: Dict[str, Any]) -> None:
        key = v.get("temp") or ("temp_fear_only" if v.get("fear_only") else "temp_strict")
        w = {d: {**x, "temp": x[key]} for d, x in weekly.items()}
        res = simulate(days, w, {**_DEFAULTS, **v})
        results[name] = {"halted": res["halted"],
                         "periods": {p: metrics(res, s, e) for p, s, e in PERIODS},
                         "plus3": _plus3(res, days)}

    for name, v in VERSIONS:
        one(name, v)
    # B は A で残った形 (A1: 口座の上限だけ) と、投資戦略.md の案 (A5) の両方に当てる
    for prefix, base in (("A1+", VERSIONS[1][1]), ("A5+", VERSIONS[5][1])):
        for name, change in B_CHANGES:
            one(prefix + name, {**base, **change})
    return {
        "fng_agreement": _fng_agreement(list(weekly), inp["breadth"], inp["vi"], inp["jgb"]),
        "strict_minus3_weeks": sum(1 for x in weekly.values() if x["temp_strict"] == -3),
        "fear_only_minus3_weeks": sum(1 for x in weekly.values() if x["temp_fear_only"] == -3),
        "weekly": weekly, "results": results,
    }


def _plus3(res: Dict[str, Any], days: List[Dict[str, Any]]) -> Dict[str, Any]:
    """+3 の出入り: +2 と +3 を行き来した回数と、+3 に入ってから4週・8週の保有のリターン。"""
    weeks = res["weeks"]
    flips = sum(1 for a, b in zip(weeks, weeks[1:]) if {a["temp"], b["temp"]} == {2, 3})
    dates = [d["date"] for d in days]
    after = []
    for a, b in zip(weeks, weeks[1:]):
        if b["temp"] == 3 and a["temp"] != 3:
            i = dates.index(b["date"])
            out = []
            for n in (20, 40):
                seg = days[i + 1:i + 1 + n]
                out.append(math.prod(1 + d["r"] for d in seg) - 1 if len(seg) == n else None)
            after.append({"date": b["date"], "r4w": out[0], "r8w": out[1]})
    return {"flips": flips, "entries": after}


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def report(out: Dict[str, Any]) -> None:
    ag = out["fng_agreement"]
    log_print(f"F&G 3成分と5成分の一致: {ag['same']}/{ag['weeks']}週 ({_pct(ag['rate'])})")
    log_print(f"−3 の週: 信用の確認あり {out['strict_minus3_weeks']}週 / Extreme Fear だけ "
              f"{out['fear_only_minus3_weeks']}週")
    for p, _, _ in PERIODS:
        log_print(f"## {p}")
        log_print("| 版 | 最大DD | リターン | 年率 | 平均倍率 | 往復 | 上限の週 |")
        log_print("|---|---|---|---|---|---|---|")
        for name, r in out["results"].items():
            m = r["periods"][p]
            if not m:
                continue
            log_print(f"| {name}{' (運用不能 ' + r['halted'] + ')' if r['halted'] else ''} | "
                      f"{m['max_dd'] * 100:.1f}% | {_pct(m['return'])} | {_pct(m['cagr'])} | "
                      f"{m['avg_level']:.2f} | {m['round_trips']} | {m['cap_weeks']} |")
    p3 = out["results"]["A4 +過熱で減らす"]["plus3"]
    log_print(f"+3 の出入り (A4): +2⇔+3 の行き来 {p3['flips']}回、入った後の保有のリターン "
              + ", ".join(f"{e['date']} 4週{_pct(e['r4w'])} 8週{_pct(e['r8w'])}" for e in p3["entries"]))


def _episodes(flags: List[bool], gap: int = 60) -> List[int]:
    """合図が出た日のうち、前の合図から gap 営業日より離れた初日 (同じ局面を重ねて数えない)。"""
    out, last = [], -10 ** 9
    for i, f in enumerate(flags):
        if f and i - last > gap:
            out.append(i)
        if f:
            last = i
    return out


def nikkei_study() -> None:
    """日経平均の長期で、(1) 暴落の合図のあとの値動き (2) 最終案と一定倍率を比べる。

    保有の日次リターンは約2年しか無く、信用の底の合図がほとんど出ないため、市場全体で確かめる。
    保有 (成長株中心) とは値動きが違うので、市場全体の傾向として読む。
    """
    import yfinance as yf

    import market_breadth as mb

    df = yf.Ticker("^N225").history(start="1999-01-01", auto_adjust=False)["Close"]
    nk = [(i.date().isoformat(), float(v)) for i, v in df.items() if v == v]
    vi = {r["date"]: r["nikkei_vi"] for r in mb.fetch_nikkei_vi()}
    credit = mb.fetch_credit_balance_weekly()
    days = [d for d, _ in nk if d >= "2001-01-04"]
    px = [v for d, v in nk if d >= "2001-01-04"]
    hi = [max(px[max(0, i - 250):i + 1]) for i in range(len(px))]
    bottom = [_credit_confirms(d, credit)[0] for d in days]
    signals = {
        "日経VI が40超": [vi.get(d, 0) > 40 for d in days],
        "52週高値から −20%": [px[i] / hi[i] - 1 <= -0.20 for i in range(len(px))],
        "信用 −15%以下で改善": bottom,
    }
    log_print("## 暴落の合図のあとの日経平均 (2001〜。信用は 2009〜)")
    log_print("| 合図 | 回数 | 3か月後 平均 (プラスの割合) | 6か月後 平均 (プラスの割合) | 3か月の間のさらなる下落 平均 / 最悪 |")
    log_print("|---|---|---|---|---|")
    for name, flags in [("ふだん (全日)", [True] * len(px))] + list(signals.items()):
        ep = list(range(len(px))) if name.startswith("ふだん") else _episodes(flags)
        cells = []
        for n in (63, 126):
            v = [px[i + n] / px[i] - 1 for i in ep if i + n < len(px)]
            cells.append(f"{sum(v) / len(v) * 100:+.1f}% ({sum(x > 0 for x in v) / len(v) * 100:.0f}%)")
        dd = [min(px[i:i + 64]) / px[i] - 1 for i in ep if i + 63 < len(px)]
        tail = "—" if name.startswith("ふだん") else f"{sum(dd) / len(dd) * 100:.1f}% / {min(dd) * 100:.1f}%"
        log_print(f"| {name} | {'—' if name.startswith('ふだん') else len(ep)} | {cells[0]} | {cells[1]} | {tail} |")

    # 最終案と一定倍率 (信用のデータがある 2009-06 以降)
    sim_days = [{"date": days[i], "r": px[i] / px[i - 1] - 1} for i in range(1, len(px)) if days[i] >= "2009-06-12"]
    dates = [d["date"] for d in sim_days]
    weekly, last = {}, -10 ** 9
    for d in _weekly_dates(dates):
        i = dates.index(d)
        if _credit_confirms(d, credit)[0]:
            last = i
        weekly[d] = {"temp": 1 if i - last <= BOOST_DAYS else 0, "vol_high": False, "vi_cap": math.inf}
    flat = {d: {**x, "temp": 0} for d, x in weekly.items()}
    log_print("## 日経平均 2009-06〜 で最終案を当てる (週1回見直し)")
    log_print("| 運用 | 年率 | 最大DD | 平均倍率 | 上限に当たった週 | 1.6倍の週 |")
    log_print("|---|---|---|---|---|---|")
    for name, w, v in (("一定 1.2倍", flat, {"table": "flat", "cap": False}),
                       ("口座の上限だけ", flat, {"table": "flat"}),
                       ("口座の上限だけ (置き換えなし)", flat, {"table": "flat", "reset": False}),
                       ("最終案", weekly, {"table": "FINAL"}),
                       ("最終案 (置き換えなし)", weekly, {"table": "FINAL", "reset": False}),
                       ("信用の底で1.6倍 (上限なし)", weekly, {"table": "FINAL", "cap": False})):
        res = simulate(sim_days, w, {**_DEFAULTS, **v})
        mt = metrics(res, None, None)
        log_print(f"| {name}{' (運用不能)' if res['halted'] else ''} | {_pct(mt['cagr'])} | {mt['max_dd'] * 100:.1f}% | "
                  f"{mt['avg_level']:.2f} | {mt['cap_weeks']}/{len(res['weeks'])} | "
                  f"{sum(1 for x in res['weeks'] if x['level'] >= 1.6)} |")


def main() -> int:
    parser = argparse.ArgumentParser(description="資金管理のルールを復元した日次リターンで比べる (issue #594)")
    parser.add_argument("command", choices=["run", "nikkei"],
                        help="run: 保有で全版を計算して保存 / nikkei: 日経平均の長期で確かめる (保存しない)")
    args = parser.parse_args()
    if args.command == "nikkei":
        nikkei_study()
        return 0
    out = run()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    report(out)
    log_print(f"保存しました: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
