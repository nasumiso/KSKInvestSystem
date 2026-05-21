"""theme-news skill のラッパー実行スクリプト。

cron / 手動実行両対応。同一営業日の重複実行を完了マーカーで防ぐ。

Usage:
    python run_theme_news.py            # 手動: 履歴あっても再実行
    python run_theme_news.py --cron     # cron: 完了マーカーあればスキップ
    python run_theme_news.py --force    # 重複ガード無視で強制実行
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ks_util import get_price_day, log_print, log_warning, log_error


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "history"

# claude CLI のタイムアウト (秒)。WebSearch 5-10 回 + 推察生成想定で 15 分
CLAUDE_TIMEOUT_SEC = 900


def _today_history_path() -> Path:
    today = get_price_day(datetime.today())
    return HISTORY_DIR / f"{today.isoformat()}.md"


def _today_done_marker_path() -> Path:
    # 完了マーカー (空ファイル)。ラッパー成功時のみ作成し、
    # cron 重複判定は history ファイル単独ではなくこのマーカーで行う。
    # claude -p 途中失敗で half-written history が残っても当日中に再実行できる。
    return _today_history_path().with_suffix(".md.done")


def _should_skip(args) -> bool:
    if args.force:
        return False
    if not args.cron:
        return False
    done_marker = _today_done_marker_path()
    if done_marker.exists():
        log_print(f"[theme-news] 当日完了マーカー有、スキップ: {done_marker.name}")
        return True
    return False


def _run_claude_skill() -> int:
    log_print("[theme-news] claude -p '/theme-news' を起動")
    cmd = [
        "claude", "-p", "/theme-news",
        "--allowed-tools", "Read,Write,Bash,WebSearch,Glob,Grep",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            timeout=CLAUDE_TIMEOUT_SEC,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        log_error(f"[theme-news] claude -p タイムアウト ({CLAUDE_TIMEOUT_SEC}s)")
        return 1
    except FileNotFoundError:
        log_error("[theme-news] claude CLI が見つかりません")
        return 1

    if result.returncode != 0:
        log_warning(f"[theme-news] claude -p 異常終了: rc={result.returncode}")
        log_warning(f"stderr: {result.stderr[-500:]}")
        return result.returncode

    history_file = _today_history_path()
    if not history_file.exists():
        # skill が期待と違う日付で書いた可能性。直近書込みファイルを特定して詳細を出す。
        recent = _detect_recent_history(history_file)
        if recent is not None:
            log_warning(
                f"[theme-news] history 日付不一致: 期待={history_file.name} / "
                f"skill が生成={recent.name}。SKILL.md 1-0/7 の価格日 (= get_price_day) "
                f"でファイル名を書く規約に違反している。skill 側を確認すること。"
            )
        else:
            log_warning(f"[theme-news] claude -p は成功したが {history_file.name} が見つからない")
        return 1
    if history_file.stat().st_size <= 0:
        log_warning(f"[theme-news] history が空ファイル: {history_file.name}")
        return 1

    # 完了マーカー作成。ここに到達したときのみ「当日完了」扱いになり
    # /market 表示と次回 cron スキップ判定で使われる。
    _today_done_marker_path().touch()
    log_print(f"[theme-news] 完了: {history_file.name}")
    return 0


def _detect_recent_history(expected: Path) -> "Path | None":
    """期待ファイル名 (expected) と違う日付で skill が書いた可能性のあるファイルを返す。

    検出条件: HISTORY_DIR 直下の *.md のうち、mtime が claude -p 実行中
    (= now から 30 分以内) で、ファイル名が expected と異なるもの。
    複数あれば mtime 最新を返す。無ければ None。
    """
    import time
    if not HISTORY_DIR.exists():
        return None
    now = time.time()
    candidates = []
    for p in HISTORY_DIR.glob("*.md"):
        if p == expected:
            continue
        try:
            if now - p.stat().st_mtime <= 30 * 60:
                candidates.append(p)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cron", action="store_true",
                        help="cron 経由起動。完了マーカーあればスキップ")
    parser.add_argument("--force", action="store_true",
                        help="重複ガードを無視して強制実行")
    args = parser.parse_args()

    if _should_skip(args):
        return 0
    return _run_claude_skill()


if __name__ == "__main__":
    sys.exit(main())
