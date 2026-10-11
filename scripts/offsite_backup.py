"""不可逆データを運用機の外 (Google Drive) へ複製する (issue #482)。

research / portfolio の最新バックアップ世代と trade_history/ ・ trade_review/ ・ theme_news/ を
zip 1ファイルにまとめ、KS_DATA_DIR/offsite_backup_drive (運用機にだけ置く Drive フォルダへの
symlink) へ置く。日次バッチ (shintakane_cron.sh) の末尾で呼び、失敗は終了コードで知らせる。

Drive 上に持つのは直近1世代だけ。破損の巻き戻しは運用機の14世代、SSD の全損は Drive の zip、
と役割を分ける。戻し方は doc/運用手順.md の「復旧」を参照。

使い方:
    python offsite_backup.py
"""

import glob
import os
import shutil
import sys
import zipfile

from ks_util import DATA_DIR, is_dev_data_dir, log_error, log_print

DRIVE_DIRNAME = "offsite_backup_drive"
ZIP_FILENAME = "shintakane_irreplaceable.zip"
SHELVE_NAMES = ("research_shelve", "portfolio_shelve")
SHELVE_EXTENSIONS = (".dat", ".dir", ".bak")
COPY_DIRS = ("trade_history", "trade_review", "theme_news")


def _latest_generation(data_dir, name):
    """最新日付のバックアップ世代を [(実ファイル, zip 内の名前)] で返す。

    稼働中の shelve 本体ではなく世代ファイルを読む。世代は flock 下で作った整合した3点セットで、
    zip 内では日付を除いた名前にして、展開すればそのまま本体として開けるようにする。
    """
    base = os.path.join(data_dir, "stock_data", name)
    dats = sorted(glob.glob(f"{base}_[0-9][0-9][0-9][0-9][0-9][0-9].dat"))
    if not dats:
        raise FileNotFoundError(f"{name} のバックアップ世代がありません")
    stem = os.path.splitext(dats[-1])[0]
    return [
        (stem + ext, f"stock_data/{name}{ext}")
        for ext in SHELVE_EXTENSIONS
        if os.path.exists(stem + ext)
    ]


def _dir_files(data_dir, dirname):
    """dirname 配下の通常ファイルを [(実ファイル, zip 内の名前)] で返す (ロックと .DS_Store は除く)。"""
    files = []
    for root, _, names in os.walk(os.path.join(data_dir, dirname)):
        for fname in sorted(names):
            if fname == ".DS_Store" or fname.endswith(".lock"):
                continue
            path = os.path.join(root, fname)
            files.append((path, os.path.relpath(path, data_dir)))
    return files


def build_zip(zip_path, data_dir=DATA_DIR):
    """不可逆データを zip_path にまとめ、入れたファイル数を返す。"""
    targets = []
    for name in SHELVE_NAMES:
        targets += _latest_generation(data_dir, name)
    for dirname in COPY_DIRS:
        targets += _dir_files(data_dir, dirname)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in targets:
            zf.write(path, arcname)
    with zipfile.ZipFile(zip_path) as zf:
        broken = zf.testzip()
    if broken:
        raise OSError(f"zip が壊れています: {broken}")
    return len(targets)


def run(data_dir=DATA_DIR):
    """zip を作って Drive へ置く。成功 (または開発用データでスキップ) なら 0、失敗なら 1。"""
    if is_dev_data_dir():
        log_print("offsite_backup: 開発用データのためスキップ")
        return 0
    drive_dir = os.path.join(data_dir, DRIVE_DIRNAME)
    if not os.path.isdir(drive_dir):
        log_error(f"offsite_backup: 複製先がありません: {drive_dir}")
        return 1
    local_tmp = os.path.join(data_dir, ZIP_FILENAME + ".tmp")
    dest = os.path.join(drive_dir, ZIP_FILENAME)
    try:
        count = build_zip(local_tmp, data_dir)
        size = os.path.getsize(local_tmp)
        # 既存の zip を直接上書きしない。コピーの途中で落ちても前回の完成品が残るよう、
        # Drive 側の一時名へ書き切ってから切り替える (ID で参照されないので ID が変わってよい)
        shutil.copyfile(local_tmp, dest + ".tmp")
        if os.path.getsize(dest + ".tmp") != size:
            raise OSError("Drive へコピーした zip のサイズが一致しません")
        os.replace(dest + ".tmp", dest)
    except Exception as exc:
        log_error(f"offsite_backup: 失敗: {exc}")
        return 1
    finally:
        if os.path.exists(local_tmp):
            os.remove(local_tmp)
    log_print(f"offsite_backup: 完了 files={count} size={size} => {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
