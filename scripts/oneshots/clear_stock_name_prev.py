"""issue #236 Step 1: stock_name_prev クリーンアップ。

issue #183 (PR #235) で導入された自動退避により、既存 research_shelve の
`stock_name_prev` には旧名が大量に入っている。issue #236 Step 2 で導入する
「prev が空でない限り sync が上書きしない」ルールを安全に適用するため、
本スクリプトで既存の `stock_name_prev` を全て None にリセットする。

本スクリプトは Step 2 の前提として 1 回だけ実行する想定。実行後は手動で
入力された stock_name_prev のみが残り、Step 2 のロジックで保護される。

research_shelve 規約 (research_shelve.py L15-17) に従い、直接 ShelveDB を
開かず公開 API (list_research_records / upsert_research_record) を使う。
upsert_research_record は内部で _flock を取るため webapp / 他バッチとの
並行書き込みから保護される。

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

    # 1. 読み取り: 既存 prev を持つレコードを列挙
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

    # 2. 書き込み: 公開 API upsert_research_record で完全上書き
    cleared = 0
    for r in targets:
        r["stock_name_prev"] = None
        rs.upsert_research_record(r)
        cleared += 1

    log_print(f"cleared {cleared} records (stock_name_prev を None にリセット)")


if __name__ == "__main__":
    main()
