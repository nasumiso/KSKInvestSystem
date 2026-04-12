# アーキテクチャ詳細

## コアデータフロー

1. **データ取得** (`shintakane.py`): 株探から新高値・出来高急増銘柄をスクレイピング → 候補リスト生成
2. **DB更新パイプライン** (`make_stock_db.py` の `update_db_rows()`):
   - master（`master.py`）→ price（`price.py`）→ gyoseki（`gyoseki.py`）→ shihyo（`shihyou.py`）→ rironkabuka（`rironkabuka.py`）
3. **ランキング** (`make_stock_db.py` の `list_all_db()`): 業績40% + 指標20% + モメンタム25% + ファンダメンタルズ15% の総合スコア → CSV出力
4. **市場分析** (`make_market_db.py`): 指数追跡、セクター/テーマ強度分析

## 市場分析 (`make_market_db.py`)

`update_market_db()` が以下のデータを収集し、市場DB（shelve）に保存する。

### テーマランク

株探のテーマ人気ランキングをスクレイピングし、モメンタムを加味した独自順位を算出する。

**データフロー:**

1. **Kabutan生ランキング取得**: `get_theme_rank_list()` で `theme_rank.html` を取得・パース → 上位30テーマのリスト
2. **数日前のランキング取得**: 同関数内で `get_prev_fname(cach_path, cach_date - timedelta(2))` により2〜3日前のバックアップHTMLを取得・パース
3. **モメンタム順位算出**: `make_theme_data()` でKabutan生ランキングと数日前ランキングの差分（勢い）を加味して再ソート
   - `rank_pt = 31 - rank + moment`（momentは数日前との順位差）
   - 数日前に圏外だったテーマは `prev_rank = 31` として扱う
4. **差分ラベル計算**: 当日のモメンタム順位と**前日のモメンタム順位**（DB上の `prev_theme_rank`）を比較
   - 前日に存在しない → `NEW`
   - 順位上昇 → `↑N`、下降 → `↓N`、変化なし → `←`

**前日モメンタム順位の退避ルール (`update_market_db()`):**

- `prev_theme_rank` は**カレンダー日**（`datetime.today().date()`）が変わった時のみ退避する
- `get_price_day()` は18時境界で日付が変わるため退避判定には使わない（同日中に18時前後で2回実行すると退避が二重に走り「当日vs当日」比較になるため）
- 同日中に何度再実行しても `prev_theme_rank` は前日の値が保持され、差分ラベルは常に「前日の最新モメンタム順位 vs 当日の最新モメンタム順位」で計算される

**用語の区別:**

| 用語 | 説明 | 変数名 |
|------|------|--------|
| Kabutan生ランキング | スクレイピングで取得した元のランキング | `theme_rank_list`, `theme_rank_dict` |
| 数日前ランキング | 2〜3日前のバックアップHTMLから取得した生ランキング | `prev_theme_rank_list`, `prev_theme_rank_dict` |
| モメンタム順位 | 生ランキング + 勢い補正で再ソートした最終順位 | `theme_rank2_list`, `market_db["theme_rank"]` |
| 差分ラベル | 当日モメンタム順位 vs 前日モメンタム順位の変動 | `market_db["theme_rank_diff"]` |

**バックアップ:** `theme_rank.html` は取得のたびに `theme_rank_YYMMDD.html` として `DATA_DIR/market_data/` に日付付きで保存される。モメンタム計算の数日前比較にこのバックアップを使用。

### テーマ別騰落率

`calc_theme_price_momentum()` でDB全銘柄の直近取引日の価格変動をテーマ別に集計。`market_db["theme_momentum"]` に `{テーマ名: (平均騰落率%, 銘柄数)}` として保存。

### 市場指数

以下の指数を `make_db_common()` 経由で取得し、RS・トレンドテンプレート等を算出:

| DB キー | 指数 | 株探コード |
|---------|------|-----------|
| `topix` | TOPIX | `0010` |
| `mothers` | グロース250 | `0012` |
| `nikkei225` | 日経225 | `0000` |
| `nasdaq` | NASDAQ | `0802` |

### 市場DB構造 (`market_data/market_db_shelve`)

| キー | 型 | 内容 |
|------|---|------|
| `theme_rank` | list[str] | モメンタム順位（テーマ名リスト） |
| `theme_rank_diff` | dict[str, int\|None] | 差分ラベル用（正=上昇, 負=下降, None=NEW） |
| `prev_theme_rank` | list[str] | 前日のモメンタム順位（日付変更時に自動退避） |
| `theme_momentum` | dict[str, tuple] | テーマ別騰落率 `(平均%, 銘柄数)` |
| `access_date_theme_rank` | datetime | テーマランク最終取得日時 |
| `topix`, `mothers`, `nikkei225`, `nasdaq` | dict | 各指数のprice/RS/トレンドデータ |
| `distribution_days` | list | ディストリビューション・デイ |
| `direction_signal` | str | 市場方向シグナル |

## 株式データベース（shelveベース）

- **shelve DB**: `data/stock_data/stocks_shelve`（`db_shelve.py` の `ShelveDB` クラス）
- DBアクセスは `db_shelve.get_stock_db()` でシングルトン取得 → `with` 文で使用
- `ShelveDB` はスレッドセーフ（RLock）、メモ化キャッシュ（`enable_memo()`）対応
- バックエンドは `dbm.dumb`（macOSの `dbm.ndbm` はハッシュ衝突問題があるため）

## 銘柄調査データベース（research_shelve）

stocks_shelve（日次更新の揮発性キャッシュ）とは別に、銘柄調査の蓄積データを保持する専用DB。手動メモと決算スナップショットを不可逆な資産として管理する。詳細な設計仕様は [doc/requirements/phase1_requirements.md](requirements/phase1_requirements.md) を参照。

### DB分離の原則

| | stocks_shelve | research_shelve |
|---|---|---|
| 性質 | 揮発性キャッシュ（最新値で上書き） | 不可逆な蓄積資産（時系列＋手動メモ） |
| 更新 | 毎日自動（TTL管理） | 決算検知時に自動追記＋随時手動 |
| 復元 | スクレイピングで再構築可 | 消えたら復元不可 |

依存関係は一方向: stocks_shelve → 読み取り → research_shelve に書き込み。

### レコード構造

`code_s` をキーに、3ブロックで構成:

- **識別・評価**: 銘柄名、総合評価（S〜E）、企業概要、機関投資家コメント
- **手動メモ**: OpenWork、ジムクレイマー、四季報コメント、メモ・総括
- **スナップショット** (list): 決算タイミングごとの IR定量データ・クォリティ指標・理論株価乖離（`date_yy_m` 降順）

### 決算スナップショットの自動追記

`make_stock_db.py` の `update_research_snapshots()` が `list_all_db()` 末尾で実行:

- research_shelve管理銘柄のうち、決算発表日/修正通知日から14日以内（`KESSAN_WINDOW_DAYS`）の銘柄が対象
- `gyoseki`・`shihyou`・`rironkabuka` から自動抽出、`data_source: "auto"` で記録
- 同一 `date_yy_m` のスナップショットは冪等上書き

### モジュール構成

| ファイル | 役割 |
|---------|------|
| `scripts/research_shelve.py` | 基盤（スキーマ・CRUD・バリデーション・CLI） |
| `scripts/webapp/` | ブラウザUI（Flask、閲覧・編集） |
| `scripts/migrate_research_from_csv.py` | スプレッドシートCSVからの移行（実行済み） |

### CLI

```bash
python research_shelve.py show <code_s>                          # 調査データ全表示
python research_shelve.py list [--rating S,A] [--keyword ...]    # フィルタ一覧
python research_shelve.py backup                                 # DBバックアップ
```

### Webアプリ (`scripts/webapp/`)

research_shelveの閲覧・編集用ブラウザUI。Flask製、`http://localhost:5001` で起動。

**主な機能:**
- 銘柄検索・フィルタ（評価ランク、キーワード、銘柄コード）
- 銘柄詳細表示（スナップショット時系列、クォリティ指標、理論株価乖離）
- クリック編集（評価ランク、メモ、四季報コメント、IRコメント）

**構成:**

| ファイル | 役割 |
|---------|------|
| `webapp/app.py` | エントリポイント（`python -m webapp.app`） |
| `webapp/__init__.py` | アプリファクトリ（`create_app()`） |
| `webapp/helpers.py` | research_shelveとのデータ連携層 |
| `webapp/routes/` | ルート定義（search / detail / memo） |
| `webapp/templates/` | Jinja2テンプレート |
| `webapp/static/` | CSS・JS（PicoCSS、変更検知・保存バー） |

**排他制御:** helpers.pyの書き込み操作は `_flock()` でプロセス間排他を行い、バッチスクリプトとの同時実行に対応。

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

## Google Drive アップロード (`googledrive.py`)

分析結果をGoogle Driveにアップロードし、スプレッドシートまたはHTMLとして閲覧可能にする。

### アップロード対象

| 対象 | 形式 | API | 呼び出し元 |
|------|------|-----|-----------|
| `shintakane_result.csv` | Sheets API セル更新 | `upload_csv_via_sheets()` | `shintakane.py` |
| `code_rank.csv` | Sheets API セル更新 | `upload_csv_via_sheets()` | `make_stock_db.py` |
| `market_data.html` | Drive API ファイル更新 | `upload_html()` | `make_market_db.py` |

### 非同期アップロード機構

すべてのアップロードは `upload_csv_async()` / `upload_html_async()` で非同期スレッド化される。

- **スレッド間排他**: `threading.Lock` で同一プロセス内の同時アップロードを防止
- **プロセス間排他**: `fcntl.flock` によるファイルロック（`googledrive/.upload_lock`）でOAuth2認証の競合を防止
- **完了待ち**: `wait_all_uploads()` で全スレッドの完了を保証。失敗・タイムアウト時は例外送出

### cron実行時の並行化 (`shintakane_cron.sh`)

`shintakane.py` をバックグラウンド実行し、main()完了をPID付きフラグファイル（`logs/.shintakane_main_done.{PID}`）で通知。`make_stock_db.py` はフラグ検出後に開始され、`shintakane_result` のアップロードと `make_stock_db.py` のDB処理が並行実行される。

## データ保存場所

- **メインDB (shelve)**: `data/stock_data/stocks_shelve`
- **市場DB**: `data/market_data/market_db_shelve`
- **HTTPキャッシュ**: `data/today_stocks/html_cache/`
- **株価履歴**: `data/stock_data/yahoo/price/`（yfinance JSON + レガシーHTML）, `data/stock_data/kabutan/price/`
- **市場指数**: `data/sisu_data/`
- **結果CSV/HTML**: `data/shintakane_result_data/`, `data/code_rank_data/`（`market_data.html` 含む）
- **銘柄調査DB**: `data/stock_data/research_shelve`
- **ログ**: `logs/`（TimedRotatingFileHandler、7日保持、通常INFOレベル、`KS_LOG_DEBUG=1` でDEBUG出力）
