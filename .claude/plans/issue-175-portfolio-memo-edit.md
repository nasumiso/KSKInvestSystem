# issue #175 実装計画: portfolio_shelve memo の WebApp 編集機能

> 親 issue: #168 (Phase 3 全体)
> 直前: #171 (Phase 3b 一覧ダッシュボード) - 同じブランチ `issue-171-portfolio-dashboard` 上で続けて実装
> ベースブランチ: `issue-171-portfolio-dashboard` (PR #176 の HEAD `1973f33`)

---

## 1. スコープと前提

### 1-1. 目的
保有銘柄一覧画面 (`/portfolio`) の展開行に memo 編集 form を追加し、スプシ → CSV → migrate スクリプトの片道経路を廃止可能な状態にする。**売買アイデア・IN 理由を「保有一覧を見ながら同時に書き留めたい」性質**のため、銘柄詳細ページではなく一覧の展開行に form を配置する (issue 本文の案1)。

### 1-2. 編集対象 (8 項目すべて = `MEMO_FIELDS` 全要素)
- `gyoutai_theme` (業態・テーマ)
- `watch_in_reason` (IN 理由)
- `trade_idea` (売買アイデア)
- `inago_origin` (イナゴ元)
- `takaichi_sensitivity` (高市感応度)
- `last_research_update` (調査更新日)
- `stage` (ステージ)
- `jukyu_chart` (需給チャート)

### 1-3. スコープ外
- スプシ運用廃止 / `migrate_portfolio_from_csv.py` のアーカイブ判断 (別 issue)
- 銘柄詳細ページからの memo 編集 (案2、本 PR では実装しない)
- フォールバックモード解消 (PR #176 の Codex 残課題、別対応)
- memo 履歴の差分表示 (action_log には種別のみ記録、差分内容は記録しない)

### 1-4. ブランチ戦略
- 新ブランチ作成: `issue-175-portfolio-memo-edit`
- base: `issue-171-portfolio-dashboard` (PR #176 の HEAD)
- PR #176 は base=main で OPEN のままなので、**本 PR は #176 の上にスタック** (#176 がマージされたら GitHub が自動で base を main に切替)
- 別ブランチに切り替える理由: PR #176 のレビュー指摘 2 件 (codex P1/P2) を保留中なので、それと混ぜたくない

---

## 2. バックエンド実装

### 2-1. `scripts/portfolio_shelve.py` の追加

#### 新 action_type 追加
```python
VALID_ACTION_TYPES = frozenset({"初回登録", "ステータス変更", "売却", "削除", "メモ更新"})  # ← "メモ更新" 追加
```

#### 新 API: `update_memo`
```python
def update_memo(
    code_s: str,
    fields: Dict[str, str],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """既存レコードの memo フィールドを **部分更新** する。

    - fields に含まれるキーのみ更新する (受け取らないキーは現行値を保持)
    - fields のキーは MEMO_FIELDS のサブセットでなければ ValueError
    - 値は str のみ受け付ける (None は空文字に正規化)
    - 既存値と完全一致すれば no-op (action_log 追記なし、updated_at 据え置き)
    - 1 つでも変更があれば action_log に "メモ更新" を 1 件追加 (差分内容は記録しない)
    - レコード未登録なら KeyError
    - _flock で排他 (transition_status と同じパターン)

    部分更新セマンティクス (codex P1 指摘対応):
    - クライアント不具合・将来 UI 変更・手動 POST でデータ欠損が起きないよう、
      "未送信フィールド" と "明示的に空文字を送ったフィールド" を区別する
    - 空文字 "" を **明示的に渡した** 場合は「メモ削除」として扱い空文字に上書きする
    - キー自体が **fields に含まれない** 場合は現行値据え置き

    Returns:
        更新後のレコード dict (no-op 時も現行 record を返す)
    """
```

実装ロジック:
1. `validate_code_s(code_s)` → 正規化
2. `fields` のキーが `MEMO_FIELDS` 以外を含むなら `ValueError`
3. `fields` の値が str 以外なら `TypeError` (None は空文字に正規化)
4. `_flock` ブロック内で:
   - `record = db[record_key]` (なければ KeyError)
   - `current_memo = record.get("memo", {})`
   - 差分判定: `fields` の各 key について `current_memo.get(k, "") == fields[k]` がすべて成立すれば no-op で return
   - 変更があれば `record["memo"] = {**current_memo, **fields}` で **部分マージ** (fields に含まれないキーは current_memo の値を保持)
   - `record["updated_at"] = now_iso()`
   - `db[record_key] = record`
5. `_flock` ブロック内で `append_action_log(normalized, "メモ更新")` を呼ぶ
   - reason は空文字、status_from / status_to なし

#### 戻り値: 更新後 record dict
no-op 時は変更前 record (= 現行 record) を返す。呼び出し側で「変更があったか」を知りたいなら `updated_at` の差分で判定可能 (本 PR では不要)。

### 2-2. `scripts/webapp/routes/portfolio.py` の追加

#### 新ハンドラ: `POST /portfolio/<code_s>/memo`
```python
@portfolio_bp.route("/portfolio/<code_s>/memo", methods=["POST"])
def update_memo(code_s: str):
    """memo 8 項目を一括保存する。"""
    rejected = _reject_when_fallback()  # 既存3ハンドラと同じガード
    if rejected is not None:
        return rejected

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))

    fields = _extract_memo_fields_from_form(request.form)  # MEMO_FIELDS のみ抽出
    try:
        ps.update_memo(code_s, fields)
    except KeyError:
        flash(f"{code_s} は portfolio_shelve に未登録です", "error")
        return redirect(url_for("portfolio.dashboard"))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return _redirect_to_current_tab(code_s, fallback_query=DEFAULT_TAB)

    flash(f"{code_s} のメモを保存しました", "info")
    # txt 同期は memo 編集では不要 (txt は code/name/status のみで memo を持たない)
    return _redirect_to_current_tab(code_s, fallback_query=DEFAULT_TAB)
```

#### ヘルパ: `_extract_memo_fields_from_form(form) -> Dict[str, str]`
- **`form` に実際に存在するキーのみを抽出する** (codex P1 指摘対応)
  - `for field in MEMO_FIELDS: if field in form: fields[field] = form[field]`
  - `form.get(field, "")` でデフォルト値を埋めると「未送信フィールドを空文字で上書き」してしまうため不可
- 抽出した値は前後 strip + 改行正規化 (`\r\n` → `\n`、`\r` → `\n`)
- `MEMO_FIELDS` 外のキーは無視 (form に紛れ込んでも reject しない)
- ブラウザの form は 8 項目すべての textarea を持つので通常は 8 キー全部含む。手動 POST で部分送信された場合は送られたキーだけが更新対象になる

### 2-3. `scripts/webapp/helpers.py` の追加 (検討中)

issue 本文には `save_portfolio_memo(code_s, form_data)` ヘルパを置く案があるが、**ハンドラ内で完結する 10 行程度の処理**なので、ヘルパ抽出は overcomplicated。`_extract_memo_fields_from_form` だけ `routes/portfolio.py` 内のモジュール private 関数で十分。

→ **helpers.py には何も追加しない**。issue 本文との差異だが、Karpathy 原則 (Simplicity First) に基づく判断。

---

## 3. UI 実装

### 3-1. `templates/portfolio_list.html` の手動メモセクション置換

#### 現状 (L122-133)
```html
<div style="flex:0 1 auto;min-width:240px;max-width:50em;">
  <strong style="font-size:0.85em;color:#666;">手動メモ</strong>
  {% if row.memo.last_research_update %}
  <span ...>調査更新日: {{ row.memo.last_research_update }}</span>
  {% endif %}
  <ul style="...">
    <li><strong>IN 理由:</strong> {{ row.memo.watch_in_reason or "—" }}</li>
    ...
  </ul>
</div>
```

#### 変更後
```html
<div style="flex:0 1 auto;min-width:300px;max-width:50em;">
  <strong style="font-size:0.85em;color:#666;">手動メモ</strong>
  {% if not fallback_mode %}
  <form action="/portfolio/{{ row.code_s }}/memo" method="POST"
        style="display:flex;flex-direction:column;gap:0.4em;margin:0.3em 0 0 0;font-size:0.85em;">
    {% for field, label in [
        ("gyoutai_theme", "業態・テーマ"),
        ("watch_in_reason", "IN 理由"),
        ("trade_idea", "売買アイデア"),
        ("inago_origin", "イナゴ元"),
        ("takaichi_sensitivity", "高市感応度"),
        ("last_research_update", "調査更新日"),
        ("stage", "ステージ"),
        ("jukyu_chart", "需給チャート"),
    ] %}
    <label style="display:flex;flex-direction:column;gap:0.1em;margin:0;">
      <span style="color:#666;">{{ label }}</span>
      <textarea name="{{ field }}" rows="2"
                style="font-size:0.85em;padding:0.3em;margin:0;font-family:inherit;"
                >{{ row.memo[field] or "" }}</textarea>
    </label>
    {% endfor %}
    <button type="submit"
            style="padding:0.3em 0.6em;font-size:0.85em;margin:0.2em 0 0 0;align-self:flex-start;">
      メモを保存
    </button>
  </form>
  {% else %}
  <!-- フォールバックモード時は読み取り専用表示を維持 -->
  <ul style="font-size:0.85em;margin:0.3em 0 0 1em;padding:0;">
    <li><strong>IN 理由:</strong> {{ row.memo.watch_in_reason or "—" }}</li>
    <li><strong>売買アイデア:</strong> {{ row.memo.trade_idea or "—" }}</li>
    <li><strong>イナゴ元:</strong> {{ row.memo.inago_origin or "—" }}</li>
    <li><strong>高市感応度:</strong> {{ row.memo.takaichi_sensitivity or "—" }}</li>
    <li><strong>需給チャート:</strong> {{ row.memo.jukyu_chart or "—" }}</li>
  </ul>
  {% endif %}
</div>
```

#### ポイント
- **8 項目すべて textarea (`rows="2"`) で統一** — `stage` `last_research_update` のような短文も textarea で揃える
- **1 列縦並び** — 既存の操作セクションと並ぶレイアウトを保つ
- **`row.memo[field]` で値を取得** — 現状 `row.memo.gyoutai_theme` のような attribute 形式でアクセスしているが、Jinja の dict は `[]` でも `.` でもアクセス可能なので問題なし
- **フォールバックモード時は form を出さず、現行の読み取り専用 ul 表示を維持** — `_reject_when_fallback` で reject するので form を出してもエラーで戻されるが、UX 上「保存ボタンが押せるけど押すと失敗する」のは混乱を招くため
- **L84-88 の常時表示列の業態・テーマ表示は変更不要** (textarea ではなく `<td>` での 2 行表示。既存ロジック維持)

### 3-2. テンプレート Jinja 構造への注意

`{% for field, label in [...] %}` のリストリテラルは Jinja2 で動作する (Jinja2 はタプル展開対応)。
動作確認できなければ事前に `tuple` を辞書化 + ordered iteration で書き直す。

---

## 4. テスト

### 4-1. `tests/test_portfolio_shelve.py` 追加 (`TestUpdateMemo` クラス)

| ケース | 期待結果 |
|---|---|
| `update_memo("4377", {"trade_idea": "上値追い"})` (1 項目のみ送信) | trade_idea のみ更新、他の 7 項目は据え置き、action_log "メモ更新" 1 件、updated_at 更新 |
| 既存値と完全一致で更新 (1 項目) | no-op、action_log 追記なし、updated_at 据え置き |
| 8 項目を一括更新 | 全フィールド反映、action_log 1 件 |
| **空 dict `{}` で呼び出し** | no-op、KeyError なし (差分なしと同等) |
| **明示的に空文字 `{"trade_idea": ""}` を送信** (現行値が非空) | trade_idea が "" に上書きされる (= メモ削除扱い)、action_log 1 件 |
| **未送信フィールドが据え置きされる** (`{"trade_idea": "X"}` だけ送って残り 7 項目に既存値あり) | trade_idea のみ更新、残り 7 項目の既存値は変化なし |
| `MEMO_FIELDS` 外のキー (`{"unknown_field": "x"}`) | ValueError |
| 値が str 以外 (`{"trade_idea": 123}`) | TypeError |
| 値が None (`{"trade_idea": None}`) | "" に正規化されて保存される (空文字としての更新扱い) |
| レコード未登録 (`update_memo("9999", {"trade_idea": "X"})`) | KeyError |
| 不正な code_s (`update_memo("abc", {...})`) | ValueError |
| 排他確認: 並行 update_memo を 2 スレッドで叩いて両方成功 | action_log seq が 2 件、最終値が一方の上書き勝ち (data race なし) |

### 4-2. `tests/test_webapp_portfolio_routes.py` 追加 (`TestUpdateMemoRoute` クラス)

| ケース | 期待結果 |
|---|---|
| `POST /portfolio/4377/memo` (form: 全 8 項目) | 302 リダイレクト → 銘柄の現在ステータスのタブ、shelve 反映、action_log 1 件 |
| **`POST /portfolio/4377/memo` (form: 3 項目のみ)** | **送られた 3 項目のみ更新、残り 5 項目は据え置き** (codex P1 対応)、action_log 1 件 |
| `POST /portfolio/4377/memo` (form: 全 8 項目すべて空文字) | 全項目が "" で上書きされる (= 全メモ削除)、現行値が非空なら action_log 1 件 |
| `POST /portfolio/4377/memo` (差分なし: 全項目を現行値そのまま送る) | 302 → flash "保存しました" は出るが action_log 追記なし |
| `POST /portfolio/9999/memo` (未登録) | flash "未登録"、リダイレクト |
| `POST /portfolio/abc/memo` (不正コード) | flash エラー、リダイレクト |
| フォールバックモード時の POST | flash "未移行モード" + redirect (既存 `_reject_when_fallback` の挙動と整合) |

#### 「部分送信」と「空文字送信」の区別
- **キー自体が form に含まれない** (= 手動 POST で省略された) → 該当フィールドは現行値据え置き
- **キーは含まれるが値が ""** (= textarea を空にして送信) → 該当フィールドは "" に上書き (メモ削除の意図)

ブラウザ form は常に 8 項目すべての textarea を持つので、通常運用では「キー自体が含まれない」ケースは発生しない。手動 POST や将来の AJAX 部分編集で安全に動くよう、サーバ側で区別する。

### 4-3. 既存テストの回帰確認

```bash
.venv/bin/pytest tests/ -v -m "not local_db and not live_html"
```

現行 PR #176 で 188 件 + Phase 3b 分が追加された状態。本 PR で増えるのは `TestUpdateMemo` (約 10 件) + `TestUpdateMemoRoute` (約 6 件) = 16 件程度。

---

## 5. ファイル構成

### 5-1. 修正
| ファイル | 変更内容 | 推定行数 |
|---|---|---|
| `scripts/portfolio_shelve.py` | `VALID_ACTION_TYPES` に "メモ更新" 追加 (1 行) + `update_memo()` 新規 (約 50 行) | +51 |
| `scripts/webapp/routes/portfolio.py` | `update_memo` ハンドラ + `_extract_memo_fields_from_form` ヘルパ (約 40 行) | +40 |
| `scripts/webapp/templates/portfolio_list.html` | 手動メモ ul を form に置換 + フォールバック分岐維持 (約 30 行差し替え) | +25 |

### 5-2. 新規
なし (既存ファイルへの追記で完結)

### 5-3. テスト
| ファイル | 追加内容 |
|---|---|
| `tests/test_portfolio_shelve.py` | `TestUpdateMemo` クラス (約 10 ケース、約 120 行) |
| `tests/test_webapp_portfolio_routes.py` | `TestUpdateMemoRoute` クラス (約 6 ケース、約 80 行) |

---

## 6. 実装順序

1. **`portfolio_shelve.update_memo` 実装 + ユニットテスト**
   - `VALID_ACTION_TYPES` 拡張
   - `update_memo()` 実装
   - `tests/test_portfolio_shelve.py::TestUpdateMemo` 全ケース pass

2. **`webapp/routes/portfolio.py` ハンドラ追加 + ルートテスト**
   - `update_memo` ハンドラ + ヘルパ
   - `tests/test_webapp_portfolio_routes.py::TestUpdateMemoRoute` 全ケース pass

3. **テンプレート差し替え**
   - 手動メモ ul → form 置換
   - フォールバック時の読み取り専用表示を維持
   - ブラウザ動作確認 (port 5002)

4. **既存テスト回帰確認**
   - `.venv/bin/pytest tests/ -v -m "not local_db and not live_html"` 全 pass

5. **動作確認**
   - `cd scripts && python -m webapp.app`
   - `/portfolio` の 3 タブで memo 編集 → 保存 → リロードで反映確認
   - 同一値を保存して action_log に追記されないこと確認 (簡易な shelve 直読みで OK)

---

## 7. リスク・オープンクエスチョン

### 7-1. textarea の改行ハンドリング

- `request.form.get("trade_idea")` は textarea の改行 (`\r\n`) を保持する
- 既存テンプレ L87 で `theme_line in row.memo.gyoutai_theme.split("\n")[:2]` のように `\n` で分割しているため、ブラウザが `\r\n` で送ってくると `\r` が混入する
- 対策: `update_memo` 内で改行を `\n` に正規化 (`value.replace("\r\n", "\n").replace("\r", "\n")`) するか、ハンドラ側で正規化する

→ **方針**: ハンドラ側 (`_extract_memo_fields_from_form`) で改行正規化 + 前後 strip。

### 7-2. flash メッセージの粒度

「変更がない場合 (no-op) でも保存メッセージを出す」のは UX 的に微妙。

→ **方針**: 簡素化のため「保存しました」は no-op でも出す。差分検出して「変更ありませんでした」と出し分けるのは overcomplicated。

### 7-3. 排他制御と action_log の整合性

`update_memo` の内部で `_flock` を取り、その中で `db[key] = record` した後に `append_action_log` を呼ぶ。`append_action_log` も `_flock` を取るが、`_flock` は `_flock_holder.depth` で再入対応済み (L227)。

→ 既存 `transition_status` と同じパターンなので問題なし。

### 7-4. 「未送信」と「空文字送信」の区別 (codex P1 対応)

部分更新方式を採用するため、サーバ側で 2 つの状態を区別する:

| 入力 | 動作 |
|---|---|
| キー自体が `fields` dict (= form) に含まれない | 該当フィールドは現行値据え置き |
| キーは含まれるが値が空文字 `""` | 該当フィールドは "" に上書き (メモ削除の意図) |
| 値が None | "" に正規化 (空文字送信と同じ扱い) |

ブラウザの form は常に 8 項目すべての textarea を含むので、通常運用では「未送信」は発生しない。手動 POST や将来の AJAX 部分編集で安全に動くよう、`_extract_memo_fields_from_form` は `form.get(field, "")` ではなく `form[field] if field in form else SKIP` のロジックで抽出する。

### 7-5. CSRF トークン未導入

既存 webapp ルートは CSRF 未対応 (`portfolio.transition` `portfolio.delete` も同様)。memo 編集だけ CSRF を入れるのは不整合。

→ **方針**: 既存慣習に合わせ CSRF 未対応。webapp 全体での導入は別 issue。

---

## 8. Definition of Done (issue #175)

- [ ] `MEMO_FIELDS` の 8 項目すべてが `/portfolio` 一覧画面の展開行から編集・保存できる
- [ ] 保存後、shelve に反映され、action_log に "メモ更新" が記録される
- [ ] 既存値と完全一致で保存した場合は action_log 追記なし
- [ ] 一覧画面のレイアウトを大きく崩さない (常時表示列・操作セクションは変更なし)
- [ ] フォールバックモード時は form を出さず、現行の読み取り専用表示を維持
- [ ] **部分送信時に未送信フィールドが現行値据え置きになる** (codex P1 対応)
- [ ] `pytest tests/test_portfolio_shelve.py tests/test_webapp_portfolio_routes.py -v` 全 pass
- [ ] `pytest tests/ -v -m "not local_db and not live_html"` 全 pass (回帰なし)
- [ ] ブラウザ目視確認: memo 編集 → 保存 → リロードで反映、改行入力が `\n` で保存される

---

## 9. 開発コマンド

```bash
# テスト
.venv/bin/pytest tests/test_portfolio_shelve.py::TestUpdateMemo -v
.venv/bin/pytest tests/test_webapp_portfolio_routes.py::TestUpdateMemoRoute -v
.venv/bin/pytest tests/ -v -m "not local_db and not live_html"  # 回帰

# ローカル起動 (動作確認)
cd scripts && python -m webapp.app
# → http://localhost:5001/portfolio
```
