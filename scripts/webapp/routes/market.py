"""
市場データ ルート。

GET  /market                         : 市場データページ (market_data.html を取り込み + テーマニュース)
POST /market/theme_news/run          : theme-news skill を非同期起動 (issue #165)
GET  /market/theme_news/status       : theme-news 実行状況を JSON で返す (issue #165)
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, render_template

from ks_util import get_price_day, log_print, log_warning
from webapp.helpers import get_market_html_parts

market_bp = Blueprint("market", __name__)

# issue #165: theme-news 当日 history の格納場所
# scripts/webapp/routes/market.py から見て project root は 3 階層上
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_THEME_NEWS_HISTORY_DIR = _PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "history"
_RUN_THEME_NEWS_SCRIPT = _PROJECT_ROOT / "scripts" / "run_theme_news.py"
# issue #165: 株カレンダー events.json (theme-news skill が更新する)
_CALENDAR_EVENTS_JSON = _PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "events.json"


def _history_paths_for(target_date) -> Dict[str, Path]:
    base = _THEME_NEWS_HISTORY_DIR / f"{target_date.isoformat()}.md"
    return {
        "md": base,
        "done": base.with_suffix(".md.done"),
        "running": base.with_suffix(".md.running"),
        "meta": base.with_suffix(".md.meta.json"),
    }


def _read_history_payload(target_date) -> Optional[Dict[str, Any]]:
    """.md と .done が揃ったときだけ payload (date / markdown / meta) を返す。"""
    paths = _history_paths_for(target_date)
    if not (paths["md"].exists() and paths["done"].exists()):
        return None
    try:
        markdown = paths["md"].read_text(encoding="utf-8")
    except OSError as e:
        log_warning(f"[market] theme-news 読み込み失敗: {e}")
        return None
    meta = _read_meta(paths["meta"])
    return {"date": target_date.isoformat(), "markdown": markdown, "meta": meta}


def _read_meta(meta_path: Path) -> Optional[Dict[str, Any]]:
    """meta.json があれば dict、無ければ None。"""
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log_warning(f"[market] theme-news meta 読み込み失敗: {e}")
        return None


def _find_recent_done_history(today, max_lookback_days: int = 30) -> Optional[Dict[str, Any]]:
    """today より前の最新の .md.done 付き history を返す。無ければ None。

    手動運用のため「前営業日」だけでなく、土日や未実行日を跨いだ最新を拾う。
    最大 30 日まで遡る (それより古ければ非表示で実害なし)。
    """
    if not _THEME_NEWS_HISTORY_DIR.exists():
        return None
    today_iso = today.isoformat()
    # done マーカーがあるファイルだけ列挙し、今日より前で最新を選ぶ
    candidates = []
    for done in _THEME_NEWS_HISTORY_DIR.glob("*.md.done"):
        # ファイル名 "YYYY-MM-DD.md.done" → 日付
        date_str = done.name[:-len(".md.done")]
        if date_str >= today_iso:
            continue
        candidates.append(date_str)
    if not candidates:
        return None
    candidates.sort(reverse=True)
    for date_str in candidates[:max_lookback_days]:
        try:
            from datetime import date as _date
            d = _date.fromisoformat(date_str)
        except ValueError:
            continue
        payload = _read_history_payload(d)
        if payload is not None:
            return payload
    return None


def _load_theme_news_for_display() -> Dict[str, Any]:
    """/market 表示用 theme-news payload。

    返り値: {"current": dict|None, "is_today": bool, "running": bool}
      - current: 当日 (done 有) があれば当日、無ければ最新の前日 done。両方無ければ None
      - is_today: current が当日かどうか (UI で「(前日)」表記分岐に使う)
      - running: 当日分が実行中マーカーを持っているかどうか
    """
    today = get_price_day(datetime.today())
    today_paths = _history_paths_for(today)
    running = today_paths["running"].exists()
    today_payload = _read_history_payload(today)
    if today_payload is not None:
        return {"current": today_payload, "is_today": True, "running": running}
    fallback = _find_recent_done_history(today)
    return {"current": fallback, "is_today": False, "running": running}


def _load_calendar_payload() -> Dict[str, Any]:
    """events.json を読み /market テンプレに渡す dict を返す (issue #165)。

    返り値:
      available: bool — events.json が読めて配列だったか
      events_json: str — テンプレ <script type="application/json"> に埋め込む JSON 文字列
      events_count: int — イベント数 (summary に表示)
      today: str — YYYY-MM-DD (get_price_day 基準、JS の TODAY に渡す)

    失敗時 (ファイル無し / 壊れた JSON / 非配列) は available=False で空配列を返す。
    """
    today = get_price_day(datetime.today()).isoformat()
    if not _CALENDAR_EVENTS_JSON.exists():
        return {"available": False, "events_json": "[]", "events_count": 0, "today": today}
    try:
        events = json.loads(_CALENDAR_EVENTS_JSON.read_text(encoding="utf-8"))
        if not isinstance(events, list):
            raise ValueError("events.json is not a list")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log_warning(f"[market] calendar events 読み込み失敗: {e}")
        return {"available": False, "events_json": "[]", "events_count": 0, "today": today}
    # <script type="application/json"> 内で安全に解釈されるよう `<` をエスケープ
    # (`</script>` がイベント本文に混入したケースの保険)
    events_json = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    events_json = events_json.replace("<", "\\u003c")
    return {"available": True, "events_json": events_json, "events_count": len(events), "today": today}


@market_bp.route("/market", methods=["GET"])
def market_page():
    """市場データページ。静的 market_data.html (決算日セクション除く) と theme-news を表示する。"""
    market_parts = get_market_html_parts()
    theme_news = _load_theme_news_for_display()
    calendar = _load_calendar_payload()

    return render_template(
        "market.html",
        market_parts=market_parts,
        theme_news=theme_news,
        calendar=calendar,
    )


@market_bp.route("/market/theme_news/run", methods=["POST"])
def theme_news_run():
    """theme-news skill を非同期起動 (issue #165)。

    既に当日分が実行中 (.md.running 存在) なら 409。
    起動成功 (Popen が走り出す) なら 202、即時失敗 (FileNotFoundError 等) なら 500。
    実行時間が長い (5〜15 分) ので待たずに返し、status エンドポイントで poll してもらう。
    """
    today = get_price_day(datetime.today())
    paths = _history_paths_for(today)
    if paths["running"].exists():
        return jsonify({"status": "already_running", "date": today.isoformat()}), 409

    # Popen で fire-and-forget。stdout/stderr は logs/theme_news.log に追記する。
    # 失敗時のデバッグ用 (`tail logs/theme_news.log` で claude -p の出力が読める)。
    # shintakane_cron.sh と同じ場所に集約。ローテーションは cron 経由起動時に
    # rotate_log が走るので、手動連発でログが肥大したらユーザーが手で削除する想定。
    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "theme_news.log"
    try:
        log_fh = open(log_path, "ab")
        # 起動マーカーを書き込んで、ログ末尾を見たときに最新実行の開始位置が分かるようにする
        log_fh.write(
            f"\n===== {datetime.now().isoformat(timespec='seconds')} "
            f"/market から theme-news 手動起動 (date={today.isoformat()}) =====\n".encode("utf-8")
        )
        log_fh.flush()
    except OSError as e:
        log_warning(f"[market] theme-news ログファイルを開けない: {e}")
        log_fh = None
    try:
        subprocess.Popen(
            [sys.executable, str(_RUN_THEME_NEWS_SCRIPT), "--web-trigger"],
            cwd=str(_PROJECT_ROOT / "scripts"),
            stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh is not None else subprocess.DEVNULL,
            start_new_session=True,  # webapp 再起動でも skill 実行は継続させる
        )
    except (FileNotFoundError, OSError) as e:
        if log_fh is not None:
            log_fh.close()
        log_warning(f"[market] theme-news 起動失敗: {e}")
        return jsonify({"status": "spawn_failed", "error": str(e)}), 500
    finally:
        # 親プロセス側 fd は閉じてよい (子プロセスが dup して保持しているため書込み継続)
        if log_fh is not None:
            log_fh.close()

    log_print(f"[market] theme-news skill 起動 (date={today.isoformat()}, log={log_path})")
    return jsonify({"status": "started", "date": today.isoformat(), "log_path": str(log_path)}), 202


@market_bp.route("/market/theme_news/status", methods=["GET"])
def theme_news_status():
    """theme-news 実行状況を JSON で返す (UI ポーリング用)。

    返り値:
      date: 当日 (price_day)
      running: 実行中マーカーが存在するか
      done: 当日完了マーカーが存在するか
      has_today_history: 当日 history (.md) が存在するか (running 中の途中ファイルでも True になり得る)
    """
    today = get_price_day(datetime.today())
    paths = _history_paths_for(today)
    return jsonify({
        "date": today.isoformat(),
        "running": paths["running"].exists(),
        "done": paths["done"].exists(),
        "has_today_history": paths["md"].exists(),
    })


