# プラン: ポートフォリオ — 業態・テーマのマスター管理と編集画面

## 目的

現在、業態・テーマは銘柄の `memo["gyoutai_themes"]` (list[str]) に文字列として直接保存され、入力欄の datalist 候補は「全銘柄に実際に入力されている値を集計」して生成している。マスターテーブルが無いため:

- 同義語・誤字が混在しやすい
- テーマの一括リネーム / 削除ができない
- テーマに説明文を持たせられない

GitHub Labels に近い体験で、テーマをマスター管理しつつ、編集画面 (`/portfolio/themes`) から追加・編集・削除できるようにする。

## スコープ

1. portfolio_shelve に「テーママスター」キー名前空間を追加
2. `/portfolio/themes` 編集画面 (Flask ルート + テンプレート) を新規追加
3. ポートフォリオ画面ヘッダのテーマフィルタ select の横に「✏️ テーマを編集」ボタンを追加
4. 銘柄行のテーマ入力 UI を datalist 入力欄から `<select>` 2 個に置き換え
   - マスター登録済みテーマのみ選択可
   - 先頭に `— (未設定)` 項目を置き、それを選ぶと当該スロットがクリア
   - **テーマ名そのもの (文字列) の編集機能は廃止** — 銘柄行で行えるのは「選ぶ / 外す」のみ。テーマ名のリネームはマスター編集画面 (`/portfolio/themes`) でのみ可能。これにより「銘柄から書き換え」と「マスター編集」の二重導線による曖昧さを排除する
   - 既存の Ctrl/Cmd+クリック → テーマフィルタ機能は select 上でも維持
5. 既存データ → マスター移行スクリプト (`scripts/migrate_themes_to_master.py`) を 1 本作成、1 回だけ実行

スロット上限 `GYOUTAI_THEMES_MAX_SLOTS = 2` は維持する。

## 非スコープ

- テーマに「色」を持たせる
- 「お気に入りテーマ」のピン留め
- マスター未登録テーマの自由入力 (登録済みのみ選択可)
- 複数 PR / Phase 分割 (1 issue / 1 PR で完結)

---

## データモデル

### 新規キー名前空間 (portfolio_shelve)

```
theme:<name>     -> {"name": str, "description": str, "created_at": str(iso)}
```

- name は重複不可 (キー自体がユニーク制約)
- description は説明文 (空文字許容)
- created_at は ISO 形式 (`now_iso()`)
- 既存の `record:` / `action_log:` / `_seq:` とは完全分離

#### name の文字種制約 (URL 安全のため)

ルートで `<name>` をパスパラメータとして使うため、URL に含めると曖昧になる文字を name に許可しない:

- 禁止: `/` `?` `#` `&` `%` `+` 制御文字 (`\x00-\x1F`)
- 許可: 日本語・英数字・空白・ハイフン・アンダースコア・中点・記号など上記禁止文字以外

`create_theme` / `update_theme` のリネーム時に正規表現でバリデーションして ValueError。これにより `AI/半導体` のような name が作れず、URL `/portfolio/themes/<name>/delete` でルーティングが破綻しない。

### 既存スキーマ (変更なし)

`memo["gyoutai_themes"]: list[str]` はそのまま。マスター登録済み name と照合して整合性を保つ。

### portfolio_shelve.py 追加定数

```python
KEY_THEME_PREFIX = "theme:"
THEME_FIELDS = frozenset({"name", "description", "created_at"})
THEME_NAME_MAX_LEN = 30  # UI バッジを崩さない上限
```

---

## API 追加 (portfolio_shelve.py)

すべて既存の `_flock` + `ShelveDB` パターンに従う。

### `list_themes(*, db_path=None) -> List[Dict[str, Any]]`

- 全 `theme:*` キーを読み、name 昇順でリストを返す
- 各要素は `{"name", "description", "created_at"}`

### `create_theme(name: str, description: str = "", *, db_path=None) -> Dict[str, Any]`

- name バリデーション:
  - `isinstance(name, str)` でない → TypeError
  - `name.strip() == ""` → ValueError
  - `len(name) > THEME_NAME_MAX_LEN` → ValueError
- description は `str` のみ (None は `""` に正規化)
- 既存キー `theme:<name>` あり → ValueError (重複)
- 成功時は作成済みレコードを返す

### `update_theme(name: str, new_name: str | None = None, description: str | None = None, *, db_path=None) -> Dict[str, Any]`

- 既存キーが無ければ KeyError
- `new_name` が指定され、かつ現行と異なれば **リネーム**:
  1. `theme:<new_name>` が既存なら ValueError (重複)
  2. 旧キー削除 + 新キー作成 (created_at は維持)
  3. **全 record:* を走査し、memo["gyoutai_themes"] 内の旧 name を new_name に書き換え**
     - 変更があった record は updated_at 更新 + action_log "メモ更新" 追加
- `description` が指定されていればその値で上書き
- 戻り値は更新後の theme レコード

### `delete_theme(name: str, *, db_path=None) -> int`

- 既存キーが無ければ KeyError
- `theme:<name>` を削除
- **全 record:* を走査し、memo["gyoutai_themes"] から name を除去**
  - 変更があった record は updated_at 更新 + action_log "メモ更新" 追加
- 戻り値: 影響を受けた銘柄数

### `count_theme_usage(*, db_path=None) -> Dict[str, int]`

- 全 record:* を 1 回スキャンし、各 theme name → 使用銘柄数を返す
- 編集画面表示用

### 並行性・原子性

- `update_theme` のリネームと `delete_theme` の銘柄側除去は **1 つの `_flock` + 1 つの ShelveDB セッション** 内で実行する (途中失敗で不整合が残らないようにする)
- 影響を受けた record の updated_at 更新と action_log 追記は同 flock 内で完了させる

### 既存 `update_memo` の変更

`gyoutai_themes` の値の扱いは「**マスター未登録値を保持したまま保存を許可する (ただし新規追加は不可)**」とする:

- POST されたリストの各値について:
  - 空文字なら除去 (スロットクリア)
  - マスター登録済み name ならそのまま採用
  - マスター未登録 name で **かつ現行レコードの memo[gyoutai_themes] にも入っていない** → ValueError (UI を経由した新規付与は登録済みのみ)
  - マスター未登録 name で **かつ現行レコードに既に入っている** → 移行漏れの既存値なので保持を許可

これにより以下のシナリオが破綻しない:

- 移行漏れで未登録 name が残っている銘柄で、もう片方のスロットだけ別テーマに変更したい → 未登録値はそのまま、もう片方は登録済み name に更新できる
- 未登録 name を select で「— (未設定)」にすれば外せる (空文字扱いなので除去される)
- routes/portfolio.py のフォームハンドラ側は現行レコードを load してから上記判定を行う

---

## 移行スクリプト (`scripts/migrate_themes_to_master.py`)

- 全 record:* の memo["gyoutai_themes"] を走査し、ユニーク化
- 各 name について:
  - 既にマスター登録済みならスキップ
  - 未登録なら `create_theme(name, description="")` を呼ぶ
- 実行サマリーを log_print (新規登録件数、既存件数、影響銘柄数)
- 冪等 (再実行しても重複作成しない)
- `--dry-run` フラグで集計のみ
- name バリデーション (URL 安全制約) で弾かれる既存値があれば、件数と code_s を一覧して **失敗終了**。ユーザーに手動でリネーム判断させる
- **旧 `memo["gyoutai_theme"]` (str 単数フィールド、issue #187 移行期間用) の残存検知**:
  - 旧フィールドに非空値が残っている record があれば件数と code_s を一覧
  - デフォルトは **失敗終了** (新フィールドへの移行を先に済ませる必要があるため)
  - `--include-legacy` フラグを付けた場合のみ、旧フィールドの値も改行/カンマ分割してマスター登録対象に含める (運用判断で残存データを救済したい場合の逃げ道)

---

## WebApp 変更

### 新規ルート (`scripts/webapp/routes/portfolio.py` に追加)

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/portfolio/themes` | 一覧 + 編集フォーム表示 |
| POST | `/portfolio/themes/create` | 新規作成 |
| POST | `/portfolio/themes/<name>/update` | リネーム/説明文編集 |
| POST | `/portfolio/themes/<name>/delete` | 削除 (確認ダイアログは JS 側で confirm()) |

すべてフォーム送信 → 完了後 `/portfolio/themes` に redirect (PRG パターン)。AJAX 化はしない (シンプル優先)。

### 新規テンプレート (`scripts/webapp/templates/portfolio_themes.html`)

レイアウト:

```
[← ポートフォリオに戻る]                    [+ 新規テーマ]

テーマ一覧 (14 件)
─────────────────────────────────────────
| 名前        | 説明           | 使用 | 操作        |
| 半導体      | 半導体製造装置 | 12   | [編集][削除] |
| 防衛        |                |  3   | [編集][削除] |
| ...                                            |
─────────────────────────────────────────
```

- 新規作成: モーダルではなく一覧上部のインラインフォーム (name 入力 + description 入力 + 作成ボタン)
- 編集: 行内で「編集」ボタンを押すと行が name/description の input に切り替わる (JS で行単位トグル)
- 削除: ボタンクリックで `confirm("テーマ 「XXX」 を削除しますか? N 銘柄から除去されます。")` → POST

### 既存 portfolio_list.html の変更

#### ヘッダのテーマフィルタ select の横にボタン追加 (162-167 行付近)

```html
<a href="{{ url_for('portfolio.themes_index') }}"
   style="..." title="テーマを編集">✏️ テーマを編集</a>
```

#### 銘柄行のテーマ入力 (276-288 行付近)

`<input list="gyoutai-theme-choices">` 2 個を `<select name="gyoutai_themes_0">` / `<select name="gyoutai_themes_1">` 2 個に置き換える。

```html
<select name="gyoutai_themes_0" class="theme-select" ...>
  <option value="">— (未設定)</option>
  {% for theme in theme_master %}
  <option value="{{ theme.name }}" {% if theme.name == current[0] %}selected{% endif %}>{{ theme.name }}</option>
  {% endfor %}
</select>
```

- `theme_master` は `list_themes()` の結果を `routes/portfolio.py` のリスト表示ハンドラで context 注入
- 現行値がマスター未登録 (移行漏れ等) の場合は、選択肢に **「⚠️ <name>」をその場限り追加 + selected** で表示崩れを防ぐ (移行スクリプト実行後は基本起こらない想定)
- `update_memo` 側は「現行レコードに既に入っている未登録値」の保持を許可するため、ユーザーは未登録値が残った銘柄でも他方のスロットを編集可能。未登録値を外したいときは select で「— (未設定)」を選べばよい
- 既存の `<datalist id="gyoutai-theme-choices">` (381-384 行) は削除
- `collect_gyoutai_theme_choices()` (helpers.py:1661) は使われなくなるので削除

#### Ctrl/Cmd+クリック → フィルタ機能 (870-878 行付近)

`<select>` でも `onmousedown` イベントは発火するため、現行ロジックをほぼそのまま使える。ただし `mousedown` 時点では `select.value` が選択中項目で固定されているため、`event.target` がドロップダウン項目 (`<option>`) であることを使って判定する必要がある (要動作確認。実装難なら **select の横に小さなフィルタアイコンを置く** に fallback)。

### routes/portfolio.py の memo 更新ハンドラ (345-371 / 386-426 行)

- スロット入力値が空文字 (`— (未設定)` を選んだケース) なら list から除去
- マスター未登録の値については `update_memo` の判定に委ねる:
  - 現行レコードに既に入っている未登録 name はそのまま保持 (保存成功)
  - 純新規の未登録 name (現行レコードに無い) は `update_memo` が ValueError を投げるので 400 を返す

---

## テスト追加

`tests/test_portfolio_shelve.py` に以下を parametrize で集約 (CLAUDE.md: 1 PR で 5 本以下、自明な動作は書かない):

1. `create_theme` 重複・空文字・上限超過・URL 禁止文字のバリデーション
2. `update_theme` のリネームで既存銘柄の `memo["gyoutai_themes"]` も書き換わる
3. `delete_theme` で既存銘柄から name が除去され、影響件数が返る
4. `update_memo` の未登録 name 判定 (parametrize):
   - 純新規の未登録 name → ValueError
   - 現行レコードに既にある未登録 name を維持 → 成功
   - 未登録 name を「— (未設定)」相当で外す (空文字経由で list から除去) → 成功
5. リネーム / 削除中の例外で部分書き込みが残らない (同 flock 内完結確認)

WebApp 側のテストは既存 `test_webapp_routes.py` パターンに合わせて smoke のみ:

- `GET /portfolio/themes` が 200
- `POST /portfolio/themes/create` で DB に 1 件追加される
- `POST /portfolio/themes/<name>/delete` で削除される

---

## 検証ポイント

1. `pytest tests/test_portfolio_shelve.py tests/test_webapp_routes.py -v`
2. 移行スクリプト dry-run → 実行 → 再実行 (冪等性確認)
3. `python -m webapp.app` 起動 → `/portfolio` でテーマ select が表示される
4. `/portfolio/themes` で作成・編集・削除が動く
5. リネーム後、`/portfolio` の銘柄行の表示が新 name に追従する
6. 削除後、`/portfolio` の銘柄行から該当 name が消える
7. Ctrl/Cmd+クリック → テーマフィルタが select でも動く

---

## ロールバック

- shelve は単一ファイル。事前に portfolio_shelve のバックアップを取れば、`theme:*` キー削除 + 移行スクリプト未実行で完全に戻せる
- portfolio_list.html / routes/portfolio.py は git revert で巻き戻し

---

## 想定リスク

- **リネーム/削除中のクラッシュ**: 同 flock 内で完結させるため、ShelveDB の writeback タイミングが鍵。`ShelveDB` ラッパが `with` ブロック終了時に flush する前提を再確認する
- **マスター未登録テーマの「⚠️」表示**: 移行漏れがあると select に出るが、ユーザーが select 操作した時点でマスターに無い値は選択肢から消える (実害は表示のみ)
- **`collect_gyoutai_theme_choices` の削除**: 他から参照されていないか grep で確認すること
