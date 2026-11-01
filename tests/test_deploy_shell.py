"""日次のコード反映とMCP起動日時表示を、運用処理を動かさずに確認する。"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _command(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("changed, pull_rc, diff_rc, tunnel", [
    ("scripts/stock_ratings.py", 0, 0, True),
    ("", 0, 0, False),  # docsのみ・空振りは scripts/ の差分が空
    ("scripts/mcp/shikiho_server.py", 1, 0, False),
    ("", 0, 1, False),
])
def test_cron_pull_restarts_mcp_only_for_scripts(tmp_path, changed, pull_rc, diff_rc, tunnel):
    # 実スクリプトの自動pullまでを実行し、データ更新・外部通信へ進ませない。
    source = (ROOT / "shintakane_cron.sh").read_text()
    script = tmp_path / "shintakane_cron.sh"
    script.write_text(source.split("# ファイルディスクリプタ上限を引き上げる。", 1)[0] + "\nexit 0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launch_log = tmp_path / "launch.log"
    _command(bin_dir, "shlock", "exit 0\n")
    _command(bin_dir, "git", '''case "$1" in
rev-parse) echo before ;;
pull) exit "$TASK_PULL_RC" ;;
diff) printf '%s\\n' "$TASK_CHANGED"; exit "$TASK_DIFF_RC" ;;
esac
''')
    _command(bin_dir, "launchctl", 'printf "%s\\n" "$*" >> "$TASK_LAUNCH_LOG"\n')
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=10, env={
        **os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "SHINTAKANE_WEB_TRIGGER": "1", "SHINTAKANE_AUTO_PULL": "1",
        "KS_DATA_DIR": str(tmp_path / "data"), "TASK_CHANGED": changed,
        "TASK_PULL_RC": str(pull_rc), "TASK_DIFF_RC": str(diff_rc),
        "TASK_LAUNCH_LOG": str(launch_log),
    })
    assert result.returncode == 0, result.stderr
    launches = launch_log.read_text() if launch_log.exists() else ""
    assert ("shintakane-tunnel" in launches) is tunnel
    assert ("shintakane.webapp" in launches) is (pull_rc == 0)
    if diff_rc:
        assert "手動で再起動" in result.stdout


@pytest.mark.parametrize("running", [True, False])
def test_status_shows_mcp_start_time(tmp_path, running):
    source = (ROOT / "deploy/macmini.sh").read_text()
    block = source.split('echo "== MCP サーバー (PID / 起動日時) =="', 1)[1].split('echo "== WebApp =="', 1)[0]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _command(bin_dir, "pgrep", "echo 2468\n" if running else "exit 1\n")
    _command(bin_dir, "ps", 'echo "2468 Sun Nov  1 22:00:00 2026"\n')
    result = subprocess.run(["bash", "-c", "uid=501\n" + block], capture_output=True, text=True, env={
        **os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
    })
    assert result.returncode == 0
    if running:
        assert "2468 Sun Nov  1 22:00:00 2026" in result.stdout
    else:
        assert "停止中" in result.stdout
