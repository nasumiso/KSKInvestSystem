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

# 仮想環境を有効化
echo "仮想環境を有効化します"
source ../.venv/bin/activate

# 実行ログ
echo "shintakane.py と make_stock_db.py を実行します"
python shintakane.py > ../logs/shintakane.log 2>&1
RET1=$?
if [ $RET1 -ne 0 ]; then
  echo "❌ エラー: shintakane.py （終了コード: $RET1）"
else
  echo "✅ 正常終了: shintakane.py"
fi

python make_stock_db.py > ../logs/make_stock_db.log 2>&1
RET2=$?
if [ $RET2 -ne 0 ]; then
  echo "❌ エラー: make_stock_db.py （終了コード: $RET2）"
else
  echo "✅ 正常終了: make_stock_db.py"
fi