#!/bin/bash

# スクリプトのあるディレクトリに移動（どこから実行してもOK）
cd "$(dirname "$0")" || exit 1
mkdir -p logs

# ===== 1日1回ガード (案C: ノートPC運用、スリープ復帰後に最初の起動で走らせる) =====
# launchd の StartInterval=1800 (30分) で何度も起動されるが、本ガードにより
# 「19時以降」かつ「当日まだ未実行」の最初の起動でのみ Python を起動する。
# 19時前の起動 (RunAtLoad=true での起動含む) は即座にスキップ。
LAST_RUN_FILE="$HOME/.shintakane_cron_last_run"
TODAY=$(date +%Y-%m-%d)
TARGET_HOUR=19
CURRENT_HOUR=$(date +%-H)  # %-H は0埋めなし (例: 09 ではなく 9)
if [ -f "$LAST_RUN_FILE" ]; then
  LAST_DATE=$(cat "$LAST_RUN_FILE" 2>/dev/null)
  if [ "$LAST_DATE" = "$TODAY" ]; then
    # 既に当日実行済み — サイレント終了 (launchdが30分毎に呼んでもログを汚さない)
    exit 0
  fi
fi
if [ "$CURRENT_HOUR" -lt "$TARGET_HOUR" ]; then
  # 19時前 — サイレント終了
  exit 0
fi

cd scripts

# KS_DATA_DIR が未設定の場合はデフォルト値を設定
export KS_DATA_DIR="${KS_DATA_DIR:-/Users/k_sohara/Ext/GoogleDrive/shintakane_data}"
source ../.venv/bin/activate

# ログローテーション（1MB超で直近5000行に切り詰め）
rotate_log() {
  local logfile="$1"
  if [ -f "$logfile" ] && [ "$(stat -f%z "$logfile" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -5000 "$logfile" > "${logfile}.tmp" && mv "${logfile}.tmp" "$logfile"
  fi
}

# 結果表示用ヘルパー
report() {
  local name="$1" ret="$2" logfile="$3"
  if [ "$ret" -ne 0 ]; then
    echo "❌ $name 失敗（終了コード: $ret）"
    grep -E "Traceback|Error|Exception" "$logfile" | tail -3 | sed 's/^/   /'
  else
    echo "✅ $name 成功"
  fi
}

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 実行開始 ====="

# --- webapp 起動（未起動の場合のみ） ---
if ! lsof -iTCP:5001 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "webapp を起動します (port 5001)"
  rotate_log ../logs/webapp.log
  nohup python -m webapp.app >> ../logs/webapp.log 2>&1 &
  echo "webapp PID: $!"
else
  echo "webapp は既に起動中です (port 5001)"
fi

# --- shintakane.py ---
rotate_log ../logs/shintakane.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') shintakane.py 開始 =====" >> ../logs/shintakane.log
python shintakane.py >> ../logs/shintakane.log 2>&1
RET1=$?

# --- make_stock_db.py ---
rotate_log ../logs/make_stock_db.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') make_stock_db.py 開始 =====" >> ../logs/make_stock_db.log
python make_stock_db.py >> ../logs/make_stock_db.log 2>&1
RET2=$?

# --- 結果サマリー ---
echo ""
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 実行結果 ====="
report "shintakane.py" $RET1 ../logs/shintakane.log
report "make_stock_db.py" $RET2 ../logs/make_stock_db.log
echo "================================================"

# 1日1回ガード用フラグ更新: 両方成功した場合のみ「当日完了」とマーク
# (片方でも失敗していたらフラグを立てず、次の30分後の起動でリトライさせる)
if [ "$RET1" -eq 0 ] && [ "$RET2" -eq 0 ]; then
  echo "$TODAY" > "$LAST_RUN_FILE"
fi
