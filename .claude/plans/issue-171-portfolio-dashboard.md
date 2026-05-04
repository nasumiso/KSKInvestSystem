# issue #171 実装計画: Phase 3b 保有銘柄ダッシュボード UI

> 親 issue: #151 (Phase 3 全体)
> 依存: #170 (Phase 3a) のマージ後に main 取り込み。本 PR は `issue-170-portfolio-shelve` ブランチ起点でスタックする
> 要件仕様書: [doc/requirements/phase3_portfolio_requirements.md](../../doc/requirements/phase3_portfolio_requirements.md) §5 / §6-1
> 上位プラン: [.claude/plans/issue-151-portfolio.md](issue-151-portfolio.md) §4

---

## 1. スコープと前提

### 1-1. 本 PR でやること

- `/portfolio` ダッシュボード (3 タブ: 1保 / 2準 / 3監) の GET 実装
- ステータス変更・追加・売却・削除の POST エンドポイント実装
- 銘柄詳細ページに「3監に追加」ボタン追加
- ナビに「保有銘柄」リンク追加
- ユニットテスト + (必要なら) Playwright E2E

### 1-2. スコープ外 (Phase 4 / 別 issue)

- §5-3 警告シグナルの赤バッジ強調 (シグナルは **テキスト表示のみ**)
- §6-2 イベントログ自動記録 (Shintakane 日次バッチ統合)
- §6-3 振り返りビュー (→ Phase 3c / issue #172)
- アクションログ「見送り」「メモ」種別 (Phase 4)

### 1-3. ブランチ戦略

- ブランチ名: `issue-171-portfolio-dashboard`
- base: `issue-170-portfolio-shelve` (Phase 3a の PR #173 ブランチ)
- PR base も同じ。Phase 3a が main にマージされたタイミングで GitHub が自動的に main 相対の差分に切り替える
- 本番運用への影響: **Phase 3a がマージされるまでなし**。Phase 3b PR は main から見ると Phase 3a + 3b の累積差分になる

---

## 2. URL / Blueprint 設計

### 2-1. ルート定義

| URL | メソッド | ハンドラ | 機能 |
|---|---|---|---|
| `/portfolio` | GET | `portfolio.dashboard` | ダッシュボード (タブ切替: `?status=hold/semi/watch`、デフォルト `hold`) |
| `/portfolio/add` | POST | `portfolio.add` | 銘柄を 3監 に追加 (理由任意) |
| `/portfolio/<code_s>/transition` | POST | `portfolio.transition` | ステータス変更 (1保→2準 は内部で「売却」として記録) |
| `/portfolio/<code_s>/delete` | POST | `portfolio.delete` | 削除 (3監 からのみ。理由必須) |

`status` クエリパラメータ:
- `hold` ⇄ `1保`、`semi` ⇄ `2準`、`watch` ⇄ `3監`
- 内部で `1保` 等の正規化文字列に変換して `portfolio_shelve.list_records(status=...)` に渡す

### 2-2. Blueprint 登録

- `webapp/routes/portfolio.py` に `portfolio_bp = Blueprint("portfolio", __name__)`
- `webapp/__init__.py` の `create_app()` に `from webapp.routes.portfolio import portfolio_bp` + `app.register_blueprint(portfolio_bp)` を追加
- url_prefix なし (既存 Blueprint と統一)

---

## 3. ダッシュボード一覧表示 (`/portfolio`)

### 3-1. 表示する情報

要件 §5-2 の 6 カテゴリすべて。情報密度を抑えるため **常時表示** と **行クリック展開** に分割。

#### 常時表示 (テーブル列、左→右)

| 列 | データソース | フォーマット |
|---|---|---|
| コード | portfolio_shelve | `code_s` |
| 銘柄名 | portfolio_shelve | `stock_name` (リンクで `/stock/<code_s>`) |
| ステータス | portfolio_shelve | `1保` / `2準` / `3監` (タブ自体で絞り込みなので情報冗長だが揃え) |
| 順位 | stocks_shelve | total_pt 順位 (なければ "—") |
| PER | stocks_shelve | 整数 |
| 時価総額 | stocks_shelve | 億円表示 |
| 配当 | stocks_shelve | % |
| RS | stocks_shelve | 整数 |
| ステージ | stocks_shelve | "上昇" 等の短縮表記 |
| トレンドテンプレート | stocks_shelve | 短縮表記 (既存の helper を流用) |
| シグナル | stocks_shelve | テキスト列で「警」「売」「新高値」等を空白区切り。**赤バッジ強調なし** |
| 理論株価乖離率 | stocks_shelve | % (上限/下限の近い方を表示、既存実装に倣う) |

#### 行クリック展開 (アコーディオン or `<details>`)

行クリックで下部に縦展開:
- **業績の詳細**: 売上・利益成長率、進捗率 (stocks_shelve)
- **手動メモ**: gyoutai_theme / watch_in_reason / trade_idea / inago_origin / takaichi_sensitivity (portfolio_shelve)
- **アクションへのリンク**: 「ステータス変更」「(3監 タブのみ) 削除」「銘柄詳細を開く」

実装は `<details><summary>` で JS 不要 (Pico.css の素朴な見栄えで十分)。後で UX 改善が必要になったら JS 化を検討する。

### 3-2. タブナビ

```
[ 保有中 (1保) ] [ 準保有 (2準) ] [ 監視 (3監) ]
```

`<a href="/portfolio?status=hold">` 等の単純リンクで実装。アクティブタブはサーバー側で判定して CSS class を付与。タブ自体に件数を表示 (例: `保有中 (24)`)。

### 3-3. ヘルパ関数の責務 (`webapp/helpers.py` への追加)

```python
def list_portfolio_with_indicators(status: str) -> list[dict]:
    """portfolio_shelve のレコードに stocks_shelve から指標を補完。

    Returns:
        各 dict は { ...portfolio_record, "indicators": {per, market_cap, rs, ...},
                     "rank": int|None, "signals": list[str], ... }
    """
```

- `portfolio_shelve.list_records(status=...)` を呼ぶ
- 各レコードに `stocks_shelve` から指標を補完 (なければ "—" 等のプレースホルダ)
- ソート順: 既存総合 PT 順位昇順 (なければ末尾)

### 3-4. テンプレート構造

- `templates/portfolio_list.html` (新規):
  - `{% extends "base.html" %}`
  - 上部: タブナビ + 件数バッジ
  - メイン: テーブル (`<table>` + Pico.css)
  - 各行: `<details>` で展開セクション
- `templates/portfolio_dialogs.html` (新規 / partial):
  - 「ステータス変更」「削除」「3監に追加」のフォーム部品
  - portfolio_list.html と detail.html の両方から `{% include %}` で再利用

---

## 4. ステータス変更 / 追加 / 売却 / 削除 の UI と内部動作

### 4-1. 銘柄追加 (`/stock/<code_s>` から)

#### UI
- `templates/detail.html` の上部 `<div class="stock-header">` 右カラムに「3監に追加」ボタンを表示
- ボタン押下で確認ダイアログ (HTML5 `confirm()` でも可) → POST `/portfolio/add` (form: `code_s`, `reason`)
- `reason` は任意。Phase 3b では UI 上はシンプルに `code_s` のみ送信し、理由は空のまま `初回登録` ログを残す

#### 内部
- `portfolio_shelve.add_to_watch(code_s, stock_name, reason="WebApp 追加")` を呼ぶ
  - **`upsert_record` は使わない** (重複時に既存メモを上書きするため危険)
  - `add_to_watch` は内部で「重複なら ValueError」「初回登録ログ自動記録」「status=3監 固定」を提供する Phase 3a 実装済 API
  - `reason` は **keyword-only 引数** なので `add_to_watch(code_s, stock_name, reason="...")` のように渡す
- 既存銘柄の再追加 (= ValueError) は flash で「既に登録済みです」と警告のみ表示し no-op
- 完了後 `redirect(url_for("portfolio.dashboard", status="watch"))` (3監 タブで確認できる)
- `stock_name` は `stocks_shelve.get_stock_data(code_s)` から引く。ない場合は空文字 (移行と同じ振る舞い)

#### バリデーション
- `code_s` が `portfolio_shelve.validate_code_s` で OK か
- 銘柄が `stocks_shelve` にあるか (なければ既存 `add_stock` を先に呼ぶ。research_shelve への登録ロジックは既存 `webapp.helpers.add_stock` を再利用)

### 4-2. ステータス変更 (1保 ⇄ 2準 ⇄ 3監)

#### UI
- `/portfolio` の行クリック展開内に「ステータス変更」ボタン
- 押下で簡易ダイアログ (`<dialog>` または HTML5 `prompt()`):
  - **遷移先プルダウン** (許可遷移のみ表示): 例えば `1保` 行なら `2準 (売却)` / `3監` のみ。`portfolio_shelve.transition_status` 内部の許可表に整合
  - **理由テキストエリア** (任意。1保→2準 売却時は推奨)
- POST `/portfolio/<code_s>/transition` (form: `new_status`, `reason`)

#### 内部
- `portfolio_shelve.transition_status(code_s, new_status, reason=reason)` を呼ぶ (`reason` は **keyword-only**)
- transition_status 内部の挙動 (Phase 3a 実装済):
  - **同一ステータス遷移 (例: 1保→1保) は no-op、ValueError は投げない**
  - 不正遷移 (許可表に含まれない) は ValueError
  - レコード未登録は KeyError
  - 1保→2準 は内部で `売却` 種別ログ、それ以外は `ステータス変更` ログ自動記録
- 完了後 `redirect(url_for("portfolio.dashboard", status=<new_status_query>))`

#### バリデーション
- 遷移が `portfolio_shelve.ALLOWED_TRANSITIONS` (Phase 3a 定義) に含まれるか — transition_status 側で検証される
- 不正遷移は ValueError、未登録は KeyError → flash + 元タブへリダイレクト (4xx は返さず UX 重視)
- 同一遷移は no-op で正常 return → flash 出さずにダッシュボードに戻る (UI で disabled にしてここに来ないのが理想だが、二重発火対策で no-op を許容)

### 4-3. 削除 (3監 からのみ)

#### UI
- `/portfolio?status=watch` の 3監 タブ行のみ「削除」ボタン表示
- 1保 / 2準 タブには削除ボタン非表示 (誤操作防止 + ガード二重化)
- 押下で確認ダイアログ + 理由必須テキストエリア
- POST `/portfolio/<code_s>/delete` (form: `reason`)

#### 内部
- `portfolio_shelve.delete_record(code_s, reason=reason)` を呼ぶ (`reason` は **keyword-only**)
- delete_record 内部の挙動 (Phase 3a 実装済):
  - レコード未登録なら False を返す (例外なし)
  - status が `3監` 以外なら ValueError (1保 / 2準 から直接削除はガード)
  - 成功時は `削除` ログを 1 件記録、本体を物理削除
- UI 側でも 3監 タブのみ削除ボタン表示 (二重ガード)
- 完了後 `redirect(url_for("portfolio.dashboard", status="watch"))`

#### バリデーション
- `reason` が空文字なら flash で「削除理由は必須」と表示して reject
- 1保 / 2準 銘柄に対する直接 POST は ValueError → 4xx 相当だが flash + redirect で対応

### 4-4. shelve→txt 同期の発火タイミング

Phase 3a で `portfolio_shelve.sync_to_my_watch_list_txt()` を実装済 (明示呼び出し方式)。Phase 3b の各 POST ハンドラの末尾で呼ぶ:

```python
# 例: portfolio.transition の末尾
portfolio_shelve.transition_status(code_s, new_status, reason=reason)  # reason は keyword-only
portfolio_shelve.sync_to_my_watch_list_txt()  # ← shelve 反映後に txt も書き出す
return redirect(...)
```

ヘルパ関数 (例: `webapp/helpers.py:portfolio_after_write_hook()`) を作って 3 ハンドラから共通呼び出しする方が DRY だが、3 行程度なので各ハンドラに直接書く方がシンプル。**実装時に判断** (Karpathy 原則: シニアが overcomplicated と言うか自問)。

---

## 5. ナビゲーション統合

### 5-1. `templates/base.html` のナビ修正

既存の左 `<ul>` (L16-18) に 1 行追加:

```html
<li><a href="/portfolio">保有銘柄</a></li>
```

### 5-2. `templates/detail.html` の修正

`<div class="stock-header">` 右カラムに条件分岐:

```html
{% if not in_portfolio %}
  <form action="/portfolio/add" method="POST" style="display:inline">
    <input type="hidden" name="code_s" value="{{ code_s }}">
    <button type="submit" onclick="return confirm('3監に追加しますか?')">3監に追加</button>
  </form>
{% else %}
  <span>portfolio: {{ portfolio_status }}</span>  {# 1保/2準/3監 #}
  <a href="/portfolio?status={{ portfolio_status_query }}">→ ダッシュボードで開く</a>
{% endif %}
```

`detail.py` の handler 側で `in_portfolio` / `portfolio_status` をテンプレに渡す。

---

## 6. ファイル構成

### 6-1. 新規

| ファイル | 内容 |
|---|---|
| `scripts/webapp/routes/portfolio.py` | Blueprint + 4 ハンドラ |
| `scripts/webapp/templates/portfolio_list.html` | ダッシュボード本体 |
| `scripts/webapp/templates/portfolio_dialogs.html` | 編集ダイアログ partial (`{% include %}`) |
| `tests/test_webapp_portfolio_routes.py` | ルートのユニットテスト (test_webapp_routes.py パターン) |

### 6-2. 修正

| ファイル | 変更内容 |
|---|---|
| `scripts/webapp/__init__.py` | `portfolio_bp` 登録 (2 行追加) |
| `scripts/webapp/helpers.py` | `list_portfolio_with_indicators(status)` 追加 |
| `scripts/webapp/templates/base.html` | ナビに「保有銘柄」リンク追加 |
| `scripts/webapp/templates/detail.html` | 「3監に追加」ボタン追加 |
| `scripts/webapp/routes/detail.py` | `in_portfolio` / `portfolio_status` を render_template に渡す |

---

## 7. テスト戦略

### 7-1. ユニットテスト (`tests/test_webapp_portfolio_routes.py`)

`tests/test_webapp_routes.py` の fixture パターンを踏襲:
- `tmp_path` で一時 portfolio_shelve を作る
- `monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", tmp_db_path)` で差し替え
- `client.get("/portfolio?status=hold")` の status_code / レスポンス本文確認
- 各 POST ハンドラ:
  - 正常系: 302 リダイレクト + portfolio_shelve に反映
  - 異常系: 不正遷移 / 削除理由空 / 銘柄コード不正

#### 主要テストケース

| ケース | 期待結果 |
|---|---|
| `GET /portfolio?status=hold` で 1保 タブ表示 | 200, 1保 銘柄が一覧に出る |
| `POST /portfolio/add` (新規 code_s) | 302 → status=watch、shelve に追加、初回登録ログ記録 |
| `POST /portfolio/add` (既存 code_s) | 302 → flash 警告、shelve は no-op |
| `POST /portfolio/<c>/transition` (1保→3監) | 302 → ステータス変更ログ |
| `POST /portfolio/<c>/transition` (1保→2準) | 302 → 売却ログ |
| `POST /portfolio/<c>/transition` (1保→1保) | 302 → no-op (transition_status は同一遷移を許容、ログは追記されない) |
| `POST /portfolio/<c>/transition` (許可外遷移) | flash エラー、shelve 変化なし |
| `POST /portfolio/<c>/delete` (3監 + 理由あり) | 302 → 削除ログ、レコード削除 |
| `POST /portfolio/<c>/delete` (1保) | flash エラー (ValueError)、レコード残る |
| `POST /portfolio/<c>/delete` (理由空) | flash エラー (UI 層の必須バリデーション)、レコード残る |
| `POST /portfolio/<c>/delete` (未登録 code_s) | 302 → flash 警告 (delete_record が False 返却)、shelve 変化なし |
| txt 同期発火 | 各 POST 後に my_watch_list.txt が更新される (DATA_DIR は一時 fixture) |

### 7-2. テンプレート rendering テスト

`client.get("/portfolio?status=...")` のレスポンス本文に対して `assert "保有中" in resp.text` 等の表面的検証で十分。E2E は不要 (理由は §7-3)。

### 7-3. Playwright E2E は **本 PR では見送り**

理由:
- 既存リポに `tests/e2e/` ディレクトリがなく、Playwright も導入されていない (調査結果)
- 主要な動作 (ステータス変更・追加・削除) はサーバー側で完結し、Flask テストクライアントで十分カバーできる
- タブ切り替えは単純なリンク遷移で JS 不要
- `<details>` 展開は HTML 標準で JS 不要

E2E 導入が必要になるのは:
- 行クリック展開で AJAX 編集を導入する時
- ダイアログを `<dialog>` / モーダルライブラリで実装し JS 動作検証が必要になる時

これらが発生したら別 issue で導入する。Phase 3b の DoD には E2E を含めない。

### 7-4. CSRF / セキュリティ観点

既存 webapp は CSRF トークン未導入 (search.add_stock_route や memo 系も同様)。Phase 3b でも既存慣習に合わせ CSRF は未導入。本番運用前に webapp 全体で対応する別 issue を立てる方が筋。

ただし削除エンドポイントは特に外部からの誤発火が事故になるため、**Referer ヘッダ確認** または **method 確認** 程度のガードは入れる:

```python
# 例: portfolio.delete の冒頭
if request.referrer is None or "/portfolio" not in request.referrer:
    flash("不正なリクエスト", "error")
    return redirect(url_for("portfolio.dashboard"))
```

ただしこれは半端な対策なので、**実装時に「やる/やらない」を再判断** する。やらないなら本 PR の説明にリスクとして明記。

---

## 8. 実装順序

1. **Blueprint + GET ハンドラ + テンプレ骨格**
   - `routes/portfolio.py` 作成、`portfolio.dashboard` の最小実装 (タブ表示のみ、空テーブル)
   - `templates/portfolio_list.html` 骨格
   - `webapp/__init__.py` 登録、`base.html` ナビ追加
   - `pytest tests/test_webapp_portfolio_routes.py::test_dashboard_shows_tabs` pass

2. **`list_portfolio_with_indicators` ヘルパ**
   - `webapp/helpers.py` に追加、stocks_shelve 補完
   - 一覧テーブル本体 (常時表示列のみ) 実装
   - ユニットテストでヘルパ単体 pass

3. **行クリック展開 (`<details>`)**
   - 業績詳細・手動メモ・操作ボタンを展開部に追加
   - Pico.css の素朴な見栄えで OK

4. **POST: 追加 (`/portfolio/add`)**
   - `portfolio.add` ハンドラ実装
   - `detail.html` に「3監に追加」ボタン追加
   - `detail.py` で `in_portfolio` を渡す
   - テスト 3 ケース pass (正常 / 既存 / 不正コード)

5. **POST: ステータス変更 (`/portfolio/<c>/transition`)**
   - `portfolio.transition` ハンドラ実装
   - 一覧の展開部に変更ダイアログ追加
   - txt 同期発火追加
   - テスト 4 ケース pass

6. **POST: 削除 (`/portfolio/<c>/delete`)**
   - `portfolio.delete` ハンドラ実装
   - 3監 タブのみに削除ボタン表示
   - 理由必須バリデーション
   - テスト 3 ケース pass

7. **既存テスト回帰確認**
   - `pytest tests/ -v -m "not local_db and not live_html"` 全 pass
   - 既存 webapp ルートに影響がないことを確認

8. **動作確認**
   - `cd scripts && python -m webapp.app`
   - `http://localhost:5001/portfolio` で各タブ確認
   - 各操作を実 portfolio_shelve に対して実行 (パイロット用 DB を別途 KS_DATA_DIR 切り替えで用意)

---

## 9. リスク・オープンクエスチョン

### 9-1. 一覧表の情報密度

要件 §5-2 の 6 カテゴリすべてを表示すると 1 行が長くなる。横スクロールを許容する / 重要度の低い列を展開部に逃がす / Pico.css のレスポンシブ機構を使う、いずれの方針も合理的。**実装時に画面を見て判断**。

### 9-2. 「ステータス変更ダイアログ」の実装方式

3 候補:
- (a) HTML5 `<dialog>` + 簡易 JS (推奨)
- (b) サーバー側で別ページに遷移 (`/portfolio/<c>/edit`) → POST → リダイレクト
- (c) HTML5 `prompt()` (理由 1 行のみ。複数行入力はできない)

(a) は JS 必須だが UX 良。(b) は JS 不要だがクリック数増。Phase 3b は (b) で開始し、必要なら (a) に置き換える方針。

### 9-3. txt 同期の失敗時の挙動

`sync_to_my_watch_list_txt()` が IO エラーで失敗した場合、shelve 更新は成功している状態で txt との整合性が崩れる。**ハンドラ側で try/except + flash エラー** で対応。txt 廃止 issue で根本解決。

### 9-4. 銘柄詳細ページから 3監 追加した際の `add_stock` 呼び出し

要件: 銘柄詳細ページに到達した時点で `add_stock` (research_shelve への登録) は既に済んでいる前提。ただし新銘柄を URL 直叩きで `/stock/<未登録 code>` した場合、 `add_stock` が走るのが既存挙動 (search.add_stock_route)。Phase 3b の `/portfolio/add` でも同様に **未登録なら add_stock を先に呼ぶ** か、**登録済みのみ追加可** にするか。後者の方がガードとして堅いが、運用負荷を考えると前者が UX 良。**実装時に既存 `add_stock` の挙動を確認して判断**。

### 9-5. 1保 銘柄の総合 PT 順位が低い場合の表示

要件 §5-2 で「順位」は表示対象。stocks_shelve の `rank` フィールドは update_stock_rank で振られる。保有銘柄が常に上位とは限らないため、「順位 100 位以下です」的な気付きを促す表示にすべきか。本 PR では数値のみ表示し、強調は Phase 4 (赤バッジ強調と一緒に) に送る。

---

## 10. Definition of Done (issue #171)

- [ ] `/portfolio` の GET でタブ切替が機能、各タブで銘柄一覧が正しく表示
- [ ] 銘柄詳細ページから「3監 に追加」 → portfolio_shelve に `初回登録` ログが残る
- [ ] ダッシュボードからステータス変更が動作。1保→2準 が `売却` ログとなる
- [ ] 1保/2準 の銘柄に「削除」ボタンが表示されない、3監 からは削除可能
- [ ] 不正遷移 (例: 1保 → 削除 を直接 POST) が flash エラーで弾かれる
- [ ] `pytest tests/test_webapp_portfolio_routes.py -v` 全 pass
- [ ] `pytest tests/ -v -m "not local_db and not live_html"` 全 pass (既存テスト回帰なし)
- [ ] `python -m webapp.app` で起動し、ローカルブラウザで全シナリオを目視確認

### スコープ外 (本 PR に含まない)

- 警告シグナルの赤バッジ強調表示 → Phase 4
- 振り返りビュー (アクションログ時系列) → Phase 3c (issue #172)
- イベントログ自動記録 → Phase 4
- Playwright E2E → 必要が顕在化したら別 issue
- CSRF 対応 → webapp 全体の別 issue

---

## 11. 開発コマンド

```bash
# テスト
pytest tests/test_webapp_portfolio_routes.py -v
pytest tests/ -v -m "not local_db and not live_html"  # 全体回帰

# ローカル起動 (動作確認)
cd scripts && python -m webapp.app
# → http://localhost:5001/portfolio
```
