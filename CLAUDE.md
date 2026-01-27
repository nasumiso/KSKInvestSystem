# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリのコードを扱う際のガイダンスを提供します。

## プロジェクト概要

これは日本の株式市場分析システムで、複数のソース（株探、Yahoo Finance Japan）から財務データをスクレイピングし、複数の基準（ファンダメンタルズ、モメンタム、テクニカル指標）に基づいて株式を分析し、投資機会のためにランク付けするシステムです。このシステムは、成長株投資手法に触発されたスクリーニング基準を使用して、高成長株を特定することに焦点を当てています。

## アーキテクチャ

### コアデータフロー

1. **データ取得** (`shintakane.py` のメインフロー):
   - 株探から日々の新高値をスクレイピング (`get_todays_shintakane()`)
   - 出来高急増銘柄をスクレイピング (`get_todays_dekidakaup()`)
   - 決算発表更新を取得 (`update_todays_kessan()`)
   - これらのソースを候補銘柄リストに統合

2. **株式データベース** (Pickleベース):
   - 中央データベース: `data/stock_data/stocks.pickle`
   - すべての銘柄マスターデータ、株価、決算、指標、理論株価を含む
   - `make_stock_db.py` モジュールを介してアクセス

3. **データ更新パイプライン** (`make_stock_db.py` の `update_db_rows()`):
   - **マスターデータ**: 基本企業情報、セクター、テーマ、時価総額 (`master.py`)
   - **株価データ**: Yahooからの日次/週次株価、株探からのテクニカル指標 (`price.py`)
   - **決算データ**: 四半期/年次決算、成長率 (`gyoseki.py`)
   - **指標データ**: PER、PSR、ROE、利益率、信用残 (`shihyou.py`)
   - **理論株価**: 適正価値計算 (`rironkabuka.py`)

4. **分析とランキング** (`make_stock_db.py` の `list_all_db()`):
   - 総合スコアを計算: 業績40% + 指標20% + モメンタム25% + ファンダメンタルズ15%
   - データベース内の銘柄ランキングを更新
   - 詳細メトリクス付きCSV出力を生成

5. **市場分析** (`make_market_db.py`):
   - `data/sisu_data/` から市場指数（TOPIX、S&P500など）を追跡
   - セクターとテーマの強度を分析
   - 市場サマリーCSVを作成

### 主要な技術概念

**モメンタム分析** (`price.py`):
- **RS (相対力指数)**: 現在株価と13/26/39/52週株価の加重比較
- **モメンタムポイント**: 正規分布を使用してTOPIX RSに対して正規化されたRS
- **売り圧力比率**: ローソク足分解に基づく買い対売りの出来高比率
- **ローソク足ボラティリティ**: 平均終値で標準化された価格レンジ
- **ポケットピボット**: MA付近で最近のすべての下げ日の出来高を上回る高出来高の上昇日
- **トレンドテンプレート**: MA関係、52週ポジション、RSしきい値を含む7点チェックリスト

**業績品質** (`gyoseki.py`):
- 当四半期と通期の成長率（売上高と営業利益）
- 当四半期と前年同期を比較した進捗率
- 5年間および4四半期の過去成長一貫性

**キャッシュ管理** (`ks_util.py` の UPD_* 定数):
- `UPD_CACHE (0)`: 利用可能な場合はキャッシュデータを使用
- `UPD_INTERVAL (1)`: 間隔を超えた場合に更新、ファイルキャッシュを尊重
- `UPD_REEVAL (2)`: ロジックを再評価するがファイルキャッシュを使用
- `UPD_FORCE (3)`: 強制的に新規ダウンロード、すべてのキャッシュを無視

### モジュールの役割

- `shintakane.py`: 新高値/出来高スクリーニングワークフローのメインスクリプト
- `make_stock_db.py`: データベース管理、ランキング、CSV生成
- `price.py`: 株価データ取得とテクニカル分析（YahooとKabutan）
- `gyoseki.py`: 決算データ解析と成長スコアリング
- `shihyou.py`: 財務指標（バリュエーション、収益性、信用）
- `rironkabuka.py`: 理論株価計算
- `master.py`: 企業マスターデータとテーマ分析
- `make_market_db.py`: 市場指数とセクター/テーマランキング
- `ks_util.py`: ユーティリティ（HTTP、ロギング、pickle DB、ファイル操作）
- `kessan.py`: 決算カレンダー管理
- `portfolio.py`: 個人ポートフォリオ追跡
- `googledrive.py`: Google Driveへの結果アップロード

## 一般的な開発コマンド

### メイン分析の実行

```bash
# 完全分析（スクレイピング + 分析 + ランキング）
cd scripts
python shintakane.py

# スクレイピングなしで既存データのみを分析
python shintakane.py analyze

# データベース内のすべての銘柄を更新してランク付け
python make_stock_db.py list_all_db
```

### データベース操作

```bash
# 特定銘柄の更新
# make_stock_db.py の main() 内で command == "update" の code_list を編集
python make_stock_db.py update

# 特定銘柄データの表示
# make_stock_db.py の main() 内で command == "list" の code_list を編集
python make_stock_db.py list

# 上場廃止銘柄のクリーンアップ
python make_stock_db.py reflesh

# データベースのバックアップ
python make_stock_db.py backup
```

### 個別モジュールのテスト

```bash
# 特定銘柄の株価データ取得テスト
# price.py の main() 内で code_list を編集
python price.py

# 市場データ更新のテスト
python make_market_db.py
```

### 自動実行

cronスクリプト `shintakane_cron.sh` は両方のメインスクリプトを実行します：
1. `shintakane.py` - 新候補をスクレイピングしてデータを更新
2. `make_stock_db.py` - トップ100銘柄とポートフォリオを更新、ランキングCSVを生成

## データ保存

- **データベース**: `data/stock_data/stocks.pickle` (メイン株式DB)
- **キャッシュ**: `data/cache_data/` (HTTPレスポンスキャッシュ)
- **株価履歴**: `data/stock_data/yahoo/price/` および `data/stock_data/kabutan/price/`
- **市場指数**: `data/sisu_data/` (過去の指数株価の.txtファイル)
- **スクレイピングリスト**: `data/shintakane_data/` (日次の新高値/出来高急増CSV)
- **結果**: `data/shintakane_result_data/` および `data/code_rank_data/` (出力CSV)
- **ログ**: `logs/` ディレクトリ (日次ローテーションログ)

## 重要な注意事項

### スクレイピング元とフォーマット変更

Yahoo Finance JapanとKabutanは頻繁にHTMLフォーマットを変更します。株価/決算データが失敗した場合：
1. Yahooフォーマットについては `price.py` の `parse_price_text_yahoo_new()` を確認
2. Kabutanフォーマットについては `shintakane.py` の `convert_kabutan_*_html()` 関数を確認
3. 最近の変更対応: YahooがStyledNumberコンポーネントに切り替え（parse_price_text_yahoo_newで対応済み）

### セッション管理

HTTPリクエストは3つのセッションパターンを使用：
- **スレッドローカル**: シングルスレッドの順次リクエスト用の `use_requests_session()` コンテキストマネージャー
- **グローバル**: マルチスレッドの同時リクエスト用の `use_requests_global_session()`
- **直接**: セッションがアクティブでない場合は `requests.get()` にフォールバック

`update_db_rows()` で `sync` パラメータを介して適切なセッションタイプを選択します。

### 銘柄コード識別子フォーマット

- すべての銘柄コードは整数ではなく**文字列** (`code_s`) として保存
- フォーマット: 4桁コードの場合は `"0001"` から `"9999"`
- 特定の証券のために `"215A"` のような英数字コードもサポート
- レガシーの `code` (int) キーは存在するが非推奨

### 更新間隔と判定ロジック

各データタイプ（master、price、gyoseki、shihyo、rironkabuka）は以下を持ちます：
- `has_*_data()`: データが存在し十分に新しいかチェック
- `get_*_data()`: UPDパラメータに基づいてキャッシュまたはWebから取得
- 決算/指標には決算発表日後に強制更新する特別なロジックがあります

### マルチスレッディング考慮事項

`update_db_rows_async()` は並列データ取得のためにThreadPoolExecutor（5ワーカー）を使用。以下を確認：
- `use_requests_global_session()` コンテキストマネージャーを使用
- 共有 `stocks` 辞書へのアクセスはスレッドセーフ（取得中は読み取り、join後に書き込み）
- セマフォは同時HTTPリクエストを3に制限（`ks_util.py` の `MAX_REQUESTS`）

## Python環境

- **Python 3.9+** (`.venv/` の仮想環境を使用)
- 主な依存関係: `requests`、`scipy`、Drive アップロード用の Google API ライブラリ
- すべての依存関係は `requirements.txt` に記載
- 元々Python 2、Python 3に移行（pickleエンコーディングは `convert_pickle_latin1_to_utf8()` で処理）

## 特殊な動作

- **株価日ロジック**: 18:00より前は、株価データは前日と見なされる（`ks_util.py` の `get_price_day()`）
- **決算更新**: 現在の日付が保存された決算発表日を過ぎると、銘柄は自動更新
- **ETFフィルタリング**: ETFコードは `data/ETF_code.txt` からロードされ、株式分析から除外
- **ロギング**: すべての出力は直接print文ではなく、カスタムロガー（`ks_util.py`）を経由
