"""
開発ページ ルート (issue #321)。

GET  /dev              : 日次バッチの実行ボタン・状態・ログ末尾・PR/issue リンク
POST /dev/cron/run     : shintakane_cron.sh を非同期起動
GET  /dev/cron/status  : 実行状態 (logs/cron_status.json) とログ末尾を JSON で返す

排他・状態マーカー・ログは shintakane_cron.sh 自身が持つ。launchd の定刻実行も
同じマーカー/ログに載り、19時の kickstart で WebApp が再起動しても状態を失わない。
「実行中」の判定はスクリプトが shlock で握るロックファイルを正本とする。
"""

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, render_template

from ks_util import log_print, log_warning

dev_bp = Blueprint("dev", __name__)

# scripts/webapp/routes/dev.py から見て project root は 3 階層上
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CRON_SH = _PROJECT_ROOT / "shintakane_cron.sh"
_STATUS_JSON = _PROJECT_ROOT / "logs" / "cron_status.json"
_LOCK_FILE = _PROJECT_ROOT / "logs" / "cron.lock"
_CRON_LOG = _PROJECT_ROOT / "logs" / "cron.log"
_LOG_TAIL_LINES = 200

# GitHub の PR / issue 一覧へのリンク (トップページから移設)
PORTAL_LINKS = (
    {"title": "PR", "url": "https://github.com/nasumiso/KSKInvestSystem/pulls"},
    {"title": "issue", "url": "https://github.com/nasumiso/KSKInvestSystem/issues"},
)

# 同じ WebApp への連打をこの判定→起動で直列化する。起動直後 (子が shlock を取る前) の
# 再クリックで2本目が走っても、子は shlock に弾かれて即終了するので害はない。
_run_lock = threading.Lock()


def _read_marker() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_STATUS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_running() -> bool:
    """ロックを握るプロセスが生きているか。マーカーはロック取得の後に書かれるので正本にしない。"""
    try:
        pid = int(_LOCK_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _log_tail() -> str:
    try:
        with open(_CRON_LOG, encoding="utf-8", errors="replace") as f:
            return "".join(deque(f, maxlen=_LOG_TAIL_LINES))
    except OSError:
        return ""


@dev_bp.route("/dev", methods=["GET"])
def dev_page():
    return render_template("dev.html", portal_links=PORTAL_LINKS)


@dev_bp.route("/dev/cron/run", methods=["POST"])
def cron_run():
    """shintakane_cron.sh を起動して 202 を返す。実行中なら 409、起動失敗は 500。"""
    with _run_lock:
        if _is_running():
            return jsonify({"status": "already_running"}), 409

        env = {**os.environ, "SHINTAKANE_WEB_TRIGGER": "1"}
        # バッチに WebApp のセッション鍵は要らない (deploy/macmini.sh run と同じ扱い)
        env.pop("FLASK_SECRET_KEY", None)
        try:
            proc = subprocess.Popen(
                ["bash", str(_CRON_SH)],
                cwd=str(_PROJECT_ROOT),
                env=env,
                stdout=subprocess.DEVNULL,  # 出力はスクリプト自身が logs/cron.log に残す
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # 19時の kickstart で WebApp が落ちてもバッチは継続させる
            )
        except OSError as e:
            log_warning(f"[dev] shintakane_cron.sh 起動失敗: {e}")
            return jsonify({"status": "spawn_failed", "error": str(e)}), 500

        # 回収しないとゾンビが残り、ロックが残った場合に os.kill(pid, 0) が成功し続ける
        threading.Thread(target=proc.wait, daemon=True).start()

    log_print(f"[dev] shintakane_cron.sh 起動 (pid={proc.pid})")
    return jsonify({"status": "started", "pid": proc.pid}), 202


@dev_bp.route("/dev/cron/status", methods=["GET"])
def cron_status():
    """state は running / done / failed / interrupted (running のまま pid 消滅) / none。"""
    marker = _read_marker() or {"state": "none"}
    if _is_running():
        # ロック取得直後でマーカーがまだ前回分のこともあるので、ロックを優先する
        marker = {**marker, "state": "running"}
    elif marker.get("state") == "running":
        marker = {**marker, "state": "interrupted"}
    return jsonify({**marker, "log_tail": _log_tail()})
