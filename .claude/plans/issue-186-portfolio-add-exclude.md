# issue: 保有銘柄ダッシュボードに「銘柄追加」「ユニバース除外 (=ダッシュボード非表示)」機能を追加

## Context

`/portfolio?status=watch` (3監タブ) は新高値銘柄の研究待機リストとして使われているが、
- 新規候補をダッシュボードから追加する UI が無い (ハンドラ `POST /portfolio/add` だけ存在しフォーム未実装)
- 不要になった銘柄をユニバースから外す手段が無い (`delete_record` は物理削除しか提供しておらず、メモ・履歴ごと消える)

研究履歴とメモを残しつつ「もう追跡しない」状態にする運用が必要。
追加 UI と「ユニバース除外」機能をセットで実装し、3監タブの管理を完結させる。

## 確定要件

- **削除 = 物理削除ではなくユニバース除外** (PortfolioRecord に `excluded:bool` フラグ追加、DB レコード・ログ・メモはすべて保持)
- **対象タブ**: 3監のみ
- **可視性**: 除外済みは `/portfolio` 全タブで完全非表示
- **action_type**: `"ユニバース除外"` を新設 (`VALID_ACTION_TYPES`)
- **復活 UI**: 既存追加フォームを再利用 (除外済みコードを再入力すると `excluded=False` に戻す)。除外専用ページは作らない
- **削除 UI**: 「削除モード」トグル → 行頭チェックボックス → 一括「削除」ボタン (= ユニバース除外)。理由は任意
- **追加 UI**: 3監タブ上部のインラインフォーム (コード入力 + 追加ボタン)

## Critical Files

- `scripts/portfolio_shelve.py` (主に修正)
- `scripts/webapp/routes/portfolio.py`
- `scripts/webapp/templates/portfolio_list.html`
- `tests/test_portfolio_shelve.py`
- `tests/test_webapp_portfolio_routes.py`

## 実装内容

### 1. `scripts/portfolio_shelve.py`

#### 1-1. `VALID_ACTION_TYPES` に追加
```python
VALID_ACTION_TYPES = {"初回登録", "ステータス変更", "売却", "削除", "メモ更新", "ユニバース除外"}
```

#### 1-2. `PortfolioRecord` に `excluded` フィールド追加
- 既存スキーマに `excluded: bool = False` を加える
- `_load_record` 側で旧データ (excluded キーなし) は `False` フォールバック (後方互換)
- save 時は新キー込みで書き出し

#### 1-3. 新関数 `exclude_from_universe(code_s, *, reason="", db_path=None) -> bool`
- 3監のレコードを `excluded=True` に更新
- 既に `excluded=True` なら `False` 返す (no-op)
- 1保/2準なら `ValueError("3監のみ除外可能")`
- 未登録なら `False`
- アクションログ `"ユニバース除外"` を追記
- `_flock` 排他

#### 1-4. `add_to_watch` を「除外解除」も兼ねるよう拡張
- 既存レコードあり & `excluded=True` → `excluded=False` に戻す。アクションログは既存の `"ユニバース除外"` action_type を流用し `reason="復活"` を記録 (現行スキーマは `reason` フィールドのみで `note` は無いため)
- 既存レコードあり & `excluded=False` → 従来通り `ValueError`
- 既存レコードなし → 従来通り新規登録

注: `add_to_watch` のシグネチャ・呼び出し側は変更しない。挙動変更だけ。docstring を更新。

#### 1-5. `list_records` の挙動拡張 (除外フィルタ)
**codex 指摘 #2 反映**: `list_records()` をそのまま使うと `sync_to_my_watch_list_txt()` が除外済みも txt に書き戻し、txt フォールバック経由で復活してしまう。
- `list_records(*, include_excluded: bool = False, db_path=None)` の形で kwarg を追加 (デフォルト False)
- 既存呼び出し箇所 (routes/portfolio.py の `ps.list_records()`、`sync_to_my_watch_list_txt()`、`portfolio.parse_my_portforio()` 等) は **すべてデフォルト=除外済を返さない** で動作。すなわち全タブから自動的に隠れる
- 「除外済み一覧を見たい」用途 (今回スコープ外) のみ `include_excluded=True` を付けて取得
- 影響範囲確認: `grep -rn "list_records(" scripts/` で呼出箇所をすべて見て、対応不要なら kwarg だけ追加するシンプル変更にとどめる

### 2. `scripts/webapp/routes/portfolio.py`

#### 2-1. dashboard() の取得方針変更 + 一覧表示で除外フィルタ
**codex 指摘 #3, #5 反映**: 現行 `dashboard()` は `all_records = ps.list_records()` で 1 度だけ取得し、`fallback_mode = not all_records` 判定 + 件数カウント + 表示行抽出すべてに使い回している。`list_records()` のデフォルトを `include_excluded=False` にすると、全件除外時に fallback モード誤判定 → 追加 UI まで消えて復活不能のデッドロック。

**対策** (dashboard() の取得を 2 段階化):
```python
# fallback 判定とタブ件数カウントは「除外含む全件」で行う
all_records_inc = ps.list_records(include_excluded=True)
fallback_mode = not all_records_inc

# 表示用は除外フィルタ後
visible_records = [r for r in all_records_inc if not r.get("excluded", False)]
counts = ...  # visible_records ベース (除外済みは件数にも出さない)
active_records = [r for r in visible_records if r.get("status") == active_status]
```
- カウント (タブのバッジ) は除外済みを含めない (= visible_records ベース)。除外済みはユーザから「いない」扱いなので件数 0 のタブは fallback ではなく単に空表示
- fallback 判定だけは「DB 自体が空 (= 未移行)」を見るので除外含む全件で

**同時に修正する場所**:
- `_is_fallback_mode()` (もし別途存在するなら) も `include_excluded=True` 基準に統一
- `_build_fallback_records()` 経路に変更不要 (fallback 時は txt から仮レコード組み立て、excluded 概念なし)

#### 2-2. 既存 `POST /portfolio/<code_s>/delete` (line 121) は **撤去**
**codex 指摘 #1 反映**: 物理削除エンドポイントを残すと「削除=ユニバース除外、メモ・履歴は保持」要件と矛盾し、誤 POST で履歴が消える事故リスクがある。
- ルートハンドラと関連 import を削除 (UI からの導線も撤去するためルートが死ぬ)
- 既存テスト `test_delete_*` は移行先 (TestBulkExclude) に置き換え or 削除
- `portfolio_shelve.delete_record()` 関数自体は残してよい (内部 API、外部公開なし)。今回の plan では呼び出し元がいなくなるが、移行スクリプト等で将来必要になる可能性があるため触らない (Surgical Changes)

#### 2-3. 新ルート `POST /portfolio/bulk-exclude`
```python
@portfolio_bp.route("/portfolio/bulk-exclude", methods=["POST"])
def bulk_exclude():
    """3監 銘柄をまとめてユニバースから除外する。"""
    # フォールバックモード reject (_reject_when_fallback)
    # codes = request.form.getlist("codes")
    # reason = (request.form.get("reason") or "").strip()
    # 各 code に対し:
    #   validate_code_s → exclude_from_universe(code, reason=reason, db_path=...)
    #   ValueError (1保/2準) → 失敗リスト
    #   False (未登録 or 既除外) → スキップ
    #   True → 成功
    # _sync_txt_safely() を 1 回呼ぶ
    # flash: "N件をユニバースから除外しました" / 失敗あれば error
    # redirect → /portfolio?status=watch
```

#### 2-4. 既存 `POST /portfolio/add` (line 289) を「復活」も担うよう調整
- `add_to_watch` 側で除外済みレコードを復活させる挙動になるので、`POST /portfolio/add` の例外処理だけ調整
- 既存の `ValueError` (= 既登録 & 非除外) はそのまま flash error
- 復活時は flash info `"ユニバースに復活しました: {code}"`
- AJAX レスポンスは既存 add 同様 (今回 AJAX 化必須ではない)

**stocks_shelve 未登録ガードの調整 (codex 指摘 #4 反映)**:
現実装の `POST /portfolio/add` (line 314 付近) は `get_stock_data(normalized)` で stocks_shelve に登録があるか事前チェックしており、無ければ reject。これだと「除外済みレコードが portfolio_shelve にあるが stocks_shelve に無い」ケースで復活できない。
対策:
1. ルートの先に `ps.get_record(normalized)` で portfolio_shelve に既存レコードがあるか確認
2. 既存 (excluded 問わず) なら **stocks_shelve チェックをスキップ** して `add_to_watch` を呼ぶ (復活パス)
3. 既存無しなら従来通り stocks_shelve チェック → 無ければ reject (新規追加で未知コードは弾く)

擬似コード:
```python
existing = ps.get_record(normalized)
if existing is None:
    if not get_stock_data(normalized):
        flash("stocks_shelve に未登録のコードです", "error")
        return redirect(...)
# 既存 (除外済 含む) なら stocks_shelve チェックをスキップして add_to_watch
ps.add_to_watch(normalized, ...)
```

### 3. `scripts/webapp/templates/portfolio_list.html`

#### 3-1. 3監タブ上部 (active_query == "watch" かつ not fallback_mode のみレンダリング)

```html
<form action="/portfolio/add" method="POST" class="portfolio-add-form">
  <label>コード <input name="code_s" required pattern="[0-9A-Za-z]+"></label>
  <button type="submit">追加</button>
  <span class="hint">(除外済みコードを入れると復活します)</span>
</form>

<button type="button" id="toggle-delete-mode">削除モード</button>

<div id="bulk-delete-bar" hidden>
  <span><strong id="selected-count">0</strong> 件選択中</span>
  <button type="button" id="bulk-delete-execute">削除 (ユニバース除外)</button>
  <button type="button" id="bulk-delete-cancel">キャンセル</button>
</div>
```

#### 3-2. チェックボックス列
- `<thead><tr>` 先頭に `<th class="bulk-col">` (CSS で初期 hidden)
- `<tbody><tr>` 先頭に `<td class="bulk-col"><input type="checkbox" class="bulk-cb" value="{{ row.code_s }}"></td>`
- CSS: `body.delete-mode .bulk-col { display: table-cell; }` / 既定 `.bulk-col { display: none; }`

#### 3-3. 既存「行ごと削除フォーム」(line 200-208) を撤去
削除モードに統合する。

#### 3-4. JS 追加 (既存 IIFE の隣に新規 IIFE)
```js
(function() {
  const toggle = document.getElementById('toggle-delete-mode');
  if (!toggle) return;  // 3監タブ以外は何もしない
  const bar = document.getElementById('bulk-delete-bar');
  const countEl = document.getElementById('selected-count');
  const execBtn = document.getElementById('bulk-delete-execute');
  const cancelBtn = document.getElementById('bulk-delete-cancel');

  function refresh() {
    const checked = document.querySelectorAll('.bulk-cb:checked');
    countEl.textContent = checked.length;
    bar.hidden = !document.body.classList.contains('delete-mode');
  }
  toggle.addEventListener('click', () => {
    document.body.classList.toggle('delete-mode');
    if (!document.body.classList.contains('delete-mode')) {
      document.querySelectorAll('.bulk-cb').forEach(c => c.checked = false);
    }
    refresh();
  });
  cancelBtn.addEventListener('click', () => {
    document.body.classList.remove('delete-mode');
    document.querySelectorAll('.bulk-cb').forEach(c => c.checked = false);
    refresh();
  });
  document.addEventListener('change', e => {
    if (e.target.classList.contains('bulk-cb')) refresh();
  });
  execBtn.addEventListener('click', () => {
    const codes = [...document.querySelectorAll('.bulk-cb:checked')].map(c => c.value);
    if (!codes.length) return;
    if (!confirm(codes.length + ' 件をユニバースから除外します。よろしいですか?')) return;
    const fd = new FormData();
    codes.forEach(c => fd.append('codes', c));
    fetch('/portfolio/bulk-exclude', { method: 'POST', body: fd })
      .then(r => { if (r.redirected) location.href = r.url; else location.reload(); });
  });
})();
```

### 4. テスト追加

#### 4-1. `tests/test_portfolio_shelve.py`
- `TestExcludeFromUniverse`:
  - 3監を除外 → record.excluded=True / ログ "ユニバース除外" / 戻り値 True
  - 既に除外済 → False
  - 1保 → ValueError
  - 未登録 → False
  - reason 空 OK
- `TestAddToWatchRevive`:
  - 除外済みコードを add_to_watch → excluded=False / ログ "ユニバース除外" reason="復活"
  - 通常の既登録 (excluded=False) → ValueError (従来通り)
- `TestPortfolioRecordBackwardCompat`:
  - 旧形式 (excluded キーなし) を load → excluded=False で読める
- `TestListRecordsFilterExcluded`:
  - 3監 2 件のうち 1 件を除外 → `list_records()` は 1 件のみ返す
  - `list_records(include_excluded=True)` は 2 件返す
- `TestSyncTxtSkipsExcluded`:
  - 除外済みは `sync_to_my_watch_list_txt` の出力に含まれない

#### 4-2. `tests/test_webapp_portfolio_routes.py`
- `TestBulkExclude`:
  - 3監 2 件を bulk-exclude → 一覧から消える、shelve 上は excluded=True で残存
  - 1保混入 → 1保はそのまま、3監のみ除外 + flash error
  - 空 codes → flash error
  - 未登録コード → スキップ + flash
  - フォールバックモード → reject
- `TestAddRevival`:
  - 除外済みコードで `POST /portfolio/add` → 復活、一覧再表示、flash info
  - 除外済みかつ stocks_shelve 未登録コードでも復活成功 (stocks_shelve チェックスキップ)
- `TestExcludedHidden`:
  - excluded=True レコードは `/portfolio?status=watch` に出ない
- `TestFallbackJudgmentWithAllExcluded`:
  - 全レコード excluded=True 状態でも fallback モードと誤判定されない (`_is_fallback_mode()` が False を返す)
  - そのとき `/portfolio?status=watch` 画面で追加フォームが表示される (テンプレート上の `not fallback_mode` ガードを通過する)
  - そのとき `POST /portfolio/add` で復活が許可される

### 5. 検証手順 (Playwright E2E)

1. `/portfolio?status=watch` 開く → 追加フォーム / 削除モード ボタンあり、1保/2準タブには無いこと
2. 追加フォームに既存コードでないコードを入力 → 追加成功
3. 「削除モード」 → チェックボックス列出現、画面下バー表示
4. 2 件チェック → 「2 件選択中」
5. 削除実行 → confirm OK → リロード後該当行が消える、shelve 上は残っている (DB 直読で excluded=True 確認)
6. 同じコードを追加フォームで再投入 → 復活して再表示、ログに "ユニバース除外 (note=復活)" 残存
7. shelve に研究メモが残っていることを確認 (excluded → 復活でも MEMO_FIELDS は保持)

### 6. テスト実行コマンド (testing.md マッピング準拠)

```bash
pytest tests/test_portfolio_shelve.py tests/test_append_research_snapshots.py -v
pytest tests/test_webapp_portfolio_routes.py -v
```

## 設計上の注意

- **後方互換**: shelve スキーマに excluded を追加するため、`_load_record` で旧データを読めること必須
- **研究メモ保護**: 除外しても MEMO_FIELDS とログは残す。物理削除との明確な差別化
- **add_to_watch の挙動拡張**: 「既登録ならエラー」だった仕様が「除外済みなら復活」と分岐するため、関数 docstring を更新
- **bulk_exclude の部分成功**: 1保混入時は 3監のみ処理し flash で報告 (PR で受け取り側に明示)

## 実装後タスク (要件追加分)

実装完了後、運用 DB と `my_watch_list.txt` の整合性を 1 度だけ手動同期する。
これは UI 機能ではなく一回限りの保守作業。

### 同期スクリプト (新設 or 既存 migrate スクリプトを再利用)

**方針**:
- 入力: `${KS_DATA_DIR}/my_watch_list.txt` (現行運用の真実源)
- 出力: portfolio_shelve

**処理**:
1. txt をパース (1保 / 3監 を抽出、`migrate_my_watch_list_to_shelve.py` 既存ロジック再利用)
2. portfolio_shelve から `list_records(include_excluded=True)` で全件取得
3. 差分抽出:
   - **txt にあるが DB に無い**: `add_to_watch(code, reason="my_watch_list 同期で追加")` (1保なら add してから transition で 1保 へ)
   - **DB にあるが txt に無い**: `exclude_from_universe(code, reason="my_watch_list 同期で除外")` (3監のみ対象。1保が txt 不在になっているケースは異常として ERROR ログ + skip)
4. dry-run モード必須 (差分プレビュー → ユーザ確認 → 実行)
5. ログに追加/除外件数を出力

**配置**: `scripts/sync_portfolio_with_txt.py` (新設) もしくは `migrate_my_watch_list_to_shelve.py` に `--sync-mode` オプション追加。既存スクリプトを汚さないため新設推奨。

**テスト**: 別タスク扱い。本 plan の主要実装が終わってから着手。

## 未確定事項

なし。要件確定済み。

## Plan Review

実装着手前に `codex` でレビュー必須 (`.claude/rules/codex-plan-review.md`)。
コマンド:
```
codex exec -m gpt-5.3-codex "Review this plan. Don't nitpick trivial things. Only point out critical issues: /Users/k_sohara/.claude/plans/enumerated-puzzling-patterson.md (ref: /Users/k_sohara/Library/CloudStorage/Dropbox/document/shintakane/CLAUDE.md)"
```
