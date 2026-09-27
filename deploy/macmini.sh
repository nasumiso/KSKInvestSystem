#!/bin/bash
# 開発機 (MBA) から運用機 (MacMini) を操作するヘルパー。
#
#   deploy/macmini.sh status          運用機の稼働状況を1画面で見る
#   deploy/macmini.sh run <cmd...>    運用機の scripts/ で venv + KS_DATA_DIR 付きで実行する
#   deploy/macmini.sh pull-data [rsync opts]
#                                     運用機 → 開発機へデータを取り寄せる (一方向。ir_docs は除外)
#   deploy/macmini.sh counts          主要データの件数を運用機と開発機で並べる
#
# 接続先は ~/.ssh/config の Host macmini (Tailscale 経由)。MACMINI_HOST で上書きできる。
# キーチェーンを使う操作 (claude のログイン、tunnel の API キー登録) は SSH からは
# できないので、運用機の画面で行う (doc/OPERATIONS.md 参照)。
set -eu

HOST="${MACMINI_HOST:-macmini}"
REPO="/Users/k_sohara/dev/shintakane"

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

cmd_status() {
  # shellcheck disable=SC2029
  ssh "$HOST" "REPO=$REPO bash -s" <<'REMOTE'
set -u
uid=$(id -u)
echo "== LaunchAgent =="
for label in com.k_sohara.shintakane.cron com.k_sohara.shintakane.webapp com.k_sohara.shintakane-tunnel; do
  info=$(launchctl print "gui/$uid/$label" 2>/dev/null)
  if [ -z "$info" ]; then
    printf '  %-34s 未ロード\n' "$label"
    continue
  fi
  state=$(printf '%s\n' "$info" | grep -m1 -E '^\s+state = ' | sed 's/.*= //')
  code=$(printf '%s\n' "$info" | grep -m1 'last exit code' | sed 's/.*= //')
  runs=$(printf '%s\n' "$info" | grep -m1 -E '^\s+runs = ' | sed 's/.*= //')
  # kickstart -k で落としたジョブには last exit code 行が出ない
  printf '  %-34s %-12s runs: %-3s last exit: %s\n' "$label" "$state" "${runs:--}" "${code:--}"
done

echo "== WebApp =="
printf '  localhost:5001 -> HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5001/)"
ts=/Applications/Tailscale.app/Contents/MacOS/tailscale
[ -x "$ts" ] && "$ts" serve status 2>/dev/null | grep -m1 'tailnet only' | sed 's/^/  serve: /'

echo "== バッチ (各ログの最終実行) =="
for name in shintakane make_stock_db exposure_guide theme_news compact; do
  f="$REPO/logs/$name.log"
  [ -f "$f" ] || { printf '  %-15s ログなし\n' "$name"; continue; }
  # 最後の「開始」行以降に Traceback があれば失敗とみなす
  last=$(grep '開始 =====' "$f" | tail -1 | sed -E 's/.*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}).*/\1/')
  errs=$(awk '/開始 =====/{n=0} /Traceback/{n++} END{print n+0}' "$f")
  if [ "$errs" -gt 0 ]; then mark="❌ Traceback ${errs}件"; else mark="✅"; fi
  printf '  %-15s %s  %s\n' "$name" "${last:-不明}" "$mark"
done
running=$(pgrep -f 'shintakane.py|make_stock_db.py|exposure_guide.py|run_theme_news.py' | wc -l | tr -d ' ')
[ "$running" -gt 0 ] && echo "  ⏳ 実行中のバッチプロセス: ${running}件"

echo "== コード =="
cd "$REPO" && git fetch -q origin 2>/dev/null
head=$(git rev-parse --short HEAD); origin=$(git rev-parse --short origin/main 2>/dev/null)
if [ "$head" = "$origin" ]; then echo "  HEAD $head (origin/main と一致)"
else echo "  ⚠️ HEAD $head / origin/main $origin (次の日次バッチで pull される)"; fi
dirty=$(git status --short | grep -vc '^??')
[ "$dirty" -gt 0 ] && echo "  ⚠️ 未コミットの変更 ${dirty}件 (運用機ではローカル変更をしない)"

echo "== データ =="
set -a; . "$HOME/.shintakane_env"; set +a
printf '  stocks_shelve.dat  %s\n' "$(ls -lh "$KS_DATA_DIR/stock_data/stocks_shelve.dat" | awk '{print $5}')"
printf '  ディスク空き       %s\n' "$(df -h / | tail -1 | awk '{print $4}')"
echo
echo "(claude のログインと tunnel の API キーはキーチェーンにあり、SSH からは確認できない)"
REMOTE
}

cmd_run() {
  [ $# -gt 0 ] || usage
  # 引数をリモートのシェルで安全に再解釈できるようクォートする
  local quoted
  quoted=$(printf '%q ' "$@")
  # shellcheck disable=SC2029
  ssh "$HOST" "set -a; . ~/.shintakane_env; set +a; unset FLASK_SECRET_KEY;
    cd $REPO/scripts && PATH=$REPO/.venv/bin:\$PATH $quoted"
}

cmd_pull_data() {
  : "${KS_DATA_DIR:?KS_DATA_DIR が未設定です (取り寄せ先)}"
  # 開発機の WebApp が開いている DB を下から上書きしない
  if lsof -tiTCP:5001 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "❌ 開発機で WebApp (5001) が起動中です。止めてから実行してください" >&2
    exit 1
  fi
  # 書き込み途中の shelve を持ってこない
  if ssh "$HOST" "pgrep -f 'shintakane.py|make_stock_db.py|exposure_guide.py|run_theme_news.py'" >/dev/null; then
    echo "❌ 運用機でバッチが実行中です。終わってから実行してください" >&2
    exit 1
  fi
  echo "運用機 → $KS_DATA_DIR"
  # ir_docs は運用機側が Drive への symlink、開発機側がローカルの実ディレクトリ。
  # 除外しないと実ディレクトリを symlink で上書きしようとする (開発用は初回コピーのまま使う)
  rsync -a --exclude 'ir_docs' --exclude '*.lock' --exclude '*.dbm.lock' --exclude '.DS_Store' \
    "$@" "$HOST:/Users/k_sohara/shintakane_data/" "$KS_DATA_DIR"/
  echo "✅ 完了"
}

cmd_counts() {
  : "${KS_DATA_DIR:?KS_DATA_DIR が未設定です (開発機側のデータ)}"
  local scripts remote local_
  scripts="$(cd "$(dirname "$0")/../scripts" && pwd)"
  # 開発機のスクリプトを stdin で渡す (運用機が pull する前でも同じ定義で数える)
  # shelve の open/close ログ (stderr) は捨て、エラーだけ残す
  remote=$(cmd_run python - < "$scripts/data_counts.py" 2> >(grep -v '^shelveDB ' >&2))
  local_=$(cd "$scripts" && ../.venv/bin/python data_counts.py 2> >(grep -v '^shelveDB ' >&2))
  # printf の幅はバイト数で数えるため、全角の見出しは直書きする
  echo "                 運用機     開発機"
  # 同じ順で出力されるので行ごとに並べる。差があれば印を付ける
  paste <(printf '%s\n' "$remote") <(printf '%s\n' "$local_") | while read -r name r _ l; do
    mark=""; [ "$r" = "$l" ] || mark="  ← 差"
    printf '%-12s %10s %10s%s\n' "$name" "$r" "$l" "$mark"
  done
}

sub="${1:-}"
[ $# -gt 0 ] && shift
case "$sub" in
  status)    cmd_status ;;
  run)       cmd_run "$@" ;;
  pull-data) cmd_pull_data "$@" ;;
  counts)    cmd_counts ;;
  *)         usage ;;
esac
