#!/usr/bin/env python3
"""年末の損出し試算を出す CLI (読み取り専用)。

特定口座の含み損の建玉を、売り切り候補 (a) とクロス候補 (b) に分け、口座ごとの相殺の上限と
還付見込み・コスト概算を Markdown で stdout に出す。DB・ファイルは書かない。

相殺の上限 (今年の差し引き実現益) は CSV から再計算せず、証券会社の「特定口座 譲渡益税」画面の
今年の譲渡損益を --cap で渡す。移管した ETF の取得価額や未取込の売買で再計算がずれるため、画面を正とする。

使い方 (scripts/ から):
    python loss_harvest.py --cap 楽天=5030808 --cap SBI=613077 --cap マネックス=466601
    deploy/macmini.sh run python loss_harvest.py --cap ...   # 開発機から運用機に問い合わせる
"""

import argparse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import portfolio_shelve as ps

TAX_RATE = 0.20315
SPREAD_CROSS = 0.002  # クロス (売り+買い直し) の気配・滑りの想定: 評価額の 0.2%
SPREAD_SELL = 0.001   # 売り切りのみ: 0.1%
MARGIN_RATE = 0.028   # 制度信用の買方金利 (年率)。建て直しで新旧の建玉が重なる1日分だけ加える
UNIT = 100            # 単元株数 (ETF は銘柄ごとに違うので単元未満の判定をしない)
SELL_ONLY_STATUSES = ("Watch", "Archive", "未登録")


def deadline(year: int) -> Tuple[date, date]:
    """当年に受渡が済む最終約定日と最終受渡日を返す (受渡は約定の2営業日後、年末に祝日は無い前提)。"""
    settle = date(year, 12, 30)
    while settle.weekday() >= 5:
        settle -= timedelta(days=1)
    trade, n = settle, 0
    while n < 2:
        trade -= timedelta(days=1)
        if trade.weekday() < 5:
            n += 1
    return trade, settle


def holding_starts(fills: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], date]:
    """証券会社 × 銘柄 × 現物/信用 ごとに、株数が 0 から立ち上がった最後の約定日を返す (保有日数の起点)。

    エピソード (build_fill_episodes) は証券会社をまたいで建玉をつなぐので使わない。
    分割・併合の入庫は fill に無いので、株数が負になったら 0 に戻して数え直す。
    """
    qty: Dict[Tuple[str, str, str], int] = {}
    start: Dict[Tuple[str, str, str], date] = {}
    for f in sorted(fills, key=lambda f: (f["trade_date"], f.get("seq", 0))):
        tk, side, q = f.get("trade_kind"), f.get("side"), f.get("qty") or 0
        moves = []
        if tk in ("現物", "現物(単元未満)"):
            moves.append(("現物", q if side == "buy" else -q))
        elif tk == "信用新規" and side == "buy":
            moves.append(("信用", q))
        elif tk == "信用返済" and side == "sell":
            moves.append(("信用", -q))
        elif tk == "現引":
            moves += [("信用", -q), ("現物", q)]
        for kind, dq in moves:
            key = (f.get("broker"), f["code_s"], kind)
            before = qty.get(key, 0)
            after = max(before + dq, 0)
            if before <= 0 < after:
                start[key] = date.fromisoformat(f["trade_date"])
            qty[key] = after
    return start


def build_plan(positions: List[Dict[str, Any]], closes: Dict[str, float], ratings: Dict[str, str],
               ideas: Dict[str, Optional[str]], stops: Dict[str, Optional[float]],
               open_dates: Dict[Tuple[str, str, str], date], caps: Dict[str, Optional[int]],
               today: date) -> Dict[str, Any]:
    """候補と口座ごとの上限比較を組み立てる。

    positions: list_positions() の行。closes: 銘柄ごとの終値。ratings: 銘柄 → 台帳の status。
    ideas: 銘柄 → 戦略名。stops: 戦略名 → 損切り幅[%]。open_dates: (証券会社, 銘柄, 現物/信用) → 保有の起点日。
    caps: 証券会社 → 今年の差し引き実現益 (画面の値)。未入力は None。
    """
    targets, excluded = [], []
    for p in positions:
        if p.get("account") == "特定" and p.get("kind") in ("現物", "信用") and p.get("qty"):
            targets.append(p)
        else:
            excluded.append(p)

    candidates, skipped = [], []
    for p in targets:
        code, kind = p["code_s"], p["kind"]
        close, cost = closes.get(code), p.get("avg_price")
        if not isinstance(close, (int, float)) or close <= 0 or cost is None:
            skipped.append(p)
            continue
        qty = p["qty"]
        upl = qty * (close - cost)
        if upl >= 0:
            continue
        value = qty * close
        status = ratings.get(code) or "未登録"
        idea = ideas.get(code)
        stop = stops.get(idea) if idea else None
        sell_only = status in SELL_ONLY_STATUSES or not idea
        xcost = value * (SPREAD_SELL if sell_only else SPREAD_CROSS)
        if kind == "信用" and not sell_only:
            xcost += value * MARGIN_RATE / 365
        refund = -upl * TAX_RATE
        opened = open_dates.get((p["broker"], code, kind))
        candidates.append({
            "broker": p["broker"], "kind": kind, "code_s": code, "qty": qty, "cost": cost, "close": close,
            "upl": upl, "upl_pct": (close / cost - 1) * 100, "days": (today - opened).days if opened else None,
            "status": status, "idea": idea, "stop": stop,
            "over_stop": stop is not None and (1 - close / cost) * 100 > stop,
            "odd_lot": kind == "現物" and not ps.is_etf_code(code) and qty % UNIT != 0,
            "group": "a" if sell_only else "b",
            "refund": refund, "xcost": xcost, "net": refund - xcost,
        })
    candidates.sort(key=lambda c: c["upl"])

    accounts = []
    for broker in sorted(set(caps) | {c["broker"] for c in candidates}):
        loss = -sum(c["upl"] for c in candidates if c["broker"] == broker)
        cap = caps.get(broker)
        row = {"broker": broker, "cap": cap, "loss": loss, "over": None, "refund": None}
        if cap is not None:
            row["over"] = loss - max(cap, 0)
            row["refund"] = min(loss, max(cap, 0)) * TAX_RATE
        accounts.append(row)
    total_loss = sum(a["loss"] for a in accounts)
    total = {"cap": None, "loss": total_loss, "over": None, "refund": None}
    if accounts and all(a["cap"] is not None for a in accounts):
        total["cap"] = sum(max(a["cap"], 0) for a in accounts)
        total["over"] = total_loss - total["cap"]
        total["refund"] = min(total_loss, total["cap"]) * TAX_RATE
    return {"candidates": candidates, "accounts": accounts, "total": total,
            "excluded": len(excluded), "skipped": len(skipped)}


def _yen(v: Optional[float]) -> str:
    return "未入力" if v is None else f"{v:,.0f}"


def render(plan: Dict[str, Any], names: Dict[str, str], as_of: str, today: date) -> str:
    trade, settle = deadline(today.year)
    L = [f"# {today.year}年末 損出し試算 ({today.isoformat()})", "",
         f"- 保有の基準日: {as_of} (WebApp で保有CSVを取り込んだ日。古ければ取り込み直す)",
         f"- 期限: {trade.month}/{trade.day} の約定まで ({settle.month}/{settle.day} 受渡)。それより後の約定は翌年の受渡になる",
         "- 含み損は DB の終値 × 株数 − 取得単価 × 株数 (信用の諸経費は含まない)。信用は銘柄ごとにまとめて表示する",
         f"- 対象外: 特定口座以外 (NISA など) と信用売建 {plan['excluded']}件、終値か取得単価が無い建玉 {plan['skipped']}件", "",
         "## 口座ごとの上限", "",
         "| 口座 | 上限 (今年の差し引き実現益) | 含み損の合計 | 上限との差 | 上限まで出したときの還付 |",
         "|---|---:|---:|---|---:|"]

    def over_text(over):
        if over is None:
            return "未入力"
        return f"超過 {over:,.0f} (今年は減らず繰越のみ)" if over > 0 else f"上限内 (残り {-over:,.0f})"

    for a in plan["accounts"]:
        L.append(f"| {a['broker']} | {_yen(a['cap'])} | {a['loss']:,.0f} | {over_text(a['over'])} | {_yen(a['refund'])} |")
    t = plan["total"]
    L.append(f"| **通算 (確定申告)** | {_yen(t['cap'])} | {t['loss']:,.0f} | {over_text(t['over'])} | {_yen(t['refund'])} |")
    L += ["", "特定口座 (源泉徴収あり) の自動通算は同じ証券会社の中だけ。他社との通算と繰越には確定申告が要る。", ""]

    for group, title in (("a", "(a) 売り切り候補 (台帳が Watch・Archive・未登録、または戦略宣言なし)"),
                         ("b", "(b) クロス候補")):
        rows = [c for c in plan["candidates"] if c["group"] == group]
        L += [f"## {title}", "",
              "| 口座 | 区分 | 銘柄 | 株数 | 取得単価 | 終値 | 含み損 | 率 | 保有日数 | 台帳 | 戦略 (損切り幅) | 還付 | コスト | 正味 | 印 |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|"]
        for c in rows:
            stop = f"{c['idea']} ({c['stop']:g}%)" if c["stop"] else (c["idea"] or "なし")
            marks = " ".join(m for m, on in (("損切り幅超過", c["over_stop"]), ("単元未満あり", c["odd_lot"])) if on)
            L.append(f"| {c['broker']} | {c['kind']} | {c['code_s']} {names.get(c['code_s'], '')[:10]} | {c['qty']:,} | "
                     f"{c['cost']:,.0f} | {c['close']:,.0f} | {c['upl']:,.0f} | {c['upl_pct']:.1f}% | "
                     f"{'' if c['days'] is None else c['days']} | {c['status']} | {stop} | {c['refund']:,.0f} | "
                     f"{c['xcost']:,.0f} | {c['net']:,.0f} | {marks} |")
        L.append(f"| | | **小計** | | | | **{sum(c['upl'] for c in rows):,.0f}** | | | | | "
                 f"**{sum(c['refund'] for c in rows):,.0f}** | **{sum(c['xcost'] for c in rows):,.0f}** | "
                 f"**{sum(c['net'] for c in rows):,.0f}** | |")
        L.append("")

    L += ["## 手順 (還付の数字はこの手順どおりに実行した場合)", "",
          "- 現物 (単元株): 信用で新規買い → 約定を確かめる → 現物を売る → 後日その建玉を現引き。"
          "現物で買い増してから売ると平均法で取得単価が混ざり、出せる損が減る",
          "- 現物の単元未満の端数: 現物で「売り → 約定を確かめる → 買い」の順",
          "- 信用買建: 新規買い → 約定を確かめる → 古い建玉を指定して返済売り",
          "- 売りと買いを同時に出さない (仮装売買とみなされるおそれ)。板の薄い銘柄は指値で出す",
          f"- 期日が {trade.month}/{trade.day} より後の建玉は、期日に返済すると翌年の受渡になる (損益を翌年に回す選択肢)",
          f"- コストの想定: 気配・滑りを評価額の {SPREAD_CROSS:.1%} (売り切りは {SPREAD_SELL:.1%})、"
          f"信用の建て直しは重複1日分の金利 (年{MARGIN_RATE:.1%})。売買手数料は0円",
          "- 損出しは課税の繰り延べ。クロスした銘柄は取得単価が下がり、戻ったときの益が大きくなる"]
    return "\n".join(L)


def _parse_caps(items: List[str]) -> Dict[str, Optional[int]]:
    caps: Dict[str, Optional[int]] = {}
    for item in items or []:
        broker, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--cap は 証券会社=金額 の形で指定する: {item!r}")
        caps[broker.strip()] = int(value.replace(",", "").strip())
    return caps


def main() -> None:
    parser = argparse.ArgumentParser(description="年末の損出し試算 (読み取り専用)")
    parser.add_argument("--cap", action="append", default=[], metavar="証券会社=金額",
                        help="証券会社の特定口座 譲渡益税画面の今年の譲渡損益 (例: 楽天=5030808)。複数指定可")
    args = parser.parse_args()

    import exposure_guide
    import stock_ratings
    from webapp.helpers import resolve_stock_name

    today = date.today()
    positions, closes, _ = exposure_guide.margin_inputs()
    ratings = {r["code_s"]: r["status"] for st in (None, "Archive") for r in stock_ratings.list_ratings(status=st)}
    ideas = {r["code_s"]: (r.get("memo") or {}).get("trade_idea") for r in ps.list_records(include_excluded=True)}
    stops = {i.get("name"): (i.get("exit_rule") or {}).get("stop_loss_pct") for i in ps.list_trade_ideas()}
    plan = build_plan(positions, closes, ratings, ideas, stops, holding_starts(ps.list_fills()),
                      _parse_caps(args.cap), today)
    names = {c["code_s"]: resolve_stock_name(c["code_s"]) for c in plan["candidates"]}
    as_of = min((s["as_of"] for s in ps.list_position_sources() if s.get("as_of")), default="不明")
    print(render(plan, names, as_of, today))


if __name__ == "__main__":
    main()
