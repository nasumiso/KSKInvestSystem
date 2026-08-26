#!/usr/bin/env python3
"""イナゴ元 (inago_origin) を portfolio_shelve から research_shelve へ移す移行スクリプト。

イナゴ元 (情報源・きっかけ) は保有・監視状態と無関係に銘柄へ恒久的に紐づく属性なので、
物理削除され得る portfolio レコードではなく、蓄積資産である research レコードで持つ。

- portfolio の memo.inago_origin が非空のものを research の inago_origin へコピーする
- research 未登録の銘柄は最小限のレコードを作ってからコピーする
  (skip すると portfolio 側の列を消した後に参照経路が無くなるため)
- research 側に既に値が入っている場合は上書きしない (再実行しても安全)
- portfolio 側の値は消さない (読み出し側が無くなるだけ。後方互換維持の方針)

既定は dry-run。実際に書き込むには --apply を付ける。
"""

import argparse
import os
import sys
from typing import Any, Dict, List

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
import research_shelve as rs  # noqa: E402

from ks_util import log_print  # noqa: E402


def collect_migrations(records: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """移行対象を判定して一覧を返す (純粋関数、DBは読まない)。

    Returns: [{"code_s", "value", "action"}] の一覧。
             action は "copy" / "create+copy" / "skip(既存値)" のいずれか。
    """
    plans = []
    for record in records:
        code_s = record["code_s"]
        value = ((record.get("memo") or {}).get("inago_origin") or "").strip()
        if not value:
            continue
        existing = rs.get_research_record(code_s)
        if existing is None:
            action = "create+copy"
        elif (existing.get("inago_origin") or "").strip():
            action = "skip(既存値)"
        else:
            action = "copy"
        plans.append({"code_s": code_s, "value": value, "action": action})
    return plans


def apply_migrations(plans: List[Dict[str, str]]) -> int:
    """移行を実際に反映する。Returns: 書き込んだ件数。"""
    applied = 0
    for plan in plans:
        if plan["action"] == "skip(既存値)":
            continue
        code_s = plan["code_s"]
        record = rs.get_research_record(code_s)
        if record is None:
            record = rs.create_research_record(code_s, "")
        record["inago_origin"] = plan["value"]
        rs.upsert_research_record(record)
        applied += 1
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(
        description="イナゴ元を portfolio から research へ移行する"
    )
    parser.add_argument("--apply", action="store_true",
                        help="実際に書き込む (未指定は dry-run)")
    args = parser.parse_args()

    plans = collect_migrations(ps.list_records())
    for plan in plans:
        log_print(f"  {plan['code_s']}: {plan['value']!r} -> {plan['action']}")

    counts: Dict[str, int] = {}
    for plan in plans:
        counts[plan["action"]] = counts.get(plan["action"], 0) + 1
    log_print("migrate_inago_origin: 対象", f"{len(plans)}件", str(counts))

    if not args.apply:
        log_print("migrate_inago_origin: dry-run のため書き込みませんでした (--apply で反映)")
        return 0

    applied = apply_migrations(plans)
    log_print("migrate_inago_origin: 反映完了", f"{applied}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
