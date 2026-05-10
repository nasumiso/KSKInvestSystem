# issue #177 実装計画: 保有銘柄ダッシュボードに条件付き書式 (色分け) を移植

> 親 issue: #168 (Phase 3 全体)
> 直前: #171 (Phase 3b 一覧ダッシュボード) / PR #176 (マージ済み)
> 兄弟: #175 (memo 編集機能、PR #180 でマージ済み)
> ベースブランチ: `main` (Phase 3 系列はすべて main に取り込み済み)

---

## 1. スコープと前提

### 1-1. 目的
Phase 3b で一覧ダッシュボード `/portfolio` を実装したが、現状は色分けが未実装で「数値の羅列」として一覧性が落ちている。スプシ「保有銘柄」シートの条件付き書式を移植して、視認性を回復する。

### 1-2. 翻訳元データ
ユーザーが Apps Script で抽出したスプシ条件付き書式ダンプ (33 ルール) と、列対応 (A〜AJ) を元に翻訳する。`<C3>` タグ等の業務的意味についてはユーザーから直接ヒアリング済み。

### 1-3. 本 PR でやること
- ルール翻訳結果のドキュメント化 (`doc/PORTFOLIO_COLOR_RULES.md`)
- `webapp/helpers.py` に `compute_cell_styles(row) -> dict[str, str]` を新設
- `_extract_indicators_for_portfolio()` の戻り値に色判定用の生値 (`*_raw`) と業績クォリティ式を追加
- `list_portfolio_with_indicators()` で `row["styles"]` を計算して詰める
- `webapp/templates/portfolio_list.html` の各 `<td>` に `style="{{ row.styles.<col> }}"` を付与
- ユニットテスト + ブラウザ目視確認

### 1-4. スコープ外 (別 issue)
- AA 列 (株価向き) の中身実装と色付け (ルール 11, 12) — Q2 でユーザー確認済み
- X 列 (RSライン) の中身実装と色付け (ルール 18, 19) — 同上
- 銘柄調査シート全体 (ルール 1〜3) のWebApp 側対応 — `/portfolio` には E 列 (指標) を表示していない

### 1-5. ブランチ戦略
- ブランチ名: `issue-177-portfolio-color-rules`
- base: `main` (Phase 3 系列はすべて取り込み済み)
- スタックなし

---

## 2. 翻訳ルール一覧 (確定版)

スプシ「保有銘柄」シートの 33 ルールのうち、本 PR で実装するのは **29 ルール** (4 ルール = AA列ルール 11,12 と X列ルール 18,19 はスコープ外)。さらに銘柄調査シートの 3 ルールも対応外 (`/portfolio` は E列「指標」を表示していないため)。

### 2-1. 色定数 (`PORTFOLIO_COLORS`)

```python
PORTFOLIO_COLORS = {
    "薄黄": "#fce8b2",   # 良 (PER低い、配当>3、RS≧70 等)
    "濃黄": "#fbbc04",   # 強良 (順位<300、配当≧5、RS>80 等)
    "薄赤": "#f4c7c3",   # 警告 (ステージ2S、3Q連続向上タグ)
    "青":   "#4285f4",   # 警告シグナル (警/売、RS減速)
    "赤":   "#ea4335",   # 強警告シグナル (ポ/ブ/最)
    "薄灰": "#cccccc",   # データ古い (14日以上)
    "濃灰": "#999999",   # データ古い (1ヶ月以上)
    "水色": "#6fa8dc",   # データなし/低スコア (買い集めDD以下、トレンド空)
}
```

### 2-2. ルール優先順位

スプシは「上から順に最初にマッチしたルールが適用」される。WebApp 側でも同じ挙動を再現する。
特に同一列に複数ルールがある場合 (RS, 配当, 更新日, 決算日, トレンド) は **強い色から先に評価** する if/elif 構造で実装する。

### 2-3. 29 ルール翻訳テーブル

| # | テンプレ列 | 条件 | 色 | 元ルール |
|---|---|---|---|---|
| 1 | 順位 (`rank`) | `rank < 300` | 濃黄 | 14, 31 (※元ルールは行範囲分割だがユーザー確認済み: 業務的意味なし、統合してよい) |
| 2 | 売上成長 (`sales_growth`) | `≧ 30` | 薄黄 | 17 |
| 3 | 利益成長 (`profit_growth`) | `≧ 30` | 薄黄 | 17 |
| 4 | PER (`per`) | `(profit_growth + dividend) / per > 1` (※PEG的指標: 利益成長率% + 配当利回り% を PER で割って 1 超なら割安) | 薄黄 | 16 |
| 5 | 理論株価乖離 (`theoretical_diff`) | `> 50` | 薄黄 | 15 |
| 6 | 配当 (`dividend`) | `≧ 5` | 濃黄 | 32 |
| 7 | 配当 (`dividend`) | `> 3` (≧5でない場合) | 薄黄 | 33 |
| 8 | 進捗率乖離 (`progress_diff`) | 業績クォリティに `<C3>` タグ | 薄赤 | 9 |
| 9 | 進捗率乖離 (`progress_diff`) | 営利乖離 ≧ 20 | 濃黄 | 10 |
| 10 | 決算日 (`kessanbi_md`) | 更新日±1ヶ月以内 **かつ** 3Q | 濃黄 | 22 |
| 11 | 決算日 (`kessanbi_md`) | 更新日±1ヶ月以内 (3Q条件なし) | 薄黄 | 23 |
| 12 | 更新日 (`memo.last_research_update`) | 14 日以上前 | 薄灰 | 1 |
| 13 | 更新日 (`memo.last_research_update`) | 1 ヶ月以上前 | 濃灰 | 8 |
| 14 | ステージ (`memo.stage`) | "2S" 含む | 薄赤 | 13 |
| 15 | RS (`rs`) | `> 80` | 濃黄 | 27 |
| 16 | RS (`rs`) | `≧ 70` (>80 でない場合) | 薄黄 | 28 |
| 17 | トレンド (`trend_template`) | 空欄 / "—" | 水色 | 24 |
| 18 | トレンド (`trend_template`) | "◎" 含む | 濃黄 | 25 |
| 19 | トレンド (`trend_template`) | "◯" 含む (◎でない場合) | 薄黄 | 26 |
| 20 | シグナル (`tags`) | "警" 含む | 青 | 2 |
| 21 | シグナル (`tags`) | "売" 含む | 青 | 3 |
| 22 | シグナル (`tags`) | "押" 含む (文字色のみ) | 青 (文字色) | 4 |
| 23 | シグナル (`tags`) | "ポ" 含む | 赤 | 5 |
| 24 | シグナル (`tags`) | "ブ" 含む | 赤 | 6 |
| 25 | シグナル (`tags`) | "最" 含む | 赤 | 7 |
| 26 | 買い集め (`buy_collection`) | スコア合計 ≧ 8 (AAランク以上) | 濃黄 | 20 |
| 27 | 買い集め (`buy_collection`) | スコア合計 ≦ 4 (DD以下) | 水色 | 21 |
| 28 | 時価総額 (要 `_category`) | カテゴリ "中" | 薄黄 | 29 |
| 29 | 時価総額 (要 `_category`) | カテゴリ "大" | 薄黄 | 30 |

### 2-4. シグナル列の特殊ルール (ルール 22 = 文字色のみ)

シグナル列の "押" だけ背景色なしで文字色のみ青。他のシグナル ("警/売/ポ/ブ/最") は背景色。

→ 背景色と文字色を **両方持つスタイル文字列** にする (`"background:#4285f4;color:#fff"` のように)。"押" のみ `"color:#4285f4"`。

→ 複数シグナル混在時 (例: "警/押") は **強い順 (赤 > 青背景 > 青文字色)** で評価し、最初にマッチしたものを採用する。

### 2-5. 買い集めスコア計算

`buy_collection` は `"C,C"` 形式 (左右の文字 + カンマ区切り)。スコア:

```python
SCORE = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
def buy_collection_score(s):
    if not s or "," not in s:
        return None
    left, right = s.split(",", 1)
    return SCORE.get(left.strip(), 0) + SCORE.get(right.strip(), 0)
```

### 2-6. 時価総額カテゴリ計算

`market_cap_raw` (生値、億円単位) からカテゴリ算出:

```python
def market_cap_category(billion_yen):
    if billion_yen is None:
        return None
    if billion_yen < 100:
        return "極小"
    if billion_yen < 400:
        return "小"
    if billion_yen < 1000:
        return "中"
    if billion_yen < 3000:
        return "大"
    return "特大"
```

色付け対象は **"中"** と **"大"** のみ (極小/小/特大は色なし)。

### 2-7. 業績クォリティ式と `<C3>` 判定

`gyoseki.get_gyoseki_quarity_expr(stock)` は `[A]25±0%,41±7%[Q]25±1%,37±7%<C3>` のような文字列を返す。末尾に `<C3>` が連結されていれば「3Q連続利益率向上」マーカー。
**stocks_shelve には未保存** のため、`_extract_indicators_for_portfolio` 内で都度計算する。

```python
from gyoseki import get_gyoseki_quarity_expr  # 遅延 import (循環回避)
quarity_expr = get_gyoseki_quarity_expr(stock)  # 例: "...<C3>"
has_c3_tag = "<C3>" in (quarity_expr or "")
```

### 2-8. 更新日 (`last_research_update`) のパース

`row.memo.last_research_update` は "M/D" 形式 (年なし、例 `"4/27"`)。「14日以上前」「1ヶ月以上前」を判定するには年補完が必要。

**方針**: 「最も最近の M/D に該当する日付」(= 未来日になるなら去年扱い)

```python
def parse_research_update_md(md_str, today):
    """'4/27' → date オブジェクト。today より未来なら去年扱い。

    today: 基準日 (省略時は呼び出し側 = compute_cell_styles のデフォルトで埋まる)。
    """
    if not md_str or md_str == "—":
        return None
    try:
        m, d = md_str.split("/")
        candidate = date(today.year, int(m), int(d))
        if candidate > today:
            candidate = date(today.year - 1, int(m), int(d))
        return candidate
    except (ValueError, AttributeError):
        return None
```

「14日以上前」= `(today - parsed).days >= 14`、「1ヶ月以上前」= `(today - parsed).days >= 30` (簡易判定)。`today` は `compute_cell_styles` から伝播。

---

## 3. データソース拡張 (`_extract_indicators_for_portfolio`)

色判定には **生値** が必要。表示用文字列とは別に `*_raw` フィールドを追加する。

### 3-1. 追加する raw フィールド

| 既存表示フィールド | 追加 raw フィールド | 元データ |
|---|---|---|
| `per: "23.0"` | `per_raw: 23.0` | `shihyo["PER"]` (float) |
| `market_cap: "4960億"` | `market_cap_raw: 4960.0` | `stock["market_cap"]` (float) |
| `dividend: "1.88%"` | `dividend_raw: 1.88` | `shihyo["dividend_yield"]` (float) |
| `rs: "98"` | `rs_raw: 98` | `stock["momentum_pt"]` (int) |
| `sales_growth: "39%"` | `sales_growth_raw: 39` | `_annual_growth(stock)[0]` (int) |
| `profit_growth: "232%"` | `profit_growth_raw: 232` | `_annual_growth(stock)[1]` (int) |
| `progress_diff: "+3/+15"` | `progress_diff_eiri_raw: 15` | `_progress_quarter_and_diff(stock)` の右側 |
| `theoretical_diff: "594%"` | `theoretical_diff_raw: 594` | `_format_theoretical_diff` の元値 |
| `rank: 20` | (既に int) | — |
| `trend_template: "◎"` (例) | (文字列のままでOK、判定は `"◎" in trend_template`) | — |
| `tags: "警/売"` | (文字列のままでOK、判定は `"警" in tags`) | — |
| `buy_collection: "C,C"` | (文字列のままでOK) | — |
| `kessanbi_md: "05/11"` | `kessanbi_raw: date(2026, 5, 11)` | `_parse_kessanbi(stock["kessanbi"])` |

### 3-2. 追加する派生フィールド

| 派生フィールド | 計算元 |
|---|---|
| `gyoseki_quarity_expr: "...<C3>"` | `gyoseki.get_gyoseki_quarity_expr(stock)` |
| `market_cap_category: "中"` | `market_cap_raw` から calc |

`last_research_update_dt` は `compute_cell_styles` 内で `today` を見ながら計算するため、row には raw な M/D 文字列のみ持たせる (= 既存の `row.memo.last_research_update`)。事前に row へ詰めると基準日が `_extract_indicators_for_portfolio` 呼び出し時点で固まってしまい、テストで日付を注入できなくなる。

### 3-3. パース失敗時の挙動

すべての `*_raw` は **None 許容**。パース失敗・データ未取得時は None。`compute_cell_styles` 内で `None` チェックを行い、None ならスタイルなし。

---

## 4. `compute_cell_styles(row)` の実装

### 4-1. シグネチャ

```python
def compute_cell_styles(row: Dict[str, Any], today: Optional[date] = None) -> Dict[str, str]:
    """row の生値から各セルの inline style 文字列を返す。

    Args:
        row: list_portfolio_with_indicators が組み立てた表示用 dict (raw フィールド含む)
        today: 基準日 (省略時は date.today())

    Returns:
        dict[列名, style 文字列]。色なしの列は dict に含めない (テンプレ側は or "" でフォールバック)。
        例: {"per": "background:#fce8b2", "rs": "background:#fbbc04", "tags": "background:#ea4335"}
    """
    if today is None:
        today = date.today()
    styles = {}
    # 各列の判定 (詳細は 4-2 以降)
    return styles
```

**基準日について (CLAUDE.md 規約に対する明示的な例外合意)**:

CLAUDE.md L28 は「日付判定は `ks_util.get_price_day()` を使用 (18:00 前は前日扱い)」を規約化しているが、**本機能 (色付け) のみ `date.today()` の使用を許可する** ことをユーザーと合意済み。

理由:
- 色付けは UI の視認性補助であり、価格データ整合性 (`get_price_day` の本来用途) と無関係
- 「14日以上前 / 1ヶ月以上前 / 決算日±1ヶ月」の判定は日単位で十分粒度が粗く、18:00 境界で 1 日ズレても運用影響なし
- 価格データに連動する処理 (price.py / make_stock_db.py 等) では引き続き `get_price_day()` を使用 — 規約は機能カテゴリで分けて運用

テストでは `today` を明示的に渡せる引数として残す (固定日付を注入してテスト可能)。

### 4-2. 列ごとの判定実装パターン

優先順位を if/elif で記述。Python の最初マッチ採用がスプシ挙動と整合。

```python
# 例: 配当 (ルール 32, 33)
dividend_raw = row.get("dividend_raw")
if isinstance(dividend_raw, (int, float)):
    if dividend_raw >= 5:
        styles["dividend"] = f"background:{PORTFOLIO_COLORS['濃黄']}"
    elif dividend_raw > 3:
        styles["dividend"] = f"background:{PORTFOLIO_COLORS['薄黄']}"

# 例: シグナル (ルール 2-7)
tags = row.get("tags") or ""
if any(c in tags for c in ("ポ", "ブ", "最")):
    styles["tags"] = f"background:{PORTFOLIO_COLORS['赤']};color:#fff"
elif any(c in tags for c in ("警", "売")):
    styles["tags"] = f"background:{PORTFOLIO_COLORS['青']};color:#fff"
elif "押" in tags:
    styles["tags"] = f"color:{PORTFOLIO_COLORS['青']}"

# 例: 進捗率乖離 (ルール 9, 10)
quarity = row.get("gyoseki_quarity_expr") or ""
eiri_raw = row.get("progress_diff_eiri_raw")
if "<C3>" in quarity:
    styles["progress_diff"] = f"background:{PORTFOLIO_COLORS['薄赤']}"
elif isinstance(eiri_raw, (int, float)) and eiri_raw >= 20:
    styles["progress_diff"] = f"background:{PORTFOLIO_COLORS['濃黄']}"
```

### 4-3. テンプレート適用

各 `<td>` に `style="{{ row.styles.<col> }}"` を追加。`row.styles` は dict なので、Jinja の `{{ row.styles.per or "" }}` のように or "" で空安全にする (色なしの列は何も style 属性が出ない、または空 style が出る)。

```html
<td style="{{ row.styles.per or '' }}">{{ row.per }}</td>
<td style="{{ row.styles.dividend or '' }}">{{ row.dividend }}</td>
...
```

色付け対象列 14 列 + 既存 style があった列 (trend_template, tags の overflow) は既存 style と共存させる必要がある:

```html
<!-- trend_template は既存に max-width 等の style あり、merge する -->
<td title="{{ row.trend_template_tooltip }}"
    style="max-width:6em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;{{ row.styles.trend_template or '' }}">
  {{ row.trend_template }}
</td>
```

---

## 5. ファイル構成

### 5-1. 新規

| ファイル | 内容 |
|---|---|
| `doc/PORTFOLIO_COLOR_RULES.md` | 翻訳ルール一覧 (29 ルール + ヘッダ + 凡例) |

### 5-2. 修正

| ファイル | 変更内容 |
|---|---|
| `scripts/webapp/helpers.py` | `PORTFOLIO_COLORS` 定数 + `compute_cell_styles()` + `_extract_indicators_for_portfolio` への raw / 派生フィールド追加 + `list_portfolio_with_indicators` で `row["styles"]` 計算 |
| `scripts/webapp/templates/portfolio_list.html` | 14 列の `<td>` に `style` 属性追加 |
| `tests/test_webapp_helpers.py` | `compute_cell_styles` のユニットテスト |

---

## 6. テスト戦略

### 6-1. ユニットテスト (`tests/test_webapp_helpers.py`)

`compute_cell_styles()` を直接呼び、境界値で色が切り替わるか確認。

```python
class TestComputeCellStyles:
    def test_dividend_5_percent_or_more_is_strong_yellow(self):
        styles = compute_cell_styles({"dividend_raw": 5.0})
        assert styles["dividend"] == "background:#fbbc04"

    def test_dividend_between_3_and_5_is_light_yellow(self):
        styles = compute_cell_styles({"dividend_raw": 3.5})
        assert styles["dividend"] == "background:#fce8b2"

    def test_dividend_3_or_less_no_color(self):
        styles = compute_cell_styles({"dividend_raw": 3.0})
        assert "dividend" not in styles

    def test_dividend_none_no_color(self):
        styles = compute_cell_styles({"dividend_raw": None})
        assert "dividend" not in styles

    def test_signal_red_takes_priority_over_blue(self):
        styles = compute_cell_styles({"tags": "警/ポ"})
        assert "background:#ea4335" in styles["tags"]  # 赤優先
    ...
```

### 6-2. 主要テストケース (各ルールに 2〜3 ケース)

各ルールについて:
- 境界値 (条件ぎりぎり満たす / 満たさない)
- データなし (None / 空文字)
- 優先順位 (同一列の複数ルールがある場合)

合計 50〜60 ケース想定。

### 6-3. 既存テスト回帰確認

```bash
.venv/bin/pytest tests/test_webapp_helpers.py tests/test_webapp_portfolio_routes.py -v
.venv/bin/pytest tests/ -v -m "not local_db and not live_html"  # 全体回帰
```

特に `_extract_indicators_for_portfolio` の戻り値 dict にキーが増えるので、これを参照する `list_portfolio_with_indicators` のテストが既存にあれば壊れていないか確認。

### 6-4. 手動確認

1. `cd scripts && python -m webapp.app` (port 5001 が常駐していれば 5003 等で起動)
2. `/portfolio` を開く
3. スプシ「保有銘柄」シートのスクショと並べて、同じ銘柄が同じ色になっているか目視確認
4. 主要なケースをカバー: PER 良/悪、配当 ≧5/>3/それ以下、RS >80/≧70/<70、シグナル 警/ポ/押、買い集め AA/CC/EE、時価総額 中/大/特大

---

## 7. 実装順序

1. **データソース拡張**
   - `_extract_indicators_for_portfolio` に raw フィールド + `gyoseki_quarity_expr` + `market_cap_category` 追加
   - 単発テスト: dummy stock dict から raw フィールドが正しく出るか

2. **`PORTFOLIO_COLORS` 定数 + `compute_cell_styles()` 実装**
   - 29 ルール分の if/elif をカテゴリごとに記述
   - パース失敗 (None) の安全性確保

3. **`list_portfolio_with_indicators` で row["styles"] を詰める**
   - 各 row に対して `compute_cell_styles(row, today)` を呼んで `row["styles"] = {...}`

4. **ユニットテスト書く**
   - `tests/test_webapp_helpers.py::TestComputeCellStyles` 各ルール 2〜3 ケース
   - 全 pass を確認

5. **テンプレート修正**
   - `portfolio_list.html` の 14 列に `style` 属性追加
   - 既存 style (max-width 等) と merge

6. **既存テスト回帰確認**
   - `pytest tests/ -v -m "not local_db and not live_html"`

7. **手動確認**
   - webapp 起動 → ブラウザでスプシスクショと比較
   - 5〜10 銘柄分の色が一致することを目視確認

8. **`doc/PORTFOLIO_COLOR_RULES.md` を起こす**
   - 29 ルールの翻訳結果と元ルール番号、再現手順を記録

---

## 8. リスク・オープンクエスチョン

### 8-1. 業績クォリティの都度計算コスト

`get_gyoseki_quarity_expr(stock)` は `gyoseki_current` `gyoseki_quarter` を見て統計計算する。1 銘柄あたり数 ms 程度と予想。1 タブあたり最大 200 行程度なので合計 1秒以内に収まる見込み。
もし遅ければ `make_stock_db.py` 側で stocks_shelve に保存する別 issue を立てる (本 PR では未対応)。

### 8-2. テンプレート Jinja の HTML エスケープ

`style="{{ row.styles.per }}"` で `row.styles.per` が `'background:#fce8b2'` のような文字列。Jinja2 はデフォルトで HTML エスケープするが、`#` `:` は HTML 特殊文字でないので問題なし。XSS 対策は `compute_cell_styles` 内でカラーコード以外の文字列を返さないことで担保。

### 8-3. 色なしの列で空 style 属性が出る問題

`<td style="">` は実害なし (CSS 効かない)。ただし HTML が冗長なので、Jinja で `{% if row.styles.per %}style="{{ row.styles.per }}"{% endif %}` のように分岐する手もある。

→ **方針**: 簡素化のため空 style は許容 (`or ""` で空文字を出す)。

### 8-4. 既存 style (max-width 等) との共存

trend_template と tags は現状 inline style で max-width / overflow / text-overflow / white-space を指定済み。これに background 等を追加する形になる。

→ Jinja で連結:

```html
<td style="max-width:6em;...{{ row.styles.trend_template or '' }}">
```

末尾セミコロン省略は OK (CSS 仕様)。

### 8-5. 14日以上前 / 1ヶ月以上前の境界

ユーザー目線で「今日から N 日以上前」なので `(today - parsed).days >= 14` で OK。
`today` は `date.today()` をそのまま使う (色付けは日単位の粒度で十分、18:00 境界判定は不要)。CLAUDE.md L28 の `get_price_day()` 規約に対する例外合意は §4-1 末尾に明文化済み。

---

## 9. Definition of Done (issue #177)

- [ ] `doc/PORTFOLIO_COLOR_RULES.md` に 29 ルールが文書化されている (元ルール番号、列、条件、色を含む)
- [ ] `compute_cell_styles()` のユニットテストが green (各ルール 2〜3 ケース、合計 50+ )
- [ ] `pytest tests/ -v -m "not local_db and not live_html"` 全 pass (回帰なし)
- [ ] WebApp の `/portfolio` で対象列がスプシと同等の色分けで表示される
- [ ] スプシのスクショと WebApp の見た目を並べて、ユーザー目視 OK

### スコープ外 (本 PR に含まない)

- AA 列 (株価向き) の中身実装と色付け (ルール 11, 12)
- X 列 (RSライン) の中身実装と色付け (ルール 18, 19)
- 銘柄調査シート全体のWebApp 化 (ルール 1〜3)
- `make_stock_db.py` での `gyoseki_quarity_expr` の stocks_shelve 保存 (パフォーマンス問題が顕在化したら別 issue)
- CSS class 化によるテーマ切り替え対応 (将来必要になったら別 issue)

---

## 10. 開発コマンド

```bash
# テスト
.venv/bin/pytest tests/test_webapp_helpers.py::TestComputeCellStyles -v
.venv/bin/pytest tests/ -v -m "not local_db and not live_html"  # 回帰

# ローカル起動 (動作確認)
cd scripts && python -m webapp.app  # port 5001 (5001常駐中なら別ポート)
# → http://localhost:5001/portfolio
```
