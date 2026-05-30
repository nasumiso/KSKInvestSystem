# issue #265: 銘柄詳細ページに外部チャット (ChatGPT/Claude) リンクを手動登録

## 目的

銘柄ごとの ChatGPT/Claude チャットスレッド URL を ResearchDB に手動登録し、
銘柄詳細ページ (`/stock/<code_s>`) から即座に開ける導線を作る。

**最小機能に限定** — 外部 LLM からの DB 参照、内蔵チャット UI、URL 自動取得は別 issue。

## データモデル

`chat_links` は **ラベル付きリスト**: `[{"label": str, "url": str}, ...]`

- `label`: 自由入力 (例「ChatGPT 事業分析」「Claude IR読み」)
- `url`: `http://` / `https://` で始まる文字列のみ許容
- 既存レコードに欠損していたら空リスト `[]` 扱い (後方互換)

## 設計判断 (ユーザー確認済み)

1. **編集 UI**: 行ごとフォーム + AJAX。各リンクを「ラベル / URL」行で表示し、追加・編集・削除を fetch でサーバ送信 → 再描画。
2. **URL 検証**: `http://` / `https://` 始まりのみ許可 (corporate_url と同基準)。ラベルは自由入力。

## 変更ファイル

### 1. `scripts/research_shelve.py` — スキーマ + 後方互換

- `RECORD_FIELDS` に `"chat_links"` を追加。
- `create_research_record()` に `chat_links: Optional[List[Dict[str, Any]]] = None` 引数を追加し、
  返却 dict に `"chat_links": list(chat_links) if chat_links else []` を含める。
- `get_research_record()` の後方互換ブロック (L508-526) に
  `if not isinstance(record.get("chat_links"), list): record["chat_links"] = []` を追加。
- 各要素の正規化ヘルパー `_normalize_chat_links(links)` を新設:
  - list でなければ `[]`
  - 各要素は dict かつ `label`/`url` が str のもののみ採用 (壊れたエントリは捨てる)
  - `url` が http/https 始まりでないエントリは捨てる
  - `get_research_record()` で呼んで正規化 (shikiho_comments と同じ箇所・同じ流儀)

  → **検証ポイント**: chat_links 未設定の旧レコードを読んで `[]` が返る / 壊れたエントリが除去される。

### 2. `scripts/webapp/helpers.py` — 保存ロジック (CRUD)

`corporate_url_override` の `save_corporate_url_override()` (L428) に倣い、
**`_flock()` で read-modify-write を排他** した3関数を新設:

```python
def add_chat_link(code_s, label, url) -> List[dict]   # 末尾に追加、保存後の全リストを返す
def update_chat_link(code_s, index, label, url) -> List[dict]  # index 行を上書き
def delete_chat_link(code_s, index) -> List[dict]     # index 行を削除
```

- URL 検証 (`http://`/`https://` 始まり) は **ルート側** で行い (corporate_url と同じ層分担)、
  helpers は index 範囲チェック・レコード未登録チェックのみ。
- index は範囲外なら `IndexError` / `ValueError` を送出 → ルートが 400 にマップ。
- `label` は `strip()` のみ (空ラベルは許容: URL だけ登録したいケースもある)。

  → **検証ポイント**: add → update → delete の一連操作後に get_research_record で永続化を確認。
     並行 index ずれ (同時削除) は flock で直列化される範囲で保証。

### 3. `scripts/webapp/routes/memo.py` — AJAX ルート

`post_stock_name_prev` (L85, AJAX/JSON 204 パターン) に倣い、JSON を返す3ルート:

```
POST /stock/<code_s>/chat_link            label, url           → 201 {ok, links}
POST /stock/<code_s>/chat_link/<int:idx>  label, url           → 200 {ok, links}
POST /stock/<code_s>/chat_link/<int:idx>/delete                → 200 {ok, links}
```

- URL バリデーション: 空 or `http(s)://` 始まり以外は 400 `{ok:false, error}`。
- レコード未登録は 404、index 範囲外は 400。
- 成功時は最新の `links` 配列を JSON で返し、クライアントがその場で再描画
  (リロード不要。stock_name_prev はリロードだが links は配列を返せるので DOM 差し替えにする)。

  → **検証ポイント**: 各ルートに正常系 + 400 (不正URL) + 404 (未登録) のテスト。

### 4. `scripts/webapp/templates/detail.html` — UI (手動メモセクション内)

**「5. 手動メモ」セクション内、「メモ・総括」の `memo-field` (L315-319) の直後**
(「機関投資家」フィールドの前) に「外部チャット」の `memo-field` を挿入する (ユーザー指定)。

⚠️ **重要な制約**: 手動メモセクションは単一 `<form action="/stock/<code_s>/memo">` で
全フィールドを一括 POST する。chat_links は AJAX で個別保存するため、
**chat_links の入力要素にはこの memo フォーム送信対象になる `name` を付けない**
(リンク表示・編集ボタンは form 内に置いてよいが、保存は独立 fetch 経路にする。
`name` 付き input を置かなければ memo POST に混入しない)。

- `record.chat_links` を `{% for link in record.chat_links %}` で行表示。
  各行: `<a href="{{ link.url }}" target="_blank" rel="noopener noreferrer">{{ link.label or link.url }}</a>`
  + ✎ 編集ボタン + 🗑 削除ボタン (data-index 付き)。
- 末尾に「+ 追加」ボタン → ラベル/URL の prompt() 2連 or インライン入力欄。
  **AJAX 方針なので**: 追加・編集はインラインの `<input>` 2つ + 保存ボタンを JS で生成 or
  シンプルに `window.prompt` 2回 (label, url) で取得 → fetch。
  → 既存 editCorpUrl が prompt 方式なので、**追加/編集は prompt 2連 + fetch**、削除は confirm + fetch に統一
    (JS 量を抑えつつ AJAX 化。完全インライン編集フォームは過剰)。
- fetch 成功で返ってきた `links` 配列から行を再構築する小さな JS 関数 `renderChatLinks(codeS, links)`。
- `rel="noopener noreferrer"` 必須 (既存リンクと同様)。

  → **検証ポイント**: ブラウザで追加→表示→編集→削除→リロードで永続化を目視確認 (.playwright-mcp/ にスクショ)。

## やらないこと (issue 準拠)

- 外部 LLM からの DB 参照、内蔵チャット UI、LLM API 連携
- URL 妥当性検査 (http/https チェック以上)、スクレイピング、自動タイトル取得
- 複数銘柄への同一リンク紐付け

## テスト方針 (CLAUDE.md: 1 PR 5本以下, parametrize 集約)

- `tests/test_research_shelve.py`: `_normalize_chat_links` + 後方互換 (旧レコード→`[]`, 壊れエントリ除去) を parametrize で 1本。
- `tests/test_webapp_routes.py`: chat_link の add/update/delete 正常系 + 不正URL(400) + 未登録(404) を parametrize で 2本程度。
- テンプレートのみの表示は目視 (CLAUDE.md: HTML/JS のみはブラウザ確認)。

合計 ≦ 5本。

## 検証手順

1. `pytest tests/test_research_shelve.py tests/test_webapp_routes.py -v`
2. `cd scripts && python -m webapp.app` → `/stock/<既存銘柄>` で追加/編集/削除/リロード永続化を目視
3. chat_links 未設定の既存銘柄ページが壊れず開けることを確認 (DoD 最終項目)

## DoD 対応表

| DoD | 対応 |
|---|---|
| RECORD_FIELDS に chat_links 追加 + 補完ロジック | 変更1 |
| 外部チャット表示 (手動メモ内・総括の直後) | 変更4 |
| ラベル+URL 追加・編集・削除 | 変更2,3,4 |
| 再読込で永続化 | 変更2 (upsert) |
| chat_links 未設定でも壊れない | 変更1 (後方互換) |
