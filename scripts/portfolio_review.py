#!/usr/bin/env python3
"""保有ポートフォリオの点検 (/portfolio-review) に渡す数字を作る (issue #589)。

月次の /trade-review が終わった売買を見るのに対し、こちらはいま持っている建玉を見る。
数字は Python が出し、LLM は照合と問いかけをする。build_review_dataset は引数だけで
計算し、DB を読むのは CLI 側 (_load_inputs) に限る。

使い方 (scripts/ から):
    python portfolio_review.py json                               # 標準出力へ。保存しない
    python portfolio_review.py save --decisions d.json < report.md  # JSON を作り直し、決定・md と保存

保存先は $KS_DATA_DIR/portfolio_review/YYYY-MM-DD.{json,md}。不可逆データとして扱い、
既にあれば書かない (やり直すときは人がファイルを消す)。
"""

import argparse
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import ks_util  # noqa: E402
import portfolio_shelve as ps  # noqa: E402
import trade_review  # noqa: E402
from ks_util import log_print, log_warning  # noqa: E402

REVIEW_DIR = os.path.join(ks_util.DATA_DIR, "portfolio_review")

# 決算前の行動を先に書く対象にする、決算までの日数
UPCOMING_DAYS = 14
# 保有数の目安 (投資戦略.md「ポートフォリオ構造」の実保有10 + 打診5)
HOLDING_TARGET = 15
# 前回の点検がないときに、新規建て・買い増しをさかのぼる日数
ADDITIONS_FALLBACK_DAYS = 30
# 点検のきっかけ: 推定維持率が前回の点検時からこれだけ下がったら (ポイント)
MARGIN_DROP_POINTS = 10
# 基準のログ (前回の日付以前の最新の行) をさかのぼる日数
BASELINE_LOOKBACK_DAYS = 7
# 市場ステートの良し悪し (大きいほど良い)
STATE_RANK = {"confirmed_uptrend": 2, "uptrend_under_pressure": 1, "market_in_correction": 0}
# 段 (感情スコア) ごとの1銘柄の最大ウェイト (自己資本比、%)。投資戦略.md の
# 「レバレッジ別の1銘柄ウェイトと逆指値幅」の表から、幅のある値は上側を取る。
# 表に無い段 (+1・+3) は隣の段に寄せる
WEIGHT_CAP_BY_STEP = {3: 20, 2: 20, 1: 15, 0: 15, -1: 12, -2: 10, -3: 8}

ACTIONS = ("cut", "reduce", "hold")
EXIT_TYPES = ("price", "margin_order", "earnings", "deadline")


def _days(d1: str, d2: str) -> int:
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def _widths(trade_ideas: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    out = {}
    for t in trade_ideas:
        w = (t.get("exit_rule") or {}).get("stop_loss_pct")
        out[t.get("name", "")] = float(w) if isinstance(w, (int, float)) and w > 0 else None
    return out


def open_lots(episodes: List[Dict[str, Any]], trade_ideas: List[Dict[str, Any]],
              fill_strategies: Dict[Any, Dict[str, Any]], today: str) -> List[Dict[str, Any]]:
    """保有中の建玉をロット (往復行のうち未決済の行) 単位で返す。

    エピソード単位の判定 (trade_review._stop_loss_widths) は、ロットに別の戦略が付くと
    判定しない。買い増しを短期の戦略で建てたロットの幅超え・時間軸超えは、ロット単位で
    見ないと漏れる。幅はそのロットの戦略の損切り幅、損益率は建単価からの今の値。
    """
    from webapp import trade_episodes  # 遅延 import (trade_review と同じ理由)

    widths = _widths(trade_ideas)
    horizons = {t.get("name", ""): t.get("time_horizon") or "" for t in trade_ideas}
    lots = []
    for ep in episodes:
        if ep["closed"] or ep.get("split_suspect"):
            continue
        work = dict(ep, round_trips=trade_episodes.build_round_trips(ep))
        trade_episodes.attach_lot_strategies(work, fill_strategies)
        for r in work["round_trips"]:
            if r["closed"] or not r["open_seqs"] or not r.get("open_date"):
                continue
            idea = r.get("trade_idea") or ""
            width = None if ep.get("strategy_drift") else widths.get(idea)
            hold_days = _days(r["open_date"], today)
            limit = ps.TIME_HORIZON_HOLD_DAYS.get(horizons.get(idea, ""), (None, None))[1]
            pct = r.get("return_pct")
            lots.append({
                "lot_key": f"{ep['episode_key']}#{'-'.join(str(s) for s in r['open_seqs'])}",
                "episode_key": ep["episode_key"],
                "code_s": ep["code_s"],
                "stock_name": ep.get("stock_name", ""),
                "kind": ep["kind"],
                "is_short": bool(ep.get("is_short")),
                "trade_idea": idea,
                "episode_trade_idea": ep.get("trade_idea") or "",
                "strategy_drift": bool(ep.get("strategy_drift")),
                "open_date": r["open_date"],
                "qty": r["qty"],
                "open_price": r["open_price"],
                "return_pct": pct,
                "stop_loss_pct": width,
                "line_price": (r["open_price"] * (1 - width / 100.0)
                               if width is not None and r["open_price"] and not ep.get("is_short") else None),
                "hold_days": hold_days,
                "time_horizon": horizons.get(idea, ""),
                "over_width": width is not None and pct is not None and pct < -width,
                "over_horizon": limit is not None and hold_days > limit,
            })
    return sorted(lots, key=lambda x: (x["code_s"], x["open_date"], x["lot_key"]))


def _latest_on_or_before(history: List[Dict[str, Any]], day: str) -> Optional[Dict[str, Any]]:
    """day 以前で最も新しい平日のログ。BASELINE_LOOKBACK_DAYS より前しか無ければ None。"""
    floor = (date.fromisoformat(day) - timedelta(days=BASELINE_LOOKBACK_DAYS)).isoformat()
    rows = [h for h in history if h.get("date") and floor <= h["date"] <= day
            and date.fromisoformat(h["date"]).weekday() < 5]
    return max(rows, key=lambda h: h["date"]) if rows else None


def triggers_since(last: Optional[Dict[str, Any]], lit_now: List[str], lot_over_now: List[str],
                   exposure_history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """前回の点検より後に起きた、点検のきっかけ (今日のアクションと dataset で共用)。

    last: 前回の保存 {"date", "dataset"} か None。dataset が None (11/15 の初回分) なら、
      今の「防」と幅超えのロットを全部「新しい」と数える。
    市場ステートと維持率は、前回の日付以前の最新の平日のログを基準にする。基準が無い、
    または比べる値が None なら、その理由は判定しない。
    """
    if last is None:
        return {"first": True, "last_date": None, "new_lit": sorted(lit_now),
                "new_lot_over": sorted(lot_over_now), "state_worse": None, "margin_down": None}
    defense = (last.get("dataset") or {}).get("defense") or {}
    prev_lit = {x["code_s"] for x in defense.get("lit") or []}
    prev_over = set(defense.get("lot_over_keys") or [])
    base = _latest_on_or_before(exposure_history, last["date"])
    after = sorted((h for h in exposure_history if base and h.get("date", "") > base["date"]),
                   key=lambda h: h["date"])
    state_worse = None
    if base and base.get("state") in STATE_RANK:
        worse = [h for h in after if STATE_RANK.get(h.get("state"), 9) < STATE_RANK[base["state"]]]
        if worse:
            state_worse = {"from": base["state"], "to": worse[-1]["state"], "date": worse[0]["date"]}
    margin_down = None
    latest = after[-1] if after else None
    if base and latest:
        turned_over = latest.get("position") == "over" and base.get("position") not in (None, "over")
        b, n = base.get("margin_rate_pct"), latest.get("margin_rate_pct")
        dropped = b is not None and n is not None and n <= b - MARGIN_DROP_POINTS
        if turned_over or dropped:
            margin_down = {"from": b, "to": n, "date": latest["date"],
                           "position": latest.get("position"), "turned_over": turned_over}
    return {
        "first": False, "last_date": last["date"],
        "new_lit": sorted(set(lit_now) - prev_lit),
        "new_lot_over": sorted(set(lot_over_now) - prev_over),
        "state_worse": state_worse, "margin_down": margin_down,
    }


def has_trigger(t: Dict[str, Any]) -> bool:
    return bool(t["first"] or t["new_lit"] or t["new_lot_over"] or t["state_worse"] or t["margin_down"])


_STATE_LABEL = {"confirmed_uptrend": "上昇", "uptrend_under_pressure": "上昇・圧力あり",
                "market_in_correction": "調整"}


def trigger_text(t: Dict[str, Any]) -> str:
    """きっかけを今日のアクションの1行の文言にする。"""
    parts = []
    if t["new_lit"]:
        parts.append(f"防 {len(t['new_lit'])}銘柄")
    if t["new_lot_over"]:
        parts.append(f"幅超えのロット {len(t['new_lot_over'])}")
    if t["state_worse"]:
        s = t["state_worse"]
        parts.append(f"市場ステート悪化 ({_STATE_LABEL.get(s['from'], s['from'])}→{_STATE_LABEL.get(s['to'], s['to'])})")
    if t["margin_down"]:
        m = t["margin_down"]
        rate = (f" {m['from']:.0f}%→{m['to']:.0f}%" if m["from"] is not None and m["to"] is not None else "")
        parts.append("維持率が下がった" + rate + (" (適正エリアより多い)" if m["turned_over"] else ""))
    if t["first"]:
        return "初回の点検がまだ" + (f" ({'・'.join(parts)})" if parts else "")
    d = date.fromisoformat(t["last_date"])
    return f"前回 ({d.month}/{d.day}) 以降に " + "・".join(parts)


def check_previous(previous: Optional[Dict[str, Any]], today: str, held_qty: Dict[str, float],
                   sold_qty: Dict[str, float], closes: Dict[str, Dict[str, float]],
                   exposure_history: List[Dict[str, Any]],
                   kessan_dates: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    """前回の約束を、その後の約定・終値・日次ログで照合する。

    sold_qty: 前回の日付より後の売り (返済) の株数 {code_s: 株数}
    closes: 前回の日付より後の終値 {code_s: {"YYYY-MM-DD": 終値}}
    実行すべきだった約束 (cut・reduce・線を割った hold・発動した sell_order) のうち、
    実行された割合を rate に出す (月次の §0 が引く)。
    """
    if not previous:
        return None
    since = previous["date"]
    decisions = previous.get("decisions") or {}
    rows, due, done = [], 0, 0
    for d in decisions.get("holdings") or []:
        code, action = d["code_s"], d["action"]
        sold, now = sold_qty.get(code, 0), held_qty.get(code, 0)
        row = {"code_s": code, "stock_name": d.get("stock_name", ""), "action": action,
               "qty": d.get("qty"), "sold_qty": sold, "held_qty": now}
        if action in ("cut", "reduce"):
            want = d.get("qty")
            if want:
                status = "done" if sold >= want else "partial" if sold > 0 else "not_done"
            elif action == "cut":
                status = "done" if now == 0 else "partial" if sold > 0 else "not_done"
            else:
                status = "done" if sold > 0 else "not_done"
            row["status"] = status
            due += 1
            done += status == "done"
        else:
            ex = d.get("exit") or {}
            row["exit"] = ex
            series = {k: v for k, v in (closes.get(code) or {}).items() if k > since}
            if ex.get("type") == "price" and ex.get("price") and series:
                low_day = min(series, key=lambda k: series[k])
                broke = series[low_day] < ex["price"]
                row.update(low_close=series[low_day], low_date=low_day, broke_line=broke)
                if broke and not ex.get("proposed"):  # 案のまま未確定の線は約束に数えない
                    due += 1
                    done += sold > 0
                if broke:
                    row["status"] = "sold_after_break" if sold > 0 else "held_after_break"
                else:
                    row["status"] = "within_line"
            elif ex.get("type") == "earnings":
                passed = [k for k in kessan_dates.get(code, []) if since < k <= today]
                row["status"] = "earnings_passed" if passed else "earnings_pending"
                row["earnings_date"] = passed[0] if passed else None
                row["after_earnings"] = d.get("after_earnings")
            elif ex.get("type") == "deadline" and ex.get("date"):
                row["status"] = "deadline_passed" if ex["date"] <= today else "deadline_pending"
            else:
                row["status"] = "no_check"
        rows.append(row)

    order = decisions.get("sell_order") or {}
    sell_order = None
    if order.get("trigger_margin_rate") is not None:
        hit = [h for h in exposure_history if h.get("date", "") > since
               and h.get("margin_rate_pct") is not None
               and h["margin_rate_pct"] < order["trigger_margin_rate"]]
        codes = [{"code_s": c["code_s"], "qty": c.get("qty"), "sold_qty": sold_qty.get(c["code_s"], 0)}
                 for c in order.get("codes") or []]
        sell_order = {"trigger_margin_rate": order["trigger_margin_rate"],
                      "triggered_date": min(h["date"] for h in hit) if hit else None,
                      "min_margin_rate": min((h["margin_rate_pct"] for h in hit), default=None),
                      "codes": codes}
        if hit:
            due += 1
            done += bool(codes) and codes[0]["sold_qty"] > 0  # 順番の先頭を売ったか
    return {"date": since, "holdings": rows, "sell_order": sell_order,
            "due": due, "done": done, "rate": done / due if due else None}


def build_review_dataset(*, today: str, generated_at: str,
                         episodes: List[Dict[str, Any]],
                         records: List[Dict[str, Any]],
                         stocks: Dict[str, Dict[str, Any]],
                         trade_ideas: List[Dict[str, Any]],
                         fill_strategies: Dict[Any, Dict[str, Any]],
                         exposure_history: List[Dict[str, Any]],
                         margin: Optional[Dict[str, Any]],
                         exits: Dict[str, Dict[str, Any]],
                         ratings: Dict[str, Optional[Dict[str, Any]]],
                         kessan_dates: Dict[str, List[str]],
                         strategy_histories: Dict[str, List[Dict[str, Any]]],
                         previous: Optional[Dict[str, Any]] = None,
                         closes: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
    """点検に渡す数字を作る (DB を読まない)。

    Args:
        today: "YYYY-MM-DD"
        episodes: trade_episodes.build_fill_episodes() の戻り値
        records: portfolio_shelve.list_records("1保")
        stocks: {code_s: {"price", "stock_name", "kessan_date" ("YYYY-MM-DD" か None)}}
        margin: exposure_guide.build_margin_state() の combined (推定できなければ None)
        exits: action_queue._recorded_exits() の戻り値 {code_s: {"signal", "first_defense"}}
        ratings: 保有銘柄の評価台帳 {code_s: レコードか None}
        kessan_dates: 決算発表日 {code_s: ["YYYY-MM-DD", ...]} (過去分)
        strategy_histories: 保有銘柄の戦略の変更履歴 {code_s: [...]}
        previous: 前回の保存 {"date", "dataset", "decisions"} か None
        closes: 前回の日付より後の終値 {code_s: {"YYYY-MM-DD": 終値}} (価格の線の照合用)
    """
    stop_widths = trade_review._stop_loss_widths(episodes, trade_ideas, fill_strategies)
    pf = trade_review._portfolio(episodes, exposure_history, records, stocks, stop_widths)
    holdings = {h["code_s"]: h for h in pf["holdings"]}
    memo = {r["code_s"]: r.get("memo") or {} for r in records}
    since = previous["date"] if previous else (
        date.fromisoformat(today) - timedelta(days=ADDITIONS_FALLBACK_DAYS)).isoformat()
    widths = _widths(trade_ideas)

    lots = [x for x in open_lots(episodes, trade_ideas, fill_strategies, today)
            if x["code_s"] in holdings]
    lot_over = [x for x in lots if x["over_width"]]
    lit = [{"code_s": c, "stock_name": holdings[c]["stock_name"],
            "first_defense": e.get("first_defense"),
            "reasons": (e.get("signal") or {}).get("reasons") or []}
           for c, e in sorted(exits.items()) if e.get("signal") and c in holdings]

    # 前回以降の戦略の変更で、損切り幅が広がった (または幅が無くなった) もの
    widened = []
    for code, hist in strategy_histories.items():
        for h in hist:
            if (h.get("effective_date") or "") <= since or h.get("source") == "baseline":
                continue
            old, new = widths.get(h.get("old") or ""), widths.get(h.get("new") or "")
            if old is not None and (new is None or new > old):
                widened.append({"code_s": code, "scope": h.get("scope"), "date": h["effective_date"],
                                "old": h.get("old"), "new": h.get("new"),
                                "old_width": old, "new_width": new})

    # 前回以降の新規建て・買い増し (保有中のロットのみ)
    additions = []
    for x in lots:
        if x["open_date"] <= since:
            continue
        earlier = [y for y in lots if y["episode_key"] == x["episode_key"] and y["open_date"] < x["open_date"]]
        additions.append({**x, "is_addition": bool(earlier),
                          "below_earlier": bool(earlier) and x["open_price"] is not None
                          and all(y["open_price"] and x["open_price"] < y["open_price"] for y in earlier)})

    # 推定維持率と1銘柄ウェイト。上限は自己資本基準なので、受入保証金で割った値も出す
    latest = max(exposure_history, key=lambda h: h.get("date") or "", default={})
    step = latest.get("step")
    cap = WEIGHT_CAP_BY_STEP.get(step) if step is not None else None
    equity = (margin or {}).get("guarantee")
    weights = []
    for h in pf["holdings"]:
        eq_w = h["value"] / equity * 100.0 if equity else None
        weights.append({"code_s": h["code_s"], "stock_name": h["stock_name"],
                        "weight_pct": h["weight_pct"], "equity_weight_pct": eq_w,
                        "over_cap": cap is not None and eq_w is not None and eq_w > cap})
    themes: Dict[str, float] = {}
    for h in pf["holdings"]:
        for t in memo.get(h["code_s"], {}).get("gyoutai_themes") or []:
            themes[t] = themes.get(t, 0.0) + (h["weight_pct"] or 0.0)
    no_width_names = {n for n, w in widths.items() if w is None}

    # 台帳: 無い・Watch・Archive の保有と、直近の決算より古い更新
    ledger_off, stale = [], []
    for code in holdings:
        r = ratings.get(code)
        if not r or r.get("status") != "Active":
            ledger_off.append({"code_s": code, "stock_name": holdings[code]["stock_name"],
                               "status": (r or {}).get("status")})
        if r:
            past = [k for k in kessan_dates.get(code, []) if k <= today]
            if past and (r.get("updated_at") or "") < past[-1]:
                stale.append({"code_s": code, "stock_name": holdings[code]["stock_name"],
                              "updated_at": r.get("updated_at"), "kessan_date": past[-1]})

    # 決算が近い保有
    horizon = (date.fromisoformat(today) + timedelta(days=UPCOMING_DAYS)).isoformat()
    upcoming = []
    for code, h in holdings.items():
        k = (stocks.get(code) or {}).get("kessan_date")
        if k and today <= k <= horizon:
            upcoming.append({"code_s": code, "stock_name": h["stock_name"], "kessan_date": k,
                             "weight_pct": h["weight_pct"],
                             "margin_qty": sum(x["qty"] for x in lots if x["code_s"] == code and x["kind"] == "信用"),
                             "has_outlook": bool((stocks.get(code) or {}).get("has_outlook"))})
    upcoming.sort(key=lambda u: (u["kessan_date"], u["code_s"]))

    # 問いの材料にする台帳の文言 (①④に触れた銘柄と、前回以降に建てた・買い増した銘柄)
    touched = ({x["code_s"] for x in lot_over} | {x["code_s"] for x in lit}
               | {x["code_s"] for x in stale} | {x["code_s"] for x in additions}
               | {x["code_s"] for x in lots if x["over_horizon"]})
    materials = {c: {k: (ratings.get(c) or {}).get(k) for k in (
        "status", "confidence", "thesis", "risks", "checkpoints", "fair_price", "updated_at")}
        | {"total": (ratings.get(c) or {}).get("total")}
        for c in sorted(touched)}

    # 価格が取れない銘柄も _portfolio から落ちるので、株数は保有レコードから取る
    held_qty = {r["code_s"]: r.get("qty") or 0 for r in records}
    sold = {}
    for ep in episodes:
        if ep.get("is_short"):
            continue
        for f in ep["fills"]:
            if f["side"] == "sell" and f["trade_date"] > since and f.get("trade_kind") != "現引":
                sold[ep["code_s"]] = sold.get(ep["code_s"], 0) + f["qty"]

    return {
        "date": today,
        "generated_at": generated_at,
        "triggers": triggers_since(previous, [x["code_s"] for x in lit],
                                   [x["lot_key"] for x in lot_over], exposure_history),
        "previous": check_previous(previous, today, held_qty, sold, closes or {},
                                   exposure_history, kessan_dates),
        "defense": {
            "lot_over": lot_over,
            "lot_over_keys": [x["lot_key"] for x in lot_over],
            "over_horizon": [x for x in lots if x["over_horizon"]],
            "lit": lit,
            "no_width": [x for x in lots if x["stop_loss_pct"] is None],
            "widened": widened,
        },
        "additions": {"since": since, "lots": additions},
        "count": {
            "holding_count": pf["holding_count"], "target": HOLDING_TARGET,
            "small": pf["small"], "ledger_off": ledger_off,
        },
        "exposure": {
            **pf["exposure"],
            "falls": (margin or {}).get("falls"),
            "equity": equity,
            "weight_cap_pct": cap,
            "weights": weights,
            "themes": sorted(({"theme": t, "weight_pct": w} for t, w in themes.items()),
                             key=lambda r: -r["weight_pct"]),
            "margin_no_width": [x for x in lots if x["kind"] == "信用" and x["trade_idea"] in no_width_names],
            # 投資戦略.md: 逆張りでレバを上げてよいのは、幅超えが0件のときだけ
            "contrarian_with_over": bool(step is not None and step < 0 and lot_over),
        },
        "hypothesis": {"stale_ratings": stale, "materials": materials},
        "upcoming": upcoming,
        "lots": lots,
    }


def validate_decisions(decisions: Dict[str, Any], previous_reviews: List[Dict[str, Any]]) -> List[str]:
    """決定の必須項目を確かめ、足りないものを返す (空なら通る)。

    previous_reviews: 保存済みの点検 (古い順)。前回までに価格の線で持った銘柄を、また
      価格の線で持つとき (線の引き直しが2回目以降) は期限 (exit.date) も必須にする。
    """
    errors = []
    order_codes = {c.get("code_s") for c in (decisions.get("sell_order") or {}).get("codes") or []}
    redrawn = {d["code_s"] for r in previous_reviews
               for d in (r.get("decisions") or {}).get("holdings") or []
               if d.get("action") == "hold" and (d.get("exit") or {}).get("type") == "price"}
    for d in decisions.get("holdings") or []:
        code, action = d.get("code_s") or "?", d.get("action")
        if not d.get("code_s"):
            errors.append("code_s が無い決定がある")
            continue
        if action not in ACTIONS:
            errors.append(f"{code}: action は {'/'.join(ACTIONS)} のどれか")
            continue
        if action != "hold":
            continue
        if not str(d.get("reason") or "").strip():
            errors.append(f"{code}: 持つ理由 (reason) が無い")
        ex = d.get("exit") or {}
        if ex.get("type") not in EXIT_TYPES:
            errors.append(f"{code}: 売る条件 (exit.type) は {'/'.join(EXIT_TYPES)} のどれか")
        elif ex["type"] == "price" and not ex.get("price"):
            errors.append(f"{code}: 価格の線 (exit.price) が無い")
        elif ex["type"] == "deadline" and not ex.get("date"):
            errors.append(f"{code}: 期限 (exit.date) が無い")
        elif ex["type"] == "margin_order" and code not in order_codes:
            errors.append(f"{code}: 維持率で売る順番 (sell_order.codes) に入っていない")
        if ex.get("type") == "price" and code in redrawn and not ex.get("date"):
            errors.append(f"{code}: 線の引き直しが2回目以降なので期限 (exit.date) も要る")
    order = decisions.get("sell_order")
    if order and order.get("trigger_margin_rate") is None:
        errors.append("sell_order: trigger_margin_rate (何%を割ったら売るか) が無い")
    if order and not all(c.get("code_s") for c in order.get("codes") or []):
        errors.append("sell_order: code_s が無い銘柄がある")
    return errors


def list_reviews(review_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """保存済みの点検を古い順に返す。"""
    out = []
    for path in sorted(glob.glob(os.path.join(review_dir or REVIEW_DIR, "????-??-??.json"))):
        with open(path, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def latest_review(review_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    reviews = list_reviews(review_dir)
    return reviews[-1] if reviews else None


def save_review(dataset: Dict[str, Any], decisions: Dict[str, Any], report_md: str,
                review_dir: Optional[str] = None) -> List[str]:
    """JSON (dataset + decisions) とレポートを保存する。どちらかが既にあれば何も書かずに例外を出す。"""
    if ks_util.is_dev_data_dir():
        raise RuntimeError("開発機の開発用コピーには保存しません。"
                           "deploy/macmini.sh run 経由で運用機に保存してください")
    if not report_md.strip():
        raise ValueError("レポート (標準入力) が空です")
    review_dir = review_dir or REVIEW_DIR
    errors = validate_decisions(decisions, list_reviews(review_dir))
    if errors:
        raise ValueError("決定が足りません:\n" + "\n".join(errors))
    day = dataset["date"]
    paths = [os.path.join(review_dir, f"{day}.{ext}") for ext in ("json", "md")]
    existing = [p for p in paths if os.path.exists(p)]
    if existing:
        raise FileExistsError(f"既に保存済みです (上書きしません): {', '.join(existing)}")
    os.makedirs(review_dir, exist_ok=True)
    with open(paths[0], "x", encoding="utf-8") as f:
        json.dump({"date": day, "dataset": dataset, "decisions": decisions}, f, ensure_ascii=False, indent=2)
    with open(paths[1], "x", encoding="utf-8") as f:
        f.write(report_md)
    return paths


def _load_inputs() -> Dict[str, Any]:
    """build_review_dataset の入力を DB から読む (読み取りのみ)。終値は yfinance から取る。"""
    import exposure_guide
    import gyoseki
    import stock_ratings
    from webapp import action_queue, helpers, trade_episodes

    today = date.today().isoformat()
    records = ps.list_records(status="1保")
    codes = [r["code_s"] for r in records]
    stock_map = helpers._bulk_get_stock_data(codes)
    stocks = {}
    for c, s in stock_map.items():
        if not isinstance(s, dict):
            continue
        k = helpers._parse_kessanbi(s.get("kessanbi"))
        rec = next((r for r in records if r["code_s"] == c), None)
        stocks[c] = {"price": s.get("price"), "stock_name": s.get("stock_name", ""),
                     "kessan_date": k.isoformat() if k else None,
                     "has_outlook": bool(k and action_queue._has_outlook(rec, k))}
    previous = latest_review()
    closes: Dict[str, Dict[str, float]] = {}
    if previous:
        end = (date.today() - timedelta(days=1)).isoformat()
        targets = sorted({d["code_s"] for d in (previous.get("decisions") or {}).get("holdings") or []
                          if (d.get("exit") or {}).get("type") == "price"})
        if targets and previous["date"] <= end:
            closes = trade_review._fetch_closes(targets, {"start": previous["date"], "end": end})["closes"]
    return {
        "today": today,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "episodes": trade_episodes.build_fill_episodes(),
        "records": records,
        "stocks": stocks,
        "trade_ideas": ps.list_trade_ideas(),
        "fill_strategies": ps.list_fill_strategies(),
        "exposure_history": exposure_guide._load_exposure_log()["history"],
        "margin": exposure_guide.build_margin_state()["combined"],
        "exits": action_queue._recorded_exits(ps.list_records(), stock_map),
        # 総合点はシステム軸を全銘柄で出してから足すので、1銘柄ずつの get_rating ではなく一覧から引く
        "ratings": {r["code_s"]: r for r in stock_ratings.list_ratings() + stock_ratings.list_ratings("Archive")
                    if r["code_s"] in codes},
        "kessan_dates": {c: gyoseki.load_kessan_dates(c)["kessan"] for c in codes},
        "strategy_histories": {c: ps.list_strategy_history(c) for c in codes},
        "previous": previous,
        "closes": closes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="保有ポートフォリオの点検の数字を作る (issue #589)")
    parser.add_argument("command", choices=["json", "save"],
                        help="json: 標準出力へ (保存しない) / save: 決定と標準入力のレポートと一緒に保存")
    parser.add_argument("--decisions", help="save: 決定の JSON ファイル")
    args = parser.parse_args()
    if args.command == "save" and not args.decisions:
        log_warning("save には --decisions が要ります")
        return 2

    dataset = build_review_dataset(**_load_inputs())
    if args.command == "json":
        sys.stdout.write(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n")
        return 0
    with open(args.decisions, encoding="utf-8") as f:
        decisions = json.load(f)
    try:
        paths = save_review(dataset, decisions, sys.stdin.read())
    except (RuntimeError, ValueError, FileExistsError) as e:
        log_warning(str(e))
        return 1
    log_print(f"保存しました: {', '.join(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
