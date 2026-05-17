# -*- coding: utf-8 -*-
"""research_shelve の stock_name を stocks_shelve の最新値に追従させる (issue #183)。

research_shelve の全銘柄を走査し、stocks_shelve に存在しかつ stock_name が
異なるものを検出する。dry-run がデフォルトで、`--apply` を付けたときだけ
research_shelve.sync_stock_name 経由で旧名退避+新名上書きを実行する。

通常の make_stock_db 実行で TTL ベース (7日) に自動同期されるが、過去に
ズレてしまった既存銘柄を一括補正したいときに使う。
"""

import argparse

import make_stock_db
import research_shelve
from ks_util import log_print, log_warning


def collect_diffs():
    """research_shelve と stocks_shelve の stock_name 差分を一覧で返す。

    Returns:
        list[tuple[str, str, str]]: (code_s, 旧名, 新名) のリスト
    """
    try:
        research_records = research_shelve.list_research_records()
    except Exception as e:
        log_warning("research_shelve 読み込み失敗: %s" % e)
        return []
    stocks = make_stock_db.load_stock_db()
    diffs = []
    for rec in research_records:
        code_s = rec.get("code_s", "")
        if not code_s:
            continue
        stock = stocks.get(code_s) or {}
        new_name = (stock.get("stock_name") or "").strip()
        old_name = (rec.get("stock_name") or "").strip()
        if not new_name or not old_name:
            continue
        if new_name == old_name:
            continue
        diffs.append((code_s, old_name, new_name))
    return diffs


def main():
    parser = argparse.ArgumentParser(
        description="research_shelve.stock_name を stocks_shelve に追従させる (dry-run default)"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="実書き込みを行う (デフォルトはdry-run)",
    )
    args = parser.parse_args()

    diffs = collect_diffs()
    if not diffs:
        log_print("差分なし")
        return

    log_print("差分 %d 件:" % len(diffs))
    for code_s, old, new in diffs:
        log_print("  %s: %s → %s" % (code_s, old, new))

    if not args.apply:
        log_print("(dry-run) --apply で実書き込み")
        return

    # 実書き込みは research_shelve.sync_stock_name に委譲 (flock 区間内で R-M-W)。
    applied = 0
    for code_s, _old, new in diffs:
        try:
            returned_old = research_shelve.sync_stock_name(code_s, new)
        except Exception as e:
            log_warning("[sync] %s: 同期失敗: %s" % (code_s, e))
            continue
        if returned_old is None:
            log_print("[sync] %s: 既に最新 (no-op)" % code_s)
        else:
            log_print("[sync] %s: %s → %s" % (code_s, returned_old, new))
            applied += 1
    log_print("%d 件を更新しました (対象 %d 件中)" % (applied, len(diffs)))


if __name__ == "__main__":
    main()
