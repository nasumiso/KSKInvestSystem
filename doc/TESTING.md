# テスト方針

## テスト階層

| 層 | 目的 | 外部依存 | マーカー | CI |
|----|------|----------|----------|-----|
| 単体テスト | 純粋関数の計算検証 | なし | なし | ○ |
| 機能テスト | ビジネスフローの結合検証 | HTTP→モック、DB→tmp_path | `functional` | ○ |
| Live検証 | HTMLフォーマット変更検知 | 実HTTP通信 | `live_html` | × |

```bash
# CI（単体 + 機能テスト）
pytest tests/ -v -m "not local_db and not live_html"

# 機能テストのみ
pytest tests/ -v -m "functional"

# 単体テストのみ（高速）
pytest tests/ -v -m "not local_db and not live_html and not functional"

# 特定モジュールのみ
pytest tests/test_gyoseki.py -v
```

## 機能テスト

複数モジュールを結合し、ビジネスフロー全体の正常動作を検証するテスト。HTTP通信・Google Driveはモックし、shelve DBは`tmp_path`に一時作成して実際のShelveDBクラスで読み書きする。

### テストファイル一覧

| テストファイル | 対象フロー | 備考 |
|---|---|---|
| `test_functional_market.py` | `update_market_db()` → `create_market_csv()` | テーマランク差分・冪等性・日付変更 |

### 機能テスト追加の方針

- **モックするもの**: 外部I/O境界（HTTP通信、Google Drive API）
- **モックしないもの**: パース処理、計算ロジック、shelve DB操作（tmp_path使用）、CSV生成
- **冪等性テスト**: 同日複数回実行でも結果が安定することを必ず検証
- **フィクスチャ**: `tests/fixtures/` にHTML等の固定データを配置

### 既存機能の修正時

既存機能を修正する際は、**修正対象に関連する機能テストを必ず実行**して回帰がないことを確認すること。

```bash
# make_market_db 周りの修正時
pytest tests/test_functional_market.py tests/test_make_market_db.py -v
```

機能テストが存在しないモジュールを修正する場合は、修正の影響範囲に応じて機能テストの追加を検討する。

## 単体テスト

pytestで主要モジュールの純粋計算関数をテスト（DB・HTTP通信不要）。

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
| `test_shintakane.py` | `shintakane.py` | HTMLパース関数（決算含む） |
| `test_kessan.py` | `kessan.py` | 決算判定・タグ生成 |
| `test_make_market_db.py` | `make_market_db.py` | |
| `test_functional_market.py` | `make_market_db.py` | 機能テスト（`functional`マーカー） |
| `test_live_html.py` | 全パーサー | HTMLフォーマット変更検知（`live_html`マーカー） |

### CI

GitHub Actions（`.github/workflows/test.yml`）でPR/push時に自動実行。

```bash
# CIと同じ条件（local_db, live_html除外）
pytest tests/ -v -m "not local_db and not live_html"
```

## HTMLパース変更時の検証（shintakane.py --force）

`shintakane.py` はHTML取得結果をCSVにキャッシュし、同日中は再生成をスキップする。HTMLパースのロジックを変更した場合、既存CSVが残っているとパース修正が反映されない。

```bash
cd scripts && python shintakane.py --force
```

`--force` を付けるとCSV存在チェックをスキップし、HTMLキャッシュからCSVを再生成する。パース変更後は必ず `--force` で実行して `shintakane_result.csv` に反映されることを確認すること。

## HTMLフォーマット変更検知テスト（live_html）

実際にHTTPでkabutan.jpにアクセスし、各パーサーが期待通りにデータを抽出できるかを確認するテスト。CIでは除外され、ローカルで手動実行する。

```bash
# 全パーサーの検知テスト実行
pytest tests/test_live_html.py -v
```

### いつ実行するか

- データ取得でパースエラーや空データが発生した時
- kabutan.jpのHTMLフォーマット変更が疑われる時
- `log_warning("決算ページフォーマット変更?")` 等の警告がログに出た時

### テスト対象と対応モジュール

| テストクラス | 対応モジュール | 確認内容 |
|---|---|---|
| `TestLiveHtmlPrice` | `price.py` | 日足HTML取得・パース |
| `TestLiveHtmlShihyou` | `shihyou.py` | 財務指標・時価総額抽出 |
| `TestLiveHtmlMaster` | `master.py` | 銘柄基本情報抽出 |
| `TestLiveHtmlGyoseki` | `gyoseki.py` | 業績データ抽出 |
| `TestLiveHtmlShintakane` | `shintakane.py` | 新高値銘柄パース |
| `TestLiveHtmlKessan` | `shintakane.py` | 決算速報パース |
| `TestLiveHtmlTheme` | `make_market_db.py` | テーマランクパース |

失敗したテストクラスから対応モジュールのパーサーを修正する。

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
