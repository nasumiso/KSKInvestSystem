#!/usr/bin/env python3
"""アクションログの振り返りメモ (review_memo) を fill エピソード側へ移行する (issue #387 Phase2)。

振り返りメモは判断の記録として売買履歴 (fill=真実源) タブへ一本化した。
アクションログの売却ログ (または1保遷移ログ) が持つ review_memo を、対応する
fill 建玉ラウンド (エピソード) の fill_memo レイヤーへ移す。

対応付け:
    - review_memo を持つ action_log を列挙 (通常は売却ログ)。
    - その銘柄の fill エピソードのうち、クローズ日 (close_date) が action_log の
      売却日と近いものを対応先とする。決算返済ラグ等で数日ずれるため許容差は
      ±MATCH_TOLERANCE_DAYS 日。最も近い候補が1つに絞れるものだけ自動移行する。
    - 複数候補 or 候補なしは手動判断が必要として skipped に積み、警告表示する。

冪等: fill_memo に既にメモがある場合は上書きしない (既存優先)。dry-run で対応表確認可。
"""

import argparse
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
from webapp import helpers  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# 売却日と fill エピソードのクローズ日のずれ許容 (決算返済ラグ等)
MATCH_TOLERANCE_DAYS = 3


def _to_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _match_episode(
    memo_log: Dict[str, Any],
    episodes: List[Dict[str, Any]],
) -> tuple[Optional[Dict[str, Any]], str]:
    """review_memo を持つ action_log に対応する fill エピソードを1つ選ぶ。

    Returns: (episode or None, 理由文字列)
    """
    code_s = memo_log["code_s"]
    sell_date = _to_date(memo_log.get("timestamp", ""))
    if sell_date is None:
        return None, "売却日が不正"

    code_eps = [e for e in episodes if e["code_s"] == code_s and e["closed"]]
    if not code_eps:
        return None, f"{code_s} にクローズ済みエピソードが無い"

    # クローズ日が売却日と一致 (±1日許容) するエピソードを候補にする
    def close_dist(ep):
        cd = _to_date(ep.get("close_date") or "")
        if cd is None:
            return None
        return abs((cd - sell_date).days)

    scored = [(close_dist(e), e) for e in code_eps]
    scored = [(d, e) for d, e in scored if d is not None and d <= MATCH_TOLERANCE_DAYS]
    if not scored:
        return None, f"{code_s} 売却日 {sell_date} に一致するクローズ日が無い"
    scored.sort(key=lambda x: x[0])
    best_dist = scored[0][0]
    best = [e for d, e in scored if d == best_dist]
    if len(best) > 1:
        return None, f"{code_s} 売却日 {sell_date} に候補が複数 ({len(best)})"
    return best[0], f"close_date={best[0]['close_date']} (差{best_dist}日)"


def migrate(*, db_path: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    logs = ps.list_action_logs(db_path=db_path)
    memo_logs = [l for l in logs if (l.get("review_memo") or "").strip()]
    episodes = helpers.build_fill_episodes(db_path=db_path)
    existing = ps.list_fill_memos(db_path=db_path)

    log_print(f"[migrate_review_memo] 対象 review_memo: {len(memo_logs)} 件")

    migrated: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    already: List[Dict[str, Any]] = []

    for ml in memo_logs:
        ep, reason = _match_episode(ml, episodes)
        memo = (ml.get("review_memo") or "").strip()
        if ep is None:
            skipped.append({"code_s": ml["code_s"], "reason": reason, "memo": memo})
            log_warning(f"[migrate_review_memo] SKIP {ml['code_s']}: {reason}")
            continue
        key = ep["episode_key"]
        if existing.get(key):
            already.append({"code_s": ml["code_s"], "episode_key": key})
            log_print(f"[migrate_review_memo] 既存あり (上書きしない) {ml['code_s']} {key}")
            continue
        log_print(f"[migrate_review_memo] {ml['code_s']} → {key} : {reason}")
        log_print(f"    memo: {memo[:60]}")
        if not dry_run:
            ps.set_fill_memo(key, memo, db_path=db_path)
        migrated.append({"code_s": ml["code_s"], "episode_key": key, "memo": memo})

    log_print(
        f"[migrate_review_memo] 完了: 移行 {len(migrated)} / "
        f"既存 {len(already)} / スキップ {len(skipped)}"
        + (" (dry-run)" if dry_run else "")
    )
    return {"migrated": migrated, "skipped": skipped, "already": already, "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="action_log の review_memo を fill エピソードへ移行する (issue #387 Phase2)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="DB に書き込まず対応表のみ表示")
    parser.add_argument("--db-path", default=None, help="書き込み先 DB パス")
    args = parser.parse_args()
    summary = migrate(db_path=args.db_path, dry_run=args.dry_run)
    return 1 if summary["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
