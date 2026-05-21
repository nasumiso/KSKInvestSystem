"""
市場データ・決算カレンダー ルート。

GET  /market                         : 市場データページ (market_data.html を取り込み + 動的決算セクション)
GET  /api/kessan_comment/<code_s>    : 指定銘柄・決算日のコメントを JSON で返す
POST /api/kessan_comment/<code_s>    : 決算コメントを保存 (新規 or 上書き)
POST /market/theme_news/run          : theme-news skill を非同期起動 (issue #165)
GET  /market/theme_news/status       : theme-news 実行状況を JSON で返す (issue #165)
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, render_template, request

from ks_util import get_price_day, log_print, log_warning
from webapp.helpers import (
    get_kessan_comment,
    get_market_html_parts,
    get_market_kessan_data,
    save_kessan_comment,
)
from research_shelve import (
    KESSAN_REACTION_PERIODS,
    VALID_EXPECTATIONS,
    normalize_kessan_post_price_changes,
)

market_bp = Blueprint("market", __name__)

# issue #165: theme-news 当日 history の格納場所
# scripts/webapp/routes/market.py から見て project root は 3 階層上
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_THEME_NEWS_HISTORY_DIR = _PROJECT_ROOT / ".claude" / "skills" / "theme-news" / "history"
_RUN_THEME_NEWS_SCRIPT = _PROJECT_ROOT / "scripts" / "run_theme_news.py"


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


# API レスポンスに含める決算コメントエントリのキー集合
# 旧 post_price_change は API 契約上含めない（_serialize_kessan_entry_for_api で除外）
_API_ENTRY_KEYS = (
    "kessanbi",
    "quarter",
    "pre_expectation",
    "pre_outlook",
    "post_comment",
    "kessan_matagi",
    "held_before_kessan",
    "held_after_kessan",
)


def _serialize_kessan_entry_for_api(entry: Dict[str, Any]) -> Dict[str, Any]:
    """決算コメントエントリを API 用に整形する。

    新スキーマ (post_price_changes) のみを返し、旧 post_price_change キーは含めない。
    旧形式のみのデータを受け取った場合は post_price_changes に正規化して返す。
    """
    result: Dict[str, Any] = {}
    for key in _API_ENTRY_KEYS:
        if key in entry:
            result[key] = entry[key]
    result["post_price_changes"] = normalize_kessan_post_price_changes(entry)
    return result


@market_bp.route("/market", methods=["GET"])
def market_page():
    """市場データページ。静的 market_data.html を取り込み、決算セクションを動的版に差し替える。"""
    market_parts = get_market_html_parts()
    kessan_data = get_market_kessan_data()
    theme_news = _load_theme_news_for_display()

    expectation_options = sorted(VALID_EXPECTATIONS, key=lambda x: ("", "◎", "○", "▲", "△", "×").index(x))

    return render_template(
        "market.html",
        market_parts=market_parts,
        kessan_data=kessan_data,
        theme_news=theme_news,
        expectation_options=expectation_options,
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


@market_bp.route("/api/kessan_comment/<code_s>", methods=["GET"])
def get_kessan_comment_api(code_s: str):
    """指定銘柄・決算日のコメントを JSON で返す。未登録時は空エントリ。"""
    kessanbi = request.args.get("kessanbi", "").strip()
    if not kessanbi:
        return jsonify({"error": "kessanbi required"}), 400
    entry = get_kessan_comment(code_s, kessanbi)
    if entry is None:
        entry = {
            "kessanbi": kessanbi,
            "quarter": 0,
            "pre_expectation": "",
            "pre_outlook": "",
            "post_price_changes": {key: "" for key, _ in KESSAN_REACTION_PERIODS},
            "post_comment": "",
            "kessan_matagi": False,
        }
        return jsonify(entry)
    return jsonify(_serialize_kessan_entry_for_api(entry))


@market_bp.route("/api/kessan_comment/<code_s>", methods=["POST"])
def post_kessan_comment_api(code_s: str):
    """決算コメントを保存。成功時は保存後エントリを返す。"""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    try:
        entry = save_kessan_comment(code_s, form)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_serialize_kessan_entry_for_api(entry))
