#!/usr/bin/env python3
"""
shelve DBのデフラグメンテーションスクリプト。

dbm.dumb はレコード更新時にファイル内に隙間（断片化）を残すため、
長期運用で .dat ファイルが肥大化する。
このスクリプトは全データを取り出し、DBファイルを再構築することで
断片化を解消する。

使い方:
    cd scripts && python defrag_shelve.py

処理フロー:
    1. 現在のDBから全データをエクスポート（dictに読み込み）
    2. 既存DBファイルをバックアップ（.defrag_backup にリネーム）
    3. 新規DBに全データを書き戻し
    4. サイズ比較と整合性チェック
    5. 問題なければバックアップを削除（--keep-backup で保持可能）
"""

import os
import sys
import shutil
import argparse
import time

# scriptsディレクトリからの相対パスでインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_shelve import ShelveDB, STOCKS_SHELVE, MARKET_SHELVE, SECTOR_SHELVE


def get_db_size(db_path):
    """DBファイル群の合計サイズ（バイト）を返す"""
    total = 0
    for ext in [".dat", ".dir", ".bak"]:
        filepath = db_path + ext
        if os.path.exists(filepath):
            total += os.path.getsize(filepath)
    return total


def format_size(size_bytes):
    """バイト数を読みやすい文字列に変換"""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 ** 3):.1f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def defrag_db(db_path, keep_backup=False):
    """
    指定されたshelve DBをデフラグする。

    Args:
        db_path: DBパス（拡張子なし）
        keep_backup: Trueならバックアップを残す

    Returns:
        (成功したか, 圧縮前サイズ, 圧縮後サイズ)
    """
    db_name = os.path.basename(db_path)

    # 存在チェック
    if not os.path.exists(db_path + ".dat"):
        print(f"  スキップ: {db_name} （ファイルが存在しない）")
        return False, 0, 0

    size_before = get_db_size(db_path)
    print(f"  デフラグ開始: {db_name}")
    print(f"  圧縮前サイズ: {format_size(size_before)}")

    # 1. 全データ読み込み
    print(f"  データ読み込み中...")
    start = time.time()
    with ShelveDB(db_path) as db:
        all_data = db.export_to_dict()
        record_count = len(all_data)
    elapsed = time.time() - start
    print(f"  {record_count} 件のレコードを読み込み ({elapsed:.1f}秒)")

    # 2. バックアップ作成（リネーム）
    backup_path = db_path + ".defrag_backup"
    print(f"  バックアップ作成中...")
    for ext in [".dat", ".dir", ".bak"]:
        src = db_path + ext
        dst = backup_path + ext
        if os.path.exists(src):
            shutil.move(src, dst)

    # 3. 新規DBに書き戻し
    print(f"  新規DBに書き込み中...")
    start = time.time()
    try:
        with ShelveDB(db_path) as db:
            db.import_from_dict(all_data)
    except Exception as e:
        # 失敗時はバックアップから復元
        print(f"  エラー: {e}")
        print(f"  バックアップから復元中...")
        for ext in [".dat", ".dir", ".bak"]:
            src = backup_path + ext
            dst = db_path + ext
            if os.path.exists(src):
                # 壊れた新規ファイルを削除
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)
        return False, size_before, size_before
    elapsed = time.time() - start
    print(f"  書き込み完了 ({elapsed:.1f}秒)")

    # 4. 整合性チェック
    print(f"  整合性チェック中...")
    with ShelveDB(db_path) as db:
        new_count = len(db.keys())
        if new_count != record_count:
            print(f"  エラー: レコード数不一致 (元: {record_count}, 新: {new_count})")
            print(f"  バックアップから復元中...")
            for ext in [".dat", ".dir", ".bak"]:
                src = backup_path + ext
                dst = db_path + ext
                if os.path.exists(dst):
                    os.remove(dst)
                if os.path.exists(src):
                    shutil.move(src, dst)
            return False, size_before, size_before

        # サンプルレコードで中身を確認（最初の5件）
        sample_keys = list(all_data.keys())[:5]
        for key in sample_keys:
            new_val = db.get(key)
            if new_val is None:
                print(f"  エラー: キー '{key}' が新DBに存在しない")
                return False, size_before, size_before

    size_after = get_db_size(db_path)
    ratio = (1 - size_after / size_before) * 100 if size_before > 0 else 0

    print(f"  圧縮後サイズ: {format_size(size_after)}")
    print(f"  削減: {format_size(size_before - size_after)} ({ratio:.1f}%)")
    print(f"  レコード数: {record_count} 件 (変化なし)")

    # 5. バックアップ処理
    if keep_backup:
        print(f"  バックアップ保持: {backup_path}.*")
    else:
        for ext in [".dat", ".dir", ".bak"]:
            f = backup_path + ext
            if os.path.exists(f):
                os.remove(f)
        print(f"  バックアップ削除完了")

    return True, size_before, size_after


def main():
    parser = argparse.ArgumentParser(
        description="shelve DBのデフラグメンテーション"
    )
    parser.add_argument(
        "--keep-backup",
        action="store_true",
        help="デフラグ後もバックアップファイルを保持する",
    )
    parser.add_argument(
        "--target",
        choices=["stocks", "market", "sector", "all"],
        default="stocks",
        help="デフラグ対象のDB (デフォルト: stocks)",
    )
    args = parser.parse_args()

    targets = {
        "stocks": ("株式DB", STOCKS_SHELVE),
        "market": ("市場DB", MARKET_SHELVE),
        "sector": ("セクターDB", SECTOR_SHELVE),
    }

    if args.target == "all":
        db_list = list(targets.items())
    else:
        db_list = [(args.target, targets[args.target])]

    print("=" * 50)
    print("shelve DB デフラグメンテーション")
    print("=" * 50)

    total_before = 0
    total_after = 0

    for key, (label, db_path) in db_list:
        print(f"\n[{label}] {db_path}")
        success, before, after = defrag_db(db_path, args.keep_backup)
        if success:
            total_before += before
            total_after += after

    if total_before > 0:
        print(f"\n{'=' * 50}")
        print(f"合計: {format_size(total_before)} → {format_size(total_after)}")
        ratio = (1 - total_after / total_before) * 100
        print(f"削減: {format_size(total_before - total_after)} ({ratio:.1f}%)")
    print()


if __name__ == "__main__":
    main()
