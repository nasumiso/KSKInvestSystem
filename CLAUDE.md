# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

日本株式市場の成長株分析システム。株探・Yahoo Finance Japanからデータをスクレイピングし、ファンダメンタルズ・モメンタム・テクニカル指標で銘柄をスコアリング・ランキングする。

## コーディング規約

- **コメント・docstringは日本語で記述**。技術用語・関数名・外部ライブラリ名は英語のまま。
- 銘柄コードは常に**文字列** (`code_s`) を使用。`"0001"`〜`"9999"` や `"215A"` 形式。レガシーの `code` (int) は非推奨。
- ロギングは `log_print`, `log_debug`, `log_warning`, `log_error` を使用（`ks_util.py`）。直接の `print()` は不可。
  - `log_print`（INFO）: フェーズ開始/完了マーカー、サマリー、重要な処理経過など**運用時に必要な情報**
  - `log_debug`（DEBUG）: 個別銘柄の中間値、per-row詳細、キャッシュ判定など**デバッグ時のみ必要な情報**
  - ファイルハンドラは通常INFOレベル。`KS_LOG_DEBUG=1` 環境変数でDEBUGレベルに切替可能
  - 新規ログ追加時は上記の基準で `log_print` / `log_debug` を使い分けること
- DB操作は `update_db_rows()` を経由。バルク操作は `sync=False` で非同期化可能。
- 日付判定は `ks_util.get_price_day()` を使用（18:00前は前日扱い）。

## アーキテクチャ

### コアデータフロー

1. **データ取得** (`shintakane.py`): 株探から新高値・出来高急増銘柄をスクレイピング → 候補リスト生成
2. **DB更新パイプライン** (`make_stock_db.py` の `update_db_rows()`):
   - master（`master.py`）→ price（`price.py`）→ gyoseki（`gyoseki.py`）→ shihyo（`shihyou.py`）→ rironkabuka（`rironkabuka.py`）
3. **ランキング** (`make_stock_db.py` の `list_all_db()`): 業績40% + 指標20% + モメンタム25% + ファンダメンタルズ15% の総合スコア → CSV出力
4. **市場分析** (`make_market_db.py`): 指数追跡、セクター/テーマ強度分析

### 株式データベース（shelveベース）

メインDBはpickleからshelveに移行済み（`USE_SHELVE = True`）。
- **shelve DB**: `data/stock_data/stocks_shelve`（`db_shelve.py` の `ShelveDB` クラス）
- **レガシーpickle**: `data/stock_data/stocks.pickle`（`STOCKS_PICKLE`定数で参照、後方互換用に残存）
- DBアクセスは `db_shelve.get_stock_db()` でシングルトン取得 → `with` 文で使用
- `ShelveDB` はスレッドセーフ（RLock）、メモ化キャッシュ（`enable_memo()`）対応
- バックエンドは `dbm.dumb`（macOSの `dbm.ndbm` はハッシュ衝突問題があるため）

### キャッシュ戦略

3層の階層構造で、`UPD_*` 定数（`ks_util.py`）が全層を横断して制御する。

**UPD_* 定数（キャッシュ鮮度レベル）:**

| 定数 | 値 | DB層 | ファイル層 | HTTP層 |
|------|---|------|-----------|--------|
| `UPD_CACHE` | 0 | DBにあればそのまま使用 | ファイルがあれば使用 | キャッシュ使用 |
| `UPD_INTERVAL` | 1 | TTL超過時のみ更新 | TTL超過時のみ再取得 | キャッシュ使用 |
| `UPD_REEVAL` | 2 | DBキャッシュ無視、再評価 | TTL超過時のみ再取得 | キャッシュ使用 |
| `UPD_FORCE` | 3 | すべて無視 | すべて無視 | 強制取得 |

**① DB層** (`make_stock_db.py`):
- `has_*_data(stocks, code_s, latest)` でshelve DB上のレコード鮮度を確認（`access_date_*` フィールド）
- `has_active_dbdata()` ヘルパーで共通化。決算発表後は `need_kessan_upd()` で強制更新
- `_update_db_code()` 内で `has_*` → False の場合のみ `get_*_data()` を呼ぶ2段階制御

**② ファイル層** (各モジュール):
- `is_file_timestamp(fname, interval_day)` (`price.py`): ファイル更新日時を営業日ベースでTTL判定
- `is_cache_latest(url, interval_day)` (`rironkabuka.py`): HTMLキャッシュファイルのTTL判定

| モジュール | データ種別 | TTL | チェック関数 |
|-----------|-----------|-----|------------|
| `price.py` | 日次株価 | 1日 | `is_file_timestamp()` |
| `price.py` | 週次株価 | 7日 | `is_file_timestamp()` |
| `price.py` | yfinance JSON | 1日 | `is_file_timestamp()` |
| `make_stock_db.py` | マスター情報 | 7日 | `access_date` 直接比較 |
| `make_stock_db.py` | 指標 | 5日 | `access_date_shihyo` 直接比較 |
| `gyoseki.py` | 業績 | 15日 | `is_cache_latest()` |
| `rironkabuka.py` | 理論株価 | 5日 | `is_cache_latest()` |

**③ HTTP層** (`ks_util.py`):
- `http_get_html()` の `use_cache` パラメータでファイルキャッシュの読み書きを制御
- キャッシュファイル名は `get_http_cachname(url)` でURL→ファイル名変換

**④ メモリ層**:
- `ShelveDB.enable_memo()`: 読み取り集中処理時のインメモリキャッシュ（`db_shelve.py`）
- `memoize()` デコレータ: `load_pickle`, `load_file` のメモ化（`ks_util.py`）

### 価格データ取得 (`price.py`)

- **yfinance API** (`USE_YFINANCE = True`): Yahoo FinanceのJSON APIで日次価格データを取得（デフォルト）
  - `yf.download()` で100銘柄バッチ取得可能（`prefetch_yfinance_batch()`）
  - キャッシュ: `data/stock_data/yahoo/price/yfinance_price_{code_s}.json`（1日TTL）
  - 失敗時はHTMLスクレイピングに自動フォールバック
  - `USE_YFINANCE = False` で即座にHTMLスクレイパーに戻せる
- **HTMLスクレイピング**（フォールバック）: Yahoo Finance JapanのHTMLをパース（旧方式）
- **指標計算**: `parse_price_text_from_list()` でyfinance/HTML共通の指標計算（売り圧力レシオ、ポケットピボット等）

### 主要テクニカル指標 (`price.py`)

- **RS（相対力指数）**: 13/26/39/52週株価の加重比較
- **モメンタムポイント**: TOPIX RSに対して正規分布で正規化
- **トレンドテンプレート**: MA関係・52週ポジション・RSしきい値の7点チェック
- **ポケットピボット**: MA付近で下げ日出来高を上回る高出来高上昇日

### セッション管理

- **スレッドローカル**: `use_requests_session()` — シングルスレッド用
- **グローバル**: `use_requests_global_session()` — マルチスレッド用
- **直接**: セッション非使用時は `requests.get()` フォールバック
- `update_db_rows_async()` はThreadPoolExecutor（5ワーカー）使用、同時HTTP数は `MAX_REQUESTS=3` で制限

## 開発コマンド

すべてのスクリプトは `scripts/` ディレクトリから実行:

```bash
# 環境セットアップ
source .venv/bin/activate

# メイン分析（スクレイピング + 分析 + ランキング）
cd scripts && python shintakane.py

# スクレイピングなしで既存データのみ分析
cd scripts && python shintakane.py analyze

# DB全銘柄のランキング更新 + CSV出力
cd scripts && python make_stock_db.py list_all_db

# 特定銘柄の更新（main()内のcode_listを編集して実行）
cd scripts && python make_stock_db.py update

# 特定銘柄データの表示
cd scripts && python make_stock_db.py list

# 上場廃止銘柄のクリーンアップ
cd scripts && python make_stock_db.py reflesh

# DBバックアップ
cd scripts && python make_stock_db.py backup

# 市場データ更新
cd scripts && python make_market_db.py
```

### 自動実行

`shintakane_cron.sh` が `shintakane.py` → `make_stock_db.py` を順次実行。macOS launchd（`com.k_sohara.shintakane.cron.plist`）で定期実行。

### テスト

pytestで主要モジュールの純粋計算関数をテスト（DB・HTTP通信不要）。

```bash
# 全テスト実行
pytest tests/ -v

# CIと同じ条件（local_db除外）
pytest tests/ -v -m "not local_db"

# 特定モジュールのみ
pytest tests/test_gyoseki.py -v
```

| テストファイル | 対象モジュール |
|---|---|
| `test_ks_util.py` | `ks_util.py`（**変更時は全テスト実行**） |
| `test_rironkabuka.py` | `rironkabuka.py` |
| `test_gyoseki.py` | `gyoseki.py` |
| `test_price.py` | `price.py` |
| `test_make_stock_db.py` | `make_stock_db.py` |
| `test_db_shelve.py` | `db_shelve.py` |
| `test_shihyou.py` | `shihyou.py` |
| `test_master.py` | `master.py` |
| `test_make_market_db.py` | `make_market_db.py` |

GitHub Actions（`.github/workflows/test.yml`）でPR/push時に自動実行。

### 統合テスト（make_stock_db.py サブコマンド）

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

## データ保存場所

- **メインDB (shelve)**: `data/stock_data/stocks_shelve`
- **市場DB**: `data/market_data/market_db_shelve`
- **HTTPキャッシュ**: `data/cache_data/`
- **株価履歴**: `data/stock_data/yahoo/price/`（yfinance JSON + レガシーHTML）, `data/stock_data/kabutan/price/`
- **市場指数**: `data/sisu_data/`
- **結果CSV**: `data/shintakane_result_data/`, `data/code_rank_data/`
- **ログ**: `logs/`（TimedRotatingFileHandler、7日保持、通常INFOレベル、`KS_LOG_DEBUG=1` でDEBUG出力）

## 重要な注意事項

### スクレイピング元のHTML変更対応

yfinance使用時はYahoo側のHTMLフォーマット変更の影響を受けない。
HTMLスクレイピングフォールバック使用時のデータ取得失敗時:
1. Yahoo: `price.py` の `parse_price_text_yahoo_new()` を確認
2. Kabutan: `shintakane.py` の `convert_kabutan_*_html()` を確認

### DB変更時の注意

- shelve DBスキーマの後方互換性を維持すること
- `make_stock_db.py` のload/saveロジックを変更する場合はマイグレーションスニペットを追加
- DB（shelve/pickle）への並行書き込みは禁止 — 提供されたAPI経由で操作
- Google Drive認証ファイル (`data/googledrive/`) はコミット禁止

### ETFフィルタリング

ETFコードは `data/ETF_code.txt` から読み込み、株式分析対象外とする。

## Python環境

- **Python 3.9+**（`.venv/` の仮想環境）
- 主な依存: `requests`, `scipy`, `yfinance`, `pandas`, Google API ライブラリ群, `oauth2client`
- `requirements.txt` に全依存を記載
