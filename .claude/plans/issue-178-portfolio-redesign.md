# issue #178: 保有銘柄ダッシュボード再設計 (タブ廃止 → フィルタ/ソート/ページング)

## 背景・要件確定

issue 本文 (#178) と追加コメントを踏まえ、以下で確定:

- **基本要件**: 保有銘柄の一覧比較 (業態・テーマ別に PER/成長率を見比べる)
- **デフォルト表示**: 保有 (1保) のみ
- **件数対策**: ページング (50件/ページ)
- **状態保持**: URL のみ (localStorage / Cookie は使わない)
- **ソート軸**: 業態順 (デフォルト) / 順位順 の2択のみ (列ヘッダ汎用ソートは別 issue 送り)

issue 178 原案からの差分:
- デフォルトを「全件」→「保有のみ」に変更 (一覧比較の主対象は保有銘柄)
- ページング (`?page=N`, 50件/ページ) を追加 (重さ対策)

## URL 設計

```
/portfolio                              # デフォルト: 保有のみ + 業態順 + 1ページ目
/portfolio?status=hold,semi             # 保有+準保有
/portfolio?status=hold,semi,watch       # 全件
/portfolio?sort=rank                    # 保有 + 順位順
/portfolio?status=hold,semi&page=2      # 2ページ目
/portfolio?status=hold                  # 既存 URL (互換性維持)
```

- `status` 未指定 = `hold` (現状デフォルトと同じ。互換性維持)
- `status` は **2 つの送信形式を両方受理**して内部で正規化:
  - カンマ区切り単一キー: `?status=hold,semi` (URL 直接指定・既存互換)
  - 同名複数キー: `?status=hold&status=semi` (HTML form の checkbox 標準送信形式)
  - サーバ側は `request.args.getlist("status")` で全値取得 → 各値をカンマでさらに split → flat 化 → 不正値除去
  - 実装ヘルパ: `_parse_status_filter(args: MultiDict) -> list[str]` (引数を文字列ではなく MultiDict にする)
- `sort` 未指定 = `gyoutai`。値は `gyoutai` / `rank` のみ受理、他は `gyoutai` にフォールバック
- `page` 未指定 = 1。1未満や非数値は 1 にフォールバック。最大ページ数を超えたら最終ページ
- 1ページ50件固定 (定数)

## UI

```
┌─────────────────────────────────────────────────────────────┐
│ 保有銘柄ダッシュボード                            [管理]    │
│                                                              │
│ [フィルタ] ☑保有(8) ☐準保有(87) ☐監視(38)               │
│ [ソート]  ●業態順 ○順位順         [絞り込み]             │
│                                                              │
│ 8件中 1-8 件表示                                             │
│                                                              │
│ ┌──┬───┬───┬─────┬─────┬───┬─...                          │
│ │ │コード│銘柄│業態  │状態 │順位│                          │
│ │  │      │    │      │     │    │                          │
│ ...                                                          │
│                                                              │
│                          [<<前]  1/1  [次>>]                │
└─────────────────────────────────────────────────────────────┘
```

- フィルタは checkbox (3個) + ラジオ (2個) の `<form method="GET">`。submit ボタン「絞り込み」を1つ
- フィルタ/ソート変更時は `page=1` にリセット (form 側で page input を出さない or hidden=1)
- 件数表示: `{total}件中 {start}-{end} 件表示`
- ステータス列 (badge: 保有=緑/準保有=黄/監視=灰) を**常時表示**。デフォルトが保有のみでも、複数選択した瞬間に必要になるため
- ページネーション: 前/次のリンクと「現ページ/総ページ」表示。総1ページならボタン非表示
- 業態順時のみ、業態境界 (前行と業態 1 行目が異なる) に区切り線 (`border-top: 1px solid #ddd`)。区切り判定はページ内で完結 (ページ跨ぎは無視)

## 実装影響

### 変更: `scripts/webapp/routes/portfolio.py`

- `STATUS_QUERY_TO_VALUE` は維持 (status query → 内部値の変換に引き続き使う)
- `TABS` / `DEFAULT_TAB` 定数を削除
- `_resolve_status_query()` を削除し、新規 `_parse_status_filter(args) -> list[str]` を追加
  - 引数は `request.args` (MultiDict)
  - `args.getlist("status")` で取得した各値をさらにカンマ split → flat 化 → trim → 小文字化
  - `STATUS_QUERY_TO_VALUE` で内部値 (1保/2準/3監) に変換、不正値は無視
  - 結果が空 (= status 指定なし or 全不正値) は `["1保"]` (デフォルト)
  - 例:
    - `?status=hold,semi` → `["1保","2準"]`
    - `?status=hold&status=semi` → `["1保","2準"]`
    - `?status=hold,xxx&status=watch` → `["1保","3監"]`
    - `?` (なし) → `["1保"]`
- 新規 `_parse_sort(query: str) -> str` 追加。`gyoutai` / `rank` のいずれか
- 新規 `_parse_page(query: str, total_pages: int) -> int` 追加
- 新規 `_paginate(rows, page, per_page=50) -> tuple[list, int, int, int]` (rows_slice, page, total_pages, total)
- `_redirect_to_current_tab()` を `_redirect_with_filters()` 相当に置き換え or 削除
  - memo / transition 後のリダイレクトは「フィルタ・ソート・ページ」を保持して戻したい
  - フォーム POST 側で hidden に現状のクエリを持たせてサーバへ送り、リダイレクト先の URL に反映する
  - (簡易案) hidden に `return_query` を入れ、サーバがそのまま `?...` を付ける
- `dashboard()` 改修:
  - status / sort / page を解析
  - records → status フィルタ → sort → ページング の順に処理
  - counts はフィルタ前の visible_records から集計 (常に hold/semi/watch の3つを返す)
  - transitions は「複数 status 選択」だと一意の current が決まらない → 詳細行の操作 UI 側 (展開後) でその銘柄個別の status 基準で出すよう、template に渡すデータ構造を変更:
    - 旧: `transitions=[(label, value), ...]` (active タブの status を前提)
    - 新: 各 row に `row.transitions=[(label, value), ...]` を埋める (row.status から導出)
- POST ハンドラ (transition / memo / bulk-exclude / add) のリダイレクト先:
  - 既存は `?status=hold|semi|watch` 単数値で返していた
  - 新仕様では「直前のフィルタ状態を維持して戻す」のが理想だが、実装簡素化のため**最小修正**:
    - フォームに hidden `return_query` (例: `status=hold,semi&sort=rank&page=2`) を出し、サーバはそれを `?` に繋いでリダイレクト
    - hidden が無い場合は `/portfolio` (デフォルト = 保有のみ) にフォールバック
  - bulk-exclude の `return_to=hold|semi|watch` は廃止し、上記 `return_query` 方式に統一

### 変更: `scripts/webapp/templates/portfolio_list.html`

- タブ `<nav>` ブロック (line 73-85) を**フィルタバー form** に置換
  - checkbox×3 (hold/semi/watch) + radio×2 (gyoutai/rank) + submit
  - `name="status" value="hold"` の checkbox 3つ
    - HTML 標準では同名 checkbox は `?status=hold&status=semi` 形式で送信される
    - サーバは `getlist("status")` で受け、URL 設計セクションで規定したカンマ区切り形式と両方受理する正規化を通す
  - `name="sort"` の radio 2つ
  - 件数は label 内 `<span>{{ counts.hold }}</span>` で表示
- thead に**ステータス列**を追加 (`<th>状態</th>`)。コード列の左 or 右
- tbody の各 tr に**ステータス badge セル**追加: `<td><span class="status-badge status-{{ row.status_query }}">{{ row.status_label }}</span></td>`
  - CSS: `.status-hold { background:#cfc; color:#060 }` `.status-semi { background:#ffc; color:#860 }` `.status-watch { background:#ddd; color:#666 }`
- 業態順時のみ業態境界に区切り線:
  ```jinja
  {% if active_sort == 'gyoutai' and not loop.first %}
    {% set prev_first = (loop.previtem.memo.gyoutai_themes or [''])[0] %}
    {% set curr_first = (row.memo.gyoutai_themes or [''])[0] %}
    {% if prev_first != curr_first %}
      style="border-top:2px solid #bbb;"
    {% endif %}
  {% endif %}
  ```
  (実装は class を切り替えるほうが綺麗。詳細は実装時調整)
- ページネーションを `<table>` の下に追加:
  ```html
  <nav class="pagination">
    {% if page > 1 %}<a href="?{{ prev_query }}">< 前</a>{% endif %}
    <span>{{ page }} / {{ total_pages }}</span>
    {% if page < total_pages %}<a href="?{{ next_query }}">次 ></a>{% endif %}
  </nav>
  ```
  total_pages == 1 のときはナビ全体を非表示
- 各 row の操作 UI (transition セレクタ) は `row.transitions` を使うよう変更
- 削除モード (bulk-exclude) は「watch / semi がフィルタに含まれる時のみ表示」に条件変更
  - 旧: `{% if active_query in ("watch", "semi") and rows %}`
  - 新: `{% if ('2準' in active_statuses or '3監' in active_statuses) and rows %}`
- 「管理」パネル内の `return_to` hidden を `return_query` (現状の URL クエリ全体) に置換
- 各 form (transition / memo / bulk-exclude) に hidden `return_query` を追加

### 変更: `scripts/webapp/helpers.py`

- `list_portfolio_with_indicators(records, sort_key="gyoutai")`:
  - `sort_key` 引数を追加
  - `sort_key == "rank"` → 既存の rank 昇順 (None 末尾)
  - `sort_key == "gyoutai"` → 業態 1 行目昇順 → 順位昇順 (両 None は末尾)
  - 各 row に `status_query` (例: "hold") と `status_label` (例: "保有") を埋める
  - 各 row に `transitions: list[tuple[str,str]]` を埋める (status から導出)
  - (transitions 計算は portfolio.py 側のヘルパに残し、helpers では status_query/label のみ埋める案も可。実装時に判断)
- 業態の取り出しヘルパを切り出す:
  ```python
  def _gyoutai_first_line(row: dict) -> str:
      themes = (row.get("memo") or {}).get("gyoutai_themes") or []
      return themes[0].strip() if themes and themes[0] else ""
  ```

### 削除

- `TABS`, `DEFAULT_TAB` 定数 (portfolio.py)
- `_resolve_status_query()` (portfolio.py) — 単一値前提なので不要
- bulk-exclude の `return_to` 仕様 → `return_query` 方式に統一

## テスト

### 変更: `tests/test_webapp_portfolio_routes.py`

- 既存タブテストを更新:
  - `test_dashboard_default_tab_is_hold` → `test_dashboard_default_shows_hold_only`
  - `test_dashboard_watch_tab` → `test_dashboard_status_watch_filter`
  - `test_dashboard_unknown_status_falls_back_to_hold` → 同名で挙動維持確認 (デフォルトが hold のため)
- 新規:
  - `test_dashboard_multi_status_filter_csv`: `?status=hold,semi` (カンマ区切り) で両方表示
  - `test_dashboard_multi_status_filter_repeated`: `?status=hold&status=semi` (HTML form 送信形式) で両方表示
  - `test_dashboard_multi_status_filter_mixed`: `?status=hold,xxx&status=watch` (混在 + 不正値混入) で hold + watch 表示
  - `test_dashboard_all_status_filter`: `?status=hold,semi,watch` で全件
  - `test_dashboard_sort_gyoutai_orders_by_first_line`: 業態 1 行目順
  - `test_dashboard_sort_rank_orders_by_rank`: 順位順 + 業態無視
  - `test_dashboard_pagination_first_page`: per_page=50 で50件まで表示 (fixture を増やすか、page_size を test 用に小さく差し替え)
  - `test_dashboard_pagination_second_page`: `?page=2` で次の50件
  - `test_dashboard_pagination_invalid_page_falls_back`: `?page=0` `?page=abc` `?page=999` の挙動
  - `test_dashboard_status_badge_visible`: status 列・badge HTML が出る
  - `test_dashboard_filter_form_resets_page_on_submit`: フィルタ form に page hidden が無い (or 1 を出す)
  - `test_legacy_url_status_hold_still_works`: `?status=hold` (単一値) が動作
- 既存の transition / memo / bulk-exclude テスト:
  - hidden `return_query` 付きでテストし、リダイレクト先がそれを反映するか検証
  - 既存テストが `?status=watch` 等を期待している箇所を調整
  - bulk-exclude の `return_to` テスト 2 件を `return_query` 方式に書き換え

### 変更: `tests/test_webapp_helpers.py`

- 新規:
  - `test_list_portfolio_sort_by_gyoutai_then_rank`: 業態順、二次キー rank
  - `test_list_portfolio_sort_by_rank_only`: 順位順
  - `test_list_portfolio_empty_gyoutai_goes_to_end`: 空業態は末尾
  - `test_list_portfolio_gyoutai_uses_first_line_only`: gyoutai_themes[0] のみで判定 (改行なしの list なので 1 件目)
  - `test_list_portfolio_status_query_label_filled`: row に status_query / status_label が入る

## スコープ外 (issue 178 と同じ)

- 業態自体のフィルタ (例: 「人材」業態だけ抽出)
- 列ヘッダクリック式の汎用ソート
- 業態テーマの正規化
- 色分け (#177 で並行対応)
- localStorage / Cookie での状態保持 (URL のみで十分と判断)

## 実装順序 (検証ポイント付き)

1. **helpers.py** 改修: `list_portfolio_with_indicators(sort_key=...)` + status_query/label 埋め
   - 検証: `pytest tests/test_webapp_helpers.py -v` 新規テスト緑
2. **portfolio.py** 改修: status 複数解析 / sort / page / counts 集計 / 各 row.transitions
   - 検証: `pytest tests/test_webapp_portfolio_routes.py -v` 既存緑 + 新規緑
3. **portfolio_list.html** 改修: フィルタバー / ステータス列 / ページネーション / return_query hidden
   - 検証: ブラウザ目視 (`python -m webapp.app`)
     - `/portfolio` でデフォルト保有のみ
     - チェック切替で URL 変化、件数変化
     - ソート切替で並び順変化、業態境界の区切り線
     - 50件超えた場合のページング (fixture を増やすか、テストで一時的に per_page=2 等で確認)
     - transition/memo/bulk-exclude 後に元の filter/sort/page に戻る
4. テスト全実行 + simplify レビュー

## リスク・懸念

- **transitions が row 単位になる影響**: 既存の template は「アクティブタブの status から transitions を1つ計算してフォームに埋める」構造。新仕様では各 row が独自の status を持つため row.transitions が必要。template の transitions 利用箇所を確認して書き換える。
- **return_query の URL 組立**: hidden に raw クエリ文字列を入れるとエスケープに注意。Flask の `url_for` + `request.args.to_dict(flat=False)` を使うほうが安全。実装時に検討。
- **ページネーションのテスト fixture**: 既存 fixture は3件しか登録していない。50件超のページング検証には fixture を増やすか、`PORTFOLIO_PAGE_SIZE` を環境変数 or アプリ config で差し替え可能にして test 側で 2 等に下げるのが現実的。後者を推奨。
- **業態境界の判定**: ページ跨ぎを無視するため「ページ内最初の行は border 無し」になる。これは仕様として許容。

## 関連

- 親: #168 (Phase 3 全体)
- 直前: #171 (Phase 3b 一覧ダッシュボード) / PR #176 — 構造変更の対象
- 兄弟: #175 (memo 編集), #177 (色分け), #186 (削除モード), #187 (業態テーマ構造化)
