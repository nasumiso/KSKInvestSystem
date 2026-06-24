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
| `topix`, `mothers`, `nikkei225`, `nasdaq`, `sp500` | dict | 各指数のprice/RS/トレンド/State Machine データ |
| `momentum_calib` | dict | モメンタムポイント分布パラメータ `{loc, scale, sample_count, updated_at, n_days}`。`python make_stock_db.py calibrate_momentum` で手動更新（自動更新なし）。詳細は [doc/requirements/momentum_pt_requirements.md](requirements/momentum_pt_requirements.md) |

各指数 dict が持つ主要キー (issue #117 Part A/B):

| キー | 型 | 内容 |
|------|---|------|
| `market_state` | str | `confirmed_uptrend` / `uptrend_under_pressure` / `market_in_correction` |
| `state_meta` | dict | `distribution_days_with_close` (有効DD), `last_ftd_date`, `rally_attempt_*` |
| `state_history` | list | 直近30件の `(date, state, trigger)` 履歴 |
| `high52_weekly` | float | 週足52本の最高値 (Stalling Day判定で参照) |
| `price_kairi_wma10` | float | 現在価格の10週MA乖離率% (10MA明確割れ判定で参照) |
| `direction_signal` | str | 後方互換、`<state>,YYMMDD` 形式 |
| `distribution_days`, `followthrough_days` | list | 後方互換 (表示は state_meta から生成) |

詳細仕様は [doc/requirements/market_state_machine_requirements.md](requirements/market_state_machine_requirements.md) を参照。

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

- **識別・評価**: 銘柄名、旧銘柄名（`stock_name_prev`, issue #183）、総合評価（S〜E）、企業概要、機関投資家コメント
- **手動メモ**: OpenWork、ジムクレイマー、四季報コメント、メモ・総括
- **スナップショット** (list): 決算タイミングごとの IR定量データ・クォリティ指標・理論株価乖離（`date_yy_m` 降順）。日付は2軸で保持する: 業績(IR定量)は株価非依存なので `date_yy_m`=**決算日/修正日**、株価依存の指標・理論株価乖離は `acquired_date`=**取得日**。WebApp の業績テーブルは `date_yy_m`、指標テーブルは `acquired_date` を表示

### 銘柄名変更の追従（issue #183）

`make_stock_db.py` の `_update_db_code()` で master を取得した直後、stocks_shelve の旧 `stock_name` と新値を比較し、差異があれば `research_shelve.sync_stock_name()` を呼んで旧名を `stock_name_prev` に退避する。

- 排他制御: `sync_stock_name` 内部の flock 区間で read-modify-write を完結させ、UI 側の `upsert_research_record` と直列化。memo/rating などの手動資産が lost update で消えないことを保証
- 平常時のオーバーヘッド: 文字列比較のみ。変更なしなら research_shelve への書き込みは発生しない
- 表示: WebApp の各画面で「新名 (旧○○)」形式で併記 (`templates/_macros.html` の `stock_name_with_prev` マクロ)
- 既存ズレ補正: `python sync_research_stock_names.py [--apply]` (dry-run デフォルト) で一括補正

### 決算スナップショットの自動追記

`make_stock_db.py` の `update_research_snapshots()` が `list_all_db()` 末尾で実行:

- research_shelve管理銘柄のうち、決算イベント日から14日以内（`KESSAN_WINDOW_DAYS`）の銘柄が対象。トリガー日は `kessan_jisseki_date`（決算発表実績日）・`kessanbi`（発表日/次回予定日）・`kessan_mod_date`（決算修正日）を**独立に窓判定**し、窓内のものを全て採用（`_collect_trigger_dates`）。未来の次回予定日は自動的に窓外
- 決算発表と業績修正は**別イベント**として別スナップショット行に記録する（発表行・修正行を `date_yy_m` で分けて残す）。同日のトリガーは1件に集約
- 業績(IR定量)の `date_yy_m` は決算イベント日、株価依存の指標・理論株価乖離の `acquired_date` は取得日（本日）を付与
- `gyoseki`・`shihyou`・`rironkabuka` から自動抽出、`data_source: "auto"` で記録
- 同一 `date_yy_m` の auto 行は冪等上書き（指標・`acquired_date` を更新）。manual/migration 行は保護され不変
- 直近決算実績日 `kessan_jisseki_date` は `shintakane.py` の決算発表パース時に保存（`kessanbi` は次回予定日に上書きされ実績日が残らないため別フィールドで保持）

> 新規監視追加（WebApp `add_stock`）も同じ日付ルール（業績=直近決算イベント日、指標=取得日）でスナップショットを1件作る。

### モジュール構成

| ファイル | 役割 |
|---------|------|
| `scripts/research_shelve.py` | 基盤（スキーマ・CRUD・バリデーション・CLI） |
| `scripts/webapp/` | Shintakane Research（銘柄調査WebApp、Flask） |
| `scripts/migrate_research_from_csv.py` | スプレッドシートCSVからの移行（実行済み） |

### CLI

```bash
python research_shelve.py show <code_s>                          # 調査データ全表示
python research_shelve.py list [--rating S,A] [--keyword ...]    # フィルタ一覧
python research_shelve.py backup                                 # DBバックアップ
```

### Shintakane Research (`scripts/webapp/`)

銘柄調査WebApp。research_shelveの閲覧・編集用。Flask製、`http://localhost:5001` で起動。

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

### 営業日判定の方針（祝日カレンダーを持たない理由）

営業日判定は `get_price_day()`（17時前は前日扱い）＋ `recent_weekday()`（土日なら直近金曜に丸める）のみ。**祝日カレンダー（`jpholiday` 等）は導入しない**。価格・RS・MA は yfinance の営業日 index に乗るため祝日は元から不在。祝日を平日扱いする実害は「祝日に日次データを年数十回 余分取得する」軽量 HTTP のみで、データ破損は起きない。これを消すための依存追加は過剰（Simplicity First）。

**再検討トリガー**: ①取得コストが課金・rate limit に当たる ②日付指定での営業日数演算が要る新機能（決算からN営業日後の反応 等。現状 `n_business_days` は配列インデックスで代用）③休場日の「未更新」誤警告が運用ノイズ化。導入時は `ks_util.py` に祝日対応版の営業日ヘルパーを追加し、`recent_weekday()` 利用箇所を置き換える。

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
- **モメンタムポイント**: TOPIX RSに対する銘柄RSの相対比 `rs_rel = rs_raw / topix_rs_raw` を対数正規分布CDFでパーセンタイル化する (issue #104 Phase 2)。`momentum_pt = int(100 × CDF(log(rs_rel); loc, scale))`。loc/scale は `market_db['momentum_calib']` の実測値を使い、キャッシュが無い・サンプル不足・180日以上経過の場合は `price.MOMENTUM_CALIB_DEFAULT_LOC=-0.058 / SCALE=0.275` (issue #104 検証時実測値) にフォールバックする。キャッシュ更新は手動: `python make_stock_db.py calibrate_momentum`。
- **トレンドテンプレート**: MA関係・52週ポジション・RSしきい値の7点チェック
- **ポケットピボット**: MA付近で下げ日出来高を上回る高出来高上昇日。MA10 or MA25 のいずれかの乖離が4%以内（issue #110）。保存値 `[ポ]MM/DD(num)` の `num` は **MA10乖離率%**（負値＝MAを下回る良い位置）。
- **ブレイクアウト**: 20日平均出来高の1.5倍以上の高出来高上昇日（MA乖離5%以内）。保存値 `[ブ]MM/DD(num)` の `num` は **出来高超過率%**（`100×vol/avg_vol − 100`、大きいほど強）。
  - **ストップ高張り付き対応（issue #253）**: 値幅制限上限に張り付くと買いが集中しても約定せず出来高が逆に減るため、出来高基準だけでは検知できない。**前日比 +20% 以上**（値幅制限上限相当）なら出来高条件をスキップして検知する。このとき出来高超過率が小さく/負になりうるため保存値は `max(per, 0)` で0床にし、強度バケットが単調に振る舞うようにする。しきい値+20%は概算（株価レンジで実際の値幅は異なる）。
- **シグナル強度（webapp 表示, issue #253）**: signal セルは tooltip に検出日・強度・意味を出す。一覧では強度×鮮度の赤系背景を濃淡表示するが、詳細ページは背景なし（チャートのマーカーで発生日が読めるため）。詳細チャートには発生日のポ=緑三角・ブ=橙ダイヤのマーカーを X軸（日付ラベル）の下に設けた専用バンド（騰落率Y軸と無関係、ポ下段・ブ上段、株価/RS線と分離して最前面）に表示する。X は週足だが発生日（日単位）を週バー間で日割り按分、サイズは強度連動（ポは 0.8 倍に控えめ縮小、ブは菱形が細く見えるため 1.8 倍で拡大強調）。強度しきい値（暫定）— ポ: MA10乖離 `≧-1`強/`-3〜-1`中/`<-3`弱、ブ: 出来高超過率 `≧200`強/`100〜200`中/`<100`弱。鮮度: 0-2日濃/3-5日中/6-7日薄。表示対象シグナルは `make_stock_db.extract_signals()` が単一ソースで抽出し、tooltip/背景色・チャートマーカー・タグが同じフィルタ集合（Stage4除外・ポ3件/ブ1件・`access_date_price` 基準の経過日数 0〜7日）を共有する。年補完・鮮度判定は `make_signal` のタグ付与と同じ `access_date_price`（`get_price_day()` で anchor 化した `anchor_day`）基準で、`date.today()` 直解釈による年跨ぎ・更新停止銘柄の誤配置を避ける。経過日数は `anchor_day - 発生日`（calendar today ではなく価格更新日基準）なので、金曜更新の銘柄を週末に見ても最新シグナルが消えない。ただし `today - anchor_day` が 30 日（`_SIGNAL_STALE_DAYS`、他タグと同基準）を超える更新停止銘柄は当時のシグナルを復活させないよう除外する。

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
- **完了待ち**: `wait_all_uploads()` で全スレッドの完了を保証。失敗・タイムアウト時は例外送出

### cron実行 (`shintakane_cron.sh`)

`shintakane.py` → `make_stock_db.py` を逐次実行。macOS launchd（`com.k_sohara.shintakane.cron.plist`）で定期実行。

## データ保存場所

- **メインDB (shelve)**: `data/stock_data/stocks_shelve`
- **市場DB**: `data/market_data/market_db_shelve`
- **HTTPキャッシュ**: `data/today_stocks/html_cache/`
- **株価履歴**: `data/stock_data/yahoo/price/`（yfinance JSON + レガシーHTML）, `data/stock_data/kabutan/price/`
- **市場指数**: `data/sisu_data/`
- **結果CSV/HTML**: `data/shintakane_result_data/`, `data/code_rank_data/`（`market_data.html` 含む）
- **銘柄調査DB**: `data/stock_data/research_shelve`
- **ログ**: `logs/`（TimedRotatingFileHandler、7日保持、通常INFOレベル、`KS_LOG_DEBUG=1` でDEBUG出力）
