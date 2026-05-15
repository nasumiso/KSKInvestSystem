"""kessan_comments の同 (code_s, kessanbi) で複数 quarter エントリ併存を解消する (issue #207).

cron で kessan_quarter 取得失敗 → quarter=0 で `upsert_kessan_pts_change` が新規 append され、
手動メモ済みの quarter=4 エントリと別エントリとして残る事故が過去に発生。

本スクリプトは「メモなし & post_price_changes が pts のみ」のエントリを「重複空エントリ」と
判定し、PTS 値を残す側 (memo あり / quarter 大優先) にマージしてから削除する。

履歴データ破壊を避けるため、削除候補が `1d` / `5d` など pts 以外の price changes や
旧形式 `post_price_change` (str) を持つ場合は **削除対象外** (codex review 反映)。

使い方:
    python cleanup_kessan_dup_entries.py --dry-run             # デフォルト、レポートのみ
    python cleanup_kessan_dup_entries.py --apply               # 実書き込み (バックアップ取得)
    python cleanup_kessan_dup_entries.py --apply --code 7717   # 特定銘柄のみ
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を import path に追加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_shelve as rs
from research_shelve import _flock
from ks_util import log_print, log_warning


def _is_empty_pts_only_entry(entry: Dict[str, Any]) -> bool:
    """エントリが「メモなし & pts 以外の価格反応データなし」= 削除候補かを判定する。

    削除対象条件 (全て満たす):
      - pre_outlook / post_comment / pre_expectation すべて空
      - held_before_kessan / held_after_kessan / kessan_matagi すべて False
      - 旧形式 post_price_change が空
      - post_price_changes は空 OR キーが {"pts"} のサブセット (1d/3d/5d 等を持たない)
    """
    if (entry.get("pre_outlook") or "").strip():
        return False
    if (entry.get("post_comment") or "").strip():
        return False
    if (entry.get("pre_expectation") or "").strip():
        return False
    if entry.get("held_before_kessan"):
        return False
    if entry.get("held_after_kessan"):
        return False
    if entry.get("kessan_matagi"):
        return False
    legacy_pc = entry.get("post_price_change")
    if legacy_pc not in ("", None):
        return False
    ppc = entry.get("post_price_changes") or {}
    # pts 以外のキーで「非空の値」が入っていたら触らない (履歴データ保護)。
    # 空文字値の 1d/5d キーは PTS upsert 時に normalize で埋まるだけのプレースホルダなので無視。
    for k, v in ppc.items():
        if k == "pts":
            continue
        if (v or "").strip():
            return False
    return True


def _has_memo(entry: Dict[str, Any]) -> bool:
    """メモあり判定 (helpers.get_market_kessan_data の has_comment と同基準)。"""
    return bool(
        (entry.get("pre_outlook") or "").strip()
        or (entry.get("post_comment") or "").strip()
        or (entry.get("pre_expectation") or "").strip()
    )


def select_winner_and_dups(
    entries: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """同 (code_s, kessanbi) のエントリ群から残す側 1 件と削除候補リストを返す.

    戻り値:
        (winner, dups)
        - winner: 残すエントリ (None なら全件保持 = 削除候補なし)
        - dups: 削除するエントリのリスト (winner にマージ済み想定)

    ロジック:
      1. 削除候補 (`_is_empty_pts_only_entry`) と保持対象を分ける
      2. 削除候補が無ければ何もしない (winner=None, dups=[])
      3. 保持対象がある: 保持側を winner、削除候補を dups
         winner 選定: (1) memo あり優先 → (2) quarter 大優先 → (3) 最初のもの
      4. 保持対象が無い (全部 pts-only): どれか 1 件を winner に残し、残りを dups
    """
    if not entries:
        return None, []

    keep: List[Dict[str, Any]] = []
    dups: List[Dict[str, Any]] = []
    for e in entries:
        if _is_empty_pts_only_entry(e):
            dups.append(e)
        else:
            keep.append(e)

    if not dups:
        return None, []

    if keep:
        # winner = (memo あり ↑ → quarter 大 ↑ → 安定順)
        keep_sorted = sorted(
            enumerate(keep),
            key=lambda iv: (
                -1 if _has_memo(iv[1]) else 0,  # memo あり (-1) を先頭
                -int(iv[1].get("quarter", 0) or 0),  # quarter 大 を先頭
                iv[0],  # 安定 (元順)
            ),
        )
        winner = keep_sorted[0][1]
        # keep の残りはそのまま保持 (winner 以外は触らない)
        return winner, dups
    else:
        # 全部 pts-only: quarter 大の方を残す (任意性ありだが安定的選択)
        dups_sorted = sorted(
            enumerate(dups),
            key=lambda iv: (-int(iv[1].get("quarter", 0) or 0), iv[0]),
        )
        winner = dups_sorted[0][1]
        rest_dups = [iv[1] for iv in dups_sorted[1:]]
        return winner, rest_dups


def merge_pts_into_winner(
    winner: Dict[str, Any], dups: List[Dict[str, Any]]
) -> Optional[str]:
    """dups から PTS 値を抽出し、winner に既存 PTS が無ければマージする.

    Returns:
        実際にマージした PTS 値 (winner に既存 PTS があれば既存値を返す)、無ければ None
    """
    winner_ppc = dict(winner.get("post_price_changes") or {})
    winner_pts = (winner_ppc.get("pts") or "").strip()
    if winner_pts:
        return winner_pts  # winner 優先、何もしない (既存値を返すだけ)
    for d in dups:
        d_ppc = d.get("post_price_changes") or {}
        d_pts = (d_ppc.get("pts") or "").strip()
        if d_pts:
            winner_ppc["pts"] = d_pts
            winner["post_price_changes"] = winner_ppc
            return d_pts
    return None


def cleanup_record(
    record: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, Dict[str, Any], List[Dict[str, Any]], Optional[str]]]]:
    """1 レコードの kessan_comments を走査し、(新コメント配列, 変更ログ) を返す.

    変更ログ: list of (kessanbi, winner, dups, merged_pts)
    """
    comments = list(record.get("kessan_comments") or [])
    if len(comments) <= 1:
        return comments, []

    # (code_s, kessanbi) でグループ化 (code_s は record 全体で一意なので kessanbi だけで OK)
    groups: Dict[str, List[int]] = {}
    for i, e in enumerate(comments):
        k = e.get("kessanbi", "")
        if not k:
            continue
        groups.setdefault(k, []).append(i)

    changes: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]], Optional[str]]] = []
    delete_indices: set = set()

    for kessanbi, indices in groups.items():
        if len(indices) <= 1:
            continue
        group_entries = [comments[i] for i in indices]
        winner, dups = select_winner_and_dups(group_entries)
        if not dups:
            continue
        merged_pts = merge_pts_into_winner(winner, dups)
        # winner はそのまま (オブジェクト参照を update 済み)、dups の index を削除対象に
        for d in dups:
            for i in indices:
                if comments[i] is d:
                    delete_indices.add(i)
                    break
        changes.append((kessanbi, winner, dups, merged_pts))

    if not delete_indices:
        return comments, []

    new_comments = [e for i, e in enumerate(comments) if i not in delete_indices]
    return new_comments, changes


def cleanup_db(
    *,
    db_path: Optional[str] = None,
    dry_run: bool = True,
    target_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """research_shelve 全レコード (or target_codes のみ) を走査して重複エントリを統合する.

    Args:
        db_path: 書き込み先 (None なら本番 RESEARCH_SHELVE)
        dry_run: True ならレポートのみ、False で実書き込み (バックアップ取得)
        target_codes: 対象銘柄リスト (None なら全件)

    Returns:
        サマリ dict (processed, modified, deleted_entries, errors)
    """
    if not dry_run:
        try:
            backup_paths = rs.backup_research_db(db_path=db_path)
            log_print(f"[cleanup] バックアップ(実行前): {backup_paths}")
        except Exception as e:
            log_warning(f"[cleanup] バックアップ失敗(継続): {e}")

    if dry_run:
        log_print("[cleanup] dry_run=True: DB に書き込まず検証のみ実行")

    processed = 0
    modified = 0
    deleted_entries = 0
    errors = 0

    with _flock(db_path):
        # 全レコードを走査
        all_records = rs.list_research_records(db_path=db_path)
        for rec in all_records:
            code_s = rec.get("code_s", "")
            if not code_s:
                continue
            if target_codes and code_s not in target_codes:
                continue
            processed += 1
            try:
                new_comments, changes = cleanup_record(rec)
                if not changes:
                    continue
                modified += 1
                for kessanbi, winner, dups, merged_pts in changes:
                    deleted_entries += len(dups)
                    log_print(
                        f"[cleanup] {code_s} {kessanbi}: "
                        f"{len(dups) + 1} entries → 1 winner "
                        f"(kept q={winner.get('quarter', 0)}, "
                        f"merged pts={merged_pts or '(none)'})"
                    )
                if not dry_run:
                    rec["kessan_comments"] = new_comments
                    rs.upsert_research_record(rec, db_path=db_path)
            except Exception as e:
                errors += 1
                log_warning(f"[cleanup] {code_s} 失敗: {e}")

    log_print(
        f"[cleanup] 完了: processed={processed}, modified={modified}, "
        f"deleted_entries={deleted_entries}, errors={errors}"
    )
    return {
        "processed": processed,
        "modified": modified,
        "deleted_entries": deleted_entries,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "kessan_comments の同 (code_s, kessanbi) 重複空エントリを統合する "
            "(issue #207)"
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="実 DB に書き込む (デフォルトは dry-run)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="(デフォルト) DB に書き込まずレポートのみ。明示指定用",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="書き込み先 DB パス (デフォルト: 本番 RESEARCH_SHELVE)",
    )
    parser.add_argument(
        "--code", default=None,
        help="対象銘柄コードを限定 (カンマ区切り、例: --code 7717,5032)",
    )
    args = parser.parse_args()

    target_codes: Optional[List[str]] = None
    if args.code:
        target_codes = [c.strip() for c in args.code.split(",") if c.strip()]

    summary = cleanup_db(
        db_path=args.db_path,
        dry_run=not args.apply,
        target_codes=target_codes,
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
