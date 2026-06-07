# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

日本株式市場の成長株分析システム。株探・Yahoo Finance Japanからデータをスクレイピングし、ファンダメンタルズ・モメンタム・テクニカル指標で銘柄をスコアリング・ランキングする。

## 行動原則 (Karpathy 4原則)

less is more の方針でコーディングする。出典: [andrej-karpathy-skills/CLAUDE.md](https://github.com/forrestchang/andrej-karpathy-skills/blob/main/CLAUDE.md)

1. **Think Before Coding**: 仮定は明示する。複数解釈があれば提示し、勝手に選ばない。シンプルな代案があれば述べる。不明点は実装前に質問する。
2. **Simplicity First**: 要求された問題を解く最小コードのみ書く。投機的な抽象化・configurability・ありえないシナリオへのエラー処理は不要。「シニアが overcomplicated と言うか?」を自問する。
3. **Surgical Changes**: 必要な箇所だけ触る。隣接コードの "改善"・既存スタイルからの逸脱・既存のdead code削除はしない。各変更行が user の依頼に直接トレースできること。
4. **Goal-Driven Execution**: タスクを検証可能なゴールに変換する。「バリデーション追加」→「不正入力のテストを書いて通す」のように。複数ステップなら計画と検証ポイントを述べる。

## コーディング規約

- **コメント・docstringは日本語で記述**。技術用語・関数名・外部ライブラリ名は英語のまま。
- 銘柄コードは常に**文字列** (`code_s`) を使用。`"0001"`〜`"9999"` や `"215A"` 形式。レガシーの `code` (int) は非推奨。
- ロギングは `log_print`, `log_debug`, `log_warning`, `log_error` を使用（`ks_util.py`）。直接の `print()` は不可。
  - `log_print`（INFO）: フェーズ開始/完了マーカー、サマリー、重要な処理経過など**運用時に必要な情報**
  - `log_debug`（DEBUG）: 個別銘柄の中間値、per-row詳細、キャッシュ判定など**デバッグ時のみ必要な情報**
  - ファイルハンドラは通常INFOレベル。`KS_LOG_DEBUG=1` 環境変数でDEBUGレベルに切替可能
  - 新規ログ追加時は上記の基準で `log_print` / `log_debug` を使い分けること
- DB操作は `update_db_rows()` を経由。バルク操作は `sync=False` で非同期化可能。
- 日付判定は `ks_util.get_price_day()` を使用（17:00前は前日扱い）。
- `DATA_DIR` のパス解決は `ks_util._resolve_data_dir()` で行う。環境変数 `KS_DATA_DIR` で上書き可能。詳細は [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) の「データパス解決」を参照。
  - 現在の運用環境では `KS_DATA_DIR=/Users/k_sohara/Ext/GoogleDrive/shintakane_data`（`.zshrc` で設定済み）
- テストは「書けば書くほど良い」ものではない。1 PR で追加するテストは 5本以下を目安に、parametrize で集約する。自明な動作・getter/setter 素通し・ファクトリの各フィールド個別確認は書かない。詳細は [doc/TESTING.md](doc/TESTING.md) の「テスト量・粒度の方針」を参照。
- Playwright MCP・`screencapture` 等でスクリーンショットを保存する前に [.claude/rules/playwright.md](.claude/rules/playwright.md) を参照。
- 開発中に同じ系統の再現可能なワンショット処理 (`python -c` や複数行 Bash) を2回以上叩いたら、CLIサブコマンド/関数への昇格を1行で提案する。承認されたら [promote-to-command](.claude/skills/promote-to-command/SKILL.md) スキルの手順 (既存CLI確認→標準形選択→既存関数再利用→COMMANDS.md追記) で実施。`calc_*` 等のスコア計算はCLI化せずテストでカバーする。

## アーキテクチャ

データ取得→DB更新→ランキング→市場分析のパイプライン構成。詳細は [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) を参照。

## 開発コマンド

すべて `scripts/` から実行。`source .venv/bin/activate` でvenv有効化。

よく使うもの:

```bash
cd scripts && python shintakane.py                  # メイン分析(スクレイピング + 分析)
cd scripts && python shintakane.py analyze          # 既存データのみ分析
cd scripts && python make_stock_db.py list_all_db   # 全銘柄ランキング更新
cd scripts && python make_stock_db.py update 6324   # 特定銘柄の更新
cd scripts && python -m webapp.app                  # 調査WebApp (http://localhost:5001)
```

全コマンド一覧 (update/list/reflesh/backup/calibrate_momentum, 移行スクリプト, cron運用詳細など) は [doc/COMMANDS.md](doc/COMMANDS.md) を参照。テストは [doc/TESTING.md](doc/TESTING.md) を参照。

## 実装プラン作成ルール

プラン作成・レビューのルールは [.claude/rules/codex-plan-review.md](.claude/rules/codex-plan-review.md) を参照。

## 重要な注意事項

### スクレイピング元のHTML変更対応

Yahoo価格データはyfinance API経由で取得するため、HTMLフォーマット変更の影響を受けない。
Kabutan HTMLスクレイピングのデータ取得失敗時:
1. Kabutan: `shintakane.py` の `convert_kabutan_*_html()` を確認
2. HTMLフォーマット変更検知テストを実行: `pytest tests/test_live_html.py -v`
   - 失敗したテストクラスから対応モジュールのパーサーを特定・修正する
   - 詳細は [doc/TESTING.md](doc/TESTING.md) の「HTMLフォーマット変更検知テスト」を参照

### DB変更時の注意

- shelve DBスキーマの後方互換性を維持すること
- `make_stock_db.py` のload/saveロジックを変更する場合はマイグレーションスニペットを追加
- DB（shelve）への並行書き込みは禁止 — 提供されたAPI経由で操作
- Google Drive認証ファイル (`data/googledrive/`) はコミット禁止

### ETFフィルタリング

ETFコードは `data/ETF_code.txt` から読み込み、株式分析対象外とする。

## Python環境

- **Python 3.9+**（`.venv/` の仮想環境）
- 主な依存: `requests`, `scipy`, `yfinance`, `pandas`, Google API ライブラリ群, `oauth2client`
- `requirements.txt` に全依存を記載

## 関連ドキュメント

- [doc/COMMANDS.md](doc/COMMANDS.md) — 開発コマンドリファレンス（全CLI、移行スクリプト、cron運用詳細）
- [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) — アーキテクチャ詳細（データフロー、DB構成、キャッシュ戦略、テクニカル指標）
- [doc/TESTING.md](doc/TESTING.md) — テスト方針（ユニットテスト、統合テスト、HTMLパース変更時の検証）
- [doc/システム概要.md](doc/システム概要.md) — システム概要（非エンジニア向け）
- [doc/review/SPEC_REVIEW.md](doc/review/SPEC_REVIEW.md) — 投資システム評価レビュー
- [doc/投資戦略.md](doc/投資戦略.md) — 投資スタイル分析
