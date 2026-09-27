分離元: #139 (IR資料DL + テキスト化) / 関連: #433 (MCP経由でChatGPTから参照)

## 背景

AIファンダメンタル分析において、中期経営計画は**会社が自ら示す成長シナリオ**として
決算説明資料と並ぶ重要な一次情報。四季報コメント (#427) や決算説明資料 (#139) が
「直近の実績と短期見通し」を担うのに対し、中計は**複数年のレンジでの目標と戦略**を担う。

#139 の再定義時に「中計も適時開示経由で取れないか」を実測したが、**取れないことが確定した**
ため、経路の異なる本issueとして分離する。

## 実測: 適時開示経路では取得できない (2026-09-22)

`disclosure_db.csv` (開示1,821件 / 284銘柄) に対し
`中期経営計画|中期計画|中計|経営計画|中長期|ビジョン` でマッチングした結果、
**7件 / 6銘柄** しかヒットせず、しかも**資料本体は実質0件**だった:

```
3193  中期経営計画の数値目標修正に関するお知らせ            ← 案内文
3446  中期経営計画策定に関するお知らせ                      ← 案内文
6899  中期経営計画の策定に関するお知らせ                    ← 案内文
7318  DAIBOUCHOU氏と徹底対談 中計『…』の成長戦略を深堀り     ← 対談記事
9163  …施工管理技術検定の総資格保有者数が573名に…           ← PR
4436  【説明資料】…中長期成長ビジョン～ (2件)              ← 説明資料であり中計ではない
```

適時開示に出るのは「策定しました」という**案内文であって資料本体ではない**。
資料本体は**会社のIRページにのみ掲載される**ケースが多い。

→ **#139 (適時開示起点) とは経路が根本的に異なるため、別issueとする。**

## 起点: `corporate_url_override`

WebApp 詳細画面に会社HP URL の手動上書き機能が既にある (#208)。
`research_shelve` の `corporate_url_override` を実測したところ、
**6銘柄で設定済みで、すべて IRライブラリ / プレゼン資料ページの直リンク**だった:

```
3660  https://www.istyle.co.jp/ir/
4369  https://www.trichemical.com/ir/
4980  https://www.dexerials.jp/ir/library/
6324  https://www.hds.co.jp/ir/
6965  https://www.hamamatsu.com/jp/ja/investor-relations/ir-library.html
9880  https://www.innotech.co.jp/ir/library/presentation.html
```

ユーザーが手動で IR ページに向けているため、**これを起点にすれば探索範囲が大幅に狭まる**。
未設定の銘柄は `stocks_shelve.corporate_url` (会社トップ) にフォールバックする。

## スコープ

### 方針: 全自動クロールは作らない (半自動)

企業IRサイトの構造は銘柄ごとにバラバラで、全自動探索は当たり判定の作り込みに
コストがかかる割に精度が読めない。universe 約300銘柄のうち中計PDFを公開している
企業は限られるため、**候補提示 → ユーザーが選択 → 確定DL** の半自動とする。

手動確定が面倒になってから自動化を検討する、という順序で進める。

### 収集フロー

1. `corporate_url_override` (無ければ `corporate_url`) のページを1枚 GET
2. ページ内の `<a href>` から候補を抽出:
   - href が `.pdf` で終わる
   - かつ アンカーテキスト or href が `中期経営計画|中計|中長期|経営計画|Mid-?term|ビジョン|Vision` にマッチ
3. **1階層だけ掘る**。IRトップにPDFが無い場合、リンク先のうち
   `IR|ライブラリ|library|プレゼン|presentation|資料` にマッチするページを**1つだけ**辿る。
   それ以上は追わない (無限クロール防止)
4. 候補一覧を提示し、ユーザーが選択して確定DL

### CLI

```bash
# 候補を表示するだけ (DLしない)
cd scripts && python ir_docs.py plan-candidates 6324

# URL を指定して確定DL
cd scripts && python ir_docs.py fetch-plan 6324 <url>

# DL済み中計の一覧
cd scripts && python ir_docs.py list 6324 --doc-type chuki_plan

# 旧版になった中計を手動で非最新化 (後述の is_latest 参照)
cd scripts && python ir_docs.py mark-superseded 6324 <doc_id>
```

## 保存形式

**#139 の `ir_docs/<code_s>/index.json` に相乗りする** (別ディレクトリを作らない)。
`documents` 配列に `doc_type: "chuki_plan"` で混在させる。

```json
{
  "doc_id": "a3f2c8b1d4e5f6a7",
  "doc_type": "chuki_plan",
  "date": "20260515",
  "date_estimated": true,
  "heading": "中期経営計画 2026-2028",
  "url": "https://www.hds.co.jp/ir/pdf/chuki2026.pdf",
  "source": "corporate_ir_page",
  "source_page": "https://www.hds.co.jp/ir/",
  "pdf_path": "20260515_a3f2c8b1d4e5f6a7_中期経営計画2026-2028.pdf",
  "text_path": "20260515_a3f2c8b1d4e5f6a7_中期経営計画2026-2028.json",
  "sha256": "...",
  "pages": 32,
  "total_chars": 21043,
  "text_quality": "ok",
  "is_latest": true
}
```

#139 との差分:

| 項目 | #139 (適時開示) | 本issue (会社IR) |
|---|---|---|
| `doc_id` | TDnet ID | **`sha256[:16]`** (TDnet ID が無いため) |
| `source` | `TDnet (via Kabutan)` | **`corporate_ir_page`** |
| `date` | 開示日 (確実) | **推定。下記「日付の信頼性」参照** |
| `is_latest` | 会計期間 + `doc_type` で判定 | **自動では落とさない。全件 true で並立**。後述 |
| `fiscal_period` / `quarter` | あり | **なし** (中計は特定四半期に紐付かない) |

`text_quality` の判定ロジック (`ok` / `garbled` / `image_based`) は **#139 と共通の関数を使う**。

### `is_latest` は自動で落とさない (重要)

2つの理由から、**中計の `is_latest` は自動では `false` にしない**。

**理由1: 推定日付で正本を決めてはいけない。**
`date` は常に推定値 (後述) で、特に DL日フォールバックは**取得順で結果が変わる**。
推定日付を根拠に旧版を非最新化すると、**古い中計を最新として扱い、
正しい資料を #433 から隠す**。これは #139 で「関連付けのキーが抽出できなければ
両方 `is_latest: true` のまま残す」とした方針と同じ理屈。

**理由2: `index.json` を #139 と共有している。**
更新対象を `doc_type == "chuki_plan"` に限定しないと、中計を1件DLしただけで
**同じ配列にある最新の短信・説明資料の `is_latest` が `false` になり、
#433 の既定 (`is_latest: true` のみ返す) から #139 の資料が丸ごと消える**。

→ 仕様:

- DLした中計は**すべて `is_latest: true`** とし、複数件が並立することを許容する
- 旧版を落とすのは**ユーザーの明示操作のみ**:
  `python ir_docs.py mark-superseded <code_s> <doc_id>` で手動指定する。
  本issueは半自動フロー (ユーザーが候補から選んでDLする) のため、
  「これは旧版」の判断もユーザーが持てる
- `is_latest` を書き換える処理は **`doc_type == "chuki_plan"` のレコードに限定する**

どちらも silent に壊れる経路なので、テストで担保する。

### 日付の信頼性 (`date_estimated`)

中計PDFは適時開示と違い**確実な公開日が取れない**。
資料内の表記もファイル名も、公開日とは限らない (策定日・対象期間の開始年など)。

→ **`corporate_ir_page` 経由で取得した資料は、常に `date_estimated: true` とする。**

`date` フィールドには最も確からしい推定値 (資料内表記 → ファイル名 → DL日の優先順) を入れるが、
**それが推定であることはフラグで必ず示す**。HTTP の `Last-Modified` も
CDN やサイト再構築で更新されるため信頼しない。

`date_estimated: false` になるのは、将来 TDnet 等の**開示日が確定する経路**を
追加した場合のみ。現時点の本issueのスコープでは常に `true`。

## 想定される課題

- **中計を公開していない企業が一定数ある**。経路の問題ではないため解決不能。
  候補0件で warning 継続、エラーにしない
- **`corporate_url` が会社トップの場合、IRページに辿り着けない**ことがある。
  1階層しか掘らないため。その場合はユーザーが `corporate_url_override` を
  IRページに設定する運用でカバーする (既存の #208 の機能)
- **資料の日付が取れない**。中計PDFは確実な公開日が存在しない。
  上記「日付の信頼性」のとおり常に `date_estimated: true` とする
- **`robots.txt` / レート制限**。企業サイトは株探と違い一律の扱いができない。
  1銘柄あたり最大2リクエスト (IRトップ + 1階層) に抑え、1秒1リクエストを守る

## #433 への影響

`list_earnings_documents` に `doc_type` フィルタ (`tanshin` / `setsumei` / `chuki_plan`) を追加する。

中計は開示日が確実でないため、`as_of` は **`date_estimated: true` の場合 null を返す**
(#427 の四季報と同じ扱い。不確実な日付を確定値として LLM に渡さない)。
本issueのスコープでは常に `date_estimated: true` のため、**中計の `as_of` は常に null** になる。

`date` 自体は一覧の並び順と人間の目視用に返してよいが、
**`date_estimated: true` を必ず添えて**、推定値であることを LLM が認識できるようにする。

また中計は `is_latest: true` が複数並立しうる (後述) ため、
**#433 は中計について「最新1件」を前提にした実装をしない**。
複数件返った場合は LLM に並べて渡し、どれが現行計画かの判断は LLM 側に委ねる。

## 変更・新規ファイル

| ファイル | 変更 |
|---|---|
| `scripts/ir_docs.py` | `plan-candidates` / `fetch-plan` / `mark-superseded` サブコマンドを追加 |
| `tests/test_ir_docs.py` | 候補抽出のテストを追加 (既存ファイルに追記 / httpはモック) |
| `doc/COMMANDS.md` | CLI を追記 |

`scripts/webapp/` は変更しない (`corporate_url_override` は既存の #208 の機能をそのまま読むだけ)。

## 検証方法

1. `plan-candidates` を `corporate_url_override` 設定済みの6銘柄
   (3660 / 4369 / 4980 / 6324 / 6965 / 9880) で実行し、**何件中何件で中計PDFの候補が出るか**を実測する
   - ここで当たり率が低ければ、半自動フロー自体を見直す (フィルタの問題か、1階層では届かないのか)
2. `corporate_url` が会社トップの銘柄で候補0件になること (誤検知で無関係PDFを拾わない)
3. `fetch-plan` でDLした資料が #139 の `index.json` に `doc_type: "chuki_plan"` で入ること
4. `text_quality` 判定が #139 と同じ結果になること (共通関数を使っている確認)
5. **`is_latest` の巻き込みが起きないこと**: #139 の資料がある銘柄に中計をDLし、
   短信・説明資料の `is_latest` が `true` のまま維持されること
6. **中計を2件DLしても両方 `is_latest: true` のまま**で、自動で旧版が落ちないこと。
   `mark-superseded` を明示実行したときだけ `false` になること
7. 中計レコードに `date_estimated: true` が必ず付くこと
8. 1銘柄あたりのリクエストが2回を超えないこと

## 前提

- **#139 の完了が前提。** `index.json` の形式・`text_quality` 判定・PDF保存処理を再利用する
- 2026-H2 は機能追加凍結フェーズだが、本issueは #426/#427/#433 と同じ MCP 対応の
  一環として、その例外枠に含める整理とする

## 関連

- 分離元: #139 (IR資料DL + テキスト化)
- 後続: #433 (MCP経由でChatGPTから参照) — `doc_type` フィルタの追加が必要
- 関連: #208 (会社HP URL の手動上書き) — 本issueの起点となる機能
- 関連: #426 (Shintakane MCP連携対応 / 親)、#427 (四季報 MCP)
