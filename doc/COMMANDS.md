# 開発コマンドリファレンス

すべてのスクリプトは `scripts/` ディレクトリから実行する。

## 環境セットアップ

```bash
source .venv/bin/activate
```

## メイン分析

```bash
# スクレイピング + 分析 + ランキング
cd scripts && python shintakane.py

# スクレイピングなしで既存データのみ分析
cd scripts && python shintakane.py analyze
```

## 銘柄DB操作 (`make_stock_db.py`)

```bash
# DB全銘柄のランキング更新 + CSV出力
cd scripts && python make_stock_db.py list_all_db

# 特定銘柄の更新 (引数で指定可。未指定時はソース内デフォルト)
cd scripts && python make_stock_db.py update 6324             # 単一銘柄
cd scripts && python make_stock_db.py update 6324 7203 215A   # 複数銘柄
cd scripts && python make_stock_db.py update 6324 --snapshot  # 更新後にスナップショットも自動追記

# 特定銘柄データの表示 (引数で指定可)
cd scripts && python make_stock_db.py list 6324

# 上場廃止銘柄のクリーンアップ
cd scripts && python make_stock_db.py reflesh

# DBバックアップ
cd scripts && python make_stock_db.py backup

# PTSランキング再取得 + 当日決算銘柄の kessan_comments['pts'] 上書き
# (list_all_db を回さず PTS だけ最新化したい時に使う)
cd scripts && python make_stock_db.py refresh_pts

# 指定銘柄の master/price/shihyo/gyoseki/rironkabuka を強制再取得
# (株探で上方修正/最新業績が反映されたとき、特定銘柄だけ手早く最新化したい時に使う)
# 決算速報 (kessan_quarter / kessan_mod_date) は別経路のため、必要なら shintakane.py を別途実行
cd scripts && python make_stock_db.py refresh_stock 421A
cd scripts && python make_stock_db.py refresh_stock 421A 6324 3496  # 複数銘柄

# モメンタムポイント動的キャリブレーション (issue #104)
cd scripts && python make_stock_db.py calibrate_momentum
```

## 市場DB操作

```bash
cd scripts && python make_market_db.py
```

## 銘柄調査DB (`research_shelve`)

```bash
cd scripts && python research_shelve.py show 3496
cd scripts && python research_shelve.py list --rating S,A --keyword 駐車場
cd scripts && python research_shelve.py backup
```

## Shintakane Research（銘柄調査WebApp）

```bash
cd scripts && python -m webapp.app    # http://localhost:5001 で起動
```

## データ移行スクリプト

### スプシCSV → research_shelve 移行 (issue #92)

```bash
cd scripts && python migrate_research_from_csv.py "<csv_path>" --dry-run                                   # DB を触らず検証
cd scripts && python migrate_research_from_csv.py "<csv_path>" --db-path /tmp/verify --show 3496,247A,6920 # 一時DBで目視確認
cd scripts && python migrate_research_from_csv.py "<csv_path>"                                             # 本番移行
```

### 過去決算メモ log → research_shelve.kessan_comments 移行 (issue #131)

```bash
cd scripts && python migrate_kessan_comments_from_log.py ../data/kessan_comments_log.txt --dry-run                                   # パースのみ検証
cd scripts && python migrate_kessan_comments_from_log.py ../data/kessan_comments_log.txt --db-path /tmp/verify_kessan --show 5032,9556 # 一時DBで目視確認
cd scripts && python migrate_kessan_comments_from_log.py ../data/kessan_comments_log.txt                                             # 本番移行
```

## 自動実行

`shintakane_cron.sh` が `shintakane.py` → `make_stock_db.py` を逐次実行。macOS launchd（`com.k_sohara.shintakane.cron.plist`）で平日19:00に定期実行。

詳細仕様:
- 平日(月〜金)19:00 の `StartCalendarInterval` が定刻発火
- `StartInterval=1800` (30分ごと) でスリープ復帰後の最初のwakeも補完
- `RunAtLoad=true` でログイン/再起動時にも起動
- shintakane_cron.sh の冒頭ガードで「19時以降」「当日未実行」の最初の起動でのみ Python 起動 (`~/.shintakane_cron_last_run` フラグ管理)
- 両プロセス (shintakane.py / make_stock_db.py) 成功時のみフラグを更新

## テスト

テスト方針・テストファイル一覧・統合テスト手順は [TESTING.md](TESTING.md) を参照。
