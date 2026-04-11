# Phase 1 要件定義：銘柄調査DB（research_shelve）の構築

> 銘柄調査スプレッドシートの手動管理から、Shintakaneと連携した半自動管理への移行

---

## 1. 背景

### 現状の運用フロー

銘柄調査データはGoogleスプレッドシート（849銘柄 × 16列、保有銘柄139銘柄 × 36列）で手動管理されている。

データは以下の3種類から成る：

| データ種別 | 該当列 | 現在の運用 |
|-----------|-------|----------|
| Shintakane出力のコピペ | IR分析（定量部分）、クォリティ指標、機関投資家 | code_rank.csv出力を決算のたびに手動で追記 |
| 外部ソースの転記 | 四季報コメント1〜5 | 四季報発売時に手動コピペ |
| 完全手動メモ | 総合評価、IR分析（コメント部分）、OpenWork、メモ・総括、ジムクレイマー | 随時手動入力 |

### 課題

- **コピペの非効率**: Shintakaneが毎日算出しているデータを、手動でスプレッドシートに転記している
- **時系列データの所在**: shelve DBは常に最新値で上書きされるため、過去の時系列スナップショットはスプレッドシートにしか存在しない。この不可逆な資産がスプレッドシートに閉じ込められている
- **データ構造の限界**: 1セルに時系列データを改行区切りで積み重ねる形式は、検索・フィルタ・比較に不向き

---

## 2. Phase 1のゴール

1. **スナップショットの自動蓄積**: Shintakaneの日次実行時に、決算更新を検知してcode_rank.csv相当の整形データを自動でDBに追記する → コピペ作業の廃止
2. **手動メモのDB化**: スプレッドシートの手動メモ列をDBに格納する受け皿を作る
3. **既存データの移行**: スプレッドシート849銘柄分の蓄積データをDBに一括移行する

---

## 3. アーキテクチャ方針

### DB分離の原則

既存のstocks_shelveと新規のresearch_shelveは**別DBとして分離運用**する。

| | stocks_shelve（既存） | research_shelve（新規） |
|---|---|---|
| 性質 | 揮発性キャッシュ（常に最新値で上書き） | 不可逆な蓄積資産（時系列履歴＋手動メモ） |
| 更新頻度 | 毎日自動（TTL管理） | 決算検知時に半自動＋随時手動 |
| 再構築可否 | スクレイピングし直せば復元可能 | 消えたら復元不可能 |
| バックアップ要件 | 日次で十分 | 変更のたびにバックアップが望ましい |

**依存関係は一方向**: stocks_shelveを「読み取り」→ research_shelveに「書き込む」のみ。既存のstocks_shelve側のコードは変更しない。

### ファイル配置

```
data/stock_data/stocks_shelve       ← 既存（変更なし）
data/stock_data/research_shelve     ← 新規
```

### データフロー

```
■ 自動（毎日19時、決算検知時のみ）
stocks_shelve → code_rank.csv整形ロジック流用 → research_shelve にスナップショット追記

■ 手動（随時）
ユーザー → CLI or 将来のWebビュー → research_shelve にメモ入力・編集

■ 参照
research_shelve + stocks_shelve → 統合ビュー（Phase 2）
```

---

## 4. research_shelve 基本設計

> **実装状況**: 基盤モジュール・CRUD・バックアップ・表示CLIは issue #91 で実装済み (`scripts/research_shelve.py`)。以下は実装後の確定仕様。

### レコード構造（確定版）

キーは `code_s`（"3496" 等）で、stocks_shelve と同一の命名規則。`code_s` は入力時に `.strip().upper()` で正規化して保存する（実データに `135a` のような小文字混入例が1件あるため）。

1レコードは純粋な `dict` で、以下の3ブロック11フィールドから成る:

**識別・基本情報ブロック**:
- `code_s` (str): 証券コード（大文字正規化済み）
- `stock_name` (str): 銘柄名
- `overview` (str): 企業概要（スプシの IR 分析列の冒頭ヘッダ行から移行される）
- `overall_rating` (str): 総合評価 `S`/`A`/`B`/`C`/`D`/`E` または `""`
- `institutional_comment` (str): 機関投資家の概況フリーコメント（例: `"あまりいない\n個人多い"`）。**理論株価数値はここに入れない**（スナップショットの `rironkabuka_kairi` に分離保持）

**手動メモ群ブロック**:
- `memo` (str): メモ・総括
- `openwork` (str): OpenWork 評価
- `cramer` (str): ジムクレイマー分析
- `shikiho_comments` (list[str]): 四季報コメント（最新が先頭。スプシは最大5件だが件数上限は設けない）

**時系列スナップショットブロック**:
- `snapshots` (list[dict]): 1決算=1スナップショット、最新が先頭（`date_yy_m` 降順ソート）

各スナップショットは1つの決算タイミングに対応し、以下のフィールドを持つ:
- `date_yy_m` (str): `"26.1"` のような `YY.M` 表記。**内部も同形式で保持**（スプシ原文準拠、決算月までしか情報がないため ISO 化しない）
- `ir_quant` (str): IR 分析の定量部分を原文保持（例: `"[A]26%,21%[Q]25%,25%[P]1Q21%(22%),20%(19%)"`)
- `ir_comment` (str): IR 分析の手動コメント（`・` 箇条書き行を改行区切りで連結）
- `quality_indicators` (str): クォリティ指標を原文保持（改行含む。例: `"555億 PER27 PBR9.3\n配当2.8 ROE36"`)
- `rironkabuka_kairi` (str): 理論株価乖離を原文保持（例: `"75%(-%)|243%,-91%"`、既存 `rironkabuka.get_rironkabuka_expr()` と同一形式）
- `data_source` (str): `"manual"` / `"migration"` / `"auto"` のいずれか

### 設計判断（issue #91 での確定事項）

- **dict を使う**: 既存 `stocks_shelve` が純 dict を使っており一貫性を保つ。pickle 経由のクラス解決パス固定も避ける
- **parsed（数値化 dict）フィールドは今は持たない**: 将来必要になった時点で `snapshot["ir_quant_parsed"] = {...}` のように dict にキーを後付け追加できるため、最初から枠を用意しない
- **理論株価は機関投資家列から分離**: スプシは1列に混在していたが、DB 上は `institutional_comment` (識別・基本情報) と `rironkabuka_kairi` (スナップショット) に分離する
- **日付は内部も `YY.M`**: スプシ原文のフォーマットが `YY.M` で統一されていて決算月までしか情報がない。ISO 化すると「決算月の1日」を便宜的に当てることになり、疑似的な情報を追加してしまう。ソートは `(int(yy), int(m))` タプルで行う

### 公開 API（issue #91 で実装済み）

`scripts/research_shelve.py` が提供する関数。後段の issue #92 / #94 の呼び出し元として利用可能:

- **スキーマ**: `create_research_record()` / `create_snapshot()` / `normalize_code_s()` / `validate_code_s()` / `validate_date_yy_m()` / `date_yy_m_sort_key()`
- **CRUD**: `get_research_record()` / `upsert_research_record()` / `delete_research_record()`
- **スナップショット**: `upsert_snapshot(code_s, snapshot, *, overwrite_same_date=True, db_path=None)` — 同一 `date_yy_m` は冪等上書き、追記後に降順ソート、レコード非存在時は `KeyError`（自動作成はしない）
- **フィルタ**: `list_research_records(*, rating=None, keyword=None, db_path=None)` — `rating` はカンマ区切り複数指定可 (`"S,A"`)、`keyword` は大文字小文字無視の部分一致
- **バックアップ**: `backup_research_db(db_path=None)` — `.dat/.dir/.bak` 3点セットを `ks_util.backup_file()` で日付サフィックス付きでコピー
- **表示 CLI**: `show <code_s>` / `list [--rating S,A] [--keyword 駐車場]` / `backup`

すべての CRUD 関数は `db_path: Optional[str] = None` 引数を受け、テストの `tmp_path` 差し替えや将来の別パス書き出しに対応する。

### CLAUDE.md「DB 操作は `update_db_rows()` 経由」規約との関係

CLAUDE.md の当該規約は **`stocks_shelve` に対するスクレイピング結果のマージ更新**（空値での上書き防止、`access_date_*` の管理など `make_stock_db.py` の `update_db()` で行っている保護ロジック）を指す。**別 DB である `research_shelve` には直接適用されない**。

代わりに、`research_shelve` では **公開関数（`upsert_research_record` / `upsert_snapshot` / `delete_research_record` 等）を経由した更新のみ**を行うルールとし、他のモジュールが `ShelveDB(RESEARCH_SHELVE)` を直接開いて書き換えてはならない。

### スナップショット自動追記の要件（issue #94 で実装予定）

- **トリガー**: 既存の `need_kessan_upd()` による決算発表検知。決算更新が走った銘柄のみが対象
- **整形ロジック**: code_rank.csv 出力時に使用している既存の整形関数 (`get_shihyo_expr` / `get_gyoseki_expr` / `get_rironkabuka_expr` 等) を流用する
- **冪等性**: `upsert_snapshot(..., overwrite_same_date=True)` により、同一 `date_yy_m` の再実行で重複が積まれない
- **追記タイミング**: `list_all_db()` の処理末尾（CSV 出力後）
- **データソース区分**: `data_source="auto"` として記録
- **呼び出しパターン**: レコード非存在時はまず `upsert_research_record(create_research_record(...))` で最低限の土台を作ってから `upsert_snapshot` を呼ぶ（`upsert_snapshot` は自動作成しない）

---

## 5. 既存スプレッドシートからの移行

> **実装状況**: issue #91 では実データ (`銘柄調査 - 銘柄調査.csv`, 853 行 × 17 列) を精査してレコード構造を確定したのみ。実際のパース・移行は issue #92 で対応する。

### 移行対象

**銘柄調査シート（853 行、17 列）**: 全列を移行対象とする

| スプレッドシート列 | 移行先 | パース難易度 |
|---|---|---|
| 証券コード | `code_s`（`.strip().upper()` で正規化） | 低 |
| 銘柄名 | `stock_name` | 低 |
| 分析日 / 決算日 | （今回は格納対象外。レコード自体の作成日時は別途検討） | 低 |
| 総合評価 | `overall_rating` | 低 |
| OpenWork / メモ・総括 / ジムクレイマー | `openwork` / `memo` / `cramer`（そのまま格納） | 低 |
| 四季報コメント1〜5 | `shikiho_comments` (list、順序保持) | 低 |
| クォリティ指標 | スナップショットの `quality_indicators`（日付で分割、改行込み原文保持） | 中 |
| 機関投資家 | 先頭コメント部を `institutional_comment`、日付行を各スナップショットの `rironkabuka_kairi` に分離 | 中 |
| IR 分析 | 冒頭ヘッダ行を `overview`、各決算ブロックの定量行を `ir_quant`、箇条書きコメント行を `ir_comment` | **高** |

**保有銘柄シート（139 行）**: Phase 1 では移行対象外。Phase 2 で対応。

### 実データの観察（issue #91 で精査済み）

- **3 列（IR 分析 / クォリティ指標 / 機関投資家）の日付は同じ決算タイミングで揃っている** → 1 スナップショットとして統合可能
- **機関投資家列は混在**: 先頭の日付なし行 = 概況コメント（例: `あまりいない` / `個人多い`、281 銘柄に存在）、日付付き行 = 理論株価乖離（728 銘柄に存在）
- **理論株価フォーマット**: `YY.M 乖離%(先行%)|上限%,下限%` — 既存 `rironkabuka.get_rironkabuka_expr()` と同一形式
- **IR 分析列の構造**: 先頭に企業概要（日付なしヘッダ行）、以降は `YY.M` 区切りの決算ブロック
- **IR 分析の決算ブロック内**: `[A]売上%,営利%[Q]四半期売上%,四半期営利%[P]NQ進捗%(予想%),営利進捗%(予想%)` 形式の定量行 + `・〜` の箇条書きコメント行
- **決算ブロック内の `・` コメントは直前の決算に紐づく**: 例えば `25.11` ブロック内の `・新中経~30 CAGR35%(つよい)` は 25.11 決算への感想として扱う
- **code の異常値**: 実データに `135a` のような小文字末尾が 1 件ある → 移行時は `normalize_code_s()` で正規化
- **総合評価の分布**: C=326, B=198, D=150, 空=96, A=51, E=24, S=8（合計 853）
- **紛らわしい日付行**: `22.9.1Q決算説明資料` のような行が境界検出で誤認されやすい → 正規表現は `^\d{2}\.\d{1,2}$` や「後続が空白 / 行末 / `[`」で厳密化する必要あり

### IR 分析列のパース方針（issue #92 で実装）

IR 分析列は 1 セル内に「定量データ行」と「手動コメント行」が混在しており、移行の最難関。基本方針は、行頭パターンでスナップショット境界を検出し、定量データとコメントを分離すること。

- 行頭が `YY.M[` または `YY.M<空白>` にマッチする行を新しいスナップショットの開始点として扱う
- スナップショット開始行の `[A]...[Q]...[P]...` 部分を `ir_quant` に、それ以外の続き行（`・〜` など）を `ir_comment` に改行区切りで連結
- 冒頭の日付を持たない行（企業概要）は `overview` へ回す
- 末尾に決算と関係ない自由テキスト（`2020年9月決算説明会` など）が入っているケースは、「最後のスナップショットに紐づけない孤立メモ」として扱うか、最後のスナップショットの `ir_comment` に追記するかを実データで判断する

パースの精度は 100% を目指さない。移行後に `show` / `list` CLI でサンプル確認し、調整するサイクルで進める。

### 移行ワークフロー（推奨）

1. 入力 CSV をパース → 各銘柄ごとに `create_research_record(...)` と `create_snapshot(..., data_source="migration")` を組み立てる
2. `upsert_research_record(record)` でレコード全体を一括書き込み（スナップショットも含めて）
3. 処理後に `backup_research_db()` を呼んで初期スナップショットを保存
4. `python research_shelve.py show <code_s>` で代表銘柄のサンプル確認 → パーサーを調整 → 再実行

---

## 6. CLIインターフェース

Phase 1では最低限のCLIコマンドで操作できるようにする。

- 特定銘柄の調査データ表示（スナップショット時系列含む）
- 評価やキーワードでの銘柄一覧表示
- 移行スクリプトの実行
- バックアップ

---

## 7. スコープ外（Phase 2以降）

- Webビュー（FastAPI + ブラウザでの閲覧・編集UI）
- 保有銘柄シートの移行・管理機能
- 銘柄間の比較ビュー
- バックテスト用データとしての活用
- 売りシグナルの自動生成

---

## 8. 実装順序

1. research_shelve 基盤モジュール + CLIコマンド **✅ 完了（issue #91 / PR #107）**
2. 移行スクリプト（パース + 一括移行） → **issue #92 で対応**
3. 実データで移行実行 → 目視確認 → 調整 → **issue #93 で対応**
4. Shintakane連携（`list_all_db()` へのスナップショット自動追記） → **issue #94 で対応**
5. 運用開始
