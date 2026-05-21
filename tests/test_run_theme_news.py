"""run_theme_news.py の重複ガード / claude -p 起動の単体テスト (issue #165)"""

import subprocess
import sys
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
    # rtn.datetime は datetime クラス本体。today() だけ固定し、now() 等は本物を使う
    class _FakeDatetime(datetime):
        @classmethod
        def today(cls):
            return fixed_date
    monkeypatch.setattr(rtn, "datetime", _FakeDatetime)
    return history_dir


@pytest.mark.parametrize("force,web_trigger,marker_exists,expected_skip", [
    # 手動運用モード (issue #165 後): 重複ガードは webapp 側で .running を見る方式に移行したため、
    # ラッパー単体ではどのフラグでも skip しない (将来 --cron 復活時に拡張)
    (False, False, True, False),
    (False, False, False, False),
    (True, False, True, False),
    (False, True, False, False),
])
def test_should_skip_matrix(tmp_history, force, web_trigger, marker_exists, expected_skip):
    """重複ガード方針: ラッパー単体では skip しない (手動運用のため常時起動)。"""
    if marker_exists:
        rtn._today_done_marker_path().touch()
    args = SimpleNamespace(force=force, web_trigger=web_trigger)
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

    # ケース4: skill が期待と違う日付でファイルを書いた → 不一致検知 + rc=1
    # SKILL.md 1-0/7 の価格日規約違反を検知できることを担保する。
    history_path.unlink(missing_ok=True)
    wrong_path = history_path.parent / "2026-05-22.md"  # 期待は 2026-05-21.md
    def fake_run_wrong_date(*args, **kwargs):
        wrong_path.write_text("# wrong date\n", encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with patch.object(rtn.subprocess, "run", side_effect=fake_run_wrong_date):
        assert rtn._run_claude_skill() == 1
    assert not marker_path.exists()
    # 検知ヘルパーが直近書込みファイルを掴むこと
    detected = rtn._detect_recent_history(history_path)
    assert detected == wrong_path


def test_main_creates_history_dir_for_clean_checkout(tmp_path, monkeypatch):
    """初回 (HISTORY_DIR が存在しない) 起動でも .running 作成が落ちないことを担保する。

    .gitignore で history/ が Git 管理されていないため、クリーン checkout 直後に
    /market の手動実行ボタンが押された場合 HISTORY_DIR が無い状態で touch される。
    rtn.main() が mkdir(parents=True, exist_ok=True) で先に作る必要がある。
    """
    missing_dir = tmp_path / "history"  # まだ作らない
    assert not missing_dir.exists()
    monkeypatch.setattr(rtn, "HISTORY_DIR", missing_dir)
    fixed_date = datetime(2026, 5, 21, 19, 0, 0)
    monkeypatch.setattr(rtn, "get_price_day", lambda _now: fixed_date.date())
    class _FakeDatetime(datetime):
        @classmethod
        def today(cls):
            return fixed_date
    monkeypatch.setattr(rtn, "datetime", _FakeDatetime)
    # claude -p 起動はモック (rc=0 で history も書く)
    def fake_run(*args, **kwargs):
        (missing_dir / "2026-05-21.md").write_text("# 本文", encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(rtn.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_theme_news.py", "--web-trigger"])

    rc = rtn.main()
    assert rc == 0
    assert missing_dir.exists()
    assert (missing_dir / "2026-05-21.md.done").exists()


def test_usage_meta_saved_from_claude_json_output(tmp_history):
    """claude -p --output-format json の result/usage を meta.json に保存することを確認。

    Anthropic CLI が --output-format=json で末尾に出力する単一 result オブジェクトを
    パースして、estimated_cost_usd 込みで .md.meta.json に書ける挙動の単体テスト。
    """
    import json
    history_path = rtn._today_history_path()
    meta_path = rtn._today_meta_path()
    fake_result_json = json.dumps({
        "type": "result",
        "subtype": "success",
        "total_cost_usd": 0.1234,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 500},
            "server_tool_use": {"web_search_requests": 0},
        },
    })

    def fake_run(*args, **kwargs):
        history_path.write_text("# テスト\n本文", encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode=0, stdout=fake_result_json, stderr="")

    with patch.object(rtn.subprocess, "run", side_effect=fake_run):
        assert rtn._run_claude_skill() == 0
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["estimated_cost_usd"] == 0.1234
    assert meta["usage"]["input_tokens"] == 100
