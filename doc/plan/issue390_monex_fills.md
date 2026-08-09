# issue #390 実装プラン: マネックス証券 取引履歴CSV の fill 取込

## ゴール

マネックス証券の取引履歴CSV (過去データ、一度きりのバックフィル) を既存 fill レイヤーへ
取り込み、売買履歴タブのエピソード損益にマネックス分を合流させる。

検証可能なゴール:
1. `python scripts/import_monex_fills.py <csv> --dry-run` が有効約定のみを fill 化する
2. `/trade-history/import` でマネックスCSVをアップロードすると自動判別されて取り込まれる
3. `show_fill_episodes.py` でマネックス建玉のラウンド損益が表示される

## 実CSVから確定した仕様

サンプル: `~/Downloads/20260101-20260807.csv` (Shift-JIS/CP932, 119行, 25列)

- 1行目は `データ作成日：2026/08/09 15:15:44` のメタ行。**ヘッダは2行目**。
- 列 (0-indexed):

| idx | ヘッダ | 用途 |
|---|---|---|
| 0 | 約定日 | `YYYY/MM/DD` |
| 3 | 商品 | `信用新規`/`信用返済`/`現引`/`株式`/空 |
| 4 | 取引 | `半年新規買い`/`半年返済売り`/`お買付`/`ご売却`/`半年現引` 等 |
| 5 | 銘柄コード | **5桁+パディング空白** (`54710    `, `471A0    `) |
| 7 | 数量（株/口）/返済数量 | |
| 8 | 単価/返済約定単価 | |
| 12 | 受渡金額(円) | 信用返済行では**決済損益**(諸経費控除後) |
| 13 | 建約定日 | 信用返済/現引行のみ |
| 14 | 建単価 | 信用返済/現引行のみ |

### 確定した3つの判断材料

1. **建約定日・建単価が両方入っている** → issue 本文の懸念「無ければ信用は損益算出対象外」は
   不要。楽天と同じ `tate_date`/`tate_price` を渡せばエピソード損益が組める。

2. **受渡金額(12列) は信用返済行では決済損益** (SBI の `settle_pl` と同じ意味)。実測検証:
   - 大同特鋼: (2165-1888)*200 = 55,400 に対し受渡 54,831 (差 -569 = 手数料360+税36+順日歩173)
   - スクリン: (16075-16150)*100 = -7,500 に対し受渡 -8,889 (差 -1,389)

   → 諸経費控除後の実質損益。`settle_pl` として持たせるのが真実源として正しい。

3. **銘柄コードが5桁**。`ps.CODE_S_PATTERN` は `^(?:\d{4}|\d{3}[A-Z])$` なので、
   末尾1桁を落とす正規化が必須 (`54710`→`5471`, `471A0`→`471A`)。これを怠ると全行スキップ。

### 取引区分の全出現 (サンプル119行)

| 商品 | 取引 | 件数 | 扱い |
|---|---|---|---|
| 信用新規 | 半年新規買い | 23 | buy / 信用新規 |
| 信用返済 | 半年返済売り | 28 | sell / 信用返済 |
| 信用新規 | 半年新規売り | 2 | sell / 信用新規 |
| 信用返済 | 半年返済買い | 2 | buy / 信用返済 |
| 現引 | 半年現引 | 1 | buy / 現引 |
| 株式 | お買付 | 1 | buy / 現物 |
| 株式 | ご売却 | 8 | sell / 現物 |
| 株式 | 入庫/出庫 | 12 | **スキップ** (振替であって約定でない) |
| 株式 | 配当金 | 1 | **スキップ** |
| (空) | 源泉徴収税/還付金/銀行出金/ご入金/振替入金 | 31 | **スキップ** |

## 実装

### 1. `scripts/import_monex_fills.py` (新規)

`import_sbi_fills.py` をベースに 4層構成 (読込/パース/統合/実行) を踏襲。
共通処理 (`RowSkip` / `_normalize_trade_date` / `_parse_num`) は `import_rakuten_fills`
から再利用 (SBI と同じ依存の張り方)。

```python
CSV_ENCODING = "shift_jis"
EXPECTED_COL_COUNT = 25
HEADER_FIRST_COL = "約定日"
HEADER_MARKER = "建単価"   # 楽天/SBI に無い列名でヘッダ行を同定
BROKER = "マネックス"

# (商品, 取引) → (side, trade_kind)。trade_kind は楽天語彙に合わせる
_ACTION_MAP = {
    ("信用新規", "半年新規買い"): ("buy",  "信用新規"),
    ("信用新規", "半年新規売り"): ("sell", "信用新規"),
    ("信用返済", "半年返済売り"): ("sell", "信用返済"),
    ("信用返済", "半年返済買い"): ("buy",  "信用返済"),
    ("現引",     "半年現引"):     ("buy",  "現引"),
    ("株式",     "お買付"):       ("buy",  "現物"),
    ("株式",     "ご売却"):       ("sell", "現物"),
}
_SETTLE_ACTIONS = frozenset({("信用返済", "半年返済売り"), ("信用返済", "半年返済買い")})
```

`_ACTION_MAP` に無い (商品, 取引) はすべて `RowSkip` → 税金・入出金・入出庫・配当金は
明示的な除外リストを持たずに自然に落ちる。

#### 銘柄コード正規化

```python
def _normalize_monex_code(raw: str) -> str:
    """マネックスの5桁銘柄コード (`54710    `) を4桁 code_s (`5471`) に変換する。

    末尾1桁はチェック用の付加桁 (英字コードは `471A0` → `471A`)。
    5桁でなければそのまま返し、後段の validate_code_s に判定を委ねる。
    """
    s = (raw or "").strip().upper()
    return s[:-1] if len(s) == 5 and s.endswith("0") else s
```

末尾が `0` の5桁のときだけ落とす (誤変換の予防)。サンプル28コード全てが末尾 `0`。

#### パース層

`parse_fill_row` は SBI 版と同構造。差分は:
- `code_s` に `_normalize_monex_code` を適用してから `validate_code_s`
- `_ACTION_MAP` のキーが (商品, 取引) タプル
- `settle_pl` (信用返済のみ) **と** `tate_date`/`tate_price` の両方を返す
- 除外は **ETF除外 (`ps.is_etf_code`) のみ**。SBI 版の `resolve_stock_name` による
  ウォッチリスト外除外は**採用しない** (下記「除外方針」参照)

#### 除外方針: ウォッチリスト外除外を使わない理由

SBI 版 (`import_sbi_fills.py:143`) は `resolve_stock_name(code_s)` が空なら投信等として
スキップする。マネックスは**過去データの一度きりバックフィル**であり、当時売買したが
現在のウォッチリスト (stocks_shelve/research_shelve) に載っていない銘柄が原理的にありうる。
それを落とすと実約定が欠落し、ゴール1「有効約定のみを fill 化」・ゴール3「ラウンド損益」が
成立しない (売りだけ残って建玉が消える等、片肺のエピソードになる)。

サンプルCSVでの実測: 全28コード中、`resolve_stock_name` が空なのは `1540` 純金信託 と
`1681` 上場ＭＳエマ の2件のみで、**どちらも `is_etf_code` が True**。つまり本サンプルでは
ETF除外だけで投信も落ちる。ウォッチリスト外除外は効果ゼロで欠落リスクだけを負う。

→ ETF除外のみを採用。ETF_code.txt に無い投信が将来混ざったら dry-run のスキップ一覧で
気付ける (バックフィルは目視確認前提の一度きり作業)。

#### 統合層

`import_csv_to_fills` は SBI 版とほぼ同一。`ps.create_fill` に
`broker="マネックス"`, `settle_pl`, `tate_date`, `tate_price` を渡す。
dedup 空間分離のため `make_dedup_key` の `baibai_kubun` には `f"{商品}/{取引}"` を渡す。

**`settle_pl` と `tate_price` が両方ある場合の損益計算**: `helpers.py:4292-4301` は
`settle_pl is not None` を優先し、`total_cost` に `tate_price` を使う。つまり
両方渡すのが最良の組み合わせ (損益=諸経費控除後の実額、コスト=建玉簿価) で、
既存ロジックの変更は不要。

### 2. `scripts/webapp/routes/trade_history.py` (1分岐追加)

```python
if rakuten.is_rakuten_csv(tmp_path):
    module = rakuten
elif sbi.is_sbi_csv(tmp_path):
    module = sbi
elif monex.is_monex_csv(tmp_path):
    module = monex
else:
    flash(f"楽天/SBI/マネックス の取引履歴CSVとして認識できませんでした: {filename}", "error")
```

判定衝突の確認:
- 楽天 = 先頭行がヘッダ・28列 → マネックスは先頭行がメタ行なので不一致
- SBI = ヘッダ行 index>0 かつ **14列** → マネックスは25列なので不一致
- マネックス = ヘッダ行 index>0 かつ 25列 かつ `建単価` 列あり

3者は排他。エラーメッセージ文言も更新。

### 3. `tests/test_import_monex_fills.py` (新規、4本)

`tests/test_import_sbi_fills.py` の構成を踏襲。**5本以下**の方針に従う。

1. `test_parse_side_and_kind` — parametrize で (商品,取引) 7種 → side/trade_kind
2. `test_code_normalization_and_etf` — parametrize で `54710`→`5471`, `471A0`→`471A`,
   `15400`(ETF)→スキップ。あわせて**ウォッチリスト外の非ETFコードが取り込まれる**こと
   (除外方針の回帰防止) を確認
3. `test_settle_pl_and_tate_price` — 信用返済行で settle_pl と tate_price が両方入る /
   信用新規行では settle_pl=None
4. `test_skip_non_trade_rows_and_dedup` — 税金・入出庫行がスキップされ、
   再取込が冪等 + broker="マネックス"

### 4. `doc/COMMANDS.md` に `import_monex_fills.py` を追記

楽天/SBI の記載箇所に並べる。

## 実行手順 (バックフィル)

```bash
source .venv/bin/activate
cd scripts
python import_monex_fills.py ~/Downloads/20260101-20260807.csv --dry-run   # 確認
python import_monex_fills.py ~/Downloads/20260101-20260807.csv            # 本取込
python show_fill_episodes.py                                              # 検算
```

サンプルCSV (117データ行) での想定内訳 (実測):

| 区分 | 件数 |
|---|---|
| 取込 (fill 化) | **62** |
| ETF除外 (純金信託・上場ＭＳエマ) | 3 |
| 非約定スキップ (税金・入出金・入出庫・配当金) | 52 |

`--dry-run` の `imported=62` / `skipped_invalid=55` が期待値。

## スコープ外

- マネックスの継続運用 (現在は未使用、一度きりのバックフィル)
- 米国株口座 (`振替入金（米国株口座から）` 行はスキップ)
- 配当金・税金の損益への反映 (既存 fill レイヤーが扱わない)
