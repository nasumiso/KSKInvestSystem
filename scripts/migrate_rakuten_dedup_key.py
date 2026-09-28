#!/usr/bin/env python3
"""楽天 fill の dedup_key を現行形式へ移行し、二重取込された fill を統合する。

9b76d73 (2026-08-31) で楽天CSVの dedup_key から受渡金額を外した (amount=0 固定、
occurrence の素材からも除外) が、既存 fill のキーは旧形式のまま残した。そのため
以降の取込 (09-14, 09-22) では同じ約定が別キーになり、fill が二重に登録された。

処理:
    - 楽天 fill のうち旧形式キーのものについて、現行形式のキーを再計算する。
      売買区分の生文字列は保存していないが、楽天では (取引区分, side) から一意に
      決まる (_RAKUTEN_BAIBAI)。occurrence は同一約定本体の中で seq 順に振り直す。
    - 同じ現行キーを持つ後発 fill があれば二重取込とみなし、補完可能な値を旧 fill へ
      移してから後発 fill を削除する。旧 fill を残すのは、エピソードキーが先頭 seq
      (fill_episode_key) なので、メモ・戦略ひもづけが旧 seq に乗っているため。
    - 後発 fill の無い旧 fill はキーの書き換えだけ行う (今後の再取込で重複させない)。

冪等: 現行形式のキーはそのまま。dry-run (既定) で件数と削除対象を確認できる。

使い方:
    cd scripts && python migrate_rakuten_dedup_key.py          # dry-run
    cd scripts && python migrate_rakuten_dedup_key.py --apply  # 実行
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
from ks_util import log_print, log_warning  # noqa: E402

# 楽天CSVの (取引区分, side) → 売買区分の生文字列 (import_rakuten_fills.parse_fill_row の逆)
_RAKUTEN_BAIBAI = {
    ("現物", "buy"): "買付",
    ("現物", "sell"): "売付",
    ("現物(単元未満)", "buy"): "買付",
    ("現物(単元未満)", "sell"): "売付",
    ("信用新規", "buy"): "買建",
    ("信用返済", "sell"): "売埋",
    ("現引", "buy"): "",
}


def _current_key(fill: Dict[str, Any], baibai: str, occurrence: int) -> str:
    """import_rakuten_fills.import_csv_to_fills と同じ素材で現行形式のキーを作る。"""
    return ps.make_dedup_key(
        trade_date=fill["trade_date"], code_s=fill["code_s"],
        trade_kind=fill["trade_kind"], baibai_kubun=baibai,
        qty=fill["qty"], price=fill["price"], amount=0, occurrence=occurrence,
    )


def plan_migration(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """移行内容を計算する (DB非更新)。

    Returns: {"rekey": [(old_fill, new_key)], "merge": [(old_fill, dup_fill)],
              "unknown": [fill]}
    """
    rakuten = [f for f in fills if (f.get("broker") or "楽天") == "楽天"]
    by_key = {f["dedup_key"]: f for f in rakuten}

    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    unknown = []
    for f in sorted(rakuten, key=lambda f: (f["code_s"], f["seq"])):
        baibai = _RAKUTEN_BAIBAI.get((f.get("trade_kind"), f["side"]))
        if baibai is None:
            unknown.append(f)
            continue
        body = (f["code_s"], f["trade_date"], f["trade_kind"], baibai, f["qty"], f["price"])
        groups.setdefault(body, []).append(f)

    rekey, merge = [], []
    for body, rows in groups.items():
        baibai = body[3]
        current = {_current_key(rows[0], baibai, i) for i in range(len(rows))}
        old_rows = [f for f in rows if f["dedup_key"] not in current]
        for occurrence, f in enumerate(old_rows):
            new_key = _current_key(f, baibai, occurrence)
            dup = by_key.get(new_key)
            if dup is not None:
                merge.append((f, dup))
            else:
                rekey.append((f, new_key))
    return {"rekey": rekey, "merge": merge, "unknown": unknown}


def apply_migration(plan: Dict[str, Any], *, db_path: Optional[str] = None) -> None:
    """plan_migration の結果を DB に反映する。"""
    path = ps._resolve_db_path(db_path)
    with ps._flock(db_path):
        with ps.ShelveDB(path) as db:
            for old, new_key in plan["rekey"]:
                key = ps._fill_key(old["code_s"], old["seq"])
                value = db[key]
                value["dedup_key"] = new_key
                db[key] = value
            for old, dup in plan["merge"]:
                key = ps._fill_key(old["code_s"], old["seq"])
                value = db[key]
                ps._backfill_fill(value, dup)
                value["dedup_key"] = dup["dedup_key"]
                db[key] = value
                del db[ps._fill_key(dup["code_s"], dup["seq"])]


def _referenced_episode_keys(db_path: Optional[str]) -> set:
    """メモ・戦略がひもづいているエピソードキー。削除する seq が先頭になっていないか確認用。"""
    keys = set(ps.list_fill_memos(db_path=db_path).keys())
    path = ps._resolve_db_path(db_path)
    with ps.ShelveDB(path) as db:
        for key in db.keys():
            if key.startswith(ps.KEY_EPISODE_STRATEGY_PREFIX):
                keys.add(key[len(ps.KEY_EPISODE_STRATEGY_PREFIX):])
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="DB に反映する (既定は dry-run)")
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()

    plan = plan_migration(ps.list_fills(db_path=args.db_path))
    codes = sorted({old["code_s"] for old, _ in plan["merge"]})
    log_print(f"キー書き換え: {len(plan['rekey'])} 件 / 二重取込の統合: {len(plan['merge'])} 件 "
              f"({len(codes)} 銘柄) / 区分不明で対象外: {len(plan['unknown'])} 件")
    for f in plan["unknown"]:
        log_warning(f"  区分不明: {f['code_s']} seq={f['seq']} {f.get('trade_kind')} {f['side']}")

    deleted = {(dup["code_s"], dup["seq"]) for _, dup in plan["merge"]}
    for episode_key in sorted(_referenced_episode_keys(args.db_path)):
        code_s, _, first_seq = episode_key.split("|")
        if (code_s, int(first_seq)) in deleted:
            log_warning(f"  削除する fill を先頭にするエピソードにメモ/戦略あり: {episode_key}")

    if not args.apply:
        log_print("dry-run のため DB は更新していません (--apply で反映)")
        return 0
    apply_migration(plan, db_path=args.db_path)
    log_print("反映しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
