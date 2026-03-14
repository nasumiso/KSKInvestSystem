# テスト方針

## ユニットテスト

pytestで主要モジュールの純粋計算関数をテスト（DB・HTTP通信不要）。

```bash
# 全テスト実行
pytest tests/ -v

# CIと同じ条件（local_db除外）
pytest tests/ -v -m "not local_db"

# 特定モジュールのみ
pytest tests/test_gyoseki.py -v
```

### テストファイル一覧

| テストファイル | 対象モジュール | 備考 |
|---|---|---|
| `test_ks_util.py` | `ks_util.py` | **変更時は全テスト実行** |
| `test_rironkabuka.py` | `rironkabuka.py` | |
| `test_gyoseki.py` | `gyoseki.py` | |
| `test_price.py` | `price.py` | |
| `test_make_stock_db.py` | `make_stock_db.py` | |
| `test_db_shelve.py` | `db_shelve.py` | |
| `test_shihyou.py` | `shihyou.py` | |
| `test_master.py` | `master.py` | |
| `test_shintakane.py` | `shintakane.py` | HTMLパース関数 |
| `test_make_market_db.py` | `make_market_db.py` | |

### CI

GitHub Actions（`.github/workflows/test.yml`）でPR/push時に自動実行。

## HTMLパース変更時の検証（shintakane.py --force）

`shintakane.py` はHTML取得結果をCSVにキャッシュし、同日中は再生成をスキップする。HTMLパースのロジックを変更した場合、既存CSVが残っているとパース修正が反映されない。

```bash
cd scripts && python shintakane.py --force
```

`--force` を付けるとCSV存在チェックをスキップし、HTMLキャッシュからCSVを再生成する。パース変更後は必ず `--force` で実行して `shintakane_result.csv` に反映されることを確認すること。

## 統合テスト（make_stock_db.py サブコマンド）

スコアリングやランキングのロジックを変更した場合、ローカルDB上で実際の銘柄データを使って検証する。

```bash
source .venv/bin/activate
cd scripts

# ランキング全体を再生成して確認
python make_stock_db.py list_all_db
```

**注意: コンソールに出力されない。** `log_print` 経由のためすべてログファイルとCSVに出力される。確認先:
- **ログ**: `logs/make_stock_db.log`（処理経過・エラー）
- **ランキングCSV**: `data/code_rank_data/code_rank.csv`（最終結果）
- 正常終了時はGoogle Driveへの自動アップロードも実行される（`Upload Complete` ログで確認）
