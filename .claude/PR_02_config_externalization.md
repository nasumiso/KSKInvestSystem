# PR #2: 設定管理の外部化

## 概要
ハードコードされた定数をYAML設定ファイルに外部化し、運用変更を容易にする。

## 背景・課題

現在、以下の定数が各モジュールにハードコードされています：
- `INTERVAL_DAY = 7` (キャッシュ有効期限)
- `MAX_REQUESTS = 3` (同時リクエスト数)
- スコア比率（40% earnings + 20% indicators + 25% momentum + 15% fundamentals）
- データディレクトリパス
- スレッド数（5）、セマフォ制限（3）

**問題点：**
- 運用変更のたびにコード修正が必要
- 環境（開発/本番）による設定切り替えが困難
- 設定値が散在していて把握しづらい

## 実装予定

### ディレクトリ構造
```
config/
├── default.yaml      # デフォルト設定
├── production.yaml   # 本番環境設定
└── development.yaml  # 開発環境設定
```

### 設定ファイル例（config/default.yaml）
```yaml
# キャッシュ設定
cache:
  interval_days: 7
  http_cache_dir: data/cache_data

# パフォーマンス設定
performance:
  max_concurrent_requests: 3
  thread_pool_workers: 5

# データパス
paths:
  data_dir: data
  stocks_db: data/stock_data/stocks.pickle
  market_db: data/market_data/market.pickle

# スコアリング設定
scoring:
  weights:
    earnings: 0.40
    indicators: 0.20
    momentum: 0.25
    fundamentals: 0.15
```

### 新規ファイル
- `scripts/config.py` - 設定読み込みモジュール
- `config/default.yaml`
- `config/production.yaml` (オプション)

### 変更ファイル
- `scripts/ks_util.py` - パス定数を設定から読み込み
- `scripts/make_stock_db.py` - スコア計算の重みを設定から読み込み
- `scripts/shintakane.py` - 各種定数を設定から読み込み

## メリット

- **運用変更が容易**: コード変更不要、YAMLファイル編集のみ
- **環境切り替え**: `ENV=production` で本番設定に切り替え
- **設定の可視化**: 全設定が一箇所に集約
- **バリデーション**: Pydanticで型チェック可能

## 工数見積もり
1日
