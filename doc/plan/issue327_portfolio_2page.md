# issue #327 実装プラン: portfolio一覧の2ページ化と売買アイデア可視化

## ゴール

portfolio一覧を「自動算出データ(ページ1) / 手動入力データ(ページ2)」の2ページに分割し、
JSトグルで列セットを切り替える。売買アイデアを定型リスト化してページ2に色分けバッジ表示する。
高市感応度を「売買メモ」にリネームする。

検証可能なゴール:
- ページ1/2トグルで列セットが切り替わり、データ取得は1回(ページ遷移なし)
- ページ2に「更新日/ステージ/チャートパターン/売買アイデア(バッジ)/イナゴ元/IN理由(短縮)/売買メモ(短縮)」が出る
- 売買アイデアは定型リスト単一選択(+未分類)、未分類は警告色
- 高市感応度のUIラベルが「売買メモ」になる(DBキー takaichi_sensitivity は不変)
- pytest 緑、`python shintakane.py` 等は不要(HTMLパース無関係)

## スコープ外(issue準拠)

- 戦略ミックスサマリー
- 売買アイデアの時間軸属性(中期/短期イベント) ← #326連携、本PRでは持たせない
- ページ2デフォルト表示
- 過去メモの自動再分類

## 設計判断

### 1. 列のページ切り替え方式: セルに data-page を付与しCSSでtoggle
- 各 `<th>`/`<td>` に `data-page="1"` または `data-page="2"` を付ける
- 両ページ共通の列(コード・銘柄名・状態・評価・業態テーマ・順位)は `data-page="both"`
- `body.portfolio-page-2` クラスの有無で `[data-page="1"]{display:none}` / `[data-page="2"]{display:none}` を切替
- 既存の sticky thead・inline編集・行展開(details)はDOM構造を変えないため影響なし
- トグルUIはダッシュボードヘッダ付近にボタン2つ(「指標」「メモ」)を置く

理由: 列セットを2テーブルに分けるとsticky/行展開/モーダルJSのセレクタが二重化し複雑になる。
セル属性+CSSなら1テーブルのまま、表示/非表示だけで済む(Simplicity First)。

### 2. 売買アイデアの定型リスト: portfolio_shelve.py に定数で定義
- `TRADE_IDEA_OPTIONS` を `portfolio_shelve.py` に tuple で定義(順序保持)
  - 初期値(実装時にユーザー確認で確定。暫定): GARP / テーマ / イベント・カタリスト / モメンタム / 底値リバ
  - 空文字 = 「未分類」(選択肢には出さず、未選択状態を未分類とみなす)
- gyoutai_themes と違い動的マスターではないので shelve ではなく定数が適切(ETF_code等の
  外部txtにするほど可変でない)
- バリデーション: `update_memo()` で trade_idea が `TRADE_IDEA_OPTIONS ∪ {""}` 以外なら ValueError
  - **既存の自由記述値の救済**: 現行レコードに既に入っている値は、リスト外でも保持を許可
    (gyoutai_themes の移行漏れ救済と同じパターン)。これにより過去メモが保存拒否で壊れない
  - **移行層(migrate_portfolio_from_csv)の扱い【確定】**: 移行は `create_record()` で memo を
    そのまま格納し、`update_memo()` を経由しない(確認済: portfolio_shelve.py create_record は
    trade_idea を検証しない)。よって**移行は自由記述をそのまま保持する仕様で確定**。定型チェックは
    かけない。バリデーションは update_memo(=UIからの編集保存)にのみ追加する。
    過去CSVの `押し目買い` 等はそのまま移行され、UI で「リスト外現行値=⚠️option差し込み +
    未分類バッジ」として表示し、ユーザーが手動再分類する(issue方針「自動分類しない」と一致)

### 3. 売買アイデアの編集UI: ページ2セル内の select inline編集
- ページ2の売買アイデア列セルに `<select>` を直接置く(gyoutai_themes と同じ inline 編集系統)
- change で `/portfolio/<code_s>/memo` に POST(既存AJAX動線を再利用)
- バッジ色は定型値ごとに固定色をCSSで割当。未分類は警告色(赤系)

**既存の自由記述値の扱い(codex指摘1)**:
現行 trade_idea は自由記述で `押し目買い` 等の既存値が入っている(test_portfolio_shelve.py:405,
test_migrate_portfolio_from_csv.py:63 に実在)。issue方針は「過去メモの自動分類はしない=手動再分類」。
そこで gyoutai_themes と同じ「リスト外の現行値を選択肢に差し込む」方式を採る:
- select の option を `TRADE_IDEA_OPTIONS` + 空(未分類) で構成
- **現行値がリスト外なら、先頭に `⚠️ <現行値>` option を selected で差し込む**(gyoutai_themes の
  未登録name表示と同じパターン: portfolio_list.html:358-360)
- ユーザーが別の定型値を選べば再分類完了。選ばない限り既存値は保持され消えない
- これにより事前マイグレーション不要。バッジは未分類(警告色)扱いで表示し、再分類を促す

### 4. IN理由・売買メモの短縮表示: 既存 jukyu_chart パターンを踏襲
- セルは1行省略表示(`overflow:hidden;text-overflow:ellipsis;white-space:nowrap`)
- `title`(ホバー)で全文。クリックで既存 `.editable`+`data-multiline="1"` の textarea inline編集
- jukyu_chart 列が全く同じ実装なので、それを2列(watch_in_reason / takaichi_sensitivity)に展開するだけ

### 5. 高市感応度→売買メモ リネーム
- UIラベルのみ変更。DBキー `takaichi_sensitivity` は維持(マイグレーション不要)
- 変更箇所: portfolio_list.html のラベル文字列、detail系に出ていれば同様
- コメントに「旧:高市感応度」を残し、キー名との対応が追えるようにする

### 6. 展開パネルの扱い
- IN理由・売買メモ・売買アイデア・イナゴ元はページ2の列に出るため、展開パネル内の
  該当フォームは**不要**になる。展開パネル(手動メモフォーム)は削除する
- ただし行展開(details)の仕組み自体は他に使っていなければ撤去。要確認: 展開パネルが
  手動メモ表示専用なら、行頭の ▸ ボタンごと削除する
  - フォールバックモードの読み取り専用 ul も整理(売買メモへのラベル変更のみ反映)

## 変更ファイルと具体的変更

### A. `scripts/portfolio_shelve.py`
1. `TRADE_IDEA_OPTIONS` 定数を追加(tuple)
2. `update_memo()` の trade_idea バリデーション追加
   - リスト外かつ現行レコードに無い値 → ValueError
   - リスト外だが現行レコードに既存 → 許容(救済)

### B. `scripts/webapp/routes/portfolio.py`
1. `update_memo()` の保存 → 行再構築は既存だが、AJAX応答の `display` は現状
   `last_research_update`/`stage`/`jukyu_chart`/`gyoutai_*` のみ(portfolio.py:447付近)。
   今回 inline編集する `trade_idea`/`watch_in_reason`/`inago_origin`/`takaichi_sensitivity`
   を **`display` に追加**しないと保存後にクライアントが正しい表示値・バッジを同期できない
   (codex指摘2)。これら4フィールドを display に含める
   - trade_idea は再分類後のバッジ色判定に必要なので display 値で `data-trade-idea` 属性 or
     バッジ class をクライアントが付け替える。JS側で再描画する
2. ValueError(リスト外新規値)を AJAX エラーJSON `{ok:false, error:...}` として返す既存
   ハンドリングを確認・流用

### C. `scripts/webapp/templates/portfolio_list.html`
1. ヘッダにページトグルボタン(指標/メモ)を追加
2. `<style>` に:
   - `body.portfolio-page-2 [data-page="1"]{display:none}` / 既定で `[data-page="2"]{display:none}`
   - 売買アイデアバッジ色(定型値ごと + 未分類警告色)
3. thead の各 `<th>` に `data-page` 属性を付与し、更新日/ステージ/チャートパターンを page2 に
4. tbody の対応する各 `<td>` に同じ `data-page` を付与
5. ページ2列を追加:
   - 売買アイデア(select inline編集 + バッジ表示)
   - イナゴ元(短縮表示 + inline編集)
   - IN理由(短縮表示 + inline編集、jukyu_chart パターン)
   - 売買メモ(短縮表示 + inline編集、jukyu_chart パターン)
6. 展開パネル(手動メモフォーム)を削除。行頭 ▸ もメモ専用なら削除
7. フォールバック表示の「高市感応度」→「売買メモ」
8. `<script>` にページトグルのJS(body class付け外し)を追加
9. colspan(展開行)を削除 or 調整

### D. `scripts/webapp/helpers.py`
- 売買アイデアバッジ・未分類判定に必要なら row に値を載せる(memo.trade_idea は既に row.memo にある)
- compute_cell_styles で trade_idea の未分類警告色を返すか、テンプレ側CSSで処理するか決める
  → テンプレ側CSS(セレクタ)で処理しstyles関数は触らない方が surgical

### E. detail.html(該当あれば)
- 高市感応度ラベルが出ていれば「売買メモ」に変更

## テスト(5本以下、parametrize集約)

`tests/test_portfolio_shelve.py` に(shelve層のバリデーション):
1. `update_memo` で trade_idea にリスト内の値 → 保存成功 / リスト外新規値 → ValueError
   / リスト外でも現行レコードに既存なら保持(救済) を parametrize で1関数に集約

`tests/test_webapp_portfolio_routes.py` に(route層の契約。codex指摘3 — 最も壊れやすい):
2. `/portfolio/<code>/memo` AJAX応答の `display` に
   trade_idea/watch_in_reason/inago_origin/takaichi_sensitivity が含まれる
3. 既存テスト(:185付近の一覧列・inline編集)が列再編で壊れていないか確認・必要なら更新
   (列の data-page 化で参照セレクタがズレる可能性。回帰を吸収)

**既存テストの一括更新(codex指摘 — pytest緑の必須条件)**:
trade_idea を定型値制限すると、自由記述値を保存している既存テストが ValueError で落ちる。
新規保存値は「現行レコードに無い新規値」なので救済対象外=直撃する。該当箇所を定型値
(TRADE_IDEA_OPTIONS のいずれか)へ一括置換する:
- test_webapp_portfolio_routes.py:592, 602, 680, 824 付近(`trade_idea="X"` 等)
- test_portfolio_shelve.py:414, 443 付近(`"X"`/`"X2"`/`"val_trade_idea"` 等)
- test_migrate_portfolio_from_csv.py:63 付近: **変更不要**。移行は自由記述を保持する仕様で
  確定(create_record は trade_idea を検証しない)ため、既存の `押し目買い` テストはそのまま
  維持する。むしろ「リスト外値が移行後も保持される」回帰防止として残す価値がある
これは「テストを5本以下」の新規追加方針とは別枠の既存修正(回帰吸収)。

テンプレート(HTML/JS/CSS)のページトグル・短縮表示・バッジ色はブラウザ目視確認(Playwright)。

実行: `pytest tests/test_portfolio_shelve.py tests/test_webapp_portfolio_routes.py tests/test_migrate_portfolio_from_csv.py -v`

## 検証手順

1. pytest 上記
2. `python -m webapp.app` 起動 → /portfolio
3. ページトグル(指標/メモ)で列セットが切り替わるか
4. ページ2で売買アイデア select 変更 → 即保存・バッジ色反映
5. 未分類銘柄が警告色か
6. IN理由・売買メモが短縮表示+ホバー全文+クリック編集できるか
7. 高市感応度ラベルが「売買メモ」になっているか
8. Playwright スクショ(.playwright-mcp/issue327-*.png)

## 確定事項(ユーザー確認済み)

- 売買アイデアの定型リスト内容: **暫定値で進める**
  (GARP / テーマ / イベント・カタリスト / モメンタム / 底値リバ)
- トグルボタンのラベル文言: **「指標 / メモ」**
- 行展開(▸): **完全撤去する**(手動メモがページ2列に出るため不要)
