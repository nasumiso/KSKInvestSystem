#!/bin/bash

# 調査WebApp 起動ヘルパー
# プロジェクトルートから `./shintakane_research.sh` で起動する。

cd "$(dirname "$0")" || exit 1

export KS_DATA_DIR="${KS_DATA_DIR:-/Users/k_sohara/shintakane_data_dev}"
source .venv/bin/activate
cd scripts
exec python -m webapp.app
