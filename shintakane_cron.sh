#!/bin/bash

# スクリプトのあるディレクトリに移動（どこから実行してもOK）
cd "$(dirname "$0")" || exit 1
mkdir -p logs

RUN_THEME_NEWS=1
for arg in "$@"; do
  case "$arg" in
    --skip-theme-news|--no-theme-news)
      RUN_THEME_NEWS=0
      ;;
    *)
      echo "Usage: $0 [--skip-theme-news]"
      exit 2
      ;;
  esac
done

# launchd 経由の起動 (TTYなし) のみ「19時前ならスキップ」を適用。
# 朝マシンを開いた時に RunAtLoad=true で発火しても、株価終値が揃ってない時間帯では
# 走らせたくない。一方、手動で `bash shintakane_cron.sh` を打った時は時刻問わず実行する。
if [ ! -t 1 ] && [ "$(date +%-H)" -lt 19 ]; then
  exit 0
fi

# 運用機 (MacMini) のみ main を追従する。開発機では未設定なので何もしない。
# pull に失敗しても続行する: 前回のコードで実行する方が、日次データ更新を
# 丸ごと落とすより損失が小さい。
if [ "${SHINTAKANE_AUTO_PULL:-0}" = "1" ]; then
  if git pull --ff-only; then
    # 常駐 WebApp は古いコードのまま動き続けるため再起動する
    # (LaunchAgent 未登録の環境ではエラーを無視する)
    launchctl kickstart -k "gui/$(id -u)/com.k_sohara.shintakane.webapp" 2>/dev/null \
      || echo "ℹ️ webapp LaunchAgent の再起動をスキップしました (未登録)"
  else
    echo "❌ git pull --ff-only 失敗。前回のコードのまま実行を継続します"
  fi
fi

cd scripts

# KS_DATA_DIR は各ホストの .zshrc / plist で設定する。未設定のまま走らせると
# 存在しないパスに空 DB を作る事故になるため fail-fast する。
if [ -z "${KS_DATA_DIR:-}" ]; then
  echo "❌ KS_DATA_DIR が未設定です。.zshrc か LaunchAgent の EnvironmentVariables で設定してください"
  exit 1
fi
export KS_DATA_DIR
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

# --- エクスポージャーログ (issue #362) ---
# 市場ステート・各指標が更新済みである必要があるため make_stock_db.py の後に置く。
# 終了コードでは実行可否を判定しない (指標の鮮度は exposure_guide 側の
# read_* が個別にチェックしており、RET1/RET2 は鮮度の証明にならないため)。
rotate_log ../logs/exposure_guide.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') exposure_guide.py 開始 =====" >> ../logs/exposure_guide.log
python exposure_guide.py log >> ../logs/exposure_guide.log 2>&1
RET_EXPOSURE=$?

# --- ここまでの結果サマリー ---
echo ""
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 実行結果 ====="
report "shintakane.py" $RET1 ../logs/shintakane.log
report "make_stock_db.py" $RET2 ../logs/make_stock_db.log
report "exposure_guide.py" $RET_EXPOSURE ../logs/exposure_guide.log
echo "================================================"

# --- theme-news ---
# 数分かかるため、当日分のシステム更新データを先に閲覧できるよう、
# make_stock_db.py 成功ログを出した後に実行する。
RET3=0
if [ "$RUN_THEME_NEWS" -eq 0 ]; then
  echo "theme-news は --skip-theme-news 指定のため実行しません"
elif [ "$RET2" -ne 0 ]; then
  echo "theme-news は make_stock_db.py 失敗のため実行しません"
else
  rotate_log ../logs/theme_news.log
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') theme-news 開始 =====" >> ../logs/theme_news.log
  echo "theme-news を実行します (数分かかります)"
  python run_theme_news.py >> ../logs/theme_news.log 2>&1
  RET3=$?
  report "theme-news" $RET3 ../logs/theme_news.log
fi

# --- 週1 compact (金曜のみ) ---
# stocks_shelve は dbm.dumb の追記構造で 100〜120MB/日 肥大するため週1で詰める。
# 独立 LaunchAgent にはしない: 同一スクリプトの逐次実行なら「バッチが
# stocks_shelve を閉じた後」が構造的に保証される。
# 金曜にするのは、失敗して .compact_backup が残った場合 (次回実行が
# RuntimeError で停止する) に土日で対処できるため。
if [ "$(date +%u)" = "5" ]; then
  rotate_log ../logs/compact.log
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') compact 開始 =====" >> ../logs/compact.log
  python make_stock_db.py backup >> ../logs/compact.log 2>&1
  python make_stock_db.py compact >> ../logs/compact.log 2>&1
  RET_COMPACT=$?
  report "compact" $RET_COMPACT ../logs/compact.log
fi
