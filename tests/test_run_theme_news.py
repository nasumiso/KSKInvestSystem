"""run_theme_news.py の重複ガード / claude -p 起動の単体テスト (issue #165)"""

import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run_theme_news as rtn


@pytest.fixture
def tmp_history(tmp_path, monkeypatch):
    """HISTORY_DIR を tmp_path に差し替え、固定日付 (2026-05-21) で動かす。"""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(rtn, "HISTORY_DIR", history_dir)
    # get_price_day を固定 (シナリオを日付揺れから独立させる)
    fixed_date = datetime(2026, 5, 21, 19, 0, 0)
    monkeypatch.setattr(rtn, "get_price_day", lambda _now: fixed_date.date())
    monkeypatch.setattr(rtn, "datetime", SimpleNamespace(today=lambda: fixed_date))
    return history_dir


@pytest.mark.parametrize("force,cron,marker_exists,expected_skip", [
    (False, True, True, True),    # --cron + マーカー有 → スキップ
    (False, True, False, False),  # --cron + マーカー無 → 実行
    (False, False, True, False),  # 手動 (--cron 無) → 履歴問わず実行
    (True, True, True, False),    # --force あり → ガード無視
])
def test_should_skip_matrix(tmp_history, force, cron, marker_exists, expected_skip):
    """完了マーカーベースの重複ガードが想定どおり動くこと。"""
    if marker_exists:
        rtn._today_done_marker_path().touch()
    args = SimpleNamespace(force=force, cron=cron)
    assert rtn._should_skip(args) is expected_skip


def test_run_claude_skill_creates_marker_on_success(tmp_history):
    """claude -p 成功 + history ファイル生成時のみ完了マーカーが作られる。

    途中失敗時 (claude rc!=0 / history 欠損 / 空ファイル) はマーカーを作らず
    次回 cron で再実行されることを担保する。
    """
    history_path = rtn._today_history_path()
    marker_path = rtn._today_done_marker_path()

    # ケース1: 正常終了 + history 書込み → マーカー作成 + rc=0
    def fake_run_ok(*args, **kwargs):
        history_path.write_text("# 2026-05-21\n本文", encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with patch.object(rtn.subprocess, "run", side_effect=fake_run_ok):
        assert rtn._run_claude_skill() == 0
    assert marker_path.exists()

    # ケース2: claude rc=1 → マーカー作らず、rc 伝播
    marker_path.unlink()
    history_path.unlink()
    def fake_run_fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=2, stdout="", stderr="boom")

    with patch.object(rtn.subprocess, "run", side_effect=fake_run_fail):
        assert rtn._run_claude_skill() == 2
    assert not marker_path.exists()

    # ケース3: rc=0 だが history 空ファイル → マーカー作らず rc=1
    def fake_run_empty(*args, **kwargs):
        history_path.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with patch.object(rtn.subprocess, "run", side_effect=fake_run_empty):
        assert rtn._run_claude_skill() == 1
    assert not marker_path.exists()
