#!/bin/bash

# スクリプトのあるディレクトリに移動（どこから実行してもOK）
cd "$(dirname "$0")" || exit 1
mkdir -p logs

# launchd 経由の起動 (TTYなし) のみ「19時前ならスキップ」を適用。
# 朝マシンを開いた時に RunAtLoad=true で発火しても、株価終値が揃ってない時間帯では
# 走らせたくない。一方、手動で `bash shintakane_cron.sh` を打った時は時刻問わず実行する。
if [ ! -t 1 ] && [ "$(date +%-H)" -lt 19 ]; then
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

# theme-news 調査は /market のボタンから手動実行する運用 (issue #165)。
# claude -p が将来従量課金化される可能性 + 1 回 5〜15 分かかる重い処理のため、
# 毎日 cron で走らせず、必要な日だけユーザー判断で起動する。

# --- 結果サマリー ---
echo ""
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 実行結果 ====="
report "shintakane.py" $RET1 ../logs/shintakane.log
report "make_stock_db.py" $RET2 ../logs/make_stock_db.log
echo "================================================"
