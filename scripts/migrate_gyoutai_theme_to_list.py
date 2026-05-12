#!/usr/bin/env python3
"""portfolio_shelve.memo の旧 gyoutai_theme (str/改行区切り) を新 gyoutai_themes (list) に移行する 1 回限りマイグレーション (issue #187)。

要件:
- 旧 `gyoutai_theme` の改行区切り文字列を split → strip → 空除去 → 先頭 N 件
- 新 `gyoutai_themes` に保存
- 旧 `gyoutai_theme` は空文字に更新 (フィールド自体は残す)
- excluded 銘柄も対象 (移行漏れさせない)
- デフォルト dry-run、`--apply` で実書き込み

usage:
    python migrate_gyoutai_theme_to_list.py            # dry-run (デフォルト)
    python migrate_gyoutai_theme_to_list.py --apply    # 本番実行
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
    from ks_util import log_print
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)


def migrate_gyoutai_theme_to_list(
    *,
    apply: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """旧 gyoutai_theme (str) を新 gyoutai_themes (list) に移行する。

    Returns: {"total": int, "converted": int, "skipped": int}
    """
    # excluded 銘柄も対象 (issue #187 codex P3 対応)
    records = ps.list_records(include_excluded=True, db_path=db_path)
    total = len(records)
    converted = 0
    skipped = 0

    for rec in records:
        code_s = rec.get("code_s", "")
        memo = rec.get("memo") or {}
        old = memo.get("gyoutai_theme", "") or ""
        existing_new = memo.get("gyoutai_themes") or []

        # 既に新フィールドに値が入っている / 旧フィールドが空 → スキップ
        if existing_new or not old.strip():
            skipped += 1
            continue

        themes = [t.strip() for t in old.split("\n") if t.strip()][
            : ps.GYOUTAI_THEMES_MAX_SLOTS
        ]
        log_print(
            f"migrate_gyoutai_theme: {code_s}: {old!r} -> {themes}"
        )
        if apply:
            ps.update_memo(
                code_s,
                {
                    "gyoutai_themes": themes,
                    "gyoutai_theme": "",  # 空に更新 (フィールドは残す)
                },
                db_path=db_path,
            )
        converted += 1

    log_print(
        f"migrate_gyoutai_theme_to_list: 全 {total} 件、"
        f"変換 {converted} 件、スキップ {skipped} 件 (apply={apply})"
    )
    return {"total": total, "converted": converted, "skipped": skipped}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="portfolio_shelve.memo の gyoutai_theme (str) を gyoutai_themes (list) に移行",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際に書き込む。指定しない場合は dry-run (デフォルト)",
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
    result = migrate_gyoutai_theme_to_list(apply=args.apply, db_path=args.db_path)
    print(
        f"total={result['total']} converted={result['converted']} "
        f"skipped={result['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
