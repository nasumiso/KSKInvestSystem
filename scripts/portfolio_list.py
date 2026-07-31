#!/usr/bin/env python3
"""保有銘柄リストの銘柄コードを出力する CLI。

他コマンドへの流し込み用 (例: 保有銘柄をまとめて update):

    for c in $(python portfolio_list.py --status 1保); do
        python make_stock_db.py update $c
    done

status を省略すると全ステータス (1保/2準/3監) を出力する。
コードは code_s 昇順・スペース区切りで stdout に出す (ログは混ぜない)。
"""

import argparse

import portfolio_shelve as ps


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
    args = parser.parse_args()

    records = ps.list_records(status=args.status)
    codes = [r["code_s"] for r in records]
    # 流し込み用途のため stdout にはコードのみをスペース区切りで出す。
    print(" ".join(codes))


if __name__ == "__main__":
    main()
