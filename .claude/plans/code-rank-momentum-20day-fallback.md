# code_rank.csv モメンタム 20日比 欠落の修正

## 背景

`code_rank.csv` の「モメンタム(現在.20日比/5日比)」列で、20日比が「-」表示になる銘柄が 3854 件中 1805 件 (約 47%) に達している。5日比はほぼ全銘柄で出ている。

## 原因

`get_rs_line_changes_expr()` (`scripts/make_stock_db.py:225-239`) は rs_line が 21 本以上必要 (`_change(20)` が `rs_line[20]` を参照)。
しかし実データでは rs_line ≥ 21 の銘柄は 457/3854 (12%) のみ。

連鎖を遡ると:

| 場所 | 設定 | 問題 |
|---|---|---|
| `scripts/price.py:866` (yfinance) | `period="1mo"` | 約 22 営業日しか取らない |
| `scripts/price.py:1568` (yfinance パス) | `LOG_DAY = 25` | 25 日で切り詰め |
| `scripts/price.py:441` (Kabutan TOPIX パス) | `LOG_DAY = 25` | Kabutan は 30 本返すのに 5 本捨てている |

実データの price_log 長分布: 最頻値 = 10 本 (2171銘柄)、19 本 (1064銘柄)、22 本 (415銘柄)。

## 方針

ユーザー判断で `period="1mo"` は据え置き (yfinance 通信負荷増加を避けたい)。代わりに:

1. **`LOG_DAY = 25 → 30`**: Kabutan は元々 30 本返すので切り詰めロスが消える。yfinance は 1mo のままで現状の取得日数 (≤22) を全部使う形になる。
2. **20日前データが無い場合、15〜19日前で代替**: 表示は末尾 `*` を付けて「20日未満で代替した」ことを明示 (B 案)。

## 変更内容

### Phase 1: LOG_DAY 拡張

ファイル: `scripts/price.py`

- L441: `LOG_DAY = 25` → `LOG_DAY = 30` (Kabutan/共通 `_calc_daily_indicators`)
- L1568: `LOG_DAY = 25` → `LOG_DAY = 30` (yfinance パス `parse_price_text_from_list`)

### Phase 2: rs_line 騰落率のフォールバック

ファイル: `scripts/make_stock_db.py`

#### 用語定義 (オフバイワン回避)

- 「N日前データがある」= `rs_line[N]` にアクセス可能 = `len(rs_line) >= N + 1`
- よって、20日前データには rs_line 長 ≥ 21 が必要。15日前データには長 ≥ 16 が必要。
- 「20日比」の計算 `_change(20)` は `rs_line[20]` を読む。「19日比」なら `_change(19)`、`rs_line[19]` を読む。

#### 変更

- `_rs_line_changes_from_line(rs_line)` (内部関数) の戻り値型を変更:
  - 旧: `(a_short, b_mid)` の `(float|None, float|None)`
  - 新: `(a_short, b_mid, b_is_approx)` の `(float|None, float|None, bool)`
  - `b_mid` 計算ロジック: offset 20 で取れたらそれを採用 (`b_is_approx=False`)。取れなければ offset を 19, 18, 17, 16, 15 と順に下げて `_change(offset)` を試し、最初に取れたものを採用 (`b_is_approx=True`)。offset 15 でも取れなければ `(a_short, None, False)`。
  - 短期 A (`_change(5)`) は変更なし (元々十分取れている)。
  - rs_line 空のとき `(None, None, False)`。

- `_fmt_rs_change(v)` は変更なし (None → "-"、それ以外は "%+d")。

- `get_rs_line_changes_expr()`:
  - 内部関数 `_rs_line_changes_from_line` から `(a, b, b_is_approx)` を受けて、`b` が None でなく `b_is_approx=True` のときに末尾に `*` を追加。
  - 例: 通常時 `+12/+5`、フォールバック時 `+10*/+5`、20日比が完全に取れない場合は `-/+5` (現状通り、* なし)。
  - 両方 None なら `""` を返す現状挙動は維持。

- `compute_rs_line_changes()` (public API) は **後方互換のため 2-tuple `(a, b)` のまま** とする。内部関数からの 3要素戻り値を `(a, b, _)` で受けて 2-tuple に絞って返す。docstring に「rs_line が 16〜20 本のときは b は 15-19日前のうち最も 20 に近いものを使う近似値」を追記。
  - これにより既存テスト (`a, b = compute_rs_line_changes(...)`) は無修正で通る。`b_is_approx` フラグの公開はしない (`get_rs_line_changes_expr` 内部のみで使用)。

### Phase 3: テスト

ファイル: `tests/test_price.py`

- `TestCalcDailyIndicators` の price_log 長期待値 (もしあれば 25 → 30 に修正)。
- `TestParsePriceTextFromList` の期待件数を 25 → 30 に修正。

ファイル: `tests/test_make_stock_db.py`

- `TestComputeRsLineChanges`: **戻り値は 2-tuple `(a, b)` のままなので既存テストは無修正**。
- 新規テストの主軸は `TestGetRsLineChangesExpr` に集中させる (内部フラグ `b_is_approx` の有無は文字列出力の `*` で観測する)。
  - rs_line 21 本 → `"+N/+M"` (末尾 * なし、`_change(20)` で計算)
  - rs_line 20 本 (= index 0..19) → `"+N*/+M"` (offset 19 で計算、末尾 *)
  - rs_line 16 本 (= index 0..15) → `"+N*/+M"` (offset 15 で計算、末尾 *)
  - rs_line 15 本 (= index 0..14) → `"-/+M"` (offset 15 でも届かず None、末尾 * なし)
  - rs_line 6 本 → `"-/+M"` (5日比のみ、現状通り)
  - rs_line 0 本 → `""` (現状通り)
- 追加で `TestComputeRsLineChanges` に 1 件、フォールバックが効くケース (rs_line 20 本 → `b` が None でなく数値) のテストを追加して、`compute_rs_line_changes` 自身もフォールバック値を返すことを担保する。`b_is_approx` は公開しないので、値が None でないことだけ確認。

### Phase 4: 仕様外

- `period="3mo"` 等の通信負荷増は今回スコープ外 (ユーザー判断)。
- 5日比のフォールバックは導入しない (元々ほぼ全銘柄で取れている)。
- フォールバックの上限 (15日) を更に緩める案 (10日まで等) は採用しない。
- mom_pt スコアリング側への組み込みなし。

## 検証手順

1. `pytest tests/test_price.py tests/test_make_stock_db.py -v` でグリーン。
2. `cd scripts && python make_stock_db.py list_all_db` で `code_rank.csv` 再生成。
3. 再生成後の CSV を Python ワンライナーで集計し、20日比 "-" の件数が変更前 (1805) より大幅に減ること、`*` 付き件数を確認。
4. 既存の 20日比が出ていた銘柄について、改修後も値が変わっていないこと (回帰がないこと) を 2-3 件抜き取りで確認。

## リスク

- `compute_rs_line_changes()` の戻り値型は 2-tuple のまま維持するので公開 API 破壊なし (tests/test_make_stock_db.py:590 等の `a, b = compute_rs_line_changes(...)` は無修正で通る)。
- フォールバック許容の下限 (15日) は実データ分布から見て妥当だが、極端に薄い銘柄では誤読を招く可能性あり。表示の `*` で明示することで緩和。
- yfinance `period="1mo"` は据え置きのため、LOG_DAY=30 にしても銘柄側 price_log は実質 22 営業日程度。それでも 21 本確保すれば足りるが、月初・連休直後のタイミングでは確保数が減りフォールバック発動率が上がる可能性あり。これは許容する設計とする。
