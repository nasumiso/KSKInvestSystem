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
- `DATA_DIR` のパス解決は `ks_util._resolve_data_dir()` で行う。環境変数 `KS_DATA_DIR` で上書き可能。詳細は [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) の「データパス解決」を参照。

## アーキテクチャ

データ取得→DB更新→ランキング→市場分析のパイプライン構成。詳細は [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) を参照。

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

テスト方針・テストファイル一覧・統合テスト手順は [doc/TESTING.md](doc/TESTING.md) を参照。

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

## 関連ドキュメント

- [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) — アーキテクチャ詳細（データフロー、DB構成、キャッシュ戦略、テクニカル指標）
- [doc/TESTING.md](doc/TESTING.md) — テスト方針（ユニットテスト、統合テスト、HTMLパース変更時の検証）
- [doc/SYSTEM_OVERVIEW.md](doc/SYSTEM_OVERVIEW.md) — システム概要（非エンジニア向け）
- [doc/SPEC_REVIEW.md](doc/SPEC_REVIEW.md) — 投資システム評価レビュー
- [doc/MY_INVESTER_STRATEGY.md](doc/MY_INVESTER_STRATEGY.md) — 投資スタイル分析
