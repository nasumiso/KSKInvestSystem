"""theme-news skill のラッパー実行スクリプト。

/market ボタンからの手動実行用 (cron 経由は撤回、issue #165)。
同一営業日の重複実行を完了マーカーで防ぐ。

Usage:
    python run_theme_news.py                # 手動: 履歴あっても再実行
    python run_theme_news.py --force        # 重複ガード無視 (=履歴あっても再実行と同等、明示用)
    python run_theme_news.py --web-trigger  # /market ボタン経由。.running マーカーを管理
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ks_util import get_price_day, log_print, log_warning, log_error


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "history"

# claude CLI のタイムアウト (秒)。WebSearch 5-10 回 + 推察生成想定で 15 分
CLAUDE_TIMEOUT_SEC = 900

# Opus 4.x の従量課金単価 ($/token)。将来 Sonnet 等に切り替える場合は要更新。
# usage 表示のための概算値。サブスク (Max plan) で動かしている場合の実請求は 0。
_PRICE_INPUT = 15.0 / 1_000_000
_PRICE_OUTPUT = 75.0 / 1_000_000
_PRICE_CACHE_CREATE_5M = 18.75 / 1_000_000
_PRICE_CACHE_CREATE_1H = 30.0 / 1_000_000
_PRICE_CACHE_READ = 1.50 / 1_000_000
_PRICE_WEB_SEARCH = 10.0 / 1000  # $/request


def _today_history_path() -> Path:
    today = get_price_day(datetime.today())
    return HISTORY_DIR / f"{today.isoformat()}.md"


def _today_done_marker_path() -> Path:
    # 完了マーカー (空ファイル)。ラッパー成功時のみ作成。
    # /market 表示判定にも使い、half-written history を公開しないようにする。
    return _today_history_path().with_suffix(".md.done")


def _today_running_marker_path() -> Path:
    # 実行中マーカー。--web-trigger 時のみ作成し、終了時 (成功/失敗問わず) 削除。
    # /market 側で「実行中バッジ表示 + 重複起動 409」判定に使う。
    return _today_history_path().with_suffix(".md.running")


def _today_meta_path() -> Path:
    # トークン使用量メタ (JSON)。--output-format json の usage を保存し、
    # /market でコスト目安バッジを出すために使う。
    return _today_history_path().with_suffix(".md.meta.json")


def _should_skip(args) -> bool:
    # 重複ガードは done マーカーベース。--force/--web-trigger は無条件起動。
    if args.force or args.web_trigger:
        return False
    return False  # 通常モード (引数なし) も常時実行 (将来 --cron 復活時に拡張)


def _run_claude_skill() -> int:
    log_print("[theme-news] claude -p '/theme-news' を起動")
    cmd = [
        "claude", "-p", "/theme-news",
        "--allowed-tools", "Read,Write,Bash,WebSearch,Glob,Grep",
        "--output-format", "json",  # 末尾に result/usage が含まれる JSON が出力される
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

    # usage 抽出 → .meta.json 保存 (失敗してもメイン処理は止めない)
    _try_save_usage_meta(result.stdout)

    # 完了マーカー作成。ここに到達したときのみ「当日完了」扱いになる。
    _today_done_marker_path().touch()
    log_print(f"[theme-news] 完了: {history_file.name}")
    return 0


def _try_save_usage_meta(stdout: str) -> None:
    """claude -p --output-format json の出力末尾から usage を抽出し meta.json に保存。

    出力フォーマットは Anthropic の仕様で {"type":"result", "usage":{...}, "total_cost_usd":...}
    の単一 JSON が末尾にある (それより前は各メッセージの JSON)。
    フォーマットが想定外なら警告だけ出して保存はスキップする (機能の主目的ではないため)。
    """
    try:
        # 出力は jsonl っぽい形式の場合と、単一 JSON object の場合がある
        text = stdout.strip()
        if not text:
            return
        usage = None
        cost = None
        # 末尾 1 オブジェクトを試す
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                usage = obj.get("usage")
                cost = obj.get("total_cost_usd")
        except json.JSONDecodeError:
            # jsonl 形式の場合: 末尾行から取る
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "result":
                    usage = obj.get("usage")
                    cost = obj.get("total_cost_usd")
                    break
        if not usage:
            log_warning("[theme-news] usage 情報が claude -p 出力に見つからない (meta.json 保存スキップ)")
            return
        # コストが claude -p から取れない場合 (サブスクで$0表示など) は自前計算で目安を出す
        if cost is None:
            cost = _estimate_cost_usd(usage)
        meta = {
            "executed_at": datetime.now().isoformat(timespec="seconds"),
            "usage": usage,
            "estimated_cost_usd": round(cost, 4) if cost is not None else None,
            "model_note": "Opus 4.x の従量課金単価で概算。Max plan ではこの請求は発生しない",
        }
        _today_meta_path().write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log_print(f"[theme-news] usage 保存: {_today_meta_path().name} (cost≒${cost:.2f})")
    except Exception as e:
        log_warning(f"[theme-news] usage 抽出失敗 (本体処理は継続): {e}")


def _estimate_cost_usd(usage: dict) -> float:
    """usage dict から Opus 4.x 単価でコスト概算 (USD)。"""
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    cr_tok = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation", {}) or {}
    cc5 = cc.get("ephemeral_5m_input_tokens", 0)
    cc1h = cc.get("ephemeral_1h_input_tokens", 0)
    stu = usage.get("server_tool_use", {}) or {}
    ws = stu.get("web_search_requests", 0)
    return (
        in_tok * _PRICE_INPUT
        + out_tok * _PRICE_OUTPUT
        + cr_tok * _PRICE_CACHE_READ
        + cc5 * _PRICE_CACHE_CREATE_5M
        + cc1h * _PRICE_CACHE_CREATE_1H
        + ws * _PRICE_WEB_SEARCH
    )


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
    parser.add_argument("--force", action="store_true",
                        help="重複ガードを無視して強制実行 (現行は無条件起動と同等)")
    parser.add_argument("--web-trigger", dest="web_trigger", action="store_true",
                        help="/market ボタン経由で起動。.md.running マーカーを作成/削除する")
    args = parser.parse_args()

    if _should_skip(args):
        return 0

    # クリーン checkout や履歴削除後の初回起動でも marker/meta が touch できるよう、
    # 先に HISTORY_DIR を確実に作る (.gitignore 対象なので Git では追跡されない)。
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    running_marker = _today_running_marker_path() if args.web_trigger else None
    if running_marker is not None:
        # 多重起動防止: 既に .running があれば 1 を返す (webapp 側が 409 を返す前提)
        if running_marker.exists():
            log_warning(f"[theme-news] 既に実行中: {running_marker.name}")
            return 1
        running_marker.touch()
    try:
        return _run_claude_skill()
    finally:
        if running_marker is not None and running_marker.exists():
            try:
                running_marker.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
