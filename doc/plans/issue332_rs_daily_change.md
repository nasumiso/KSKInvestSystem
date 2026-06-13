# issue #332: portfolio RS(20,5)列に前日比RSライン騰落率を追加 (ツールチップ + ソート)

## 目的

保有銘柄を「当日の対TOPIX相対挙動 (前日比RSライン騰落率)」で可視化・ランキングできるようにし、
急落時のポジション削減 (相対的に弱い銘柄から切る) の判断材料を提供する。

## 前提 (調査済みの現状)

- **計算ロジックは実装済み**: `make_stock_db.compute_rs_line_changes(stock, market_db, topix_map)` が
  `(a, b, d)` を返す。`d` = 前日比RSライン騰落率(%) = `(rs_line[今日] - rs_line[1日前]) / rs_line[1日前] * 100` (issue #283)。
- **業態テーマページに前例あり**: `helpers.py` の theme_summary 構築 (3679-3692行付近) が
  `compute_rs_line_changes` を呼び `d` を集計し「1日乖離」列・ソートに利用 (issue #328)。
- **portfolio リスト本体にソート機構は既存**: `routes/portfolio.py` の
  `PORTFOLIO_SORT_KEYS = {"position","rank","gyoutai"}` / `_parse_sort_key` / `_build_query_string` /
  `sort_urls` が既にあり、`list_portfolio_with_indicators(records, sort_key)` でソートを切替。
  → #332 はこの機構に前日比キーを1つ足す形。
- **portfolio 側は前日比 d を計算していない**: `list_portfolio_with_indicators` は
  `build_stock_chart_payload(stock, market_db, mode="mini")` で SVG + tooltip を作るが、
  `compute_rs_line_changes` は呼んでおらず、前日比は row に存在しない。
- **#327 (2ページ化) は main 未マージ**: このブランチには `data-page` 属性なし。RS(20,5) 列は単一テーブル内。
  #327 とのマージコンフリクトは並行作業のため後で解消する前提 (本プランのスコープ外)。

## 確定した仕様判断

- **ソート方向**: 前日比 **降順のみ固定** (業態テーマ dev_1d と同じ向き = 強い順が上)。
  昇順/降順トグルは入れない。急落時の「弱い順から切る」は一覧下部を見る運用。
- **ツールチップ表記**: RS(20,5) セルの既存 tooltip (株価 / RSライン乖離 の2行) に
  「前日比: +X.X%」の行を追記する。
- **総合スコアリングには組み込まない** (issue スコープ通り)。code_rank.csv にも追加しない。

## 変更点

### 1. `scripts/webapp/helpers.py`

**1-a. `list_portfolio_with_indicators` で前日比 d を計算し row に格納**

theme_summary と同じパターンを流用する。market_db ロード済みのループ内で、銘柄ごとに
`compute_rs_line_changes(stock, market_db, topix_map=topix_map)` を呼び、`d` を `row["rs_change_1d"]` に格納。

- topix_map: theme_summary は `compute_rs_line_changes` に topix_map を渡して TOPIX マップ再構築を
  避けている。portfolio 側も同様に topix_map を1回構築して渡す (N銘柄 × 再構築を防ぐ)。
  topix_map の構築方法は theme_summary 実装 (helpers.py 内) と同じヘルパを再利用する。
- 例外時は `row["rs_change_1d"] = None`。
- market_db が None のときは全 row で None。

**1-b. ツールチップに前日比を追加 (mini 経路のみ、opt-in)**

`build_stock_chart_payload` → `build_price_rs_chart_mini` → `_build_chart_tooltip` の経路で
tooltip 文字列が生成される。

⚠️ **codex 指摘対応**: `_build_chart_tooltip` は mini (日足、portfolio 一覧) だけでなく
**full (週足、詳細ページ) からも共有**されている (`build_price_rs_chart_full` が
`unit_label="週"` で呼ぶ、helpers.py:2738)。tooltip 内に無条件で前日比行を足すと、
issue #332 の対象外である詳細ページ週足チャートにも波及してしまう。

→ **opt-in 引数方式を採用**:
- `_build_chart_tooltip(..., include_prev_change: bool = False)` を追加。
  True のときだけ末尾に「前日比: {符号付き}%」行を足す。
- `build_price_rs_chart_mini` (portfolio 経路) からは `include_prev_change=True` で呼ぶ。
- `build_price_rs_chart_full` (詳細ページ経路) はデフォルト False のまま = 挙動不変。
- 前日比は `_build_chart_tooltip` 内の `rs_values` (= mini の `rs_asc`、昇順 rs_line 値列) の
  末尾2点 `rs_values[-1]` / `rs_values[-2]` から計算する (d を引数で渡さず自己完結)。

  ⚠️ 整合性チェック: `rs_asc` は `_asc_series_from_log(rs_line, _SPARK_LOOKBACK)` で得た
  **直近20本の昇順列**。末尾2点 = 最新・前日。`compute_rs_line_changes` の d も
  `compute_rs_line` の rs_line 先頭2点 (最新・前日) 比較。両者の「最新」「前日」は同一営業日を
  指すため数値は一致する。`rs_values` が2点未満、または前日値が0の場合は「前日比: —」表示にする
  (compute_rs_line_changes の d=None 条件と揃える)。

  注: ソート用の `row["rs_change_1d"]` (1-a) と tooltip 表示値は同じ定義だが、前者は
  `compute_rs_line_changes` 経由、後者は tooltip 内で rs_values から直接計算という二経路になる。
  両者は同一 rs_line に由来し定義も一致するため値はズレない (どちらも隣接2点比較)。
  実装簡潔性のため二経路を許容する (共通化のための引数引き回しはしない)。

### 2. `scripts/webapp/routes/portfolio.py`

- `PORTFOLIO_SORT_KEYS` に `"rs_change_1d"` を追加。
- `sort_urls` の生成対象キー (`("position","rank","gyoutai")`) に `"rs_change_1d"` を追加。
  `sort_urls` は route 側で `"?" + _build_query_string(...)` と**先頭 `?` 付き**で生成される。
  → テンプレートでは `href="{{ sort_urls['rs_change_1d'] }}"` (= `?` 前置しない)。既存リンクと同形。
- `_build_query_string` / `_parse_sort_key` は `PORTFOLIO_SORT_KEYS` を参照しているので追加だけで動く。

### 3. `scripts/webapp/helpers.py` の `list_portfolio_with_indicators` ソート分岐

- `sort_key == "rs_change_1d"` の分岐を追加。前日比 **降順**、None は末尾、同値はコード順:
  ```python
  rows.sort(key=lambda r: (
      r.get("rs_change_1d") is None,
      -(r.get("rs_change_1d") or 0.0),
      r.get("code_s", ""),
  ))
  ```

### 4. `scripts/webapp/templates/portfolio_list.html`

- RS(20,5) 列ヘッダをソートリンク化:
  ```html
  <th class="{% if active_sort == 'rs_change_1d' %}sort-active{% endif %}"
      title="...3点ミニチャート...。クリックで前日比RSライン騰落率の降順ソート">
    <a href="{{ sort_urls['rs_change_1d'] }}">{% if active_sort == 'rs_change_1d' %}▼前日比{% else %}RS(20,5){% endif %}</a>
  </th>
  ```
  - ソート適用中はヘッダ表示を「▼前日比」に切替 (issue 要件: ソートキー明示)。
  - `sort-active` の CSS は theme_summary の `th.sort-active a { color: #2c5282; }` を流用。
    portfolio_list.html に同等の最小 CSS を追加。
- ツールチップは helpers 側で生成済み (row.price_rs_chart.tooltip) なので template 変更不要。

## テスト (`tests/test_webapp_portfolio_routes.py` / 既存に集約)

CLAUDE.md「1 PR 5本以下・parametrize 集約」に従い最小限:

1. **ソート動作**: `?sort=rs_change_1d` で前日比降順・None末尾・同値コード順になることを1本で検証
   (rs_change_1d を直接 row に注入 or stock データを用意)。
2. **ソートキー受理**: `_parse_sort_key` が `rs_change_1d` を受理し、不正値が DEFAULT に落ちることを確認
   (既存の parse テストがあれば parametrize に1ケース追加)。
3. **ツールチップ**: `_build_chart_tooltip(..., include_prev_change=True)` が前日比行を含むこと /
   `include_prev_change=False` (デフォルト = full 経路) では前日比行を含まないこと /
   末尾2点不足時・前日値0時「前日比: —」になることを parametrize 1本で検証。

`compute_rs_line_changes` の d 自体は make_stock_db 側で既存テスト済みのため再テストしない。

## 検証手順

1. 上記テストを実行 (`pytest tests/test_webapp_portfolio_routes.py -v`、helpers 変更のため
   `pytest tests/test_webapp_helpers.py -v` も)。
2. WebApp 起動し、RS(20,5) ヘッダクリックで前日比降順に並ぶこと・ヘッダが「▼前日比」になること・
   tooltip に前日比行が出ることをブラウザ確認。

## スコープ外 (issue 記載通り)

- 前日比指標の code_rank.csv 追加 / スコアリング組み込み / 急落日自動バナー。
- #327 とのマージコンフリクト解消 (並行ブランチ統合時に別途対応)。
