# issue #401 実装プラン

## 目的

`/disclosure` の決算日カードと適宜開示テーブルで、上方・下方修正など株価インパクトの大きい開示を同じ基準で分かりやすく表示する。あわせて、両セクションへ移動する目次リンクを追加する。

## 方針

### 1. 見出し判定を `disclosure.py` に集約する

- `classify_disclosure_impact(heading)` を追加する。戻り値は `None` または、区分・表示ラベル・ポジ/ネガ・強/弱・`一転` フラグを持つ辞書とする。
- 判定は issue の優先順に、`下方修正`、`上方修正`、`最高益`、`増額修正`/`増配`/`復配`、`減配`/`減額修正`/`無配` とする。
- `一転` は分類とは独立した `surprise=True` とし、分類済みのバッジ・表の双方に小さなマーカーを付ける。
- 増益・減益の着地だけの見出しは、上記キーワードに当たらない限り `None` とする。
- `HEAD_TYPE_DIC` の `newsctg3_kk_b` / `newsctg3_ks_b` の誤った上方・下方コメントを「決算・修正」に直す。

### 2. 適宜開示テーブルを判定結果で強調する

- `make_market_db._html_disclosure()` で、種類が「決算」または「修正」の行だけ共通判定関数を呼ぶ。
- 対象外の決算・修正行は既存の `disc-row-gyoseki` のままにする。
- 上方/下方修正には強いポジ/ネガ背景、最高益・増配/復配・減配/無配には控えめな背景クラスを追加する。`一転` は内容欄にマーカーを併記する。
- `make_market_db.py` の生成CSSに新規行クラスを追加し、適宜開示の見出しを `<h2 id="disclosure-section">` にする。

### 3. 決算日カードへ該当開示を紐付ける

- `webapp.helpers` に、`disclosure_db.csv` を一度だけ読み込んで `(code_s, kessanbi)` ごとに該当開示を返す内部ヘルパを追加する。
- CSVの「決算」「修正」行のみを対象に、決算日の -14日〜+1日（両端含む）を日付として比較する。リンク式から銘柄コード・URL・見出しを抽出し、共通判定関数で未分類を除外する。
- 各決算エントリに、表示対象の `disclosure_impacts`、全件数、全見出しを格納する。重複区分は1つにまとめ、最大2バッジを表示する。ポジ・ネガが同時にあれば各1つを優先し、3件目以降は `+N` と全見出しtooltipで示す。
- CSVがない・不正行の場合は空リストにして、既存の決算カレンダー表示を維持する。

### 4. `/disclosure` のUIを追加する

- `disclosure.html` のコンテンツ先頭に、`#kessan-section` と `#disclosure-section` への横並び目次リンクを置く。内容が長いページでの移動を優先し、今回は sticky にはしない。
- 決算日見出しに `id="kessan-section"` を付ける。
- 決算カードでは、既存の期待度バッジの直後に、記事URLへ遷移するインパクトバッジと `一転` マーカー、必要に応じて `+N` を描画する。全見出しは tooltip で確認できるようにする。
- `webapp/static/style.css` に、目次リンクと強/弱・ポジ/ネガ別のバッジスタイルを追加する。

### 5. テストと確認

- `tests/test_disclosure.py` に、上方/下方/最高益/増配/減配/一転/着地のみを1つの parametrized testで追加する。
- `tests/test_webapp_helpers.py` に、-14日/-1日/+0日/+1日を紐付け、-15日/+2日を除外し、ポジ・ネガ同時を両方返すテストを1本追加する。
- 既存の `tests/test_make_market_db.py` に、適宜開示テーブルの強調クラスと見出しidを確認するテストを追加する。
- `.venv/bin/python -m pytest tests/test_disclosure.py tests/test_webapp_helpers.py tests/test_make_market_db.py -q` を実行する。
- `cd scripts && python make_market_db.py html` で静的開示HTMLを再生成し、ローカル `/disclosure` で目次リンク・決算カードのバッジ・表の色分けを確認する。

## 変更対象

- `scripts/disclosure.py`
- `scripts/make_market_db.py`
- `scripts/webapp/helpers.py`
- `scripts/webapp/templates/disclosure.html`
- `scripts/webapp/static/style.css`
- 関連テスト
