# アーキテクチャ詳細

## コアデータフロー

1. **データ取得** (`shintakane.py`): 株探から新高値・出来高急増銘柄をスクレイピング → 候補リスト生成
2. **DB更新パイプライン** (`make_stock_db.py` の `update_db_rows()`):
   - master（`master.py`）→ price（`price.py`）→ gyoseki（`gyoseki.py`）→ shihyo（`shihyou.py`）→ rironkabuka（`rironkabuka.py`）
3. **ランキング** (`make_stock_db.py` の `list_all_db()`): 業績40% + 指標20% + モメンタム25% + ファンダメンタルズ15% の総合スコア → CSV出力
4. **市場分析** (`make_market_db.py`): 指数追跡、セクター/テーマ強度分析

## 株式データベース（shelveベース）

メインDBはpickleからshelveに移行済み（`USE_SHELVE = True`）。
- **shelve DB**: `data/stock_data/stocks_shelve`（`db_shelve.py` の `ShelveDB` クラス）
- **レガシーpickle**: `data/stock_data/stocks.pickle`（`STOCKS_PICKLE`定数で参照、後方互換用に残存）
- DBアクセスは `db_shelve.get_stock_db()` でシングルトン取得 → `with` 文で使用
- `ShelveDB` はスレッドセーフ（RLock）、メモ化キャッシュ（`enable_memo()`）対応
- バックエンドは `dbm.dumb`（macOSの `dbm.ndbm` はハッシュ衝突問題があるため）

## キャッシュ戦略

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

## 価格データ取得 (`price.py`)

- **yfinance API** (`USE_YFINANCE = True`): Yahoo FinanceのJSON APIで日次価格データを取得（デフォルト）
  - `yf.download()` で100銘柄バッチ取得可能（`prefetch_yfinance_batch()`）
  - キャッシュ: `data/stock_data/yahoo/price/yfinance_price_{code_s}.json`（1日TTL）
  - 失敗時はHTMLスクレイピングに自動フォールバック
  - `USE_YFINANCE = False` で即座にHTMLスクレイパーに戻せる
- **HTMLスクレイピング**（フォールバック）: Yahoo Finance JapanのHTMLをパース（旧方式）
- **指標計算**: `parse_price_text_from_list()` でyfinance/HTML共通の指標計算（売り圧力レシオ、ポケットピボット等）

## 主要テクニカル指標 (`price.py`)

- **RS（相対力指数）**: 13/26/39/52週株価の加重比較
- **モメンタムポイント**: TOPIX RSに対して正規分布で正規化
- **トレンドテンプレート**: MA関係・52週ポジション・RSしきい値の7点チェック
- **ポケットピボット**: MA付近で下げ日出来高を上回る高出来高上昇日

## セッション管理

- **スレッドローカル**: `use_requests_session()` — シングルスレッド用
- **グローバル**: `use_requests_global_session()` — マルチスレッド用
- **直接**: セッション非使用時は `requests.get()` フォールバック
- `update_db_rows_async()` はThreadPoolExecutor（5ワーカー）使用、同時HTTP数は `MAX_REQUESTS=3` で制限

## データパス解決

`DATA_DIR` は `ks_util.py` の `_resolve_data_dir()` で以下の優先順位で解決される:

1. **環境変数 `KS_DATA_DIR`**: 明示指定。`data/` を別の場所に移動した場合に使用
2. **Git commondir**: `git rev-parse --git-common-dir` でメインリポジトリの `.git/` を取得し、その親の `data/` を参照。ワークツリーからメインの `data/` を自動検出する
3. **フォールバック**: `ROOT_DIR/data`（従来通り `__file__` 起点）

```
# data/ を別の場所に移動した場合
export KS_DATA_DIR=/path/to/new/data

# ワークツリーからの実行時は自動でメインの data/ を参照（設定不要）
```

`LOGS_DIR` は常に `ROOT_DIR/logs`（ワークツリー側）を使用し、ログはワークツリーごとに分離される。

## データ保存場所

- **メインDB (shelve)**: `data/stock_data/stocks_shelve`
- **市場DB**: `data/market_data/market_db_shelve`
- **HTTPキャッシュ**: `data/cache_data/`
- **株価履歴**: `data/stock_data/yahoo/price/`（yfinance JSON + レガシーHTML）, `data/stock_data/kabutan/price/`
- **市場指数**: `data/sisu_data/`
- **結果CSV**: `data/shintakane_result_data/`, `data/code_rank_data/`
- **ログ**: `logs/`（TimedRotatingFileHandler、7日保持、通常INFOレベル、`KS_LOG_DEBUG=1` でDEBUG出力）
