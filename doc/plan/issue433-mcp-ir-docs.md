# issue #433 決算説明資料をMCP経由でChatGPTから参照可能にする

## 目的

#139 が収集・テキスト化した決算説明資料を、MCP 経由で ChatGPT から参照できるようにする。
現状 ChatGPT に毎回 Web から資料を探させており、取得できない場合に手動で PDF を渡す往復が発生している。

役割分担 (#426 の方針):

```text
Shintakane = 取得・保存・テキスト化 (#139)  ← 完了 (PR #458 マージ済)
MCP        = 受け渡し (本issue)
LLM        = 読解・解釈・比較
```

## スコープ

本issueは **MCP Tool 化のみ**。取得・テキスト化・品質判定は #139 の責務で、
`ir_docs.py` には手を入れない。

## 実装対象

既存の `scripts/mcp/shikiho_server.py` に Tool を2本追加する (サーバーは分けない)。
ChatGPT 側の接続先が1つで済み、トンネルと launchd 常駐も1系統のまま維持できる。

Phase 1 (#427) で確立した型をそのまま流用する:

- 純粋ロジックを `*_data()` 関数に置き、`@mcp.tool()` は薄いラッパー
- テストは `*_data()` に対して書く (MCP 層を経由しない)

## #139 が提供するデータ (実装の前提)

`{DATA_DIR}/ir_docs/<code_s>/index.json`:

| フィールド | 内容 |
|---|---|
| `collected_months` | 収集範囲。`DEPTH_MONTHS` = {latest:0, 1y:12, 2y:24} |
| `collected_depth` | `latest` / `1y` / `2y` |
| `documents[]` | 資料レコード (date 降順) |
| `collection_errors` | 収集失敗した doc_id |

各 document:

| フィールド | 内容 |
|---|---|
| `doc_id` | TDnet ID (例: `140120260302573035`)。開示ごとに一意 |
| `date` | 開示日 `YYYYMMDD` |
| `doc_type` | `setsumei` / `tanshin` |
| `heading` | 見出し |
| `fiscal_period` / `quarter` | 会計期間 / `Q1`-`Q4`・`FY` |
| `pages` | ページ数 (int) |
| `total_chars` | 総文字数 |
| `text_quality` | `ok` / `image_based` / `garbled` |
| `is_latest` / `supersedes` / `superseded_by` | 訂正版の関連付け |
| `pdf_path` / `text_path` | ディレクトリ内の相対ファイル名 |

テキスト本体は `text_path` の JSON に **ページ配列**で保存:
`{"doc_id": ..., "pages": [{"page": 1, "text": "..."}, ...]}`

→ #433 が要求する「ページ境界で切る」はこの構造で満たせる。

## Tool 1: list_earnings_documents

```python
list_earnings_documents(code_s: str, months: int = 12,
                        include_superseded: bool = False) -> Dict
```

保有資料の一覧を返す。テキスト本体は返さない (一覧でコンテキストを食わない)。

### 返却

```json
{
  "code_s": "4011",
  "coverage_status": "collected",
  "collected_months": 12,
  "last_collected_at": "2026-09-22",
  "coverage_from": "2025-09-22",
  "coverage_through": "2026-09-22",
  "requested_months": 12,
  "partial_coverage": false,
  "has_collection_errors": false,
  "total_documents": 5,
  "documents": [
    {"doc_id": "140120260302573035", "date": "2026-03-02",
     "as_of": "2026-03-02", "heading": "...", "doc_type": "setsumei",
     "fiscal_period": "2025年12月期", "quarter": "FY",
     "pages": 32, "total_chars": 18381, "text_quality": "ok",
     "is_latest": true, "superseded_by": null,
     "local_path": "/.../ir_docs/4011/20260302_..._....pdf"}
  ],
  "note": null
}
```

### coverage_status (2値) + 不完全性の明示

| 値 | 判定 | 意味 |
|---|---|---|
| `collected` | `index.json` あり | `documents` は収集できた範囲で信頼できる |
| `not_collected` | `index.json` なし | 資料の有無は**不明** |

**`index.json` の存在は「完全に収集できた」を意味しない。**
`ir_docs.py` は走査・PDF取得・テキスト抽出の失敗を `collection_errors` に
残したまま index を保存する。資料が欠落していても `documents` は返るため、
これを黙って「信頼できる」と提示すると LLM が欠落に気づけない。

`collection_errors` が非空なら `has_collection_errors: true` と
`collection_error_count` を返し、`note` に明示する:

```json
{"coverage_status": "collected", "has_collection_errors": true,
 "collection_error_count": 2,
 "note": "収集時に2件の資料でエラーが発生しており、一覧は不完全な可能性があります。"}
```

`coverage_status` 自体は2値のまま (収集を試みたか否かの事実)。
不完全性は独立したフィールドで表す。

**`coverage_status` は収集カバレッジの事実のみを表し、`months` の絞り込み結果とは独立させる。**
「収集済みだが期間内に資料がない」で `not_collected` を返すと、LLM が
「資料が存在しない」と誤認する。`total_documents` (months 無視の総数) と併せて
3状態 (未収集 / 期間内に無いだけ / 本当に0件) を区別する。

### partial_coverage — 収集**時点**から数える

`collected_months` は「最後に収集した時点」からの深度であり、**現在時点の
カバレッジではない**。半年前に `1y` 収集してそのままなら、現在から見た
直近12ヶ月のうち最後の半年は未収集なのに、素朴な `months > collected_months`
判定では `partial_coverage: false` になってしまう。決算資料は鮮度が要なので、
これは誤った「収集済み」判定になる。

月数の減算では表せない。**穴が空くのは最新側** (収集日〜現在) であって
古い側ではないため、区間の包含で判定する:

```text
実際の収集区間 = [last_collected_at - collected_months, last_collected_at]
要求区間       = [今日 - months, 今日]
partial_coverage = 要求区間 ⊄ 収集区間
```

3月15日に `1y` 収集して9月22日に参照した場合、保存済みは
「前年3月〜今年3月」。現在から見た直近6ヶ月 (3月〜9月) は**未収集**で、
ここを「保証できる」と返すのは誤情報になる。

返却は区間そのものを返し、最新側の穴を明示する:

```json
{"collected_months": 12, "last_collected_at": "2026-03-15",
 "coverage_from": "2025-03-15", "coverage_through": "2026-03-15",
 "requested_months": 12, "partial_coverage": true,
 "note": "収集済みの範囲は2025-03-15〜2026-03-15です。2026-03-15以降に開示された資料は未収集のため、この期間の資料の有無は判定できません。"}
```

`coverage_through` (= `last_collected_at`) が今日より前なら、その後に
開示された資料は**存在しても返らない**。決算資料は鮮度が要なので、
最新側の欠落を LLM が認識できることを最優先する。

**`collected_depth="latest"` では `collected_months` が 0** のため、
`months` 指定があれば常に `partial_coverage: true`。仕様通り
(latest 収集は直近1件のみで期間保証がない)。

### 訂正版

既定で `is_latest: true` のみ返す。旧版を返すと LLM が古い数値で分析するリスクがあるため。
`include_superseded=True` で旧版も含め、その場合 `superseded_by` を必ず添える。

## Tool 2: get_earnings_document

```python
get_earnings_document(code_s: str, doc_id: str,
                      page_from: int = 1, page_to: int = None,
                      max_chars: int = 15000) -> Dict
```

抽出済みテキストをページ範囲指定で返す。

### 品質が悪い資料で空テキストを黙って返さない

`text_quality` が `ok` 以外なら、空文字や文字化けを返さず理由とローカルパスを明示する:

```json
{
  "text_quality": "image_based",
  "text": null,
  "local_path": "/.../ir_docs/4436/20260814_....pdf",
  "note": "この資料は画像主体でテキストを抽出できません。上記PDFを直接添付してください。"
}
```

これを怠ると ChatGPT が空データのまま推測で分析を進める。
`garbled` も同様 (断片的に読める英数字から誤った分析を組み立てる恐れがある)。

### 返却サイズの制御

抽出テキストは実測で中央値 18,381字 / 最大 43,634字。全文を1回で返すと
「MCP で読ませる」という設計自体が破綻する。

- **切り出しは必ずページ境界で行う。** `max_chars` を超えない範囲でページを詰め、
  `truncated: true` と次の `page_from` を返す。ページ途中で切らないため
  継続位置はページ番号だけで一意に決まる
- **1ページ単独で `max_chars` を超える場合はそのページだけを返す** (`max_chars` 超過を許容)。
  こうしないと同じページを返し続けて**無限ループする**。実測では1ページ最大 5,154字
  (2,775ページ中 15,000字超はゼロ) のため通常発生しないが、防御として実装する

```json
{"code_s": "4011", "doc_id": "...", "found": true,
 "text_quality": "ok", "as_of": "2026-03-02",
 "page_from": 1, "page_to": 12, "pages": 32,
 "truncated": true, "next_page_from": 13,
 "text": "...", "local_path": "/.../....pdf"}
```

### as_of

決算説明資料は開示日が明確なため、四季報 (#427) と異なり `as_of` に開示日を入れてよい。

## 実装方針

### 読み取りヘルパーの置き場所

`ir_docs.py` に読み取り関数を足すか、MCP 側に書くか。

**MCP 側 (`shikiho_server.py`) に置く。** 理由:

- `ir_docs.py` は収集 (requests / pypdf) が主で、読み取り専用の MCP から
  重い依存を引き込みたくない
- #433 のスコープは「MCP Tool 化のみ」で `ir_docs.py` に手を入れない方針
- ただし `IR_DOCS_DIR` 定数だけは `ir_docs.py` から import して二重定義を避ける

→ import が収集用の依存を巻き込む場合は、`DATA_DIR` から MCP 側で組み立てる
(実装時に確認)。

### 日付の形式

`index.json` の `date` は `YYYYMMDD`。MCP の返却では `as_of` を含め
`YYYY-MM-DD` に正規化する (LLM が扱いやすく、#427 の `answered_at` と揃う)。

## テスト

`tests/test_mcp_shikiho_server.py` (既存) に追加。tmp_path に index.json と
ページ JSON を作って `*_data()` を直接呼ぶ。

CLAUDE.md の方針 (1 PR 5本以下・parametrize で集約) に従い **5本**:

1. `coverage_status` の3状態を parametrize
   (未収集 / 期間内0件だが total>0 / 期間内あり)
2. `partial_coverage` を区間の包含で判定する (parametrize)
   - 収集直後に `months=12` → false (要求区間が収集区間に収まる)
   - 半年前に `1y` 収集して `months=12` → true。`coverage_through` が
     今日より前で最新側に穴 (**codex 指摘2**。月数の減算では false になる)
   - `collected_depth="latest"` (`collected_months=0`) → true
3. `collection_errors` 非空で `has_collection_errors` と note が立つ (**codex 指摘1**)
4. `text_quality` != ok で text=null + local_path + note を返す (image_based / garbled)
5. ページ境界の切り出し
   (max_chars でページ単位に収まる / 1ページが max_chars 超でもそのページを返す = 無限ループ防止)

## 動作確認

**本番に `ir_docs/` が未作成** (2026-09-22 時点で確認済み)。#139 の収集は
まだ一度も本番で実行されておらず、MCP Tool を実装しても返すデータがない。

→ 実装・テスト完了後、`ir_docs.py download_all` を本番で回すかは**ユーザーに確認する**。
TDnet への実アクセスが発生するため、実行タイミングの判断が要る。

収集後の確認手順:

```bash
cd scripts && python ir_docs.py download 4011 --depth 1y   # 単一銘柄で試す
cd scripts && python ir_docs.py list 4011                  # 保存内容を確認
# MCP 経由の確認は _check_runtime_database() + *_data() の直接呼び出し
```

## スコープ外

- `ir_docs.py` の変更 (収集・品質判定は #139 の責務)
- #426 Phase 2 の Tool (`get_company_snapshot` 等) — 別issue
- PDF バイナリを MCP で返すこと — 4.9MB は base64 で約6.5MB となりコンテキストに
  載らない。ユーザーによるファイル添付が正規の経路 (2026-08-30 実機確認済み)
