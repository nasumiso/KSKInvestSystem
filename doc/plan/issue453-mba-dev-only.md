# issue #453 MBA を開発専用にする

## 背景
2026-09-27 に運用機 (MacMini) へ移行済み。正本は MacMini の `~/shintakane_data` (ローカル SSD、`ir_docs` のみ Drive への symlink)。
MBA は `KS_DATA_DIR=~/Ext/GoogleDrive/shintakane_data` (Drive ミラー同期下の旧正本) のまま。
残るリスク:
- MBA で `shintakane_cron.sh` や `make_stock_db.py list_all_db` を叩くと、古い MBA データで Google Sheets (code_rank / shintakane_result) を上書きする。cron は theme-news (claude, 約 $2.4) も二重課金する
- MBA の localhost WebApp が正本に見え、入力が消える

## 変更 (リポジトリ)

1. `ks_util.is_dev_data_dir()` を追加: `os.path.basename(DATA_DIR).endswith("_dev")`
   - 判定を1か所にし、下記 2〜4 で使う
2. `googledrive._upload_with_lock()` の先頭で `is_dev_data_dir()` なら `log_print` してスキップ。加えて `get_drive_service()` (書き込み専用) と `upload_csv_via_sheets()` の入口で RuntimeError (直接呼び出し経路: `googledrive.main`)。`get_sheets_service()` は読み取り (`reimport_rich_text`) にも使うので止めない
   - 全 async アップロード (upload_csv_async / upload_html_async) の共通経路。個別スクリプト直叩きも防げる
3. `shintakane_cron.sh`: 19時前判定・auto-pull より前で、`KS_DATA_DIR` の末尾が `_dev` なら ❌ を出して exit 1 (theme-news の課金・一連の外部書き込みを丸ごと止める)
4. WebApp: `context_processor` で `is_dev_data` を注入、`base.html` の nav 上に「DEV — 開発用コピー (更新日時)。入力は正本に反映されません。普段使いは http://kosukemac-mini:5001/」の帯を出す (運用機では出ない)
   - 更新日時は `stock_data/stocks_shelve.dat` の mtime (pull-data の鮮度の目安)
5. `shintakane_research.sh` の既定パスを `~/shintakane_data_dev` に
6. ドキュメント: CLAUDE.md (KS_DATA_DIR の説明を「開発機は `~/shintakane_data_dev`、正本は運用機・`deploy/macmini.sh run`」に、存在しない `shintakane.py analyze` の行を削除)、OPERATIONS.md「MBA での使い分け」、scripts/mcp/README.md の設定例パス
7. テスト (2本程度): アップロードスキップ、DEV 帯の表示有無 (parametrize)

## 変更 (MBA 環境・リポジトリ外)

- `~/shintakane_data_dev/` を作り `deploy/macmini.sh pull-data` で運用機から取得
- `ir_docs` は **Drive の旧フォルダからローカルにコピー** (symlink にしない)
  - WebApp の ir_docs 収集 (POST) が各銘柄の `index.json` を書くため、symlink だと開発機の操作が運用機の正本に波及する (受け入れ条件違反)
  - pull-data は引き続き ir_docs を除外。開発用 ir_docs は古くなるが開発には足りる
- `.zshrc` の `KS_DATA_DIR` と `~/.claude.json` の shintakane-shikiho MCP の env を切り替え
- 旧 `~/Ext/GoogleDrive/shintakane_data` は触らない (アーカイブ)。**Drive 上のフォルダを削除・移動・改名しない** — 運用機の `ir_docs` symlink の参照先
  - issue の「Drive ミラー同期から除外」: マイドライブのミラーモードはサブフォルダ単位で除外できず、書き込まなくなれば同期は無害なので行わない
- `/Library/LaunchAgents/` の旧 plist 削除はユーザーが sudo で実施

## 対象外
- stock_ratings.json の Drive 経由読み取り (MCP README「Mac が止まっているとき」): 移行後は Drive 上の JSON が更新されていない既存の問題。本 issue とは別に扱う (報告のみ)

## 検証
- 関連 pytest
- MBA で `KS_DATA_DIR=~/shintakane_data_dev bash shintakane_cron.sh` が即 exit 1
- dev データで `make_stock_db.py list_all_db` 相当を流さず、2 はテストで確認
- MBA WebApp で DEV 帯表示 (スクショ)、運用機で帯なし (`deploy/macmini.sh` 経由 curl で文字列が無いこと、マージ後)
