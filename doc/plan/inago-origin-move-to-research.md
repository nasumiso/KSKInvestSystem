# イナゴ元 (inago_origin) を portfolio から research へ移設する

## 背景

ポートフォリオ一覧の「イナゴ元」列は現在 `portfolio_shelve` の `MEMO_FIELDS` に
`inago_origin` として保持されている。値は「ゆーさく」「がっしー」のような
情報源の人名 (実データ 322レコード中 43件)。

これを銘柄 (stock) ページの手動メモ側 = `research_shelve` へ移す。

## 移設する理由 (ライフサイクル不一致)

portfolio のレコードは `delete_record()` で**物理削除できる** (3監のみ)。
監視対象から外すとイナゴ元の記録も消える。しかし「どこでこの銘柄を知ったか」は
保有・監視状態と無関係に**銘柄へ恒久的に紐づく属性**で、一度外して後で戻したときに
出所が失われるのは望ましくない。

`research_shelve` は「不可逆な蓄積資産 (時系列履歴 + 手動メモ)」と設計上明記されており
(research_shelve.py の冒頭 docstring)、こちらが本来の置き場。

なお同じ短縮表示グループ (`SHORT_TEXT_FIELDS`) の `watch_in_reason` (IN理由) と
`takaichi_sensitivity` (売買メモ) は**売買判断に紐づく**ためポートフォリオ側が適切。
移すのは `inago_origin` だけ。

## 方針 (ユーザー確定)

1. **research に専用フィールド `inago_origin` を新設**する (既存 `memo` への追記ではない)。
   値が構造化されたまま残り、将来「この人発の銘柄一覧」のような集計もできる。
2. **ポートフォリオ一覧からは列を消す** (読み取り専用でも残さない)。
3. **マイグレーションを用意する** (件数によらず実施)。

## 変更内容

### 1. research_shelve.py

- `RESEARCH_FIELDS` に `"inago_origin"` を追加
- レコード生成 (`create_research_record`) に `inago_origin: str = ""` を追加
- 既存レコードの後方互換: 読み出し時に `record.get("inago_origin", "")` で
  空文字補完されるので、物理マイグレーションは不要 (フィールド追加のみ)
- `format_research_record` 等のテキスト出力に「手動メモ」ブロックがあるので、
  そこへ「イナゴ元」行を追加する

### 2. webapp: 銘柄ページに入力欄を追加

- `templates/detail.html`: memo フォーム内に `memo-field` を1つ追加。
  1行なので `<input>` ではなく既存の `cramer` (textarea rows=2) と同じ扱いに揃える
- `helpers.py` の `save_memo()`: 対象フィールドに `inago_origin` を追加
  (read-modify-write の対象に含める)

### 3. portfolio 側から削除

- `portfolio_shelve.py`: `MEMO_FIELDS` から `"inago_origin"` を除去、
  `create_record()` の `inago_origin` 引数を削除
- `templates/portfolio_list.html`: `<th>イナゴ元</th>`、inline 編集ループの
  `("inago_origin", "10em")`、JS の `SHORT_TEXT_FIELDS` から除去
- `routes/portfolio.py`: display 組み立ての `"inago_origin"` 行を削除

### 4. 監視登録時に research レコードを自動作成する (codexレビュー対応)

**これが無いと移設の目的が崩れる。** `add_to_watch()` は portfolio レコードしか
作らず、research 未登録の銘柄は詳細画面が「追加プロンプト」を出すだけでメモ編集に
入れない (`routes/detail.py`)。`save_memo()` も未登録なら `ValueError` で保存できない
(`helpers.py:403`)。つまり portfolio 側の列を消した後、CSV取込などで新規登録された
銘柄は **イナゴ元の保存先が存在しない** ことになる。

対応 (ユーザー確定):

- `portfolio_shelve.add_to_watch()` で、research レコードが無ければ最小限のものを
  同時に作成する (`rs.create_research_record(code_s, stock_name)` 相当)。
  「監視する銘柄には調査レコードがある」という不変条件を成立させる
- 循環 import に注意: `portfolio_shelve` から `research_shelve` を参照する形になるため、
  既存の遅延 import パターン (`webapp/routes/detail.py` などで使用) に倣う。
  実装前に依存方向を確認し、循環するなら関数内 import にする
- 銘柄名は `stocks_shelve` から引く。取得できない場合は空文字で作る
  (research 側は `stock_name` 必須ではない)
- 既に research がある場合は**何もしない** (上書きしない)

現状の実データでは portfolio 322件すべて research 登録済みなので、この変更による
既存データへの影響は無い。将来の新規登録に対する担保。

**`add_to_watch()` を塞げば十分な根拠** (codexレビュー2周目で確認):
`ps.create_record()` / `ps.upsert_record()` の呼び出し元を全数調査した結果、
通常運用で新規 portfolio レコードを永続化する経路は
`import_portfolio_csv.py` と `webapp/routes/portfolio.py` から入る
`add_to_watch()` のみ。他は以下のとおり該当しない:

- `migrate_my_watch_list_to_shelve.py` / `migrate_portfolio_from_csv.py` —
  実行済みの一回限り移行スクリプト
- `migrate_portfolio_drop_stock_name.py` — 既存レコードの書き換えのみ (新規 code_s なし)
- `webapp/routes/portfolio.py:986-988` — txt フォールバックの**メモリ上の**組み立て。
  docstring に「書き込み API も走らせない (= 表示専用)」と明記のとおり永続化しない

### 5. マイグレーションスクリプト

`scripts/migrate_inago_origin_to_research.py` を新設 (既存 migrate_* の慣例に従う)。

- portfolio の全レコードを走査し、`memo.inago_origin` が非空のものを
  research 側の同一 `code_s` へコピー
- **research 未登録の銘柄**は上記 §4 の変更で発生しなくなるが、マイグレーション
  実行時点では起こり得る。その場合は research レコードを作ってからコピーする
  (skip して取りこぼすと、portfolio 側の列を消した後に参照経路が無くなるため。
  codexレビュー指摘)。実データで確認済み: 43件すべて research 登録済み
- research 側に既に `inago_origin` が入っている場合は**上書きしない** (冪等性)。
  再実行しても安全にする
- `--dry-run` を既定にし、`--apply` で実書き込み (既存 migrate_* と同じ作法)
- 実行後、portfolio 側の値は**消さない** (コード側が読まなくなるだけ。
  post_sell_returns 廃止と同じ後方互換方針)

## 実行順序の注意

マイグレーションは「portfolio 側にデータが残っている状態」で走らせる必要がある。
そのため:

1. research 側フィールド追加 + マイグレーションスクリプト作成
2. **マイグレーション実行 (--apply)**
3. portfolio 側の削除・UI 変更

を1つのPRに含めるが、**ユーザーがマイグレーションを実行するまで portfolio 側の
値は残す**ので、順序を誤ってもデータは失われない。

## テスト

CLAUDE.md のテスト方針 (1PR 5本以下、parametrize で集約) に従い 3本程度:

1. `research_shelve`: `inago_origin` の保存・読み出し、既存レコード (フィールド無し) の
   後方互換 (空文字補完)
2. `save_memo`: フォームから `inago_origin` を保存できる
3. マイグレーション: 既存値の非上書き (冪等性) と research 未登録時の作成を1本で
4. `add_to_watch()`: research 未登録の銘柄を監視登録すると research も作られ、
   既存 research がある場合は上書きされないこと (parametrize で2ケース1本)

既存テストの修正:
- `tests/test_portfolio_shelve.py:638` の `"inago_origin": ""` を除去
- `tests/test_webapp_portfolio_routes.py` の4箇所 (630/648/805/814) を
  `inago_origin` → 他フィールドへ置換、または該当アサーションを削除

## 確認ポイント (実装後にユーザーへ提示)

- ポートフォリオ一覧の列が1つ減るのでレイアウトが崩れていないか (2ページ目)
- 銘柄ページのメモ欄に「イナゴ元」が出て、保存・再表示できるか
- マイグレーション後、43件が research 側で参照できるか
