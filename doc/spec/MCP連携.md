# MCP連携

Shintakane が蓄積する調査・記録データを、LLM から安全に参照するための MCP
(Model Context Protocol) 連携です。

Shintakane・LLM・人間の役割分担は [AI投資活用戦略.md](../AI投資活用戦略.md) を参照してください。
MCP は銘柄推奨や自動売買を行いません。投資判断は必ず人が行います。

## 現在提供中

| MCPサーバー | 用途 | 提供ツール |
|---|---|---|
| `shintakane-shikiho` | 四季報・IR一次情報・決算資料の参照、銘柄評価台帳の参照・更新 | `get_shikiho` / `get_ir_qa` / `search_stocks` / `list_earnings_documents` / `get_earnings_document` / `list_stock_ratings` / `get_stock_rating` / `update_stock_rating` |

`shintakane-shikiho` はローカルの stdio MCP サーバーです。HTTP ポートを開かず、
`research_shelve` を既存のロック機構経由で読み取ります。LLM が DB へ直接接続することはありません。

### `get_shikiho`

指定銘柄の事業概要、四季報コメント履歴、四季報業績予想を返します。

| 項目 | 内容 |
|---|---|
| 入力 | `code_s`、`limit` (既定8件、最大50件) |
| コメント | 四季報号 (`period`)、表示用号名、本文。新しい順 |
| 事業概要 | 調査DBの `overview` |
| 業績予想 | 直前実績・今季予想・来季予想。売上高・営業利益・前期比成長率。単位は百万円 |

- `period` は四季報の版情報であり、正確なデータ時点ではありません。そのためコメントの `as_of` は常に `null` です。
- 業績予想の `updated_at` は入力日です。古い場合は陳腐化している可能性があります。
- 特に来季予想（2期先）は不確実性が高いため、スコアリングや機械的な売買判定には使いません。成長シナリオを読むための定性的な材料です。
- 貼り付け原文 (`raw_text`) はトークン節約のため返しません。

### `get_ir_qa`

指定銘柄の IR 問い合わせ回答を新しい順に返します。

| 項目 | 内容 |
|---|---|
| 入力 | `code_s`、`limit` (既定10件、最大50件) |
| 返却 | 回答日 (`answered_at`)、本文 |
| 時点 | `answered_at` は実際の回答日なので、`as_of` にも同じ日付を返す |

IR 問い合わせ回答は公開情報として流通しない非公開の一次情報です。外部へ転記・共有する際は特に注意してください。

### `search_stocks`

銘柄コードまたは社名の一部から、調査DBにある候補を検索します。コード完全一致を社名部分一致より優先します。

| 項目 | 内容 |
|---|---|
| 入力 | `query`、`limit` (既定10件、最大50件) |
| 返却 | 銘柄コード、銘柄名、四季報コメントの有無・件数 |

### 決算資料 (`list_earnings_documents` / `get_earnings_document`)

収集済みの決算短信 (`tanshin`)・決算説明資料 (`setsumei`)・中期経営計画 (`chuki_plan`) を返します。
収集範囲と方針は [IR資料の収集範囲](../decisions/2026-09-22-IR資料の収集範囲.md) を参照してください。

| ツール | 内容 |
|---|---|
| `list_earnings_documents` | 資料の一覧 (本文なし)。`months` (既定12)、`doc_type` で絞り込み。`chuki_plan` は期間に関係なく全件 |
| `get_earnings_document` | 抽出済みテキストをページ範囲で返す。`truncated` が true なら `next_page_from` で続きを取る |

- `coverage_status: not_collected` は「未収集」であって「資料が存在しない」ではありません。`partial_coverage: true` のときは `coverage_through` 以降が未収集です。
- 訂正版に置き換えられた旧版は既定で返しません。ただし訂正版が差分通知だけで原本の内容を含まない場合は、原本も返します (`superseded_by` 付き)。
- 中期経営計画は会社IRページから手動で集めたもので、`date` は推定値 (`date_estimated: true`、`as_of` は `null`) です。複数件が並立しうるため、どれが現行計画かは内容から判断します。グロース市場の「事業計画及び成長可能性に関する事項」は適時開示から自動収集され、`date` は開示日です。
- 返すのは PDF から抽出したテキストだけです。スライド資料ではグラフや表の数値が落ちることがあります。項目名だけがあって数値が続かない場合は、抽出できていないだけです。`text` が `null` の資料や数値の裏取りには PDF を見てください。`relative_path` 末尾のファイル名で Google Drive コネクタから検索できます。

### 銘柄評価台帳 (`list_stock_ratings` / `get_stock_rating` / `update_stock_rating`)

ChatGPT で付けた現在の投資判断 (ファンダ40 / 未織込20 / モメンタム20 / Valuation20、Confidence、Status) を参照・更新します。
正本は `stock_ratings.json` で、旧スプレッドシート「投資PJ_銘柄評価台帳」は 2026-09-23 に凍結しました。

| ツール | 内容 |
|---|---|
| `list_stock_ratings` | 総合点順の一覧 (長文なし)。`status` で絞り込み |
| `get_stock_rating` | 1銘柄の全項目と直近の変更履歴 |
| `update_stock_rating` | 渡した項目だけを部分更新 (未登録なら新規作成)。`reason` 必須。検証エラーは `ok: false` と `errors` で返し、何も書かない |

## 利用上の制約

- 書き込めるのは銘柄評価台帳 (`update_stock_rating`) だけです。調査DB・ポジションを変更するツールは提供していません。
- データの時点と出典を確認してください。時点が不明なものは、推測で補わず `null` として返します。
- MCP のデータは分析を助けるための材料です。投資成果や将来の株価を保証しません。

## 接続・運用

Python 3.11 の本体 `.venv` と `KS_DATA_DIR` が必要です。MCP ホストは通常のシェル環境を引き継がないため、`KS_DATA_DIR` は接続設定で明示します。未設定、またはリポジトリ内の空データを参照する設定ではサーバーは起動しません。

ローカル接続設定、Secure MCP Tunnel による常駐運用、起動確認は [scripts/mcp/README.md](../../scripts/mcp/README.md) を参照してください。

## 今後の構想

以下は構想段階であり、現在は利用できません。

| 候補 | 内容 |
|---|---|
| `get_company_snapshot` | 株価・バリュエーション・業績概要・モメンタム・保有状態 |
| `get_financials` | 四半期・通期業績、会社計画、進捗率 |
| `get_valuation` | PER / PBR / PSR / EV系指標など |
| `get_portfolio_position` | 保有状況・株数・平均取得単価・評価額・PF比率 |
| `get_investment_context` | 個別銘柄分析に必要な情報の集約 |

書き込みツールは、読み取り連携の運用が安定してから検討します。設計上の背景と段階計画は [mcp-parent-concept.md](../plan/mcp-parent-concept.md) を参照してください。
