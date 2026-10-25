#!/usr/bin/env python3
"""保有銘柄リストの銘柄コードを出力する CLI。

他コマンドへの流し込み用 (例: 保有銘柄をまとめて update):

    for c in $(python portfolio_list.py --status 1保); do
        python make_stock_db.py update $c
    done

status を省略すると全ステータス (1保/2準/3監) を出力する。
コードは code_s 昇順・スペース区切りで stdout に出す (ログは混ぜない)。

--exits は 1保 の「防」の記録を表で出す (読むだけ)。トップの今日のアクションと同じ
action_queue._recorded_exits() を使うので、今の戦略と合わない古い記録は読まない。
"""

import argparse
from datetime import date

import portfolio_shelve as ps


def print_exits():
    """1保 の銘柄ごとに、戦略・損切り幅・防の初点灯日・初点灯からの営業日数・今も防かを出す。"""
    from webapp import action_queue
    from webapp.helpers import _bulk_get_stock_data

    records = ps.list_records(status="1保")
    stocks = _bulk_get_stock_data([r["code_s"] for r in records])
    exits = action_queue._recorded_exits(records, stocks)
    rules = {i.get("name"): i.get("exit_rule") or {} for i in ps.list_trade_ideas()}
    today = date.today()
    print("\t".join(["コード", "銘柄名", "戦略", "損切り幅", "防の初点灯", "営業日", "今"]))
    for r in records:
        code_s = r["code_s"]
        strategy = (r.get("memo") or {}).get("trade_idea") or ""
        stop = (rules.get(strategy) or {}).get("stop_loss_pct")
        ex = exits.get(code_s)
        first = ex["first_defense"] if ex else ""
        days = str(action_queue._busdays_since(date.fromisoformat(first), today)) if ex else ""
        now = ""
        if ex and ex["signal"]:
            sig = ex["signal"]
            now = "防 " + "・".join(sig.get("reasons") or [])
            if sig.get("stop_loss_line"):
                now += " (損切りライン %.0f)" % sig["stop_loss_line"]
        name = (stocks.get(code_s) or {}).get("stock_name", "")
        print("\t".join([code_s, name, strategy or "-", "%g%%" % stop if stop else "-",
                          first or "-", days or "-", now or "-"]))


def main():
    parser = argparse.ArgumentParser(
        description="保有銘柄リストの銘柄コードを出力する (他コマンドへの流し込み用)"
    )
    parser.add_argument(
        "--status",
        choices=sorted(ps.VALID_STATUSES),
        default=None,
        help="絞り込むステータス (1保/2準/3監)。省略時は全件",
    )
    parser.add_argument(
        "--exits",
        action="store_true",
        help="1保 の防の記録 (戦略・損切り幅・初点灯日・営業日数・今も防か) を表で出す",
    )
    args = parser.parse_args()
    if args.exits:
        print_exits()
        return

    records = ps.list_records(status=args.status)
    codes = [r["code_s"] for r in records]
    # 流し込み用途のため stdout にはコードのみをスペース区切りで出す。
    print(" ".join(codes))


if __name__ == "__main__":
    main()
