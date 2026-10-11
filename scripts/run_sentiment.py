#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
掲示板の感情分析 (`/sentiment`) を Claude Code で非対話実行し、結果を保存する (issue #501)。

stock ページのボタンから起動する。銘柄ごとに最新1件だけを
`$KS_DATA_DIR/sentiment/<コード>.md` に上書きし、履歴は持たない。
ラベルの定義と出力形式は `.claude/skills/sentiment/SKILL.md` が正。

使い方:
    python run_sentiment.py 4493                # 動作確認用。実行中の印には触らない
    python run_sentiment.py 4493 --web-trigger  # stock ページのボタン経由。終了時に印を消す
"""

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ks_util import DATA_DIR, log_error, log_print, log_warning, setup_logger
from research_shelve import validate_code_s

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULT_DIR = Path(DATA_DIR) / "sentiment"

# claude CLI のタイムアウト (秒)。実測は1回 50〜60 秒
CLAUDE_TIMEOUT_SEC = 300
# 実行中の印がこれより古ければ、プロセスが強制終了されて残ったものとみなす
RUNNING_STALE_SEC = 600

# 掲示板の本文は誰でも書ける文章なので、それを読むモデルに許すのは投稿の取得だけにする
_ALLOWED_TOOLS = "Bash(../.venv/bin/python sentiment.py:*)"
_HEADING = "## 掲示板の感情分析"
_OUTPUT_TAIL_CHARS = 1000

# 見出し行の「投稿 N件」と、「### 掲示板の温度」の次の行 (例: "+1 (やや強気) 相当")
_RE_POST_COUNT = re.compile(r"^## 掲示板の感情分析.*?投稿\s*(\d+)件", re.MULTILINE)
_RE_TEMPERATURE = re.compile(r"^### 掲示板の温度\s*\n+\s*([+\-−]?\d\s*\([^)\n]+\))", re.MULTILINE)


def result_paths(code_s: str) -> Dict[str, Path]:
    return {
        "md": RESULT_DIR / f"{code_s}.md",
        "meta": RESULT_DIR / f"{code_s}.meta.json",
        "running": RESULT_DIR / f"{code_s}.running",
    }


def is_running(code_s: str) -> bool:
    """実行中の印があり、古すぎなければ True"""
    try:
        mtime = result_paths(code_s)["running"].stat().st_mtime
    except OSError:
        return False
    return time.time() - mtime <= RUNNING_STALE_SEC


def acquire_running_marker(code_s: str) -> bool:
    """実行中の印を排他的に作る。すでに実行中なら False

    存在確認と作成を分けると、同時に押されたときに二重起動するため、作成の成否で判定する。
    """
    marker = result_paths(code_s)["running"]
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    if marker.exists() and not is_running(code_s):
        marker.unlink(missing_ok=True)
    try:
        marker.open("x").close()
    except FileExistsError:
        return False
    return True


def release_running_marker(code_s: str) -> None:
    result_paths(code_s)["running"].unlink(missing_ok=True)


def load_result(code_s: str) -> Optional[Dict[str, Any]]:
    """保存済みの最新の分析を返す。無ければ None

    temperature / post_count は Markdown の決まった形の行から拾う。
    出力の形が変わって拾えなければ None (本文はそのまま表示できる)。
    """
    paths = result_paths(code_s)
    try:
        markdown = paths["md"].read_text(encoding="utf-8")
        executed_at = datetime.fromtimestamp(paths["md"].stat().st_mtime)
    except OSError:
        return None
    m_temp = _RE_TEMPERATURE.search(markdown)
    m_count = _RE_POST_COUNT.search(markdown)
    return {
        "markdown": markdown,
        "executed_at": executed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": m_temp.group(1) if m_temp else None,
        "post_count": int(m_count.group(1)) if m_count else None,
    }


def run(code_s: str) -> int:
    """`/sentiment <コード>` を実行して保存する。失敗したら前回の結果を残す"""
    log_print(f"[sentiment] claude -p '/sentiment {code_s}' を起動")
    cmd = [
        "claude", "-p", f"/sentiment {code_s}",
        "--model", "claude-sonnet-5-5",
        "--allowed-tools", _ALLOWED_TOOLS,
        "--output-format", "json",
    ]
    # claude CLI は ~/.local/bin にあるが、WebApp の LaunchAgent は PATH に含めていない
    env = {**os.environ, "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"}
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), env=env, timeout=CLAUDE_TIMEOUT_SEC,
            check=False, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        log_error(f"[sentiment] {code_s}: claude -p タイムアウト ({CLAUDE_TIMEOUT_SEC}s)")
        return 1
    except FileNotFoundError:
        log_error("[sentiment] claude CLI が見つかりません")
        return 1
    elapsed_sec = time.monotonic() - started

    try:
        output = json.loads(result.stdout)
    except (TypeError, ValueError):
        output = {}
    text = output.get("result") or ""
    start = text.find(_HEADING)
    if result.returncode != 0 or output.get("is_error") or start < 0:
        # 掲示板を取得できなかったときは、スキルが見出しを出さずに終わる
        log_error(
            f"[sentiment] {code_s}: 保存しません (rc={result.returncode}) "
            f"応答: {(text or result.stdout or '')[-_OUTPUT_TAIL_CHARS:]!r} "
            f"stderr 末尾: {(result.stderr or '')[-_OUTPUT_TAIL_CHARS:]!r}"
        )
        return 1

    paths = result_paths(code_s)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    # 読み手が書きかけを見ないよう、一時ファイルに書いてから置き換える
    tmp = paths["md"].with_suffix(".md.tmp")
    tmp.write_text(text[start:].rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, paths["md"])
    meta = {
        "executed_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_sec": round(elapsed_sec, 1),
        "usage": output.get("usage"),
        # 従量単価での目安。Max plan ではこの請求は発生しない
        "total_cost_usd": output.get("total_cost_usd"),
    }
    try:
        paths["meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log_warning(f"[sentiment] {code_s}: 利用量を保存できませんでした: {e}")
    log_print(f"[sentiment] {code_s}: 保存しました ({elapsed_sec:.0f}秒, cost≒${meta['total_cost_usd'] or 0:.2f})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="掲示板の感情分析を実行して保存する")
    parser.add_argument("code_s", help="銘柄コード (例: 4493, 285A)")
    parser.add_argument("--web-trigger", action="store_true",
                        help="stock ページのボタン経由。終了時に実行中の印を消す")
    args = parser.parse_args(argv)
    setup_logger("run_sentiment")
    validate_code_s(args.code_s)
    try:
        return run(args.code_s)
    finally:
        if args.web_trigger:
            release_running_marker(args.code_s)


if __name__ == "__main__":
    raise SystemExit(main())
