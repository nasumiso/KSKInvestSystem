# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

日本株の成長株投資で、裁量判断の再現性を高めるための個人システム。株探・Yahoo Finance Japan 等からデータを集めて銘柄をスコアリングし、保有銘柄の監視・売買記録・振り返りを支える。目的と方向性は [doc/方向性メモ.md](doc/方向性メモ.md)、役割分担 (Shintakane / LLM / 人間) は [doc/AI投資活用戦略.md](doc/AI投資活用戦略.md) を参照。

## 行動原則

less is more の方針でコーディングする。1-4 の出典: [andrej-karpathy-skills/CLAUDE.md](https://github.com/forrestchang/andrej-karpathy-skills/blob/main/CLAUDE.md)

1. **Think Before Coding**: 仮定は明示する。複数解釈があれば提示し、勝手に選ばない。シンプルな代案があれば述べる。不明点は実装前に質問する。
2. **Simplicity First**: 要求された問題を解く最小コードのみ書く。投機的な抽象化・configurability・ありえないシナリオへのエラー処理は不要。「シニアが overcomplicated と言うか?」を自問する。
3. **Surgical Changes**: 必要な箇所だけ触る。隣接コードの "改善"・既存スタイルからの逸脱・既存のdead code削除はしない。各変更行が user の依頼に直接トレースできること。
4. **Goal-Driven Execution**: タスクを検証可能なゴールに変換する。「バリデーション追加」→「不正入力のテストを書いて通す」のように。複数ステップなら計画と検証ポイントを述べる。
5. **Question the Frame**: 対策が2回続けて効果を出さなかったら、3回目の設計に入る前に止まる。1回ごとは前進に見えるため、失敗回数で機械的に検知する。止まったら「過去に見送られた代替案とその再検討条件」「今の枠が解ける解を排除していないか」「問題の設定自体が誤っていないか」を確認し、ユーザーに提示する。土俵を変える判断はユーザーのものだが、選択肢を差し出すのは実装側の責任。3 は依頼の範囲を勝手に広げないための原則であり、小さな変更で解けない問題に小さな変更を積み続ける理由にはしない。

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
- `DATA_DIR` のパス解決は `ks_util._resolve_data_dir()` で行う。環境変数 `KS_DATA_DIR` で上書き可能。詳細は [doc/アーキテクチャ.md](doc/アーキテクチャ.md) の「パスと実行環境」を参照。
  - 正本は運用機 (MacMini) の `~/shintakane_data`。開発機 (MBA) は `KS_DATA_DIR=/Users/k_sohara/shintakane_data_dev`（`.zshrc` で設定済み）の開発用コピーで、古い。最新データを調べるときは `deploy/macmini.sh run python ...` で運用機に問い合わせる（詳細は [doc/運用手順.md](doc/運用手順.md)）
- テストは「書けば書くほど良い」ものではない。1 PR で追加するテストは 5本以下を目安に、parametrize で集約する。自明な動作・getter/setter 素通し・ファクトリの各フィールド個別確認は書かない。詳細は [doc/テスト方針.md](doc/テスト方針.md) の「テスト量・粒度の方針」を参照。
- Playwright MCP・`screencapture` 等でスクリーンショットを保存する前に [.claude/rules/playwright.md](.claude/rules/playwright.md) を参照。
- 開発中に同じ系統の再現可能なワンショット処理 (`python -c` や複数行 Bash) を2回以上叩いたら、CLIサブコマンド/関数への昇格を1行で提案する。承認されたら [promote-to-command](.claude/skills/promote-to-command/SKILL.md) スキルの手順 (既存CLI確認→標準形選択→既存関数再利用→コマンド一覧.md追記) で実施。`calc_*` 等のスコア計算はCLI化せずテストでカバーする。
  - セッション内の2回検知だけでは取りこぼす (長いセッションでは序盤を忘れる、セッションをまたぐ繰り返しは見えない)。**タスク完了報告のタイミングで、そのセッションでDB・データに投げたワンショットの問いを `.claude/query_log.jsonl` に追記する**。書式と棚卸し手順は promote-to-command スキルの「問い合わせログ」節を参照。既存CLIで済ませたものは書かない。

## アーキテクチャ

日次バッチ (取得→DB更新→ランキング→運用比率→theme-news) と、揮発DB・蓄積DBの分離が骨格。詳細は [doc/アーキテクチャ.md](doc/アーキテクチャ.md) を参照。

**実装前に必ず読む**: [doc/用語集と不変条件.md](doc/用語集と不変条件.md) — 名前から推測できない用語と、機能をまたぐ制約 (どこで守るか)。ドキュメントに何を書き何を書かないかは [doc/AI開発原則.md](doc/AI開発原則.md) に従う。

## 開発コマンド

すべて `scripts/` から実行。`source .venv/bin/activate` でvenv有効化。

よく使うもの:

```bash
cd scripts && python shintakane.py                  # メイン分析(スクレイピング + 分析)
cd scripts && python make_stock_db.py list_all_db   # 全銘柄ランキング更新
cd scripts && python make_stock_db.py update 6324   # 特定銘柄の更新
cd scripts && python -m webapp.app                  # 調査WebApp (http://localhost:5001)
```

全コマンド一覧 (update/list/reflesh/backup/calibrate_momentum, 移行スクリプト, cron運用詳細など) は [doc/spec/コマンド一覧.md](doc/spec/コマンド一覧.md) を参照。テストは [doc/テスト方針.md](doc/テスト方針.md) を参照。

## 実装プラン作成ルール

プラン作成・レビューのルールは [.claude/rules/codex-plan-review.md](.claude/rules/codex-plan-review.md) を参照。

## 重要な注意事項

### スクレイピング元のHTML変更対応

Yahoo価格データはyfinance API経由で取得するため、HTMLフォーマット変更の影響を受けない。
Kabutan HTMLスクレイピングのデータ取得失敗時:
1. Kabutan: `shintakane.py` の `convert_kabutan_*_html()` を確認
2. HTMLフォーマット変更検知テストを実行: `pytest tests/test_live_html.py -v`
   - 失敗したテストクラスから対応モジュールのパーサーを特定・修正する
   - 詳細は [doc/テスト方針.md](doc/テスト方針.md) の「HTMLフォーマット変更検知テスト」を参照

### DB変更時の注意

- shelve DBスキーマの後方互換性を維持すること
- `make_stock_db.py` のload/saveロジックを変更する場合はマイグレーションスニペットを追加
- DB（shelve）への並行書き込みは禁止 — 提供されたAPI経由で操作
- Google Drive認証ファイル (`data/googledrive/`) はコミット禁止

### ETFフィルタリング

ETFコードは `data/ETF_code.txt` から読み込み、株式分析対象外とする。

### 文書の更新は同じ PR で

仕様・用語・不変条件・設計判断に影響する変更では、該当する文書 ([doc/アーキテクチャ.md](doc/アーキテクチャ.md)、[doc/用語集と不変条件.md](doc/用語集と不変条件.md)、`doc/spec/`、`doc/decisions/`) もコード変更と同じ PR で直す。issue を完了させる PR では、その計画書 (`doc/plan/`) を片付ける ([doc/README.md](doc/README.md) の「計画書の置き場所」)。

### 実装完了報告時の確認ポイント提示

WebApp画面の見え方・挙動、またはユーザーが直接触るデータ（保有銘柄タブの表示など）が変わる実装が完了したら、完了報告の中で「ユーザーが確認すべき点」を自分から提案する。特に以下のようなケースは見落としやすいので優先して洗い出す：

- 既存の関数・フローを新しい文脈で再利用したことで、既存動作の前提（例: 「qtyは人が入力するので売却後もクリアしない」）が新方針（例: 「CSVを真実源とし常に実態を反映する」）と食い違っていないか
- 画面上の表示とDBの実際の値がずれて見える箇所（例: ステータスは変わったが関連フィールドが旧値のまま）
- ログ・通知文言が機械的な自動生成になり、既存の人間が書いた文言と並んだときに違和感がないか

このルールが不要なのは、内部リファクタ・バグ修正のみ・テスト追加のみなど、UI/ユーザー可視データの見え方が変わらない変更。判断に迷う場合は提示する側に倒す。

## Python環境

- **Python 3.11**（`.venv/` の仮想環境）。venv は `uv venv --python 3.11 .venv` で作成する
- 主な依存: `requests`, `scipy`, `yfinance`, `pandas`, Google API ライブラリ群, `oauth2client`
- `requirements.txt` に全依存を記載

## 関連ドキュメント

- [doc/spec/コマンド一覧.md](doc/spec/コマンド一覧.md) — 開発コマンドリファレンス（全CLI、移行スクリプト、cron運用詳細）
- [doc/運用手順.md](doc/運用手順.md) — 運用機 (MacMini) の構築・データ移行・日常運用・トラブルシュート
- [doc/アーキテクチャ.md](doc/アーキテクチャ.md) — データフロー、データストアの責務と分離原則、設計の意図、画面遷移
- [doc/テスト方針.md](doc/テスト方針.md) — テスト方針（ユニットテスト、統合テスト、HTMLパース変更時の検証）
- [doc/システム概要.md](doc/システム概要.md) — 投資家視点のできること・毎日の自動処理・スコアの考え方・色の凡例
- [doc/機能紹介.md](doc/機能紹介.md) — WebApp 4画面のスクリーンショット付き機能紹介（対外説明用。画像は `doc/screenshots/`）
- [doc/review/仕様レビュー_0704.md](doc/review/仕様レビュー_0704.md) — 投資システム評価レビュー（旧版: [仕様レビュー_0314.md](doc/review/仕様レビュー_0314.md)）
- [doc/投資戦略.md](doc/投資戦略.md) — 個人メモ: 投資スタイル分析と売買ルール
- [doc/方向性メモ.md](doc/方向性メモ.md) — システムの目的と方向性の正本
- [doc/spec/市場ステート仕様.md](doc/spec/市場ステート仕様.md) — 市場ステートの判定仕様
- [doc/用語集と不変条件.md](doc/用語集と不変条件.md) — 用語の定義と機能をまたぐ制約
- [doc/AI開発原則.md](doc/AI開発原則.md) — AI と開発するときに文書へ何を書くか、開発での AI の使い方
- [doc/AI投資活用戦略.md](doc/AI投資活用戦略.md) — 投資判断での AI の使い方 (Shintakane / LLM / 人間の分業)
- [doc/decisions/](doc/decisions/README.md) — 長期参照する設計判断と却下した案
