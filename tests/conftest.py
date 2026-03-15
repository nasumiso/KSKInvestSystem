"""
テスト共通設定・フィクスチャ

- KS_DATA_DIR 環境変数を補完（シェルプロファイルから取得）
- sys.path に scripts/ を追加（各モジュールの import を可能にする）
- ロガーのファイル出力を抑制（テスト時にログファイルを生成しない）
"""

import sys
import os
import logging
import subprocess

# KS_DATA_DIR が未設定の場合、ログインシェルから取得して設定する
# （Claude Code等、.zshrcが読み込まれない環境への対応）
if not os.environ.get("KS_DATA_DIR"):
    try:
        result = subprocess.run(
            ["zsh", "-i", "-c", "echo $KS_DATA_DIR"],
            capture_output=True, text=True, timeout=5,
        )
        val = result.stdout.strip()
        if val and os.path.isdir(val):
            os.environ["KS_DATA_DIR"] = val
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

# scripts/ ディレクトリを import パスに追加
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS_DIR))

import pytest


@pytest.fixture(autouse=True)
def _suppress_file_logging():
    """テスト実行中はファイルハンドラを無効化する"""
    # ks_util のロガーからファイルハンドラを一時的に除去
    try:
        import ks_util

        logger = ks_util.get_logger()
        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        for fh in file_handlers:
            logger.removeHandler(fh)
        yield
        for fh in file_handlers:
            logger.addHandler(fh)
    except Exception:
        yield
