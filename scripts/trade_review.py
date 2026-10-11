#!/usr/bin/env python3
"""月次の売買振り返り (/trade-review) に渡す数字を作る (issue #448)。

数字は Python が出し、LLM は DB を触らない。build_review_dataset は引数だけで
計算し、DB を読むのは CLI 側 (_load_inputs) に限る。

使い方 (scripts/ から):
    python trade_review.py json 2026-09               # 標準出力へ。保存しない
    python trade_review.py save 2026-09 < report.md   # JSON を作り直し、md と一緒に保存

保存先は $KS_DATA_DIR/trade_review/YYYY-MM.{json,md}。不可逆データとして扱い、
既にあれば書かない (やり直すときは人がファイルを消す)。
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import ks_util  # noqa: E402
import portfolio_shelve as ps  # noqa: E402
from ks_util import log_print, log_warning  # noqa: E402

# 初期損切りの原則 (doc/投資戦略.md「損切り・利確」の -7〜10% の下限)
STOP_LOSS_PCT = -10.0
# 再IN とみなす、直前の決済からの日数
REENTRY_DAYS = 30
# 小口とみなす、建玉合計に対する比率 (%)
SMALL_POSITION_PCT = 3.0
# 保有日数帯の境界 (仕様レビュー_0927 で損益が分かれた 0〜3日 / 91日以上)
HOLD_BANDS = (("0-3", 0, 3), ("4-90", 4, 90), ("91+", 91, None))

REVIEW_DIR = os.path.join(ks_util.DATA_DIR, "trade_review")


def _profit(ep: Dict[str, Any]) -> Optional[float]:
    return (ep.get("pl") or {}).get("profit_amount")


def _first_fill(ep: Dict[str, Any]) -> Dict[str, Any]:
    return min(ep["fills"], key=lambda f: (f["trade_date"], f["seq"]))


def _days_between(d1: str, d2: str) -> int:
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def _find_reentry(ep: Dict[str, Any], episodes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """建てた日の前 REENTRY_DAYS 日以内に同じ銘柄の決済があれば、直近の1件を返す。

    同じ日の決済は数えない (現物を持ったまま信用で売買した、と区別できないため)。
    現引で始まる現物エピソードは信用からの振り替えなので再IN にしない。
    買いと空売りは向きが逆なので、同じ向きの決済だけを前回とみなす。
    """
    if _first_fill(ep).get("trade_kind") == "現引":
        return None
    prev = [
        e for e in episodes
        if e["code_s"] == ep["code_s"] and e["closed"] and e.get("close_date")
        and bool(e.get("is_short")) == bool(ep.get("is_short"))
        and e["close_date"] < ep["open_date"]
        and _days_between(e["close_date"], ep["open_date"]) <= REENTRY_DAYS
    ]
    if not prev:
        return None
    last = max(prev, key=lambda e: e["close_date"])
    return {
        "prev_episode_key": last["episode_key"],
        "prev_close_date": last["close_date"],
        "days_since": _days_between(last["close_date"], ep["open_date"]),
        "prev_return_pct": (last.get("pl") or {}).get("return_pct"),
        "prev_qty_peak": last.get("qty_peak"),
    }


def _unrealized_pct(eps: List[Dict[str, Any]]) -> Optional[float]:
    """保有中エピソード群の含み損益率 (%)。部分売却の実現損益は混ぜない。

    証券会社の画面と同じ基準にする。現物は平均取得単価 (open_pl)、信用は残っている
    建玉ごとの建単価 (往復行の保有中の行)。信用を open_pl の平均建単価で出すと、
    返済済みの高値の建玉が平均に残り、残り建玉の含み損が実態より大きく出る。
    """
    from webapp import trade_episodes  # 遅延 import (DB を開くモジュールを計算側で常時読まない)

    cost = unrealized = 0.0
    for ep in eps:
        if ep["kind"] == "信用":
            for r in trade_episodes.build_round_trips(ep):
                if r["closed"] or r["pl"] is None or not r["open_price"]:
                    continue
                cost += r["open_price"] * r["qty"]
                unrealized += r["pl"]
            continue
        op = ep.get("open_pl") or {}
        if not op.get("held_qty") or not op.get("avg_cost") or op.get("unrealized") is None:
            continue
        cost += op["avg_cost"] * op["held_qty"]
        unrealized += op["unrealized"]
    return unrealized / cost * 100.0 if cost else None


def _trade_row(ep: Dict[str, Any], episodes: List[Dict[str, Any]],
               log_by_date: Dict[str, Dict[str, Any]],
               horizons: Dict[str, str]) -> Dict[str, Any]:
    pl = ep.get("pl") or {}
    horizon = horizons.get(ep.get("trade_idea") or "", "")
    open_log = log_by_date.get(ep["open_date"]) or {}
    return {
        "episode_key": ep["episode_key"],
        "code_s": ep["code_s"],
        "stock_name": ep.get("stock_name", ""),
        "kind": ep["kind"],
        "is_short": bool(ep.get("is_short")),
        "trade_idea": ep.get("trade_idea") or "",
        "strategy_drift": bool(ep.get("strategy_drift")),
        "time_horizon": horizon,
        "open_date": ep["open_date"],
        "close_date": ep.get("close_date"),
        "qty_peak": ep.get("qty_peak"),
        "hold_days": pl.get("hold_days"),
        "hold_days_consistent": (ps.is_hold_days_consistent(horizon, pl.get("hold_days"))
                                 if ep["closed"] else None),
        "return_pct": pl.get("return_pct"),
        "profit_amount": pl.get("profit_amount"),
        "unrealized_pct": None if ep["closed"] else _unrealized_pct([ep]),
        "open_state": open_log.get("state"),
        "open_position": open_log.get("position"),
        "reentry": _find_reentry(ep, episodes),
        "review_memo": ep.get("review_memo") or "",
        "action_notes": ep.get("action_notes") or [],
        "fills": [
            {"trade_date": f["trade_date"], "side": f["side"], "trade_kind": f.get("trade_kind"),
             "qty": f["qty"], "price": f["price"],
             "state": (log_by_date.get(f["trade_date"]) or {}).get("state")}
            for f in sorted(ep["fills"], key=lambda f: (f["trade_date"], f["seq"]))
        ],
    }


def _summary(ym: str, closed: List[Dict[str, Any]], episodes: List[Dict[str, Any]],
             log_by_date: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    priced = [e for e in closed if _profit(e) is not None]

    def part(eps: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"count": len(eps), "pl": sum(_profit(e) for e in eps)}

    bands = []
    for name, lo, hi in HOLD_BANDS:
        eps = [e for e in priced
               if e["pl"].get("hold_days") is not None and e["pl"]["hold_days"] >= lo
               and (hi is None or e["pl"]["hold_days"] <= hi)]
        bands.append({"band": name, **part(eps)})

    # 局面別の売買額。現引は信用側・現物側の両方のエピソードに同じ約定が出るので除く。
    # 空売りは買い・売りの向きが逆になるので額に入れず、約定数だけ数える。
    by_state: Dict[str, Dict[str, Any]] = {}
    short_fill_count = 0
    for ep in episodes:
        for f in ep["fills"]:
            if not f["trade_date"].startswith(ym) or f.get("trade_kind") == "現引":
                continue
            if ep.get("is_short"):
                short_fill_count += 1
                continue
            state = (log_by_date.get(f["trade_date"]) or {}).get("state") or "unknown"
            row = by_state.setdefault(state, {"state": state, "buy_amount": 0.0, "buy_count": 0,
                                              "sell_amount": 0.0, "sell_count": 0})
            row[f"{f['side']}_amount"] += f["qty"] * f["price"]
            row[f"{f['side']}_count"] += 1

    # 運用比率。土日のログは前営業日の写しなので数えない
    days = [h for d, h in sorted(log_by_date.items())
            if d.startswith(ym) and date.fromisoformat(d).weekday() < 5
            and h.get("ratio_pct") is not None]
    correction = [h for h in days if h.get("state") == "market_in_correction"]
    return {
        "closed": {**part(priced),
                   "wins": sum(1 for e in priced if _profit(e) > 0),
                   "losses": sum(1 for e in priced if _profit(e) <= 0),
                   "no_pl_count": len(closed) - len(priced)},
        "undeclared": part([e for e in priced if not e.get("trade_idea")]),
        "hold_bands": bands,
        "by_state": sorted(by_state.values(), key=lambda r: r["state"]),
        "short_fill_count": short_fill_count,
        "exposure": {
            "ratio_min": min((h["ratio_pct"] for h in days), default=None),
            "ratio_max": max((h["ratio_pct"] for h in days), default=None),
            "logged_days": len(days),
            "correction_days": len(correction),
            "correction_days_over": sum(1 for h in correction if h.get("position") == "over"),
        },
    }


def _hypotheses(ym: str, episodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """弱点仮説を、当年1月1日から当月末までの累計で検証する。"""
    year_start, month_end = f"{ym[:4]}-01-01", f"{ym}-31"
    closed = [e for e in episodes
              if e["closed"] and year_start <= (e.get("close_date") or "") <= month_end]
    usable = [e for e in closed if not e.get("split_suspect") and _profit(e) is not None]

    losses = [e for e in usable if _profit(e) <= 0]
    over = [e for e in losses
            if e["pl"].get("return_pct") is not None and e["pl"]["return_pct"] < STOP_LOSS_PCT]
    over_keys = {e["episode_key"] for e in over}

    # 再IN は建てた側から数える (決済側から数えると、1つの建て直しを二重に数えうる)
    reentries = [e for e in episodes
                 if year_start <= e["open_date"] <= month_end and _find_reentry(e, episodes)]
    settled = [e for e in reentries
               if e["closed"] and (e.get("close_date") or "") <= month_end
               and not e.get("split_suspect") and _profit(e) is not None]
    return {
        "period": {"from": year_start, "to": ym},
        "closed_count": len(usable),
        "excluded_count": len(closed) - len(usable),
        "stop_loss_width": {
            "loss_count": len(losses),
            "loss_pl": sum(_profit(e) for e in losses),
            "over_count": len(over),
            "over_pl": sum(_profit(e) for e in over),
            "within_pl": sum(_profit(e) for e in losses if e["episode_key"] not in over_keys),
            "month_over_count": sum(1 for e in over if e["close_date"].startswith(ym)),
        },
        "reentry": {
            "count": len(reentries),
            "settled_count": len(settled),
            "settled_wins": sum(1 for e in settled if _profit(e) > 0),
            "settled_pl": sum(_profit(e) for e in settled),
            "month_count": sum(1 for e in reentries if e["open_date"].startswith(ym)),
        },
    }


def _portfolio(episodes: List[Dict[str, Any]], exposure_history: List[Dict[str, Any]],
               records: List[Dict[str, Any]], stocks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """レポートを作った日の保有PF。保有構成は 1保 で数える (保有中エピソードは実保有と一致しない)。"""
    open_eps: Dict[str, List[Dict[str, Any]]] = {}
    for ep in episodes:
        if not ep["closed"] and not ep.get("split_suspect"):
            open_eps.setdefault(ep["code_s"], []).append(ep)

    holdings = []
    for rec in records:
        code_s = rec["code_s"]
        stock = stocks.get(code_s) or {}
        qty, price = rec.get("qty") or 0, stock.get("price")
        if qty <= 0 or not isinstance(price, (int, float)) or price <= 0:
            continue
        holdings.append({
            "code_s": code_s,
            "stock_name": stock.get("stock_name", ""),
            "qty": qty,
            "value": float(price) * qty,
            "trade_idea": (rec.get("memo") or {}).get("trade_idea") or "",
            "unrealized_pct": _unrealized_pct(open_eps.get(code_s, [])),
        })
    total = sum(h["value"] for h in holdings)
    for h in holdings:
        h["weight_pct"] = h["value"] / total * 100.0 if total else None
    holdings.sort(key=lambda h: h["value"], reverse=True)
    small = [h for h in holdings if total and h["weight_pct"] < SMALL_POSITION_PCT]

    names = {h["code_s"]: h["stock_name"] for h in holdings}
    over = [
        {"episode_key": ep["episode_key"], "code_s": code_s, "stock_name": names[code_s],
         "kind": ep["kind"], "trade_idea": ep.get("trade_idea") or "",
         "open_date": ep["open_date"], "unrealized_pct": pct}
        for code_s, eps in open_eps.items() if code_s in names
        for ep in eps
        for pct in [_unrealized_pct([ep])]
        if pct is not None and pct < STOP_LOSS_PCT
    ]
    latest = max(exposure_history, key=lambda h: h.get("date") or "", default={})
    return {
        "exposure": {k: latest.get(k) for k in (
            "date", "total_value", "base_amount", "ratio_pct", "state",
            "range_lower", "range_upper", "position")},
        "holding_count": len(holdings),
        "total_value": total,
        "small": {"count": len(small), "value": sum(h["value"] for h in small)},
        "over_stop_loss": sorted(over, key=lambda r: r["unrealized_pct"]),
        "holdings": holdings,
    }


def build_review_dataset(year_month: str, *, episodes: List[Dict[str, Any]],
                         exposure_history: List[Dict[str, Any]],
                         records: List[Dict[str, Any]],
                         stocks: Dict[str, Dict[str, Any]],
                         trade_ideas: List[Dict[str, Any]],
                         generated_at: str) -> Dict[str, Any]:
    """月次の振り返りに渡す数字を作る (DB を読まない)。

    Args:
        year_month: "YYYY-MM"
        episodes: trade_episodes.build_fill_episodes() の戻り値
        exposure_history: exposure_log.json の history
        records: portfolio_shelve.list_records("1保")
        stocks: 1保 の銘柄の現在値 {code_s: {"price": 終値, "stock_name": 名前}}
        trade_ideas: 戦略マスター (portfolio_shelve.list_trade_ideas())
    """
    log_by_date = {h["date"]: h for h in exposure_history if h.get("date")}
    horizons = {t.get("name", ""): t.get("time_horizon") or "" for t in trade_ideas}

    # 取引ごとの照合の対象: 当月に完全決済したものと、当月に新規に建てたもの。
    # 現引で始まるもの (信用からの振り替え) と当月中に決済したもの (決済側に出る) は
    # 新規建てから除く。分割・併合の疑いは損益が不確かなので件数だけ出す。
    in_month = [e for e in episodes
                if (e["closed"] and (e.get("close_date") or "").startswith(year_month))
                or e["open_date"].startswith(year_month)]
    targets = [e for e in in_month if not e.get("split_suspect")]
    closed = [e for e in targets
              if e["closed"] and (e.get("close_date") or "").startswith(year_month)]
    closed_keys = {e["episode_key"] for e in closed}
    opened = [e for e in targets
              if e["open_date"].startswith(year_month) and e["episode_key"] not in closed_keys
              and _first_fill(e).get("trade_kind") != "現引"]

    def rows(eps: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
        return [_trade_row(e, episodes, log_by_date, horizons)
                for e in sorted(eps, key=lambda e: (e[key], e["code_s"]))]

    return {
        "year_month": year_month,
        "generated_at": generated_at,
        "summary": _summary(year_month, closed, episodes, log_by_date),
        "trades": {"closed": rows(closed, "close_date"), "opened": rows(opened, "open_date"),
                   "split_suspect_count": len(in_month) - len(targets)},
        "hypotheses": _hypotheses(year_month, episodes),
        "portfolio": _portfolio(episodes, exposure_history, records, stocks),
    }


def _load_inputs() -> Dict[str, Any]:
    """build_review_dataset の入力を DB から読む (読み取りのみ)。"""
    import exposure_guide
    from webapp import helpers, trade_episodes

    records = ps.list_records(status="1保")
    stock_map = helpers._bulk_get_stock_data([r["code_s"] for r in records])
    return {
        "episodes": trade_episodes.build_fill_episodes(),
        "exposure_history": exposure_guide._load_exposure_log()["history"],
        "records": records,
        "stocks": {c: {"price": s.get("price"), "stock_name": s.get("stock_name", "")}
                   for c, s in stock_map.items() if isinstance(s, dict)},
        "trade_ideas": ps.list_trade_ideas(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def save_review(year_month: str, dataset: Dict[str, Any], report_md: str,
                review_dir: Optional[str] = None) -> List[str]:
    """JSON とレポートを保存する。どちらかが既にあれば何も書かずに例外を出す。"""
    if ks_util.is_dev_data_dir():
        raise RuntimeError("開発機の開発用コピーには保存しません。"
                           "deploy/macmini.sh run 経由で運用機に保存してください")
    if not report_md.strip():
        raise ValueError("レポート (標準入力) が空です")
    review_dir = review_dir or REVIEW_DIR
    paths = [os.path.join(review_dir, f"{year_month}.{ext}") for ext in ("json", "md")]
    existing = [p for p in paths if os.path.exists(p)]
    if existing:
        raise FileExistsError(f"既に保存済みです (上書きしません): {', '.join(existing)}")
    os.makedirs(review_dir, exist_ok=True)
    with open(paths[0], "x", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    with open(paths[1], "x", encoding="utf-8") as f:
        f.write(report_md)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="月次の売買振り返りの数字を作る (issue #448)")
    parser.add_argument("command", choices=["json", "save"],
                        help="json: 標準出力へ (保存しない) / save: 標準入力のレポートと一緒に保存")
    parser.add_argument("year_month", help="対象月 (例: 2026-09)")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.year_month):
        log_warning(f"対象月は YYYY-MM で指定してください: {args.year_month}")
        return 2

    dataset = build_review_dataset(args.year_month, **_load_inputs())
    if args.command == "json":
        sys.stdout.write(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n")
        return 0
    try:
        paths = save_review(args.year_month, dataset, sys.stdin.read())
    except (RuntimeError, ValueError, FileExistsError) as e:
        log_warning(str(e))
        return 1
    log_print(f"保存しました: {', '.join(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
