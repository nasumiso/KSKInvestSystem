"""
テスト共通設定・フィクスチャ

- sys.path に scripts/ を追加（各モジュールの import を可能にする）
- ロガーのファイル出力を抑制（テスト時にログファイルを生成しない）
"""

import sys
import os
import logging

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
