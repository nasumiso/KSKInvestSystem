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

# --- run_theme_news.py ---
# 上流 (shintakane.py / make_stock_db.py) が両方成功した日のみ実行する。
# 上流失敗時は market_data.html が古い可能性があり、ニュース調査が前日データを
# 「当日分」として保存して /market に誤情報を固定するのを防ぐため。
rotate_log ../logs/theme_news.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') run_theme_news.py 開始 =====" >> ../logs/theme_news.log
if [ "$RET1" -eq 0 ] && [ "$RET2" -eq 0 ]; then
  python run_theme_news.py --cron >> ../logs/theme_news.log 2>&1
  RET3=$?
else
  echo "[run_theme_news] 上流失敗 (RET1=$RET1, RET2=$RET2) のためスキップ" >> ../logs/theme_news.log
  RET3=0
fi

# --- 結果サマリー ---
echo ""
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 実行結果 ====="
report "shintakane.py" $RET1 ../logs/shintakane.log
report "make_stock_db.py" $RET2 ../logs/make_stock_db.log
report "run_theme_news.py" $RET3 ../logs/theme_news.log
echo "================================================"
