"""
開発ページ ルート (issue #321)。

GET  /dev              : 日次バッチの実行ボタン・状態・ログ末尾・PR/issue リンク
POST /dev/cron/run     : shintakane_cron.sh を非同期起動
GET  /dev/cron/status  : 実行状態 (logs/cron_status.json) とログ末尾を JSON で返す
POST /dev/deploy       : 運用機で pull し WebApp、scripts 変更時は MCP 接続も再起動する

排他・状態マーカー・ログは shintakane_cron.sh 自身が持つ。launchd の定刻実行も
同じマーカー/ログに載り、19時の kickstart で WebApp が再起動しても状態を失わない。
「実行中」の判定はスクリプトが shlock で握るロックファイルを正本とする。
"""

import json
import os
import subprocess
import threading
from collections import deque
from datetime import datetime
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
_WEBAPP_LABEL = "com.k_sohara.shintakane.webapp"
_TUNNEL_LABEL = "com.k_sohara.shintakane-tunnel"
_GIT_TIMEOUT_SEC = 60

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
    return render_template("dev.html", portal_links=PORTAL_LINKS, version=_STARTUP_VERSION)


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


def _current_state(marker: Dict[str, Any]) -> str:
    """running / done / failed / interrupted (running のまま pid 消滅) / none。"""
    if _is_running():
        # ロック取得直後でマーカーがまだ前回分のこともあるので、ロックを優先する
        return "running"
    if marker.get("state") == "running":
        return "interrupted"
    return marker.get("state", "none")


@dev_bp.route("/dev/cron/status", methods=["GET"])
def cron_status():
    marker = _read_marker() or {"state": "none"}
    return jsonify({**marker, "state": _current_state(marker), "log_tail": _log_tail()})


@dev_bp.app_context_processor
def _inject_cron_alert():
    # 前回の日次バッチが失敗・中断していたら、全画面のナビ「開発」に印を出す。
    # 次の日次バッチが成功してマーカーが書き換わるまで出続ける
    marker = _read_marker()
    return {"cron_alert": marker is not None and _current_state(marker) in ("failed", "interrupted")}


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(_PROJECT_ROOT), capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_SEC, check=check,
    )


# WebApp が動かしているコードの版。SSH 等の別経路で pull 済みだと pull 前後の HEAD は
# 変わらないため、再起動の要否は起動時の HEAD と比べて決める
_STARTUP_HEAD = _git("rev-parse", "HEAD").stdout.strip()
# /dev の「コード反映」に出す、動作中のコードの版と反映 (= WebApp 起動) 日時
_STARTUP_VERSION = {
    "commit": _git("log", "-1", "--format=%h %cd %s", "--date=format:%m/%d %H:%M").stdout.strip(),
    "started_at": datetime.now().strftime("%m/%d %H:%M"),
}


@dev_bp.route("/dev/deploy", methods=["POST"])
def deploy():
    """main を取り込み、WebApp と、scripts に変更があれば MCP 接続を再起動する。

    日次バッチ冒頭の自動 pull (shintakane_cron.sh) を待たずに反映するためのもの。
    開発機の作業ブランチを pull しないよう、運用機 (run_webapp.sh が production を設定) でだけ動かす。
    """
    if os.environ.get("SHINTAKANE_ENV") != "production":
        return jsonify({"status": "not_production"}), 403
    # 確認から pull までの間にバッチが起動すると git がぶつかりうるが、バッチ側は
    # pull 失敗をログに出して前回のコードで続行するので、ロックの共有まではしない
    if _is_running():
        return jsonify({"status": "cron_running"}), 409

    try:
        before = _git("rev-parse", "HEAD", check=True).stdout.strip()
        pull = _git("pull", "--ff-only", check=True)
        output = (pull.stdout + pull.stderr).strip()
        after = _git("rev-parse", "HEAD", check=True).stdout.strip()
        # MCP は WebApp と起動時刻が違うため、起動時 HEAD ではなく今回の pull を比べる。
        # 関数内 import や間接依存も対象にするため scripts/ 全体を見る。
        tunnel_restarting = before != after and bool(_git(
            "diff", "--name-only", before, after, "--", "scripts/", check=True,
        ).stdout.strip())
    except subprocess.TimeoutExpired:
        return jsonify({"status": "failed", "output": "コード反映の git 処理がタイムアウトしました"}), 500
    except subprocess.CalledProcessError as e:
        output = ((e.stdout or "") + (e.stderr or "")).strip()
        log_warning(f"[dev] コード反映の git 処理失敗: {e.cmd}: {output}")
        return jsonify({"status": "failed", "output": output}), 500
    except OSError as e:
        log_warning(f"[dev] コード反映の git 起動失敗: {e}")
        return jsonify({"status": "failed", "output": str(e)}), 500

    restarting = after != _STARTUP_HEAD
    if not restarting and not tunnel_restarting:
        return jsonify({
            "status": "up_to_date", "output": output,
            "restarting": False, "tunnel_restarting": False,
        })

    # 自プロセスを落とすので、応答の後に別セッションで必要なジョブを再起動する。
    labels = ([_TUNNEL_LABEL] if tunnel_restarting else []) + ([_WEBAPP_LABEL] if restarting else [])
    commands = [f'launchctl kickstart -k "gui/$(id -u)/{label}"' for label in labels]
    try:
        subprocess.Popen(
            ["bash", "-c", "sleep 1; " + "; ".join(commands)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        log_warning(f"[dev] 再起動の予約失敗: {e}")
        return jsonify({"status": "failed", "output": f"コードは取り込み済みですが再起動を予約できませんでした: {e}"}), 500
    log_print(f"[dev] コード反映 {before[:7]} -> {after[:7]}、再起動予約: {', '.join(labels)}")
    return jsonify({
        "status": "updated", "output": output,
        "restarting": restarting, "tunnel_restarting": tunnel_restarting,
    })
