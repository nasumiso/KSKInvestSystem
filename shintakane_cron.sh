#!/bin/bash
# echo "[$(date)] cron started" >> /tmp/cron_debug.log

# スクリプトのあるディレクトリに移動（どこから実行してもOK）
cd "$(dirname "$0")" || {
  echo "ルート ディレクトリに移動できません: $SCRIPT_DIR"
  exit 1
}

# logs フォルダの存在確認・なければ作成
if [ ! -d "logs" ]; then
  echo "logs ディレクトリがないため作成します"
  mkdir logs || {
    echo "logs ディレクトリの作成に失敗"
    exit 1
  }
fi

cd scripts

# KS_DATA_DIR が未設定の場合はデフォルト値を設定
# （.zshrcが読まれないシェルやIDE内ターミナルからの実行に対応）
export KS_DATA_DIR="${KS_DATA_DIR:-/Users/k_sohara/Ext/GoogleDrive/shintakane_data}"

# 仮想環境を有効化
echo "仮想環境を有効化します"
source ../.venv/bin/activate

# ログローテーション（1MB超で直近5000行に切り詰め）
rotate_log() {
  local logfile="$1"
  if [ -f "$logfile" ] && [ "$(stat -f%z "$logfile" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -5000 "$logfile" > "${logfile}.tmp" && mv "${logfile}.tmp" "$logfile"
  fi
}

# 実行ログ
echo "shintakane.py と make_stock_db.py を実行します"
rotate_log ../logs/shintakane.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') shintakane.py 開始 =====" >> ../logs/shintakane.log
python shintakane.py >> ../logs/shintakane.log 2>&1
RET1=$?
if [ $RET1 -ne 0 ]; then
  echo "❌ エラー: shintakane.py （終了コード: $RET1）"
else
  echo "✅ 正常終了: shintakane.py"
fi

rotate_log ../logs/make_stock_db.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') make_stock_db.py 開始 =====" >> ../logs/make_stock_db.log
python make_stock_db.py >> ../logs/make_stock_db.log 2>&1
RET2=$?
if [ $RET2 -ne 0 ]; then
  echo "❌ エラー: make_stock_db.py （終了コード: $RET2）"
else
  echo "✅ 正常終了: make_stock_db.py"
fi