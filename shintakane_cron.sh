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
# shintakane.py をバックグラウンド実行（非同期アップロードと make_stock_db.py を並行化）
echo "===== $(date '+%Y-%m-%d %H:%M:%S') shintakane.py 開始 =====" >> ../logs/shintakane.log
python shintakane.py >> ../logs/shintakane.log 2>&1 &
PID1=$!
DONE_FLAG="../logs/.shintakane_main_done.${PID1}"

# shintakane.py の main() 完了（= DB操作完了）を待つ
# フラグ出現 or プロセス異常終了のどちらかで抜ける
SHINTAKANE_OK=true
while [ ! -f "$DONE_FLAG" ]; do
  if ! kill -0 $PID1 2>/dev/null; then
    # shintakane.py がフラグ作成前に異常終了
    wait $PID1
    RET1=$?
    echo "❌ エラー: shintakane.py がmain()完了前に終了（終了コード: $RET1）"
    SHINTAKANE_OK=false
    break
  fi
  sleep 1
done
rm -f "$DONE_FLAG"

if [ "$SHINTAKANE_OK" = false ]; then
  echo "shintakane.py が異常終了したため make_stock_db.py をスキップします"
  exit 1
fi
echo "✅ shintakane.py main()完了（アップロードはバックグラウンド継続中）"

rotate_log ../logs/make_stock_db.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') make_stock_db.py 開始 =====" >> ../logs/make_stock_db.log
python make_stock_db.py >> ../logs/make_stock_db.log 2>&1
RET2=$?
if [ $RET2 -ne 0 ]; then
  echo "❌ エラー: make_stock_db.py （終了コード: $RET2）"
else
  echo "✅ 正常終了: make_stock_db.py"
fi

# shintakane.py のアップロード完了を待つ
wait $PID1
RET1=$?
if [ $RET1 -ne 0 ]; then
  echo "❌ エラー: shintakane.py （終了コード: $RET1）"
else
  echo "✅ 正常終了: shintakane.py（アップロード含む）"
fi