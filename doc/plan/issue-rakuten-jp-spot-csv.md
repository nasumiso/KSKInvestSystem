# 楽天 assetbalance(JP) 形式 (国内株式のみ) の現物CSV対応

## 背景

ポートフォリオCSV取込で `assetbalance(JP)_20260818_000701.csv` をアップロードすると
`CSV種別を判別できません` エラーになる。

原因は「楽天現物」の判別が `■ 保有商品詳細` というセクション見出しに依存しており
(`import_portfolio_csv.py:104-107`)、`(JP)` 版にはこの行が存在しないため。
`(JP)` は楽天証券の「国内株式」保有残高CSVで、既対応の `(all)` (すべての資産) とは
別レイアウトの別ファイル。

システムは国内株のみを対象とする (`parse_rakuten_spot` は米国株行を捨てている) ため、
`(JP)` 版のほうが本来適している。今後は `(JP)` を主に使う想定。

## フォーマット差分

| | `assetbalance(all)` (既対応) | `assetbalance(JP)` (今回追加) |
|---|---|---|
| 1行目 | `■資産合計欄` | `■現在の評価額合計［円］` |
| セクション見出し | `■ 保有商品詳細 (すべて）` | `■特定口座` (口座区分ごと) |
| ヘッダ行 | `種別,銘柄コード・ティッカー,銘柄,口座,保有数量,...` | `銘柄コード,銘柄名,保有数量［株］,執行中［株］,(内訳...),(内訳...),平均取得価額［円］,...` |
| 種別列 | あり (`国内株式`/`米国株式` で絞る) | なし (国内株のみ) |
| 口座列 | あり (row[3]) | なし → セクション見出しから導出 |
| 銘柄コード | row[1] | row[0] |
| 保有数量 | row[4] | row[2] |
| 平均取得価額 | row[6] | row[6] |
| 末尾 | — | `,,,,,,特定口座合計,...` の合計行あり |

実データ (2026-08-18 取得) の末尾行:

```
,,,,,,特定口座合計,"9,166,700",,,"10,365,050","1,198,350"
```

## 方針 (ユーザー確認済み)

1. **両フォーマットをサポートし続ける**。既存 `parse_rakuten_spot` は変更せず、
   `(JP)` 用パーサーを追加する。どちらのCSVでも `("楽天", "現物")` を返すので、
   両方を同時にアップロードした場合は `import_csvs` の既存の重複ソースチェックで弾かれる。
2. **口座区分はセクション見出しから機械的に抽出**する。
   `■特定口座` → `特定`、`■NISA口座` → `NISA` のように、先頭 `■` と末尾 `口座` を除去。
   未知の見出しでもエラーにせず取り込む (実物の NISA 見出し文字列が未確認のため、
   ホワイトリスト方式だと NISA 保有発生時に必ず一度エラーになる)。

## 実装

### 1. 定数追加 (`import_portfolio_csv.py`)

```python
# 楽天現物 (JP版: 国内株式のみ) CSV のマーカー
RAKUTEN_JP_FIRST_ROW_MARKER = "■現在の評価額合計"
RAKUTEN_JP_HEADER_FIRST_COL = "銘柄コード"
RAKUTEN_JP_HEADER_MARKER = "保有数量［株］"
RAKUTEN_JP_SECTION_SUFFIX = "口座"
```

`RAKUTEN_JP_FIRST_ROW_MARKER` は `［円］` を含めず前方一致にする
(単位表記の揺れに引きずられないため)。

### 2. `detect_source` に分岐追加

既存の分岐順序 (SBI → 楽天現物(all) → 楽天信用) の**楽天信用の後**に追加する。
判別条件は「1行目が `■現在の評価額合計` で始まる」かつ「`銘柄コード` から始まり
`保有数量［株］` を含むヘッダ行を持つ」の AND。

他ソースとの衝突がないことは実データで確認済み:

- SBI 現物/信用 → 2行目が `保有証券一覧` / `信用建玉一覧` で先に判別される
- 楽天信用 (JP) → 1行目が `■表示形式`
- 楽天現物 (all) → 1行目が `■資産合計欄`

### 3. `parse_rakuten_jp_spot` を追加

`parse_sbi_spot` と同じ「セクション見出しから account を導出する」構造にする
(`_iter_sbi_spot_section_headers` が同じ問題を既に解いている)。JP版は列構成が
違うので専用のヘッダ走査ヘルパー `_iter_rakuten_jp_section_headers` を書く。

処理:

1. 全行を走査し、`■...口座` 形式の見出しで `current_account` を更新
   (`■特定口座` → `特定`)。`■現在の評価額合計` `■評価損益合計` など
   `口座` で終わらない `■` 行は見出しとして扱わない。
2. `銘柄コード` から始まり `保有数量［株］` を含む行をヘッダ行として記録し、
   そのときの `current_account` を紐付ける。
3. ヘッダ行が1つも無ければ `ValueError`。
4. 各セクションのデータ行 (次のヘッダ行の手前まで) を読む。
   - row[0] が空、または合計行 (`特定口座合計` のように row[0] が空で
     途中の列に `合計` が入る) はスキップ。**row[0] が空ならスキップ**の
     条件で合計行は自然に落ちる。
   - `ps.validate_code_s` → 不正なら `ValueError` (既存パーサーと同じ厳格さ)
   - `ps.is_etf_code` なら skip
   - qty = row[2] (`_parse_qty` 経由、カンマ除去)
   - avg_price = row[6] (float 化失敗時は None、既存と同じ)
   - `kind` は `"現物"` 固定

`account` は既存と同様、空なら `"特定"` にフォールバック。

### 4. `PARSERS` への登録

`PARSERS` は `(broker, kind)` がキーで、JP版も `("楽天", "現物")` なので
キーが衝突する。`detect_source` が返す `(broker, kind)` だけではどちらの
パーサーを使うか決められない。

**対応**: `detect_source` の戻り値は `(broker, kind)` のまま変えず
(下流の `EXPECTED_POSITION_SOURCES` 突合・`position_source` 保存・
missing/carried_over 判定がすべてこのキーに依存しているため)、
パーサー選択だけを別関数 `select_parser(source, rows)` に切り出す。

```python
def select_parser(source, rows):
    """(broker, kind) と行内容からパーサーを選ぶ。

    楽天現物は (all) 版と (JP) 版の2フォーマットがあり、どちらも
    ("楽天", "現物") なので、行内容で使い分ける。
    """
    if source == ("楽天", "現物") and _is_rakuten_jp_spot(rows):
        return parse_rakuten_jp_spot
    return PARSERS[source]
```

`import_csvs` の `parser = PARSERS[source]` を
`parser = select_parser(source, rows)` に置き換える。
`_is_rakuten_jp_spot(rows)` は `detect_source` の JP 判別条件をそのまま
共有するヘルパーとして切り出し、両者で使う (判定ロジックの二重定義を避ける)。

## 影響範囲

- `scripts/import_portfolio_csv.py` のみ。WebApp 側 (アップロード受け口・確認画面) は
  `import_csvs` の戻り値の形が変わらないため無変更。
- 既存の `(all)` 版の判別・パースは一切変更しない → 既存テストは全て通る想定。
- `position_source` の `(broker, kind)` は `("楽天", "現物")` のままなので、
  DB スキーマ・`EXPECTED_POSITION_SOURCES`・引き継ぎ (carried_over) ロジックへの
  影響なし。マイグレーション不要。

## テスト (`tests/test_import_portfolio_csv.py`)

追加は 3 本に抑える (CLAUDE.md のテスト方針: 1PR 5本以下、parametrize で集約)。

1. `RAKUTEN_JP_SPOT_ROWS` fixture を実CSVの縮図として追加。
   `■特定口座` と `■NISA口座` の 2 セクション、ETF 行 (1681)、末尾の合計行を含める。
2. 既存の `test_detect_source` の parametrize に
   `(RAKUTEN_JP_SPOT_ROWS, ("楽天", "現物"))` を 1 ケース追加 (新規テスト関数は不要)。
3. `test_parse_rakuten_jp_spot_sections_and_summary_row`:
   セクションごとの account 付与 (`特定`/`NISA`)、合計行スキップ、ETF 除外、
   qty/avg_price の値を 1 本で検証。
4. `test_select_parser_switches_rakuten_spot_format`:
   `(all)` 行と `(JP)` 行で `select_parser` が別のパーサーを返すことを確認。

### 実データでの検証

ユニットテストに加えて、実ファイルで取込 dry-run を通す:

```bash
cd scripts && python import_portfolio_csv.py \
  --csv "~/Downloads/assetbalance(JP)_20260818_000701.csv" \
  --csv "~/Downloads/marginbalance(JP)_20260818_000712.csv" \
  --as-of 2026-08-18 --dry-run
```

(実際のCLIオプション名は実装時に `import_portfolio_csv.py` の argparse を確認して合わせる)

現物13銘柄が全て `特定` で読めること、合計行が混入しないこと、
評価額合計が CSV 記載の 10,365,050 と整合することを確認する。

## 未確認事項

- **NISA口座の見出し文字列**: 現在の楽天国内株保有は全て特定口座のため、
  `■NISA口座` という表記は推測。機械的抽出 (`■` と末尾 `口座` を除去) なので
  `■NISA成長投資枠口座` のような表記でも `NISA成長投資枠` として取り込め、
  エラーにはならない。ただし account 文字列が `(all)` 版の `NISA成長投資枠` と
  一致するかは実物が出るまで不明。position レイヤーでは account を表示に使わない
  前提 (issue #397 §7) なので、当面の実害はない。
