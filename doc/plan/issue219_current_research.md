# issue #219 実装プラン: 銘柄詳細ページに「現在の調査材料」セクションを追加

## ゴール

- 銘柄詳細ページ (`/stock/<code_s>`) に **「現在の調査材料」セクション** を追加し、`code_rank.csv` の全列を該当銘柄1件分として表示する。
- レイアウトは **グループ別グリッド (横並び)**。issue 当初の「縦リスト」案より情報密度を高くする (ユーザー方針)。
- データは researchDB には保存せず、`stocks_shelve` から都度計算する read-only 表示。

## レイアウト方針 (確定)

セクション全体は `<details open>` で折りたたみ可能。中身は意味グループごとに横並びの行を並べる:

```
現在の調査材料                                            [▼]
────────────────────────────────────────────────────────
【ランク】   順位 1   過去 0(-3)|0(-4)   タグ 押/売   シグナル —
【スコア】   総合PT 89   プロフィット 94   バリュー 100   モメンタム 95.-12*/-4   ファンダ 53
【テクニカル】 トレンド △   ボラ(20,5) 6,8   売り圧/買集/50DMA 44,45,B,E,-5
【業績】     今季売上/営利 [A]361%,4337%   四半期 [Q]545%,6173%   進捗 [P]3Q61%(72%),73%(-55%)
【指標】     535億 PER13 PBR3.6 EVR3.5 配当0.9 ROE26 利益率40% 負債0.07 自己65%
【理論株価】 189%(537%) | 459%,-80%
【過去業績】 [A]101±149%,1037±1907% [Q]221±202%,1618±2629%<C3>
【信用】     売,出15        【テーマ】 人工知能,データセンター,美容
【更新日】   業績 5/6 | 指標 11 | 価格 14    セクター サービス業
```

- グループラベルは `【…】`。値は `ラベル 値` のスペース区切りで横並び。
- 既存 `snapshot-table` 等の太いテーブル装飾は使わず、フラットな div + flex で構築 (1セクションあたり 9 行程度に収める)。
- 値が空のキーは `—` を表示。グループ全体が空なら行ごと省略。

## 影響ファイル

### 1. `scripts/make_stock_db.py` — CSV 1行構築ロジックの関数化

- 現状 `list_all_db()` 内 (L1441〜L1531) で `stocks_active` を回しながら CSV 行を組み立てている。
- これを **`build_code_rank_row(code_s, stock_data, *, total_pt, gyoseki_pt, shihyo_pt, mom_pt, funda_pt, rank, pf_status, market_db, topix_map=None) -> dict[label, value]`** として外出し。
  - 第1引数に `code_s` を必須化 (Yahoo URL/`コード`列の組み立てに `stocks_active` のキーを使っていたため。`stock_data` だけからの推測は避ける)。
  - 戻り値の dict のキーはヘッダ行 (L1411〜L1438) と完全一致させる。
  - ハイパーリンク埋め込みは **dict 段階では生 URL なしのテキスト値** にする。CSV 書き出し側でだけ `=HYPERLINK(...)` を被せる。
    - 具体的には:
      - `順位` … dict は `"1"` (str)、CSV は `=HYPERLINK(yahoo_url, "1")`
      - `コード` … dict はコード文字列、CSV は既存 `get_code_exp()` 適用
      - `銘柄名` … dict は銘柄名文字列、CSV は既存 `get_stock_name_exp()` 適用
    - 上記3つは CSV 書き出し直前で URL を被せる薄いラッパー `_decorate_links_for_csv(code_s, row_dict, stock_data) -> list` を追加。
- 既存 CSV 出力は `rows.append(_decorate_links_for_csv(stock[0], build_code_rank_row(stock[0], ...), stock_data))` に置換。**列の中身・順序は変えない** (回帰防止)。

### 2. `scripts/webapp/helpers.py` — webapp 向けヘルパ

- 新規:
  ```python
  def get_current_research_data(code_s: str) -> Optional[dict]:
      """code_rank.csv 相当の dict を 1銘柄分返す。stock_shelve 未登録 / 必要キー欠落時は None。"""
  ```
- 内部で:
  1. `get_stock_data(code_s)` で stock_data を取得
  2. `stock_data` から `total_pt`/`gyoseki_pt`/`shihyo_pt`/`mom_pt`/`funda_pt` を再計算 (`list_all_db` 冒頭ループと同じ式)。KeyError は None 返却。
  3. 現在 rank は `get_rank_log(stock_data, "stock_rank_log", 0)` を使用 (`stock_rank_log` は新しい日付が先頭。`insert(0)` + 日付降順 sort で先頭=最新が保証されている)。値が取得不可なら `""`。
  4. `pf_status` は `portfolio_shelve` から取得 (`"監"`, `"保"` の組み立て)
  5. `make_market_db.get_market_db()` を呼んで `make_stock_db.build_code_rank_row(...)` に渡す
  6. 返却 dict から **非表示列** (`ポートフォリオ`, `決算日`, `コード`, `銘柄名`, `概要`) を除去
  7. **グループ定義をここで適用**: `[("ランク", ["タグ", "順位", "過去順位(1日/5日前)", "シグナル"]), ("スコア", [...]), ...]` の構造で返す
     - 戻り値は `List[Tuple[str, List[Tuple[str, str]]]]` (グループ名 → (短縮ラベル, 値) の順序付きリスト)
- 短縮ラベル (例: 「総合PT」「プロフィット」) は helpers 側で定義する。CSV ヘッダはユーザー視点では冗長なので UI 専用名を持つ。

### 3. `scripts/webapp/routes/detail.py`

- `record is not None` の分岐の末尾、`render_template` 呼び出しの直前で:
  ```python
  current_research = get_current_research_data(code_s)
  ```
- `render_template(... current_research=current_research)` を追加。
- `detail_add_prompt.html` 経路には渡さない (researchDB 未登録は対象外)。

### 4. `scripts/webapp/templates/detail.html`

- 挿入位置: 「適時開示」セクション (L158〜L199) の **直後**、「業績スナップショット時系列」(L201) の **直前**。
  - issue 文言「外部リンクの下、業績スナップショットの上」を満たし、かつ disclosures セクションの直後で視覚的にも自然。
- マークアップ:
  ```html
  {# ===== 1.5. 現在の調査材料 (issue #219) ===== #}
  {% if current_research %}
  <details open class="current-research">
    <summary><strong>現在の調査材料</strong></summary>
    <div class="current-research-body">
      {% for group_name, items in current_research %}
      <div class="cr-row">
        <span class="cr-group">【{{ group_name }}】</span>
        {% for label, value in items %}
        <span class="cr-item"><span class="cr-label">{{ label }}</span> <span class="cr-value">{{ value or '—' }}</span></span>
        {% endfor %}
      </div>
      {% endfor %}
    </div>
  </details>
  {% endif %}
  ```
- スタイル: 既存 CSS との衝突を避け、`<style>` ブロックを当セクション直下に置く (小規模なので CSS ファイル分離はしない):
  - `.current-research-body { font-size: 0.9em; line-height: 1.7; }`
  - `.cr-row { display: flex; flex-wrap: wrap; gap: 1em; padding: 0.15em 0; }`
  - `.cr-group { color: #666; min-width: 6em; }`
  - `.cr-item { white-space: nowrap; }`
  - `.cr-label { color: #888; font-size: 0.85em; margin-right: 0.2em; }`

## グループ定義 (helpers.py 内)

| グループ名 | 含める code_rank.csv 列 | 短縮ラベル |
|---|---|---|
| ランク | タグ / 順位 / 過去順位(1日/5日前) / シグナル | タグ / 順位 / 過去 / シグナル |
| スコア | 総合PT / プロフィット/クォリティ / バリュー/サイズ / モメンタム(現在.20日比/5日比) / ファンダメンタル | 総合PT / プロフィット / バリュー / モメンタム / ファンダ |
| テクニカル | トレンドテンプレート / ローソク足ボラティリティ(20,5) / 売り圧力レシオ(20,5) 買い集め(週,日) 50DMA乖離率 | トレンド / ボラ / 売り圧/買集/50DMA |
| 業績 | 業績(今季/今四半期 売上/営利成長率) / 進捗率(現四半期/売上(前年)利益(前年) | 売上/営利成長率 / 進捗 |
| 指標 | 指標(時価総額\|PER\|EVR\|ROE\|...) | (ラベルなし、値のみ) |
| 理論株価 | 理論株価(乖離率\|上限,下限) | (ラベルなし) |
| 過去業績 | 過去業績(5年増収増益 4Q増収増益率) | (ラベルなし) |
| 信用 | 信用(倍率\|出来高買残比) | (ラベルなし) |
| テーマ | テーマ | (ラベルなし) |
| 更新日 | 更新日(業績\|指標\|価格) / セクター | 更新日 / セクター |

「指標」「理論株価」「過去業績」「信用」「テーマ」は値そのものに既にラベル相当の情報が埋め込まれているので、UI上は **値のみ** 表示 (ラベル冗長を防ぐ)。

## テスト

`.claude/rules/testing.md` のマッピングに従い、本 PR で追加するのは以下のみ:

- `tests/test_webapp_helpers.py`:
  - `get_current_research_data` の代表ケース (parametrize で集約):
    - 通常銘柄: グループリストが期待形で返る (groups 数、各 item 数)
    - stocks_shelve 未登録 / 必要キー欠落: `None` 返却
- `tests/test_make_stock_db.py`:
  - `build_code_rank_row` リファクタが既存出力と等価であることを確認する1件のみ (代表銘柄 fixture)。

> テスト方針: 5本以下、parametrize 集約、自明な動作・各キー個別確認は書かない (CLAUDE.md 準拠)。

## 検証

- [ ] `pytest tests/test_webapp_helpers.py tests/test_make_stock_db.py -v` が通る
- [ ] WebApp 起動 → `/stock/6324`, `/stock/6574` で「現在の調査材料」が表示される
- [ ] 値が `code_rank.csv` の同銘柄行と一致する (HYPERLINK 部分はテキストのみ)
- [ ] researchDB 未登録銘柄 (`detail_add_prompt.html` 経路) でページが壊れない
- [ ] researchDB に書き込みが発生しない (read-only)
- [ ] スクリーンショットを `.playwright-mcp/issue219-current-research.png` に保存

## スコープ外 (issue 補足どおり次回以降)

- ポートフォリオ列の表示 (今回非表示のまま)
- 既存 `code_rank.csv` 生成側の挙動・列追加
