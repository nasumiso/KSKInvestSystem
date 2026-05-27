#!/usr/bin/env python3
"""portfolio_shelve.memo[gyoutai_themes] の既存値をテーママスター (theme:<name>) に移行する (issue #282)。

要件:
- 全 record:* の memo["gyoutai_themes"] を走査し、ユニーク化
- 各 name について theme:<name> が未登録なら create_theme(name) を呼ぶ
- 既に登録済みならスキップ (冪等)
- name バリデーション (URL 安全) で弾かれる既存値があれば一覧して失敗終了
- 旧 memo["gyoutai_theme"] (str, 旧フィールド) の残存検知:
    - 非空が残っている record があればデフォルトは失敗終了
    - `--include-legacy` を付けた場合のみ旧フィールド値も改行/カンマ分割してマスター対象に含める
- excluded 銘柄も対象 (移行漏れさせない)
- デフォルト dry-run、`--apply` で実書き込み

usage:
    python migrate_themes_to_master.py            # dry-run (デフォルト)
    python migrate_themes_to_master.py --apply    # 本番実行
    python migrate_themes_to_master.py --apply --include-legacy   # 旧フィールド残存も救済して取り込み
"""

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Set

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402

try:
    from ks_util import log_print
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)


_LEGACY_SPLIT_RE = re.compile(r"[\n,]")


def _collect_names_from_records(
    *,
    include_legacy: bool,
    db_path: Optional[str],
) -> tuple:
    """全 record から (new_names, legacy_codes, legacy_names, affected_count) の 4 値タプルを返す。

    - new_names: 新フィールド gyoutai_themes から抽出したユニーク name 集合
    - legacy_codes: 旧 gyoutai_theme フィールドに非空値が残っている code_s 一覧
    - legacy_names: include_legacy=True のとき旧値から抽出した name 集合 (False なら空)
    - affected_count: gyoutai_themes 非空の record 数
    """
    records = ps.list_records(include_excluded=True, db_path=db_path)
    new_names: Set[str] = set()
    legacy_codes: List[str] = []
    legacy_names: Set[str] = set()
    affected_count = 0

    for rec in records:
        code_s = rec.get("code_s", "")
        memo = rec.get("memo") or {}
        themes = memo.get("gyoutai_themes") or []
        if themes:
            affected_count += 1
        for t in themes:
            if isinstance(t, str) and t.strip():
                new_names.add(t.strip())

        legacy = memo.get("gyoutai_theme") or ""
        if isinstance(legacy, str) and legacy.strip():
            legacy_codes.append(code_s)
            if include_legacy:
                for part in _LEGACY_SPLIT_RE.split(legacy):
                    p = part.strip()
                    if p:
                        legacy_names.add(p)

    return new_names, legacy_codes, legacy_names, affected_count


def migrate_themes_to_master(
    *,
    apply: bool = False,
    include_legacy: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """既存 memo[gyoutai_themes] からテーママスターを作成する。

    Returns: {"new_count": int, "existing_count": int, "affected_records": int, "skipped_invalid": int}
    """
    new_names, legacy_codes, legacy_names, affected_count = _collect_names_from_records(
        include_legacy=include_legacy, db_path=db_path
    )

    # 旧 gyoutai_theme フィールド残存検知
    if legacy_codes and not include_legacy:
        log_print(
            "migrate_themes_to_master: ERROR — 旧 memo[gyoutai_theme] が "
            f"{len(legacy_codes)} 銘柄に残存しています: {legacy_codes[:10]}"
            + ("..." if len(legacy_codes) > 10 else "")
        )
        log_print(
            "  先に migrate_gyoutai_theme_to_list.py を実行してください "
            "(もしくは本スクリプトに --include-legacy を付けて旧値も救済)"
        )
        sys.exit(2)

    all_candidate_names: Set[str] = set(new_names) | set(legacy_names)

    # name バリデーション (URL 安全)
    valid: List[str] = []
    invalid: List[str] = []
    for name in sorted(all_candidate_names):
        try:
            ps.validate_theme_name(name)
            valid.append(name)
        except (ValueError, TypeError) as e:
            invalid.append(f"{name!r} ({e})")

    if invalid:
        log_print(
            f"migrate_themes_to_master: ERROR — name バリデーションで弾かれた既存値が "
            f"{len(invalid)} 件あります:"
        )
        for s in invalid:
            log_print(f"  - {s}")
        log_print(
            "  該当 record の memo[gyoutai_themes] を手動でリネームしてから再実行してください"
        )
        sys.exit(2)

    existing = {t["name"] for t in ps.list_themes(db_path=db_path)}
    to_create = [n for n in valid if n not in existing]
    already = [n for n in valid if n in existing]

    log_print(
        f"migrate_themes_to_master: 対象 record={affected_count} (excluded 含む全件中) "
        f"unique_names={len(all_candidate_names)} new={len(to_create)} existing={len(already)}"
    )
    if include_legacy and legacy_names:
        log_print(
            f"  legacy 救済モード: 旧 gyoutai_theme から {len(legacy_names)} name 抽出"
        )
    log_print(f"  作成予定: {sorted(to_create)[:20]}{'...' if len(to_create) > 20 else ''}")

    if not apply:
        log_print("migrate_themes_to_master: dry-run (--apply で実書き込み)")
        return {
            "new_count": len(to_create),
            "existing_count": len(already),
            "affected_records": affected_count,
            "skipped_invalid": 0,
        }

    created = 0
    for name in to_create:
        try:
            ps.create_theme(name, description="", db_path=db_path)
            created += 1
        except ValueError as e:
            log_print(f"  skip {name!r}: {e}")

    log_print(
        f"migrate_themes_to_master: 完了 created={created} already_existed={len(already)}"
    )
    return {
        "new_count": created,
        "existing_count": len(already),
        "affected_records": affected_count,
        "skipped_invalid": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実書き込みする (default: dry-run)")
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="旧 memo[gyoutai_theme] (str) の残存値も改行/カンマ分割でマスター対象に含める",
    )
    parser.add_argument("--db-path", default=None, help="portfolio_shelve のパス上書き (テスト用)")
    args = parser.parse_args()

    migrate_themes_to_master(
        apply=args.apply,
        include_legacy=args.include_legacy,
        db_path=args.db_path,
    )


if __name__ == "__main__":
    main()
