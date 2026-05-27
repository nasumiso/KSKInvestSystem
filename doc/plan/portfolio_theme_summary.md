# プラン: ポートフォリオ — 業態テーマ別サマリー表示

## 目的

監視ユニバース (portfolio_shelve に登録された全銘柄、保有 / 準保有 / 監視) を、ユーザーが手動で付けた **業態・テーマ** (`memo["gyoutai_themes"]`) でグルーピングし、テーマごとに既存指標 `momentum_pt` を集約して「自分の投資仮説ごとのテーマ強弱」を一目で把握できる画面を webapp に追加する。

O'Neil / MarketSmith のセクター RS 表示思想 (テーマ別 RS テーブル + リーダー株の同行表示) を参考にしつつ、用途を「市場全体のテーマローテーション検知」ではなく **「自分が組んだ投資仮説の事後評価 / 銘柄入れ替え判断」** に限定する。

### 既存資産との役割分担

| 機能 | 用途 | 構成銘柄ソース |
|---|---|---|
| `market_db["theme_rank"]` (既存) | 市場全体のテーマローテーション検知 (仕入れ前) | Kabutan アクセスランキング |
| **業態テーマサマリー (本機能)** | 自分の投資仮説の事後評価 (仕入れ後) | portfolio_shelve `memo["gyoutai_themes"]` |

両者は補完関係。スコープ・データソース・UI 配置すべて独立させる。

## スコープ

1. webapp に新規ルート `/portfolio/themes/summary` を追加 (一覧表示のみ、編集機能なし)
2. テンプレート `portfolio_theme_summary.html` を新規追加
3. helpers に集計関数 `build_portfolio_theme_summary()` を 1 本追加
4. `/portfolio` 画面のヘッダから本画面へのリンクを追加
5. テーマ別の指標は **既存 stocks_shelve の値の集約のみ** で完結 — DB 書込みなし、新規スキーマなし、新規キャリブレーションなし

## 非スコープ

- テーマ指数 (price_log 累積) の構築
- TOPIX 比 RS の再計算 (個別銘柄の `momentum_pt` が既に TOPIX 比正規化済みのため流用)
- breadth 指標 (新高値率、25日線上比率、出来高増加率 等)
- 時系列での 3 週前 / 6 週前ランク比較
- 株探テーマ (`stock["themes"]`) ベースの指数
- 銘柄スコアへの組込 (既存 40/20/25/15 配点は変更しない)
- portfolio_theme_master の完了待ち (本機能は移行漏れ未登録テーマも含めてグルーピング)
- DB 書込み (本画面は閲覧専用、リクエストごとに集計)

---

## データソース

すべて既存スキーマ。新規スキーマなし。

| ソース | キー / フィールド | 用途 |
|---|---|---|
| portfolio_shelve | `record:<code_s>` の `memo["gyoutai_themes"]: list[str]` | テーマ → 銘柄の逆引き |
| portfolio_shelve | `record:<code_s>` の `status` | excluded 除外、status ラベル表示 |
| stocks_shelve | `<code_s>` の `momentum_pt` | テーマ集約 (平均 / 最大) の主指標 |
| stocks_shelve | `<code_s>` の `price_log` | 20日 / 65日リターン算出 |
| stocks_shelve | `<code_s>` の `stock_name` | リーダー株表示 |

### 構成銘柄の選定ルール

- `portfolio_shelve.list_records(include_excluded=False)` で取得 (excluded 銘柄は除外、既存ヘルパーに従う)
- `memo["gyoutai_themes"]` のスロット (最大 2) を **両方とも展開** し、テーマ → `[code_s, ...]` の逆引きを作る
  - 同一銘柄が 2 テーマに属する場合は両方にカウント (テーマ視点では独立集計)
- 空文字 / `None` のスロットは無視
- `memo` 自体が無い (旧データ) レコードは寄与なし

### 最小構成銘柄数

- 制限なし。**1 銘柄のテーマも表示する**
  - 軽量版の目的は「自分が組んだテーマ別の強弱を見る」ことなので、1 銘柄でも「そのテーマに自分が持っている / 監視している銘柄が 1 つしかない」という事実が見えるべき
  - 構成銘柄数カラムを必ず表示し、ユーザーが少数構成の不安定さを目視で判断できるようにする

---

## 集計指標

各テーマについて以下を計算。すべて構成銘柄の既存指標を **等加重で集約**。

| 指標 | 定義 | 計算ソース |
|---|---|---|
| `member_count` | 構成銘柄数 | 逆引き結果の長さ |
| `momentum_pt_avg` | momentum_pt の平均 (None は除外) | stocks_shelve `momentum_pt` |
| `momentum_pt_max` | momentum_pt の最大 (None は除外) | stocks_shelve `momentum_pt` |
| `ret_20d_avg` | 20 営業日リターン (%) の平均 | stocks_shelve `price_log` |
| `ret_65d_avg` | 65 営業日リターン (%) の平均 | stocks_shelve `price_log` |
| `leaders` | momentum_pt 降順上位 3 銘柄 | (code_s, stock_name, momentum_pt) の list |

### momentum_pt が無い銘柄の扱い

- `momentum_pt` が None / 欠損のものは集計対象から除外 (count にも入れない方針は採らず、count はテーマに属する全銘柄数とする)
- 集計対象が 0 銘柄ならその指標は `None` (テンプレ側で "—" 表示)

### リターン計算

- `price_log` は `[(date, price), ...]` 形式で最新が先頭
- `ret_Nd = (price_log[0][1] - price_log[N][1]) / price_log[N][1] * 100`
- `len(price_log) <= N` の銘柄はその指標から除外
- 計算自体は既存の他箇所で行われているはずだが、helpers 内で完結する小関数 `_calc_return_pct(price_log, days)` を本機能用に追加してよい。既存の似たユーティリティが見つかればそれを流用 (実装時に grep で確認)

### ソートキー

デフォルト: `momentum_pt_avg` 降順 (None は末尾)
将来のソート列追加は非スコープ (まずは単一ソートで運用)

---

## API 追加 (`webapp/helpers.py`)

```python
def build_portfolio_theme_summary(
    records: list[dict] | None = None,
) -> list[dict]:
    """portfolio_shelve のユニバースを memo['gyoutai_themes'] でグルーピングし、
    テーマごとの集約指標と上位リーダー株を返す。

    Args:
        records: portfolio_shelve.list_records(include_excluded=False) の戻り値。
            None なら関数内で取得する (テスト時に注入できるよう引数化)。

    Returns:
        list[dict]: 各要素は
            {
                "theme": str,
                "member_count": int,
                "momentum_pt_avg": float | None,
                "momentum_pt_max": float | None,
                "ret_20d_avg": float | None,
                "ret_65d_avg": float | None,
                "leaders": list[{"code_s": str, "stock_name": str, "momentum_pt": float}],
                "members": list[{"code_s": str, "stock_name": str, "momentum_pt": float | None,
                                 "status": str}],
            }
        並び順は momentum_pt_avg 降順 (None は末尾) → テーマ名昇順。
    """
```

- 既存 `_bulk_get_stock_data` を流用して 1 回でまとめて stock dict を取る
- 銘柄名は `_bulk_resolve_stock_names` を流用
- `members` フィールドはテンプレ側で行 expand 表示に使う (詳細表示)
- 戻り値は完全な dict (テンプレで `.get` フォールバックを書かなくて済むようにキーは常に存在)

---

## WebApp 変更

### 新規ルート (`scripts/webapp/routes/portfolio.py` に追加)

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/portfolio/themes/summary` | 業態テーマサマリー一覧表示 |

- 既存 `portfolio` blueprint に追加
- POST 操作なし、PRG 不要
- fallback_mode (= portfolio_shelve 空、`_build_fallback_records` で代用) の場合は空テーブル + 案内文を表示

### 新規テンプレート (`scripts/webapp/templates/portfolio_theme_summary.html`)

レイアウト:

```
[← ポートフォリオに戻る]

業態テーマサマリー (NN テーマ / MM 銘柄)

| テーマ        | 構成銘柄 | momentum_pt 平均 | 最大 | 20日 平均 | 65日 平均 | リーダー株 (top3)           |
|--------------|---------|------------------|------|----------|----------|----------------------------|
| AI半導体CAPEX | 6       | 78               | 92   | +12.4%   | +24.1%   | 6324 (92), 4063 (85), ...  |
| 防衛          | 4       | 65               | 80   |  +5.1%   |  +9.2%   | 7011 (80), 6208 (72), ...  |
| ...                                                                                                  |

(各行は折り畳み可。展開で構成銘柄一覧を表示)
```

- リーダー株表示: `{code_s} ({momentum_pt})` 形式、`url_for("detail.stock_detail", code_s=row.code_s)` (実 URL: `/stock/<code_s>`) で既存銘柄詳細ページにリンク (portfolio_list.html line 249 と同じ導線)
- 行クリック (または ▶ ボタン) で構成銘柄一覧を展開 (JS は最小限、`<details>` タグで実装可能)
- momentum_pt 平均は条件付き書式: ≥ 70 で太字 + 緑、≤ 30 で赤 (既存 portfolio_list.html のスタイル流用)
- 構成銘柄数が 1 の行は薄いマーカー (背景色) で「少数構成」とわかるようにする (ユーザーが信頼度を即判断できる)

### 既存 portfolio_list.html の変更

ヘッダのテーマフィルタ select 付近 (162-167 行付近、`portfolio_theme_master` プランで「✏️ テーマを編集」ボタンを追加する位置と同じ領域) に **「📊 テーマサマリー」リンク** を追加。

```html
<a href="{{ url_for('portfolio.theme_summary') }}"
   style="..." title="業態テーマ別サマリー">📊 テーマサマリー</a>
```

portfolio_theme_master プランとは UI 上は別ボタンとして共存させる (「✏️ テーマを編集」= マスター管理、「📊 テーマサマリー」= 集約閲覧)。

---

## テスト追加

`tests/test_webapp_helpers.py` に parametrize で集約 (CLAUDE.md: 1 PR で 5 本以下):

1. `build_portfolio_theme_summary` 基本ケース: 2 テーマ × 3 銘柄ずつで集約値が期待通り
2. 同一銘柄が 2 テーマに属する場合、両テーマで集計される
3. `momentum_pt` 欠損銘柄は集計から除外されるが member_count には含まれる
4. `price_log` 長さ不足の銘柄は ret_Nd_avg から除外される
5. `leaders` は momentum_pt 降順上位 3 (同点時は code_s 昇順で安定ソート)

WebApp ルートのテストは `tests/test_webapp_routes.py` に 1 本追加:

- `GET /portfolio/themes/summary` が 200 で返り、テンプレートに「業態テーマサマリー」文字列を含む (smoke)

---

## 検証ポイント

1. `pytest tests/test_webapp_helpers.py tests/test_webapp_routes.py -v` 通過
2. `python -m webapp.app` 起動 → `/portfolio` ヘッダから「📊 テーマサマリー」リンクが見える
3. `/portfolio/themes/summary` でテーマ一覧が momentum_pt 平均降順に並ぶ
4. 構成銘柄数 1 のテーマも表示され、視覚マーカーが付く
5. 行展開で構成銘柄が momentum_pt 降順で並ぶ
6. fallback_mode (portfolio_shelve 空) では空テーブル + 案内文が出る

---

## ロールバック

- webapp / helpers / templates の追加のみで、既存スキーマ・既存テンプレ・既存挙動への破壊的変更なし
- git revert で完全に戻せる
- DB 書込みなしのため、ロールバック後のデータ整合性問題は発生しない

---

## 想定リスク

- **テーマ名の表記揺れ**: portfolio_theme_master 完了前は同義語・誤字が別テーマとして集計される。本機能は移行漏れも「そのまま」表示する方針なので、ユーザーは表記揺れに気づいて手動修正できる (むしろ可視化の副次効果として有用)。
- **構成銘柄 1 のテーマ**: momentum_pt 平均 = その銘柄自体になり「テーマ集約」としては意味が薄い。マーカー表示で目視抑制し、ユーザー判断に委ねる (除外しない)。
- **集計コスト**: テーマ数 30、銘柄数 200 想定で 1 リクエストあたり数十 ms 程度の見込み。`_bulk_get_stock_data` 流用で N+1 を回避すれば問題なし。リクエストごとに再計算するが、キャッシュは入れない (シンプル優先)。
