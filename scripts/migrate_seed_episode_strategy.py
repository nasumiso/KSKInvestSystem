#!/usr/bin/env python3
"""ポートフォリオの trade_idea をエピソードへ一括シードする (issue #419 レイヤー1)。

戦略 (trade_idea) は銘柄レコードにしか無く、売買履歴のエピソードと結びついて
いなかった。戦略別に成績を集計するため、現在の trade_idea を**その銘柄の未設定
エピソード**へコピーして焼き付ける。

**time_horizon 足切り**: 保有日数が戦略の時間軸と矛盾するものはシードしない。
高回転銘柄は戦略が混在しており (例: trade_idea「中長期ファンダ」の銘柄が実際は
0〜7日の回転を繰り返している)、そのまま全件シードすると戦略別の平均保有日数が
壊れて戦略定義が崩壊する。**汚れた集計は空欄より有害**なので、矛盾するものは
未分類のまま残し、人がレイヤー2/3で付け直す。

冪等: 既に戦略が付いているエピソードは触らない (未設定のみ対象)。

サブコマンド:
    seed          : シード実行 (--dry-run で件数確認のみ)
    check-drift   : 遡り取込によるひもづけのズレを一覧表示
    seal          : クローズ確定分の指紋を焼き付ける (通常は fill 取込時に自動実行)
"""

import argparse
import os
import sys
from collections import Counter
from typing import Any, Dict, Optional

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


def seed(*, db_path: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """未設定エピソードに銘柄の trade_idea を足切りつきでシードする。"""
    episodes = helpers.build_fill_episodes(db_path=db_path)
    existing = ps.list_episode_strategies(db_path=db_path)
    ideas = {r["code_s"]: (r.get("memo") or {}).get("trade_idea", "")
             for r in ps.list_records(db_path=db_path)}
    horizons = {t["name"]: t.get("time_horizon", "")
                for t in ps.list_trade_ideas(db_path=db_path)}

    seeded = 0
    rejected: Counter = Counter()
    unregistered: Counter = Counter()
    no_strategy = already = 0

    for ep in episodes:
        if ep["episode_key"] in existing:
            already += 1
            continue
        idea = ideas.get(ep["code_s"], "")
        if not idea:
            no_strategy += 1
            continue
        if idea not in horizons:
            # 旧自由記述時代の値 (record 側は救済措置で保持を許されている)。
            # 集計キーには使えないので未分類のまま残し、人が付け直す。
            unregistered[idea] += 1
            continue
        hold_days = helpers.episode_hold_days(ep)
        if not ps.is_hold_days_consistent(horizons.get(idea, ""), hold_days):
            rejected[idea] += 1
            log_print(f"[seed_episode_strategy] 却下 {ep['code_s']} {ep['episode_key']} "
                      f"{idea} (保有{hold_days}日 / {horizons.get(idea, '')})")
            continue
        if not dry_run:
            ps.set_episode_strategy(
                ep["episode_key"], idea,
                source="seed",
                # 保有中は姿が未確定なので指紋を焼かない (買い増しで seq 列が伸びる
                # のが正常動作)。クローズ確定時に seal が焼く。
                fingerprint=ps.episode_fingerprint(ep["fills"]) if ep["closed"] else None,
                hold_days=hold_days,
                db_path=db_path,
            )
        seeded += 1

    log_print(
        f"[seed_episode_strategy] 完了: シード {seeded} / 却下 {sum(rejected.values())} / "
        f"戦略なし {no_strategy} / マスター未登録 {sum(unregistered.values())} / "
        f"既存 {already} (全 {len(episodes)})"
        + (" (dry-run)" if dry_run else "")
    )
    if rejected:
        log_print(f"[seed_episode_strategy] 却下内訳: {rejected.most_common()}")
    if unregistered:
        log_warning("[seed_episode_strategy] マスター未登録のためスキップ: "
                    f"{unregistered.most_common()}")
    return {
        "seeded": seeded,
        "rejected": sum(rejected.values()),
        "rejected_by_idea": dict(rejected),
        "no_strategy": no_strategy,
        "unregistered": dict(unregistered),
        "already": already,
        "dry_run": dry_run,
    }


def check_drift(*, db_path: Optional[str] = None) -> Dict[str, Any]:
    """遡り取込によるひもづけのズレを一覧表示する。

    A (orphan): 保存キーに対応する現存エピソードが無い
    B (指紋不一致): キーは生きているが中身が変わった
    """
    episodes = helpers.build_fill_episodes(db_path=db_path)
    strategies = ps.list_episode_strategies(db_path=db_path)

    drifted = [ep for ep in episodes if ep["strategy_drift"]]
    live_keys = {ep["episode_key"] for ep in episodes}
    orphans = [k for k in strategies if k not in live_keys]

    for ep in drifted:
        log_warning(f"[check_drift] 指紋不一致 {ep['code_s']} {ep['episode_key']} "
                    f"{ep['trade_idea']}")
    for key in orphans:
        log_warning(f"[check_drift] 対応先なし {key} {strategies[key].get('trade_idea')}")

    log_print(f"[check_drift] 指紋不一致 {len(drifted)} 件 / 対応先なし {len(orphans)} 件")
    return {"drifted": len(drifted), "orphans": len(orphans)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="エピソードへの戦略シードと整合性チェック (issue #419)"
    )
    parser.add_argument("command", choices=["seed", "check-drift", "seal"],
                        help="実行するサブコマンド")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB に書き込まず件数のみ表示 (seed のみ)")
    parser.add_argument("--db-path", default=None, help="書き込み先 DB パス")
    args = parser.parse_args()

    if args.command == "seed":
        seed(db_path=args.db_path, dry_run=args.dry_run)
    elif args.command == "check-drift":
        summary = check_drift(db_path=args.db_path)
        return 1 if (summary["drifted"] or summary["orphans"]) else 0
    else:
        helpers.seal_episode_fingerprints(db_path=args.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
