#!/bin/bash
# 運用機 (MacMini) で WebApp を常駐起動する LaunchAgent 用ラッパー。
#
# このファイルはテンプレート。deploy/README.md の手順で __REPO__ を実パスへ
# 置換してから $HOME/.local/bin/ へインストールする。
# (launchd が Dropbox 配下のシェルスクリプトを直接起動できないことがあるため、
#  scripts/mcp/ と同じくホーム配下へ置く。インストール後は repo からの相対位置を
#  失うので、$0 基準ではなく置換済みの絶対パスを使う。)
set -eu

REPO="__REPO__"

# launchd は StandardOutPath の親ディレクトリを作らないので、ここで用意する
mkdir -p "$HOME/Library/Logs/shintakane"

# set -a で source した変数を子プロセス (python) へ引き継ぐ
set -a
# shellcheck source=/dev/null
[ -f "$HOME/.shintakane_env" ] && . "$HOME/.shintakane_env"
set +a

: "${KS_DATA_DIR:?KS_DATA_DIR が未設定です (~/.shintakane_env を確認)}"
: "${FLASK_SECRET_KEY:?FLASK_SECRET_KEY が未設定です (~/.shintakane_env を確認)}"

export SHINTAKANE_ENV=production

cd "$REPO/scripts"
# exec しないと launchd が PID を追えず KeepAlive が正しく効かない。
# .venv の python を絶対指定するのは、LaunchAgent がログインシェルを通らず
# system Python を掴む事故を防ぐため。
exec "$REPO/.venv/bin/python" -m webapp.app
