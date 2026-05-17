#!/usr/bin/env python3
"""portfolio_shelve の旧スキーマ (stock_name フィールド) を物理削除する 1 回限りマイグレーション。

要件 §4 の延長で「銘柄名は portfolio_shelve に保存しない」方針となったため、
既存レコードに残っている stock_name フィールドを削除する。

usage:
    python migrate_portfolio_drop_stock_name.py --dry-run  # 削除候補を表示
    python migrate_portfolio_drop_stock_name.py            # 本番実行
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


def drop_stock_name_field(
    *,
    dry_run: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """全レコードを舐めて stock_name キーがあれば削除する。

    Returns: {"total": int, "cleaned": int, "skipped": int}
    """
    records = ps.list_records(db_path=db_path)
    total = len(records)
    cleaned = 0
    skipped = 0

    for rec in records:
        if "stock_name" not in rec:
            skipped += 1
            continue
        if dry_run:
            cleaned += 1
            continue
        new_rec = {k: v for k, v in rec.items() if k != "stock_name"}
        ps.upsert_record(new_rec, db_path=db_path)
        cleaned += 1

    log_print(
        f"migrate_drop_stock_name: 全 {total} 件、"
        f"クリーン {cleaned} 件、スキップ {skipped} 件 (dry_run={dry_run})"
    )
    return {"total": total, "cleaned": cleaned, "skipped": skipped}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="portfolio_shelve の旧 stock_name フィールドを物理削除",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="削除対象件数だけ表示し、shelve は変更しない",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="portfolio_shelve のパス上書き (テスト用)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    result = drop_stock_name_field(dry_run=args.dry_run, db_path=args.db_path)
    print(
        f"total={result['total']} cleaned={result['cleaned']} "
        f"skipped={result['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
