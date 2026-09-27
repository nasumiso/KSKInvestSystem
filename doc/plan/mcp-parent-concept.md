## 概要

Shintakane が保持する投資データを、LLM (Claude Code / Claude Desktop / ChatGPT 等) から
安全かつ構造化された形で参照できるよう、MCP (Model Context Protocol) 対応を行う。

目的は Shintakane を **LLM による定性分析と接続可能な「個人投資OS」へ拡張すること**。
目的・方向性は [方向性メモ.md](../方向性メモ.md)、役割分担は [AI投資活用戦略.md](../AI投資活用戦略.md) を参照。

## 想定する利用像

> 「7729 東京精密を分析して」

と依頼した時点で、LLM 側が Shintakane から以下を取得し、
Web 上の最新決算説明資料・IR・競合情報と統合して分析できる状態を目指す。

- 直近8四半期の業績推移 / 最新会社計画
- 現在 PER / PBR / EV 系指標
- 株価モメンタム
- 四季報コメント
- 現在の保有状況
- 過去の銘柄評価 / 前回分析時点からの変化

## 基本設計

LLM から DB へ直接アクセスさせない。MCP Server は可能な限り薄い Adapter とし、
既存の Shintakane 内部ロジック (research_shelve / portfolio_shelve / stocks_shelve 等) を再利用する。

```text
ChatGPT / Codex / Claude
          ↓
      MCP Server
          ↓
 Service / Repository (既存モジュール)
          ↓
         DB
```

## Tool 構成 (構想)

投資分析上意味のある単位で Tool を定義する。万能 API は作らない。

### v0.1 相当

| Tool | 内容 |
|---|---|
| `get_shikiho` | 四季報コメント履歴 + 事業概要 |
| `get_company_snapshot` | 時価総額・株価・PER/PBR・最新業績概要・モメンタム・保有有無・ステータス |
| `get_financials` | 四半期/通期業績 (売上・営業利益・経常・純利益・EPS・YoY/QoQ・会社計画・進捗率) |
| `get_valuation` | PER / PBR / PSR / EV-Sales / EV-EBITDA / 時価総額 (必要に応じ過去レンジ) |
| `get_portfolio_position` | 保有有無・株数・平均取得単価・評価額・PF比率 |

### 次段階

| Tool | 内容 |
|---|---|
| `get_investment_context` | 個別銘柄分析の主要入口。上記を一括取得 |
| `get_thesis_history` | 過去の投資仮説 |
| `get_changes_since_analysis` | 前回分析後の変化 (業績/株価/Valuation/モメンタム/四季報/保有) |
| `get_stock_score` | 銘柄評価台帳の最新スコア |

## データ設計上の重要事項

### 1. as_of を必ず保持する

投資データは「いつ時点か」が重要。可能な限り各データに時点情報を付与する。
不明な場合は推測せず `null` を返す。

**版情報を時点情報に読み替えない**こと。例えば四季報の「26.6号」は
「2026年6月時点の事実」を意味しないため、`as_of` には入れず版情報として別フィールドで返す
(Phase 1 で確立した扱い)。

```json
{ "per": 18.3, "price": 12450, "as_of": "2026-08-29T15:00:00+09:00" }
```

### 2. source を保持する

数字の由来を LLM 側で判断できる状態にする。

```json
{ "revenue": 125000000000, "period": "FY2027Q1", "published_at": "2026-08-07", "source": "TDnet" }
```

### 3. SQL 実行 Tool は作らない

`execute_sql(...)` のような汎用 Tool は原則作らない。

理由: Schema 依存が強くなる / 誤 JOIN・誤集計リスク / 不要な大量データ取得 /
セキュリティリスク / DB 変更に弱い。

**数値収集は Shintakane、解釈は LLM** という方針を Tool 境界として強制する。

## Read / Write 方針

初期は完全 Read Only (`LLM → Shintakane` のみ)。

安定後に以下の Write Tool を検討する:
`save_analysis` / `update_stock_score` / `update_thesis` / `record_decision` / `record_skip` / `archive_stock`

最終的に以下の学習ループを構築する:

```text
Shintakane → LLM分析 → 人間が判断 → Shintakaneへ結果保存 → 次回分析で過去履歴を参照
```

## 将来的に保存したい独自データ

Shintakane の価値は公開市場データそのものではなく、
**「その時点で何を見て、どう考えて、どう判断したか」の履歴が累積すること**にある。

- 投資仮説 / 仮説変更履歴 / 仮説崩壊条件
- 未織り込み判断 / Confidence
- 買い・見送り理由 / 見送り後の結果
- ポジションサイズ / 決算跨ぎ判断 / 売買結果
- ルール違反 / ミス分類
- AI の分析 / 自分の最終判断

過去の投資メモ分析でも、課題は銘柄発掘より「どう持つか・どれだけ張るか・いつ売るか」であり、
売買前の事前設計とルール実行をシステム化する価値が大きい。
これは「見送りログ」「失敗ログ」「仮説管理」「ポジション管理」を重視する既存方針と一致する。

## インフラ

当面は MacBook Air 上で開発・動作確認。将来的に Mac mini へ移行し常時稼働サーバーとする (#174)。

```text
Mac mini
├── Shintakane Web
├── DB
├── 日次バッチ
├── データ更新処理
└── MCP Server
```

外部 LLM からアクセスさせる場合も、Flask や DB を直接インターネット公開せず、
認証された安全な接続経路を利用する。

具体的な手段として **OpenAI Secure MCP Tunnel** (2026-05 公開) が使える (Phase 1 で採用)。
`tunnel-client` を内部ネットワーク側で動かし、outbound HTTPS の long-polling で
OpenAI 側とつなぐ方式のため、**inbound ポートを開けずに ChatGPT から到達できる**。
ローカルの stdio MCP サーバーをそのままブリッジできるので、
公開用に HTTP/SSE サーバーを別途書く必要がない。

ただし `tunnel-client` は**常時起動が前提**であり、Mac mini 移行 (#174) と実質セットになる。

## 段階計画

- **Phase 1**: 四季報 MCP の切り出し → #427 ← まずここから
- **Phase 2**: `get_company_snapshot` / `get_financials` / `get_valuation` / `get_portfolio_position`
- **Phase 3**: `get_investment_context` 等の集約 Tool
- **Phase 4**: Write Tool と学習ループ
- **Phase 5**: Mac mini 常時稼働 + 安全な外部接続 (#174 と連動 / `tunnel-client` の常設先)

## 技術的前提 (Phase 1 で判明済み)

MCP Python SDK は **Python 3.10+ が必須**。当初は本プロジェクトの `.venv` が Python 3.9.6 だったため
「MCP Server だけ別 venv」という制約があったが、**2026-08-30 に本体 `.venv` を Python 3.11.14 へ移行済み**。
→ MCP Server は本体 venv でそのまま動かせる (専用 venv 不要)。

検証済み: Python 3.11 環境から `research_shelve` を import し、本番 shelve DB (895レコード) を
正常に読めることを確認済み。shelve は `dbm.dumb` (純Python形式) のためバイナリ互換性問題はなく、
パス解決 (`KS_DATA_DIR`) も正常。移行時にテストスイート 2207件全pass: 日次バッチ・WebApp とも正常動作。

## 関連

- Phase 1 実装 issue: #427 四季報データのローカルMCPサーバー
- #174 Shintakane 定常運用を MacMini M2 Pro に移行する
- #346 [四季報] 業績予想の取り込み — 完了後 `get_shikiho` に業績予想を追加できる
