# プラン: ポートフォリオ — 業態テーマ別 RS サマリー表示

## 目的

監視ユニバース (portfolio_shelve に登録された全銘柄、保有 / 準保有 / 監視) を、ユーザーが手動で付けた **業態・テーマ** (`memo["gyoutai_themes"]`) でグルーピングし、テーマごとに既存指標を集約して **「手動付けした業態テーマ (自分の分類軸) による市場のテーマローテーション検知」** を行う画面を webapp に追加する。

株探テーマベースの `theme_rank` (自動分類) を **自分の分類軸で補完する** もの。中長期の強弱 (リーダーシップ) に加え、短期の勢い (RS ラインの移動平均乖離オシレーター) も併記する。

O'Neil / MarketSmith のセクター RS 表示思想 (テーマ別 RS テーブル + リーダー株の同行表示) を参考にする。

### 用途の再定義 (issue #283 追記 2026-05-29)

当初は用途を「自分が組んだ投資仮説の事後評価 / 銘柄入れ替え判断」に限定していたが、**「手動業態テーマによる市場ローテーション検知」に改める**。

- 既存 `theme_rank` は **株探の自動テーマ分類** でローテーションを検知する。本機能は同じ "市場ローテーション検知" を **自分の分類軸で行う** もの。用途は同じ (ローテーション検知)、テーマ分類の出所が違う (株探自動 vs 手動) という補完関係
- ローテーション検知が主目的である以上、「今どの業態が強いか (中長期)」だけでなく **「今どの業態が直近平均より上振れ/過熱しているか (短期の勢いオシレーター)」も併せて示す**

### MarketSmith のグループ強弱の見せ方と本機能の位置づけ (事実整理)

issue #283 の検討過程で「グループの点火を RS ライン新高値 (Blue Dot) で出す」案を検討したが、事実関係を確認した結果、**Blue Dot のグループ集計は本機能のスコープから外す**。

- MarketSmith のグループ強弱の標準表示は **「Industry Group Rank」(1〜197 位、6ヶ月価格パフォーマンスでランク付けしたレベル値)**。グループは slow なランク数値で見るのが基本設計
- MarketSmith における **Blue Dot (RS ライン新高値の点火シグナル) は「個別銘柄」チャートの機能**であり、グループ (セクター) 単位の Blue Dot という標準機能は無い。追記コメントの「MarketSmith の点火検知は RS ライン」は銘柄レベルの話の転用だった
- 銘柄レベルの RS ライン新高値判定は本システムで既に実装済み (`compute_rs_line_new_high` / 銘柄詳細チャートの Blue Dot 表示)。これをテーマ単位で集約すると「銘柄 Blue Dot の breadth」という独自指標になるが、無用な複雑化を避けるため **今回は採用しない**

→ 本機能の短期層は、MarketSmith の「グループランクの時系列変化 (= 上昇しているグループを点火候補とする)」の思想に沿いつつ、**永続化不要で即動く既存資産** = RS ラインの移動平均乖離オシレーター (既存 `compute_rs_line_changes` を MA 化したもの、後述) のテーマ平均で代替する。

### 既存資産との役割分担 (slow × fast / 自動 × 手動)

軸を「テーマ分類の出所 × 時間軸」に整理する。**本機能は右列 (手動分類軸) の slow + fast 両方を担う。**

|  | 株探テーマ (自動分類) | 手動業態テーマ (自分の分類軸) |
|---|---|---|
| **中長期リーダーシップ (slow)** | `theme_rank` (既存) | **momentum_pt 集約 (本機能)** |
| **短期の勢い (fast)** | `theme_momentum` 1日騰落率 (既存・部分的) | **rs_line 移動平均乖離オシレーター集約 (本機能)** |

- 株探自動分類 (左列) と手動分類軸 (右列) が揃うことで、「市場が見ているテーマ」と「自分が見ているテーマ」のローテーションを両軸で比較できる
- スコープ・データソース・UI 配置は既存機能と独立させる

#### 妥当性確認: momentum_pt 集約 = MarketSmith「インダストリーグループ・ランク」相当

- MarketSmith のグループ強弱 = 全銘柄を業種に分け、6ヶ月価格パフォーマンスでランク。個別 RS の土台式は 3/6/9/12ヶ月の加重 → パーセンタイル化
- 既存 `momentum_pt` = `rs_rel` (銘柄 rs_raw / TOPIX rs_raw) を対数正規 CDF でパーセンタイル化。`rs_raw` は 13/26/39/52週 = 3/6/9/12ヶ月の加重

→ 期間構成・相対正規化・パーセンタイル化のいずれもグループランクと整合。「テーマ指数を新規構築せず `momentum_pt` を集約」する判断は中長期リーダーシップ層を正しく再現できている。

## スコープ

1. webapp に新規ルート `/portfolio/themes/summary` を追加 (一覧表示のみ、編集機能なし)
2. テンプレート `portfolio_theme_summary.html` を新規追加
3. helpers に集計関数 `build_portfolio_theme_summary()` を 1 本追加
4. `/portfolio` 画面のヘッダから本画面へのリンクを追加
5. 中長期層: **既存 stocks_shelve の `momentum_pt` の集約** — DB 書込みなし、新規スキーマなし
6. 短期層: **`compute_rs_line_changes` を MA 乖離オシレーター化し (#155 系指標ごと変更)、A (5日平均乖離) / B (20日平均乖離) を業態テーマ単位で平均**。rs_line は都度計算 (永続化しない) 方針につき DB 書込み・新規スキーマ不要
7. 点火検知用に短期の勢い (A 平均) での再ソート切替を用意

## 非スコープ

- **Blue Dot 率 (rs_line 新高値) のグループ集計** (上記「事実整理」のとおり今回は採用しない)
- momentum_pt (0〜99) の日次履歴の永続化とそのスロープ (= 案B、将来の再検討候補)
  - momentum_pt は現状 stocks_shelve に最新値のみ保持 (`price.py:682`)。過去日の正確な遡及には過去時点の TOPIX rs_raw + calib が必要で都度計算は不正確。永続化は本機能の「DB 書込みなし」方針を破る
  - MarketSmith の「グループランク (パーセンタイル) の時系列変化」に最も忠実なのは案B だが、本 PR では永続化不要で即動く案A (rs_line の MA 乖離オシレーター) を採用する。**案A の短期勢い指標がローテーション検知としてしっくりこない場合に、案B (momentum_pt 履歴) を別 issue で改めて検討する**
- rs_line そのものの永続化 (#155 方針どおり都度計算)
- テーマ指数 (price_log 累積) の構築
- TOPIX 比 RS の再計算 (個別銘柄の `momentum_pt` が既に TOPIX 比正規化済みのため流用)
- breadth 指標 (新高値率、25日線上比率、出来高増加率 等)
- 株探テーマ (`stock["themes"]`) ベースの指数 (既存 `theme_rank` がカバー)
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
| stocks_shelve | `<code_s>` の `momentum_pt` | 中長期層: テーマ集約 (平均 / 最大) の主指標 |
| stocks_shelve | `<code_s>` の `price_log` | rs_line 計算 (= 銘柄終値 / TOPIX 終値) |
| stocks_shelve | `<code_s>` の `stock_name` | リーダー株表示 |
| market_db (topix) | `price_log` | rs_line (= 銘柄終値 / TOPIX 終値) 計算の分母 |

### 構成銘柄の選定ルール

- `portfolio_shelve.list_records(include_excluded=False)` で取得 (excluded 銘柄は除外、既存ヘルパーに従う)
- `memo["gyoutai_themes"]` のスロット (最大 2) を **両方とも展開** し、テーマ → `[code_s, ...]` の逆引きを作る
  - 同一銘柄が 2 テーマに属する場合は両方にカウント (テーマ視点では独立集計)
- 空文字 / `None` のスロットは無視
- `memo` 自体が無い (旧データ) レコードは寄与なし

### 最小構成銘柄数

- 制限なし。**1 銘柄のテーマも表示する**
  - 「そのテーマに自分が持っている / 監視している銘柄が 1 つしかない」という事実が見えるべき
  - 構成銘柄数カラムを必ず表示し、少数構成の不安定さを目視で判断できるようにする

---

## 集計指標

各テーマについて以下を計算。すべて構成銘柄の既存指標を **等加重で集約**。

### 中長期リーダーシップ層 (slow)

| 指標 | 定義 | 計算ソース |
|---|---|---|
| `member_count` | 構成銘柄数 | 逆引き結果の長さ |
| `momentum_pt_avg` | momentum_pt の平均 (None は除外) | stocks_shelve `momentum_pt` |
| `momentum_pt_max` | momentum_pt の最大 (None は除外) | stocks_shelve `momentum_pt` |
| `leaders` | momentum_pt 降順上位 3 銘柄 | (code_s, stock_name, momentum_pt) の list |

### 短期の値動き傾向層 (fast) = rs_line の「勢い / 過熱」オシレーター

構成銘柄の rs_line オシレーターを業態テーマ単位で平均する。指標の定義は **#155 の「N日前の1点との比」から「今の rs vs N日移動平均の乖離率」に変更する** (下記「rs_line オシレーター化」参照)。

| 指標 | 定義 | 計算ソース (既存関数) |
|---|---|---|
| `dev_a_avg` | テーマ短期の勢い: 構成銘柄の「今日 vs 直近 5 日移動平均」乖離率 (A) の平均 | `compute_rs_line_changes()` の A (`make_stock_db.py`) |
| `dev_b_avg` | テーマ中期の勢い: 構成銘柄の「今日 vs 直近 20 日移動平均」乖離率 (B) の平均 | `compute_rs_line_changes()` の B (`make_stock_db.py`) |

UI ラベルは「5日乖離 / 20日乖離」(または「短期勢い / 中期勢い」)。値が大きいほど「今、直近平均より rs_line が上振れている = 勢い/過熱が強い」。

#### rs_line オシレーター化 (この PR で #155 系指標ごと変更)

**背景**: 現状は「今日の rs_line vs ちょうど N 日前の 1 点」の比 (`_rs_line_changes_from_line` の `_change(offset)`)。基準が 1 点なので、今日か N 日前のどちらかがヒゲ・急変した日だと値が大きくブレる。基準を N 日移動平均にすればブレが 1/N に薄まり、勢い・過熱の度合いをより安定して捉えられる。

**新定義 (今の rs vs 移動平均の乖離率 = オシレーター)**:
- 短期 A = `(rs_line[0] − mean(rs_line[0:5])) / mean(rs_line[0:5]) × 100` (今日を含む直近 5 日平均との乖離率%)
- 中期 B = `(rs_line[0] − mean(rs_line[0:20])) / mean(rs_line[0:20]) × 100` (今日を含む直近 20 日平均との乖離率%)
- これは「傾き (slope)」ではなく **平均からの乖離 (オシレーター / 過熱度)**。codex 指摘を踏まえ、命名は slope を避け `dev`(deviation) 系に統一。上昇トレンド中でも直近反落で符号が負になりうるが、それは「勢いが一服した」を意味し、ローテーション検知の用途に合致する
- B の代替: 20 本に満たないとき直近 19,18,...,15 本の平均で代替 (現行の offset 代替と同じ思想、`b_is_approx=True`)
- 必要本数: A は 5 本以上、B は 15 本以上 (代替込み)。不足は None。移動平均 (分母) が 0 の場合も None

**影響範囲 (この PR で一括変更、ユーザー確認済み)**:
1. `_rs_line_changes_from_line` (make_stock_db.py:332): `_change(offset)` の「過去 1 点との比」を「今日を含む直近 N 日平均との乖離率」に書き換え (本体)。戻り値の型・タプル形状 (A, B, b_is_approx) は不変
2. `compute_rs_line_changes` / `get_rs_line_changes_expr`: 上記を呼ぶだけなのでシグネチャ・戻り値の型は不変。**値の意味と数値が変わる**
3. code_rank.csv モメンタム列 (`get_rs_line_changes_expr` 経由、make_stock_db.py:1499): 「N日前比」→「N日平均乖離率」に意味が変わる。CSV 表記フォーマット ("中期/短期") は据え置き。表示のみの利用で運用影響は小さい
4. webapp 詳細チャート tooltip の `_format_total_change` (helpers.py:2139): 別系統の「2 点比較」だが、同じく「今 vs N日平均乖離率」に揃え、銘柄側 tooltip も一貫させる
5. テスト: `test_make_stock_db.py` の `compute_rs_line_changes` 8 本 + `get_rs_line_changes_expr` 7 本の期待値を新定義で再計算。tooltip 系テストも該当あれば更新

→ **これは表示機能追加にとどまらず、#155 由来の rs_line 系指標 (code_rank.csv のモメンタム列・銘柄詳細 tooltip) の定義変更を含む。リスク節・ロールバック節もこれを前提に記載する。**

#### rs_line 集約の計算量対策 (都度計算・キャッシュなし)

- **キャッシュは入れない** (シンプル優先、既存 `theme_rank` / 銘柄詳細と同じく都度計算)
- N+1 を避けるため、`make_stock_db._topix_close_map(market_db)` で **TOPIX 終値マップを 1 回だけ構築**し、全銘柄の rs_line 計算に `topix_map=` で渡す
- **公開 API を使い private 関数には依存しない** (simplify レビューでの方針確定): 銘柄ごとに公開関数 `compute_rs_line_changes(stock, market_db, topix_map=topix_map)` を呼んで `(A, B)` を得る。`topix_map` を渡すことで内部 `compute_rs_line` の TOPIX マップ再構築を避ける
  - 当初は再計算回避のため内部関数 `_rs_line_changes_from_line(rs_line)` を直接呼ぶ案だったが、private 関数への越境依存 (カプセル化の崩れ・テストの脆さ) を避けるため公開 API に統一。`compute_rs_line` の二重計算は発生するが、ポートフォリオは数百銘柄規模で影響は無視できる
  - `compute_rs_line_changes` のシグネチャ・戻り値の型は不変 (API 形状は変えない)。ただし算出ロジック (=値の意味) は MA 乖離率に変更する
- 銘柄数 200・テーマ数 30 想定で 1 リクエスト数百 ms 見込み。rs_line 計算は price_log のスライス比較のみで重くない

### 欠損銘柄の扱い

- `momentum_pt` が None / 欠損のものは集計対象から除外 (count には含める = テーマに属する全銘柄数)
- rs_line がデータ不足 (`compute_rs_line_changes` が A / B に None を返す) の銘柄は、その指標 (dev_a / dev_b) の平均から個別に除外する (A は取れるが B は None、というケースもあるため指標ごとに有効銘柄数で平均)
- 集計対象が 0 銘柄ならその指標は `None` (テンプレ側で "—" 表示)

### ソートキー (再ソート切替)

- **デフォルト (1次ソート)**: `momentum_pt_avg` 降順 (None は末尾) → 中長期リーダーシップ
- **短期検知用の再ソート**: `dev_a_avg` (短期の勢い) 降順を切替可能にし、今最も上振れ/過熱している業態が上に来るようにする
  - サーバ側でクエリパラメータ `?sort=momentum|dev_a` を受けて並べ替える
  - 同点時は `member_count` 降順 → テーマ名昇順で安定ソート

---

## API 追加 (`webapp/helpers.py`)

```python
def build_portfolio_theme_summary(
    records: list[dict] | None = None,
    sort_key: str = "momentum",
) -> list[dict]:
    """portfolio_shelve のユニバースを memo['gyoutai_themes'] でグルーピングし、
    テーマごとの中長期 (momentum_pt) + 短期 (rs_line 勢いオシレーター) 集約指標と
    上位リーダー株を返す。

    sort_key: "momentum" | "dev_a"。
    並び順は sort_key に従う (None は末尾) → member_count 降順 → テーマ名昇順。
    """
```

- `_bulk_get_stock_data` / `_bulk_resolve_stock_names` を流用
- market_db は `from make_market_db import get_market_db` の遅延 import で取得
- rs_line 系は `from make_stock_db import compute_rs_line_changes, _topix_close_map` を遅延 import
- 戻り値は完全な dict (テンプレで `.get` フォールバック不要)

---

## WebApp 変更

### 新規ルート (`scripts/webapp/routes/portfolio.py` に追加)

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/portfolio/themes/summary` | 業態テーマ別 RS サマリー一覧表示 |

- 既存 `portfolio` blueprint に追加、クエリ `?sort=momentum|dev_a` を受ける
- POST 操作なし、PRG 不要
- fallback_mode (portfolio_shelve 空) は空テーブル + 案内文

### 新規テンプレート (`scripts/webapp/templates/portfolio_theme_summary.html`)

中長期 slow と 短期の勢い fast を 1 行に併記。

- リーダー株: `{code_s} ({momentum_pt})`、`url_for("detail.stock_detail", code_s=...)` でリンク
- 行クリックで構成銘柄一覧を展開 (`<details>` タグ、JS 最小限)
- 並べ替えリンクは現在の sort をハイライト
- 条件付き書式: momentum_pt 平均 ≥70 緑太字 / ≤30 赤、勢い (乖離率) 正で緑 / 負で赤
- 構成銘柄数 1 の行は薄いマーカー (背景色) で「少数構成」を明示

### 既存 portfolio_list.html の変更

ヘッダのテーマ編集ボタン隣に **「📊 テーマサマリー」リンク** を追加。

---

## テスト追加

`tests/test_webapp_helpers.py` に parametrize で集約 (CLAUDE.md: 1 PR で 5 本以下):

1. 基本ケース: 2 テーマ × 3 銘柄で中長期集約値が期待通り
2. 同一銘柄が 2 テーマに属する場合、両テーマで集計される
3. `momentum_pt` 欠損銘柄は集計から除外されるが member_count には含まれる
4. 短期層: dev_a_avg / dev_b_avg が既存 rs_line 関数の集約値と一致し、データ不足銘柄は各指標から除外
5. `sort_key` 切替: momentum / dev_a でそれぞれ期待順に並ぶ

WebApp ルートのテストは `tests/test_webapp_routes.py` に 1 本追加:

- `GET /portfolio/themes/summary` および `?sort=dev_a` が 200 で返り、テンプレートに「業態テーマ別 RS サマリー」文字列を含む

---

## 検証ポイント

1. `pytest tests/test_webapp_helpers.py tests/test_webapp_routes.py -v` 通過
2. `python -m webapp.app` 起動 → `/portfolio` ヘッダから「📊 テーマサマリー」リンクが見える
3. `/portfolio/themes/summary` でテーマ一覧が momentum_pt 平均降順に並ぶ
4. 並べ替えを「短期の勢い(A)」に切替で順序が変わる
5. 5日乖離 / 20日乖離カラムが表示され、rs_line 計算可能な銘柄で値が入る
6. 構成銘柄数 1 のテーマも表示され、視覚マーカーが付く
7. 行展開で構成銘柄が momentum_pt 降順で並ぶ
8. fallback_mode (portfolio_shelve 空) では空テーブル + 案内文が出る

---

## ロールバック

- 本 PR は **2 種の変更を含む**:
  1. webapp 表示追加 (サマリー画面・ルート・テンプレート・導線リンク) — 新規追加のみ、既存スキーマ・既存挙動への影響なし
  2. **#155 由来 rs_line 系指標の定義変更** (`_rs_line_changes_from_line` / `compute_rs_line_changes` の算出ロジックを MA 乖離率に変更) — code_rank.csv モメンタム列・銘柄詳細 tooltip の **表示値が変わる挙動変更**
- DB スキーマ・DB 書込みは無し (rs_line は従来どおり都度計算)。値の意味が変わるだけでデータ整合性問題は起きない
- git revert で完全に戻せる。ただし revert すると CSV・tooltip の rs_line 表示も旧定義 (N日前比) に戻る点に留意

---

## 依存関係

- 短期層は **#155 (rs_line) の指標 A / B 実装完了が前提**。実装済み:
  - `compute_rs_line_changes()` (A / B) — `make_stock_db.py:288`
  - `compute_rs_line()` / `_topix_close_map()` — `make_stock_db.py`

## 想定リスク

- **テーマ名の表記揺れ**: portfolio_theme_master 完了前は同義語・誤字が別テーマとして集計される。可視化の副次効果として有用なのでそのまま表示
- **構成銘柄 1 のテーマ**: momentum_pt 平均 = その銘柄自体。マーカー表示で目視抑制
- **rs_line 計算コスト**: `_topix_close_map` を 1 回構築 + 銘柄ごと `compute_rs_line_changes` を呼ぶ (同一銘柄が複数テーマに属しても銘柄単位キャッシュで 1 回に集約)。数百銘柄規模で数百 ms 程度。キャッシュなし
- **#155 系指標の定義変更の波及**: code_rank.csv のモメンタム列・銘柄詳細 tooltip の rs_line 表示値が「N日前比」→「N日平均乖離率」に変わる。いずれも表示用途で、スコアリング (業績40/モメンタム25/…) には組み込まれていない (momentum_pt は別系統の rs_raw 由来) ため、ランキング順位には影響しない。ユーザー確認済み

## 実装規模見込み

- helpers 1 関数 + 小ヘルパー (`~90 行`)、ルート 1 本 (`~40 行`)、テンプレート 1 枚 (`~110 行`)、テスト 6 本
- 合計 250〜280 行程度。1 PR で完結予定
