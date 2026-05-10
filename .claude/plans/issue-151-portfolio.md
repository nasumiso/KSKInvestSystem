# issue151 実装計画: Phase 3 保有銘柄管理ダッシュボード

> 要件仕様書: [doc/requirements/phase3_portfolio_requirements.md](../../doc/requirements/phase3_portfolio_requirements.md)
> 親 issue: #151（Closes は使わず、トラッキング用に残す）

---

## 1. 全体方針

### 1-1. issue 構造（インタビュー Q1: 3a/3b/3c 分割）

`#151` を親 issue として残し、3 つの sub-issue に分割する。**親 issue には `Closes #151` を使わない**（Phase 1 だけマージで親が自動クローズしないように）。

| Sub-issue | スコープ | PR 単位 |
|---|---|---|
| **Phase 3a** | portfolio_shelve 基盤 + スプシ移行 + my_watch_list.txt 取り込み + parse 置換 + shelve→txt 同期 | 1 PR |
| **Phase 3b** | /portfolio ダッシュボード(3タブ) + ステータス変更/追加/売却/削除UI + アクションログ記録 | 1 PR |
| **Phase 3c** | 振り返りビュー(アクションログ時系列) | 1 PR |
| **(別 issue)** | my_watch_list.txt 廃止（Phase 3 マージ後、運用が安定したら別途検討） | — |

### 1-2. スコープ外（インタビュー Q9-Q10）

以下は要件仕様書に記載があるが、**Phase 3 では実装しない**:

- §5-3 警告シグナル赤バッジ強調表示
- §6-2 イベントログ自動記録（Shintakane 日次バッチへの組込み）
- §6-1 アクションログ「見送り」「メモ」種別

これらは Phase 4（別 issue）で扱う。Phase 3 のアクションログ種別は **`初回登録` / `ステータス変更` / `売却` / `削除`** の 4 種に限定する。

### 1-3. テスト戦略（インタビュー Q5）

- **ユニットテスト**: portfolio_shelve / 移行スクリプト / 状態遷移バリデーション / parse_my_portforio() 互換性
- **Playwright E2E**: ダッシュボードの主要 3 シナリオ（追加 / ステータス変更 / 削除）の happy path

---

## 2. portfolio_shelve 設計（Phase 3a の中核）

### 2-1. ファイル配置

```
data/stock_data/portfolio_shelve  ← 新規
```

`research_shelve` / `stocks_shelve` と同じ階層。`db_shelve.ShelveDB` をラップ。

### 2-2. キー名前空間（インタビュー Q4: 単一 shelve 内で名前空間分離）

| キー形式 | 内容 | レコード削除時の挙動 |
|---|---|---|
| `record:<code_s>` | 保有レコード本体（ステータス・手動メモ） | `削除` 操作時に `del` |
| `action_log:<code_s>:<seq>` | アクションログ（手動判断） | **残す**（§5-5 / §6-1 要件） |

`<seq>` は 6 桁ゼロパディングの単調増加整数。同一銘柄内で連番管理。`portfolio_shelve` 内に `_seq:<code_s>` キーで現在値を保持。

### 2-3. レコードスキーマ

`record:<code_s>` の値:

```python
{
    "code_s": "6324",
    "stock_name": "ハーモニック・ドライブ・システムズ",
    "status": "1保",         # "1保" | "2準" | "3監"
    "registered_at": "2026-05-03T09:00:00+09:00",
    "updated_at": "2026-05-03T09:00:00+09:00",
    # 手動メモ（§4 + §7-1）
    "memo": {
        "gyoutai_theme": "...",       # 業態・テーマ
        "watch_in_reason": "...",     # ウォッチ・IN理由
        "trade_idea": "...",          # 投資売買アイデア
        "inago_origin": "...",        # イナゴ元・きっかけ
        "takaichi_sensitivity": "..." # 高市感応度
    }
}
```

指標データ（PER・RS 等）は **保存しない**（§4 表示時に stocks_shelve から都度参照）。

### 2-4. アクションログスキーマ

`action_log:<code_s>:<seq>` の値:

```python
{
    "code_s": "6324",
    "seq": 1,
    "timestamp": "2026-05-03T09:00:00+09:00",
    "action_type": "初回登録",  # "初回登録" | "ステータス変更" | "売却" | "削除"
    "status_from": None,        # "ステータス変更"/"売却" 時のみ。"初回登録"時は None
    "status_to": "3監",
    "reason": "..."             # 理由メモ。"初回登録"時は省略可
}
```

### 2-5. ステータス遷移バリデーション

`portfolio_shelve.py` で遷移を強制する。以下の遷移のみ許可:

```
（新規）→ 3監                 # 追加。1保/2準への直接登録は禁止
3監 ⇄ 2準                    # 格上げ/格下げ
2準 ⇄ 1保                    # 買い/売り
3監 ⇄ 1保                    # 直接遷移（要件仕様書に明示禁止なし。一応許可）
（任意）→ 3監 → （削除）       # 1保/2準 から直接削除は禁止
```

`1保 → 2準` の遷移は内部的に「売却」として action_type=`売却` で記録。それ以外のステータス変更は `ステータス変更`。

### 2-6. 主要 API

```python
# portfolio_shelve.py

def upsert_record(code_s: str, stock_name: str, memo: dict, *, db_path=None) -> None: ...
def get_record(code_s: str, *, db_path=None) -> Optional[dict]: ...
def list_records(status: Optional[str] = None, *, db_path=None) -> List[dict]: ...
def delete_record(code_s: str, reason: str, *, db_path=None) -> None: ...

def transition_status(code_s: str, new_status: str, reason: str, *, db_path=None) -> None: ...
def add_to_watch(code_s: str, stock_name: str, *, db_path=None) -> None: ...

def append_action_log(code_s: str, action_type: str, *, status_from=None, status_to=None, reason="", db_path=None) -> None: ...
def list_action_logs(code_s: Optional[str] = None, *, db_path=None) -> List[dict]: ...
```

すべて `fcntl` フロックで保護（research_shelve と同パターン）。

---

## 3. Phase 3a 詳細

### 3-1. 成果物

| ファイル | 種別 | 内容 |
|---|---|---|
| `scripts/portfolio_shelve.py` | 新規 | §2 の DB モジュール |
| `scripts/migrate_portfolio_from_csv.py` | 新規 | スプシ「保有銘柄シート」CSV → portfolio_shelve |
| `scripts/migrate_my_watch_list_to_shelve.py` | 新規 | my_watch_list.txt → portfolio_shelve（初期取り込み + マージ） |
| `scripts/portfolio.py` | 修正 | `parse_my_portforio()` の内部実装を portfolio_shelve 参照に置換（**シグネチャ無修正**）+ shelve → txt 一方向同期書き出しの追加 |
| `tests/test_portfolio_shelve.py` | 新規 | DB 層ユニットテスト |
| `tests/test_migrate_portfolio_from_csv.py` | 新規 | CSV 移行テスト |
| `tests/test_migrate_my_watch_list_to_shelve.py` | 新規 | txt 取り込みテスト |
| `tests/test_portfolio.py` | 修正/新規 | `parse_my_portforio()` の互換性テスト |

### 3-2. スプシ移行手順（インタビュー Q2: 事前 CSV エクスポート）

#### 3-2-1. 列マッピングの事前確定（**完了済み**）

✅ ユーザーから受領した `銘柄調査 - 保有銘柄.csv` (36 列 × 139 銘柄) を分析し、§3-2-2 の列マッピングを確定済み。

確定時の重要な発見:
- スプシ「保有銘柄シート」の指標系列・順位・保有リスト列は **`code_rank.csv` からの参照値（VLOOKUP）**であり、スプシ上の手動入力ではない
- よって移行対象は識別 2 列 + 真の手動メモ 5 列の **計 7 列のみ**
- ステータス決定は **`my_watch_list.txt` のみ** を真実源とする（スプシのステータス列は無視）

この発見により §3-3 のマージルールも大幅にシンプル化された。

#### 3-2-2. 列マッピング（**確定済み**）

CSV ファイル: `銘柄調査 - 保有銘柄.csv`（36 列、先頭 1 行は空行、2 行目がヘッダ、3 行目以降が 139 銘柄分のデータ）

##### 重要な前提

スプシ「保有銘柄シート」の **指標系列（順位・PER・配当・RS・トレンドテンプレート・シグナル・時価総額・決算日 等）と保有リスト列は、`code_rank.csv` からの参照値（VLOOKUP 等で表示しているだけ）**。スプシ上の手動入力は識別情報と「真の手動メモ」のみ。

→ **移行対象は手動メモ系列の 5 列 + 識別 2 列の合計 7 列**。指標系列はすべて移行不要（要件仕様書 §4「指標データは portfolio_shelve に保存せず stocks_shelve から都度参照」と整合）。

##### 移行対象の列マッピング（確定）

| CSV col # | 列名 | 移行先 | 備考 |
|---|---|---|---|
| 0 | 銘柄コード | `code_s` | 文字列正規化（4 桁ゼロ埋め or 4 文字英数字） |
| 1 | 銘柄名 | `stock_name` | そのまま |
| 3 | 業態・テーマ | `memo.gyoutai_theme` | 改行を含むセルあり（複数行 OK） |
| 31 | ウォッチ・IN理由 | `memo.watch_in_reason` | 改行を含むセルあり |
| 33 | イナゴ元・きっかけ | `memo.inago_origin` | 改行を含むセルあり |
| 34 | 投資売買アイデア | `memo.trade_idea` | 改行を含むセルあり |
| 35 | 高市感応度 | `memo.takaichi_sensitivity` | 「A:〜」「B:〜」のような感応度ラベル付きの自由記述 |

##### 取り込まない列（参考）

| CSV col # | 列名 | 理由 |
|---|---|---|
| 2 | 保有リスト | `code_rank.csv` 由来の表示値。ステータスは **`my_watch_list.txt` のみ** を真実源とする |
| 4–30, 32 | 指標・順位・チャート・需給チャート 等 | すべて `code_rank.csv` または stocks_shelve 由来の表示値。表示時に都度参照 |

##### スプシの構造詳細

- 1 行目: 全列空（区切り文字のみ）
- 2 行目: ヘッダ（36 列）
- 3 行目以降: データ 139 行
- 文字エンコーディング: UTF-8
- 改行を含むセルは Google Sheets の RFC 4180 仕様に従いダブルクォートで囲まれている（Python `csv` モジュールで標準パース可能）

#### 3-2-3. 移行スクリプト実行手順

1. ユーザーが CSV を `${KS_DATA_DIR}/migration/portfolio_sheet.csv` に配置
2. `cd scripts && python migrate_portfolio_from_csv.py "${KS_DATA_DIR}/migration/portfolio_sheet.csv" --dry-run` で **dry-run** 実行し、件数とサンプル出力を目視確認
3. 問題なければ `--dry-run` を外して本番実行
4. 移行スクリプトは `migrate_research_from_csv.py` の 4 層構成パターンを踏襲:
   - 読込層: `read_csv_rows()`
   - パース層: `parse_status_column()` / `parse_memo_columns()`
   - 統合層: `build_record_from_row()`
   - 実行層: `migrate_csv_to_portfolio_shelve()`
5. 移行時、各レコードに `初回登録` アクションログを 1 件記録
6. 移行後、shelve のレコード数が CSV 行数とほぼ一致することを確認（ETF や空行は除外される可能性あり）

#### 3-2-4. googledrive.py 経由の自動取り込みは Phase 3 では実装しない

ユーザーが「移行後もスプシ↔shelve の同期を考えている可能性」と回答したが、Phase 3 のスコープでは **手動 CSV エクスポート** に限定する。理由:

- `migrate_research_from_csv.py` も手動エクスポート方式で運用実績あり
- `googledrive.py` には現状シート読み取り関数がなく、新規実装が必要 → スコープ膨張
- 移行は 1 回限りの作業（移行後の真実源は portfolio_shelve）

**ただし**、移行後もスプシで編集したい運用が想定される場合は、別 issue で「googledrive 経由の同期 / 再取り込みスクリプト」を後日検討する。Phase 3 の振り返り段階（PR 完了後）で運用感を確認し、必要なら新 issue を立てる。

### 3-3. my_watch_list.txt 取り込み手順（インタビュー Q3, Q7、§3-2-2 の前提を反映）

スプシのステータス列 (col2) は `code_rank.csv` 由来の表示値であり真実源ではない。よって **ステータスは `my_watch_list.txt` のみで決定**する（インタビュー Q7 の「txt 優先」原則をシンプル化）。

#### マージルール（簡素化済み）

| 状況 | ステータス | 手動メモ |
|---|---|---|
| txt のみ存在（H 接頭辞） | `1保` | 空 |
| txt のみ存在（接頭辞なし） | `3監` | 空 |
| スプシのみ存在 | **`3監`**（txt にない = ウォッチ扱いに倒す） | スプシ採用 |
| 両方存在 | **txt のステータス**（H 付き→1保 / なし→3監） | スプシ採用 |

#### 実装順序

1. **スプシ CSV 移行**: 識別情報 + 手動メモ 5 列を取り込み、ステータスは仮で `3監` とする
2. **txt 取り込み**: txt のステータスで上書き（H 付きは `1保`、なしは `3監`、txt にない銘柄は `3監` のまま）

#### `2準` の扱い

仕様書 §7-2 の通り、**txt には `2準` の概念がない**ため移行直後は `1保` / `3監` のみ。`2準` は WebApp（Phase 3b）からのステータス変更でのみ発生する。

### 3-4. parse_my_portforio() 置換（要件 §7-2 ステップ 2）

```python
# 現行
def parse_my_portforio() -> Tuple[List[str], List[str]]:
    # my_watch_list.txt をパース
    return (watch_codes, possess_codes)

# 置換後（シグネチャ無修正）
def parse_my_portforio() -> Tuple[List[str], List[str]]:
    records = portfolio_shelve.list_records()
    possess = [r["code_s"] for r in records if r["status"] == "1保"]
    watch   = [r["code_s"] for r in records if r["status"] in ("2準", "3監")]
    return (watch, possess)
```

呼び出し側（`make_stock_db.py` / `kessan.py` / `disclosure.py` / `webapp/helpers.py`）は無修正。

#### 3-4-1. **返却の互換性（重要）**

##### 下流処理の順序依存確認 ✅ 完了

実装着手前に `make_stock_db.py` `kessan.py` `disclosure.py` `webapp/helpers.py` の全呼び出し（計 6 箇所）を grep で確認し、**すべて順序非依存** であることを確認:

| 呼び出し元 | 使い方 | 順序依存 |
|---|---|---|
| make_stock_db.py:1089 | `(pf_stocks + possess_list)` を `set()` で集合化 | なし |
| make_stock_db.py:1539 | `set(watch_codes) \| set(possess_codes)` | なし |
| kessan.py:121 | `if k in code_list_s + possess_list_s` 所属チェック | なし |
| disclosure.py:251 | for ループ後、最終出力は日付ソート | 実質なし |
| webapp/helpers.py:680 | `set(possess_list)` 集合 | なし |
| webapp/helpers.py:1066 | `set(watch_list) \| set(possess_list)` 集合 | なし |

→ `migration_order` フィールドは **不要**。`parse_my_portforio()` は **`code_s` 昇順** など決定論的な順序で返せばよい。

##### 集合の扱い: 「移行で増える分」を意識的に許容する

スプシ移行で 139 件、my_watch_list.txt 取り込みで 327 件が portfolio_shelve に入る。**両者で重複しない銘柄が出るため、`parse_my_portforio()` が返す集合は移行前の txt より広くなりうる**。

- 移行直後に集合が増えると、`make_stock_db.py` の更新対象銘柄が増える可能性がある
- これは **設計上の意図された変化**（仕様書 §7-2 で portfolio_shelve を真実源にすると決めたため）
- ただし「いきなり大量の銘柄が更新対象に追加されると挙動が読めない」リスクは残る

##### 互換性検証の 2 段階アプローチ

| 段階 | 比較対象 | 期待 | 目的 |
|---|---|---|---|
| **Step 1: データ移行前の純粋な置換テスト** | 現行 txt → 旧 parse vs txt のみを取り込んだ portfolio_shelve → 新 parse | **集合が完全一致**（順序不問） | 置換ロジック自体にバグがないことを保証 |
| **Step 2: スプシ移行後の「妥当な変化」確認** | 旧 parse の結果 vs スプシ移行 + txt 取り込み後の新 parse | 集合は **新 parse ⊇ 旧 parse**（旧の銘柄はすべて含む）、増分はスプシ由来銘柄のみ | 意図した変化のみが起きたことを確認 |

##### テストとパイロット

1. `test_portfolio.py` に Step 1 テストを追加（fixture で txt のみの portfolio_shelve を作り、現行 parse と新 parse の **集合一致** を確認）
2. Step 2 は実データでの **手動パイロット**（§3-7 の diff 比較で「増えた銘柄がスプシ由来か」を目視確認）

### 3-5. shelve → txt 一方向同期（インタビュー Q6）

※ インタビュー Q3 では当初「並行運用後に txt 完全削除」を選択していたが、後の判断で **txt 廃止は Phase 3 スコープ外（別 issue）** に変更した。よって本同期は Phase 3 完了後も動き続ける（§5-3 参照）。

#### 同期トリガーの方針（実装中の調整）

当初は「shelve への書き込み API 成功時に自動同期」を想定したが、テスト容易性 / 単一責務 / バルク書き込み時のパフォーマンスを考慮し、**「同期関数 `sync_to_my_watch_list_txt()` を提供し、明示的に呼ぶ」** 方式に変更:

| Phase | 呼び出し元 |
|---|---|
| Phase 3a | 移行スクリプト末尾、parse_my_portforio() の置換テスト後 |
| Phase 3b | WebApp の各書き込みハンドラ（追加・遷移・削除）の末尾 |

DB 側で自動呼び出ししないことで、ユニットテストの DATA_DIR 設定不要で完結し、移行スクリプトのバルク処理も末尾の 1 回呼び出しで済む。

`portfolio_shelve` への書き込み API（upsert / transition / delete）が成功するたびに、txt も書き出す（旧記述、参考）:

```python
def _sync_to_txt(records: List[dict]) -> None:
    """portfolio_shelve の現在状態を my_watch_list.txt に書き出す。
    H 接頭辞付き = 1保、接頭辞なし = 3監。2準 は 3監 として書き出す（txt は 2 値）。"""
    lines = []
    for r in sorted(records, key=lambda x: (x["status"], x["code_s"])):
        prefix = "H" if r["status"] == "1保" else ""
        lines.append(f"{prefix}{r['code_s']}{r['stock_name']}")
    DATA_DIR.joinpath("my_watch_list.txt").write_text("\n".join(lines))
```

`2準` を txt 上で `1保` 扱いとするか `3監` 扱いとするかは、**「txt を見る既存コード」の挙動を変えない方針**が安全。`my_watch_list.txt` の H 接頭辞は「現在の保有」を意味するため、`2準`（直近売却 or もうすぐ買いたい）は `3監` 側に倒す。txt 廃止 issue（別 issue・将来）で txt が消えるまでの暫定処置。

### 3-6. テスト

- `test_portfolio_shelve.py`: upsert / get / list / delete / transition の各 API、ステータス遷移バリデーション、アクションログの不可削除性
- `test_migrate_portfolio_from_csv.py`: CSV パース、列マッピング、`1保`/`2〇`/`3監` 正規化
- `test_migrate_my_watch_list_to_shelve.py`: H 接頭辞パース、マージ規則（txt 優先/スプシメモ保持）
- `test_portfolio.py`: `parse_my_portforio()` の互換性（§3-4-1 Step 1: txt のみ取り込み時に旧 parse と完全一致）、戻り値の型と要素順

### 3-7. リスク・ロールバック

- portfolio_shelve 破損時は my_watch_list.txt が並行運用で残るため、再構築可能
- 万一 parse_my_portforio() の挙動が想定外に変わると make_stock_db.py / kessan.py 全体に波及。

#### パイロット手順（2 段階）

**Step A: 純粋な置換確認**（データ移行前、portfolio_shelve は空）

1. `python make_stock_db.py list_all_db` を現行コードで実行し、出力 CSV を `_baseline_pre/` に保存
2. portfolio.py を置換実装に切り替え、**txt のみを取り込んだ portfolio_shelve** を一時的に作成（テスト用 DB パスで）
3. 同コマンドを再実行し、`_baseline_post_step_a/` に保存
4. `diff -r _baseline_pre _baseline_post_step_a` → **完全一致** を期待。差分があれば置換ロジックにバグ

**Step B: スプシ移行後の妥当な変化確認**

5. スプシ CSV 移行を実行 → portfolio_shelve に 139 件 + 取り込み済み txt のマージ結果が入る
6. 同コマンドを再実行し、`_baseline_post_step_b/` に保存
7. `diff -r _baseline_post_step_a _baseline_post_step_b` → 差分は「スプシ由来銘柄が新規追加された分のみ」を期待
8. 増えた銘柄を一覧化し、ユーザーが「これらは追加されて妥当」と確認

#### 件数の目安

- Step A 時点: 保有 24 件 + ウォッチ 303 件 = 計 327 件
- Step B 時点: スプシ 139 件のうち txt にない銘柄が増分。最大 466 件、実際はスプシと txt の重複次第

---

## 4. Phase 3b 詳細

### 4-1. 成果物

| ファイル | 種別 | 内容 |
|---|---|---|
| `scripts/webapp/routes/portfolio.py` | 新規 | `/portfolio` ルート（GET / POST） |
| `scripts/webapp/templates/portfolio_list.html` | 新規 | 3 タブ + 一覧表 |
| `scripts/webapp/templates/portfolio_edit_dialog.html` | 新規 | ステータス変更/追加/売却/削除のダイアログ部品 |
| `scripts/webapp/app.py` | 修正 | Blueprint 登録、ナビ追加 |
| `scripts/webapp/templates/base.html` | 修正 | ナビに `保有銘柄` リンク追加 |
| `scripts/webapp/templates/detail.html` | 修正 | 銘柄調査ページに「3監 に追加」ボタン追加 |
| `tests/test_webapp_portfolio_routes.py` | 新規 | ルートのユニットテスト |
| `tests/e2e/test_portfolio_dashboard.py` | 新規 | Playwright E2E |

### 4-2. URL 設計（インタビュー Q8）

| URL | メソッド | 機能 |
|---|---|---|
| `/portfolio?status=hold|semi|watch` | GET | ダッシュボード（タブ切替） |
| `/portfolio/add` | POST | 銘柄を 3監 に追加 |
| `/portfolio/<code_s>/transition` | POST | ステータス変更（売却含む） |
| `/portfolio/<code_s>/delete` | POST | 削除（3監 からのみ） |

`status=hold` ⇄ `1保`、`status=semi` ⇄ `2準`、`status=watch` ⇄ `3監`。

### 4-3. ダッシュボード一覧表示

要件 §5-2 の 6 カテゴリすべてを表示。情報密度が高いので、以下の階層で整理（実装時調整可）:

- **常時表示（左から右へ）**:
  - コード / 銘柄名 / ステータス
  - PER / 時価総額 / 配当（最新指標）
  - RS / ステージ / トレンドテンプレート（テクニカル）
  - シグナル列（警/売/新高値 等のテキスト表示。**赤バッジ強調なし** = Phase 4 送り）
  - 理論株価乖離率
- **開閉式サブセクション（行クリックで展開）**:
  - 業績の詳細（売上・利益成長率、進捗率）
  - 手動メモ（IN理由、売買アイデア等）

### 4-4. ステータス変更 UI

ダイアログ:

| シナリオ | UI 動作 | アクションログ種別 |
|---|---|---|
| `/stock/<code_s>` から 3監 追加 | 「3監 に追加」ボタン → 確認ダイアログ → POST /portfolio/add | `初回登録` |
| ダッシュボード行 → 「ステータス変更」 | プルダウン（許可遷移のみ）+ 理由入力 → POST /portfolio/<code_s>/transition | `ステータス変更` または `売却`（1保→2準 の場合） |
| ダッシュボード（3監 タブ）→ 「削除」 | 確認ダイアログ + 理由入力 → POST /portfolio/<code_s>/delete | `削除` |

1保/2準 タブには「削除」ボタンを **表示しない**（誤操作防止）。

### 4-5. テスト

- `test_webapp_portfolio_routes.py`: 各ルートのリクエスト/レスポンス、不正遷移の拒否（例: 1保 → 削除）
- Playwright E2E:
  - シナリオ A: 銘柄詳細 → 「3監 に追加」 → /portfolio?status=watch で表示確認
  - シナリオ B: 3監 → 2準 → 1保 → 2準 のステータス遷移、各時点でアクションログが増える
  - シナリオ C: 1保 → 削除を試みてエラー、3監 に戻して削除成功

---

## 5. Phase 3c 詳細

### 5-1. 成果物

| ファイル | 種別 | 内容 |
|---|---|---|
| `scripts/webapp/routes/detail.py` | 修正 | 銘柄調査ページに「振り返り」セクション追加 |
| `scripts/webapp/templates/detail.html` | 修正 | アクションログ時系列表示 |
| `tests/test_webapp_detail_portfolio.py` | 新規 | 振り返り表示のユニットテスト |

### 5-2. 振り返りビュー（要件 §6-3）

`/stock/<code_s>` の既存ページに「振り返り」セクションを追加。アクションログを時系列降順で表示:

```
2026-05-03 09:00  ステータス変更 [3監 → 2準]  「決算良好。準保有に格上げ」
2026-04-15 14:30  初回登録 [→ 3監]              （理由なし）
```

イベントログは Phase 3 スコープ外なので **アクションログのみ**。Phase 4 でイベントログ実装時に同じビューに統合する。

### 5-3. my_watch_list.txt 廃止は別 issue に切り出し

ユーザー判断により、**txt 廃止は Phase 3 のスコープ外**とする。理由:

- Phase 3 完了直後に txt を消すと、shelve に問題が出た時のフォールバックがなくなる
- 「運用が安定した」と判断できる期間を 1〜2 週間と決め打ちせず、実運用感覚で判断したい
- 別 issue にすることで「いつ廃止するか」をユーザーが任意のタイミングで起票できる

#### Phase 3 完了後の状態

- `${KS_DATA_DIR}/my_watch_list.txt` は **存在し続ける**
- `portfolio.py` の `_sync_to_txt()` (Phase 3a で実装) は **動き続ける**（shelve 更新時に txt も書き出される）
- `parse_my_portforio()` は portfolio_shelve を真実源として返す
- `my_watch_list.txt` は **shelve から自動生成されるバックアップ** として残る（手編集はしない方針）

#### 別 issue で扱う作業（将来）

- `_sync_to_txt()` の停止 → 削除
- `${KS_DATA_DIR}/my_watch_list.txt` の物理削除（**repo 配下ではなく実運用 DATA_DIR 配下**。`KS_DATA_DIR=/Users/k_sohara/Ext/GoogleDrive/shintakane_data`）
- `grep -r "my_watch_list"` で残存参照がないか確認

これは Phase 3 の振り返り段階で別 issue を立てる。

---

## 6. 実装順序まとめ

### Phase 3a（1 PR）

0. **【ゲート】列マッピング確定** ✅ 完了済み（§3-2-2 参照）
1. portfolio_shelve.py 作成 + ユニットテスト
2. migrate_portfolio_from_csv.py 作成 + ユニットテスト
3. migrate_my_watch_list_to_shelve.py 作成 + ユニットテスト
4. portfolio.py の parse_my_portforio() を内部実装置換 + 互換性テスト（§3-4-1 Step 1）
5. **パイロット Step A**: 現行コードでの `make_stock_db.py list_all_db` baseline 取得 → txt のみ取り込み portfolio_shelve で再実行 → 完全一致確認
6. スプシ CSV 移行スクリプト本番実行（`--dry-run` で先に確認）
7. **パイロット Step B**: スプシ移行後の `make_stock_db.py list_all_db` 再実行 → 増分がスプシ由来のみであることを確認
8. shelve → txt 同期実装

### Phase 3b（1 PR、3a マージ後）

1. routes/portfolio.py + templates 作成
2. base.html / detail.html 修正
3. ユニットテスト + Playwright E2E

### Phase 3c（1 PR、3b マージ後）

1. 振り返りビュー実装

※ my_watch_list.txt 廃止は **別 issue** に切り出し（運用安定後にユーザー判断で起票）

---

## 7. 開発コマンド

```bash
# Phase 3a テスト
pytest tests/test_portfolio_shelve.py tests/test_migrate_portfolio_from_csv.py \
       tests/test_migrate_my_watch_list_to_shelve.py tests/test_portfolio.py -v

# Phase 3a 移行実行（ユーザー作業）
cd scripts && python migrate_portfolio_from_csv.py "${KS_DATA_DIR}/migration/portfolio_sheet.csv"
cd scripts && python migrate_my_watch_list_to_shelve.py

# Phase 3a パイロット
cd scripts && python make_stock_db.py list_all_db

# Phase 3b テスト
pytest tests/test_webapp_portfolio_routes.py -v
pytest tests/e2e/test_portfolio_dashboard.py -v

# Phase 3b 動作確認
cd scripts && python -m webapp.app
# → http://localhost:5001/portfolio
```

---

## 8. オープンクエスチョン（実装時に決める）

- ダッシュボード一覧の「常時表示」と「開閉式サブセクション」の境界（情報密度を見て調整）
- アクションログ理由欄の必須/任意（種別ごとに変える: 初回登録は任意、削除は必須）
- スプシ移行 CSV のエンコーディング・列名規約（実物 CSV を見て決定）
