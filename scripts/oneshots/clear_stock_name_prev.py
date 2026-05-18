"""issue #236 Step 1: stock_name_prev クリーンアップ。

issue #183 (PR #235) で導入された自動退避により、既存 research_shelve の
`stock_name_prev` には旧名が大量に入っている。issue #236 Step 2 で導入する
「prev が空でない限り sync が上書きしない」ルールを安全に適用するため、
本スクリプトで既存の `stock_name_prev` を全て None にリセットする。

本スクリプトは Step 2 の前提として 1 回だけ実行する想定。実行後は手動で
入力された stock_name_prev のみが残り、Step 2 のロジックで保護される。

research_shelve 規約 (research_shelve.py L15-17) に従い、公開 API のみを使う。
列挙は list_research_records() (フィルタなし全件)、クリアは
clear_stock_name_prev_field() を 1 件ずつ呼ぶ。後者は _flock 区間内で R-M-W を
完結させるため、Web UI / 他バッチが同レコードの他フィールド (memo 等) を
同時更新していても lost update を起こさない (codex P2 対応)。

Usage:
    python scripts/oneshots/clear_stock_name_prev.py --dry-run   # 対象件数のみ表示
    python scripts/oneshots/clear_stock_name_prev.py             # 実行
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import research_shelve as rs
from ks_util import log_print


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="変更せず対象件数のみ表示",
    )
    args = parser.parse_args()

    # 1. 読み取り: 既存 prev を持つレコードを列挙 (代表値のスナップショット)
    all_records = rs.list_research_records()
    targets = [r for r in all_records if r.get("stock_name_prev")]

    if args.dry_run:
        log_print(f"[dry-run] {len(targets)} 件が対象:")
        for r in targets[:20]:
            log_print(
                f"  {r['code_s']} {r.get('stock_name')} "
                f"(旧 {r['stock_name_prev']})"
            )
        if len(targets) > 20:
            log_print(f"  ... (他 {len(targets) - 20} 件)")
        return

    # 2. クリア: 公開 API で _flock 内 R-M-W を 1 件ずつ。
    #    targets[i] のスナップショットは古い可能性があるが、code_s だけ使い、
    #    実書き換えは clear_stock_name_prev_field 内で最新値を再読込してから行う。
    cleared = 0
    for r in targets:
        if rs.clear_stock_name_prev_field(r["code_s"]):
            cleared += 1

    log_print(f"cleared {cleared} records (stock_name_prev を None にリセット)")


if __name__ == "__main__":
    main()
