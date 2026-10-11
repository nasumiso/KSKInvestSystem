"""run_sentiment.py の保存・実行中の印・結果の読み出しの単体テスト (issue #501)"""

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

import run_sentiment as rs

_RESULT = "投稿を読みました。\n\n## 掲示板の感情分析: テスト (3496)  投稿 42件 (10/1 〜 10/9)\n\n### 掲示板の温度\n+1 (やや強気) 相当  ※平均 +0.30\n"


_POST = {"part": 1, "post_date": "2026/10/9 0:20", "body": "本文", "good": 1, "bad": 0, "feel_label": None}


@pytest.fixture
def result_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(rs, "get_forum_posts", lambda code_s: [_POST])
    return tmp_path


def _completed(result=_RESULT, returncode=0, is_error=False):
    stdout = json.dumps({"result": result, "is_error": is_error, "usage": {"output_tokens": 1}, "total_cost_usd": 0.2})
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_成功したら見出し以降を保存し結果を読み出せる(result_dir, monkeypatch):
    called = []
    monkeypatch.setattr(rs.subprocess, "run", lambda cmd, **k: called.append(cmd) or _completed())
    assert rs.run("3496") == 0
    # 投稿は先に取得して渡し、モデルにはツールを持たせない
    assert "本文" in called[0][2] and called[0][called[0].index("--tools") + 1] == ""
    saved = (result_dir / "3496.md").read_text(encoding="utf-8")
    assert saved.startswith("## 掲示板の感情分析")  # 見出しより前の文章は落とす
    assert json.loads((result_dir / "3496.meta.json").read_text(encoding="utf-8"))["total_cost_usd"] == 0.2
    loaded = rs.load_result("3496")
    assert (loaded["temperature"], loaded["post_count"]) == ("+1 (やや強気)", 42)


@pytest.mark.parametrize("outcome", [
    _completed(returncode=1),
    _completed(is_error=True),
    _completed(result="見出しのない応答"),
    "no_posts",
    SimpleNamespace(returncode=0, stdout="JSON ではない出力", stderr=""),
    subprocess.TimeoutExpired(cmd="claude", timeout=1),
    FileNotFoundError("claude"),
])
def test_失敗したら前回の結果を残す(result_dir, monkeypatch, outcome):
    (result_dir / "3496.md").write_text("前回の結果", encoding="utf-8")

    if outcome == "no_posts":
        monkeypatch.setattr(rs, "get_forum_posts", lambda code_s: [])

    def fake_run(*a, **k):
        assert outcome != "no_posts", "投稿が無ければ claude を起動しない"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    assert rs.run("3496") == 1
    assert (result_dir / "3496.md").read_text(encoding="utf-8") == "前回の結果"


def test_実行中の印は排他的に取り古い印は無視する(result_dir):
    assert rs.load_result("3496") is None
    assert rs.acquire_running_marker("3496") is True
    assert rs.acquire_running_marker("3496") is False  # 実行中は取れない
    assert rs.is_running("3496") is True
    # 強制終了で残った古い印は、実行中とみなさず取り直せる
    old = time.time() - rs.RUNNING_STALE_SEC - 1
    os.utime(result_dir / "3496.running", (old, old))
    assert rs.is_running("3496") is False
    assert rs.acquire_running_marker("3496") is True
    rs.release_running_marker("3496")
    assert rs.is_running("3496") is False
    # 出力の形が変わって温度を拾えなくても、本文は返す
    (result_dir / "3496.md").write_text("## 別の見出し\n本文\n", encoding="utf-8")
    assert rs.load_result("3496")["temperature"] is None
