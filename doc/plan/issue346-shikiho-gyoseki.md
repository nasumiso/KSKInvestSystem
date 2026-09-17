# issue #346 四季報業績予想の取り込みと MCP 提供

## 目的の再定義

issue 原文は「銘柄詳細ページに成長率を表示する」が主眼だが、
**本来の目的は 2期先業績予想を AI 分析の定性的情報源として使えるようにすること**。

- 2期先予想は四季報くらいしか出典がない
- 2期先の数値は信頼度の面で「定量指標」としては扱えず、定性的な材料と解釈する
- したがって **業績ポイント等のスコアリングには一切加点しない**
- **既存の四季報 MCP (`get_shikiho`) から取得できることが最重要要件**

UI (詳細ページ表示・入力フォーム) は「MCP に流すデータを人が入れる/確認する」ための付帯機能。

## スコープ

| 対象 | 内容 |
|---|---|
| 入力 | `/portfolio/shikiho` に業績テキストエリアを追加、楽天証券コピペをパース |
| 保存 | `research_shelve` レコードに `shikiho_gyoseki` キーを追加 (上書き方式) |
| 提供 | MCP `get_shikiho` の返却に `gyoseki` フィールドを追加 |
| 表示 | 銘柄詳細ページ「現在の調査材料」に独立グループ【四季報予】を追加 |

### 対象外

- スコアリング (`momentum_pt` / 業績ポイント等) への反映 — **明示的に行わない**
- EPS・配当・経常利益・純利益の取り込み
- 履歴の蓄積 (最新の1セットのみ保持)
- ウォッチ外銘柄 (research 未登録銘柄) への対応
- 新規 MCP Tool の追加 (`get_shikiho` に統合する)

## 決定事項 (ユーザー確認済み)

1. **実額 + 成長率の両方を保存する** — issue 原文は「成長率のみ、実額は対象外」だが、
   AI が「規模感」「利益率の推移」を読むには実額が要る。
   issue の「実額表示は対象外」は **UI 表示の話** として維持する (詳細ページは成長率のみ)。
2. **MCP は `get_shikiho` に統合** — 新 Tool は作らない。AI が1回の呼び出しで
   コメント + 業績予想を得られ、「四季報の定性情報」として一体で扱える。
3. **予想2期 + 直前実績1期を保存** — 成長率計算に直前確定期が必要であり、
   AI は「実績 → 今季予 → 来季予」の3点で加速/減速を読める。
4. **入力 UI は `/portfolio/shikiho`** — 四季報号更新のついでに業績も貼る運用。
5. **詳細ページの表示先は「現在の調査材料」の独立グループ【四季報予】** —
   四季報セクション (L527) ではない。理由は次節。

## 表示先の決定理由 (【四季報予】独立グループ)

「現在の調査材料」(`get_current_research_data`) は `code_rank.csv` 相当を
グループ横並びで表示するダッシュボードで、既に時間軸で並んでいる:

| グループ | 中身 | 時間軸 |
|---|---|---|
| 過去業績 | 5年増収増益 / 4Q増収増益率 | 過去 |
| 業績 | 今季/今四半期 売上・営利成長率、進捗率 | 現在進行 |
| **四季報予 (新規)** | 今季予・来季予の売上/営利成長率 | **未来** |

- **「過去業績」と合わせない**: 5年増収増益は別粒度の集計指標で、四季報予想と並べても読めない
- **四季報セクションに置かない**: あちらは click-to-edit のテキスト編集フォーム。
  数値を置くと編集 UI と閲覧 UI が混ざり、既存の `editable-field` / `data-form="shikiho"`
  の JS 規約にも乗らない。また見たいタイミングが違う (上部で一目で見る数字)
- **独立グループにする理由**: (a) `_CR_GROUPS` に1エントリ足すだけで既存グループの
  行構成を壊さない (b) 出典が違う (自動集計 vs 手入力四季報) ことがラベルで明示される
  (c) 未入力銘柄でグループごと消せる (既存グループに混ぜると空欄が残る)

表示イメージ:

```
【業績】    売上/営利成長率 +28.3%/+41.2%   進捗 62%/58%
【四季報予】 27.3予 +36.8%/+69.0%   28.3予 +21.4%/+16.2%
```

## データ構造

`research_shelve` レコードの新フィールド `shikiho_gyoseki` (dict または未設定)。

```json
{
  "prev_year":  {"label": "連26.3",   "sales": 51163, "op_profit": 2189},
  "this_year":  {"label": "連27.3予", "sales": 70000, "op_profit": 3700,
                 "sales_growth": 36.8, "op_growth": 69.0},
  "next_year":  {"label": "連28.3予", "sales": 85000, "op_profit": 4300,
                 "sales_growth": 21.4, "op_growth": 16.2},
  "raw_text": "<貼り付けた原文>",
  "updated_at": "2026-09-17"
}
```

- 単位は四季報表記のまま **百万円** (楽天証券の表示単位)。変換しない
- `sales_growth` / `op_growth` は前期比 %、小数第1位
- `prev_year` が取れない (予想行しか貼られていない) 場合、`this_year` の成長率は `None`
- `raw_text` は貼り付け原文。パース仕様を後から変えたとき再パースできる保険
  (EPS・配当を後から使いたくなっても貼り直し不要)
- 上書き方式。履歴は持たない

### レコードへの組み込み

- `RECORD_FIELDS` に `"shikiho_gyoseki"` を追加 (未知フィールド警告を避ける)
- `create_research_record()` に `shikiho_gyoseki: Optional[Dict] = None` 引数を追加し、
  返却 dict に必ずキーを含める
  → 既存テスト `test_create_research_record_minimal` が
    `set(rec.keys()) == RECORD_FIELDS` を検証しているため、
    ファクトリ側にキーを足さないと **必ず失敗する**。
    `create_research_record()` と `RECORD_FIELDS` は常に対応させる
- 読出時正規化 `_normalize_research_record_on_read()` に
  `record.setdefault("shikiho_gyoseki", None)` を入れ、既存レコードの後方互換を担保する
- 既存レコードのマイグレーションは不要 (キー無し = 未入力)

## パーサー仕様

新規モジュール `scripts/shikiho_gyoseki.py`。WebApp・MCP・テストから共用する。

```python
def parse_shikiho_gyoseki(text: str) -> Dict[str, Any]:
    """楽天証券の四季報業績テーブル貼り付けテキストをパースする。

    Raises:
        ValueError: 予想通期行が1件も取れないとき
    """
```

### 処理手順

1. 行分割。タブ or 連続空白で列分割
2. 第1列が期ラベルの行のみ採用。ラベル正規表現:
   `^(連|単|◇|※)?\s*(\d{2})\.(\d{1,2})(予|\*)?` 相当
   - 中間期行 (`中`・`26.9中` 等) は除外 → **通期行のみ**
   - `*` サフィックス (変則決算) は実績扱いで許容
3. **売上高・営業利益は期ラベルの次の2列に固定**。カンマ除去、`-`/空欄は `None`
   - 「数値としてパースできた順に2つ」方式にしない。営業利益が `-` (非開示) の
     銘柄で経常利益を営業利益として取り込み、もっともらしい誤値が DB・AI 分析
     まで流れる (銀行・REIT 等で現実に起こる)
4. **今季 = 直前実績の次に来る予想期**。位置 (`forecasts[-2]`) で選ばない
   - 予想が3期以上並ぶと、位置指定では今季を飛ばして来季・再来季を掴み、
     さらに2年分の変化を1年の成長率として表示してしまう
   - 予想行は**実績行の決算月と一致するものだけ**を対象にする
     (四季報には中間期予想 `連27.9予` が混じることがある)
   - 同月の予想が0件なら**別月に流用せず `ValueError`**。「決算期変更」と
     「貼付範囲に中間期予想しか入っていない」は貼付テキストから判別できない
     (過去に決算月を変更した会社では実績行の月が複数あるため、それも根拠に
     ならない)。誤って半期を通期として保存するより拒否する方が安全。
     決算期変更後は新しい決算月の実績行がいずれ載るので、その時点で通る
   - 予想が0件なら `ValueError`
5. `予` 無し通期行のうち `this_year` より前で最新のものを `prev_year`
6. 成長率計算: `(new - old) / old * 100`
   - 分母が `None` / 0 / 負値のときは `None` (赤字転換等は率で語れない)
7. `updated_at` は `date.today()`
8. `raw_text` は strip 済みに正規化 (呼び出し元ごとに差が出ないように)

### エラー時

`ValueError` を投げ、呼び出し側 (WebApp) がメッセージ表示して **保存しない**。

## 実装ステップ

### Step 1: パーサー + テスト

- `scripts/shikiho_gyoseki.py` 新規
- `tests/test_shikiho_gyoseki.py` 新規 — **parametrize で 3〜4 本に集約**
  - 正常系 (issue のサンプル 7318): 3期分の値と成長率
  - 予想1件のみ / 実績行なし / 予想0件(ValueError) を parametrize
  - 赤字→黒字 (分母が負) で成長率 `None`
  - 中間期行の混入を無視すること

検証: `pytest tests/test_shikiho_gyoseki.py -v`

### Step 2: research_shelve への保存

- `RECORD_FIELDS` に `"shikiho_gyoseki"` 追加
- `_normalize_research_record_on_read()` に `setdefault` 追加
- `webapp/helpers.py` に `save_shikiho_gyoseki(code_s, raw_text)` 追加
  - 空文字なら `record["shikiho_gyoseki"] = None` (クリア)
  - パース成功時のみ `upsert_research_record`
  - `analysis_date_raw` は既存 `save_shikiho` と同様、変化時に当日更新

検証: `pytest tests/test_research_shelve.py tests/test_webapp_helpers.py -v`

### Step 3: MCP への露出 (**本命**)

`scripts/mcp/shikiho_server.py` の `get_shikiho_data()` に `gyoseki` を追加。

```python
return {
    "code_s": ..., "found": True, "stock_name": ..., "overview": ...,
    "shikiho_comments": formatted,
    "gyoseki": _format_gyoseki(record.get("shikiho_gyoseki")),  # 追加
    "source": "research_shelve",
    "total_comments": len(comments),
}
```

- `_format_gyoseki()` は未入力なら `None` を返す
- 単位を明示するため `"unit": "百万円"` を含める
- `raw_text` は **MCP では返さない** (AI に生テキストを渡す価値が薄く、トークンを食う)
- `found: False` のケースでも `gyoseki: None` を含める (契約を一定に)

`MCPServer` の `instructions` と `get_shikiho` の docstring を更新し、
**AI に扱いの前提を伝える**:

> 業績予想は四季報の予想値であり、特に来季 (2期先) は不確実性が高い。
> 定量的な評価指標としてではなく、会社の成長シナリオを読むための定性的な材料として扱うこと。
> 単位は百万円。

検証: `pytest tests/test_mcp_shikiho_server.py -v`
テストは既存の `research_db` fixture に `shikiho_gyoseki` 入り銘柄を1件足し、
「入力済み銘柄で gyoseki が返る / 未入力銘柄で None が返る」を parametrize 1本で確認。

疎通確認: 本セッションの `mcp__shintakane-shikiho__get_shikiho` は main の
サーバーを見るため、ブランチの変更は MCP 再起動まで反映されない。
**実装後にユーザーへ「Claude Code 再起動して MCP で確認」を依頼する**。

### Step 4: 入力 UI (`/portfolio/shikiho`)

- `GET /portfolio/shikiho/<code_s>/data` の返却に `shikiho_gyoseki_raw`
  (= `raw_text`) と整形済み `shikiho_gyoseki` を追加
- `POST /stock/<code_s>/shikiho_gyoseki` を `webapp/routes/memo.py` に追加
  - 既存 `post_shikiho` と同じパターン。`ValueError` を 400 + メッセージで返す
- `portfolio_shikiho.html` に「四季報業績」セクション追加
  - textarea (貼り付け用) + 保存ボタン + パース結果プレビュー (今季/来季の成長率)
  - 既存の四季報コメントフォームとは **別 form / 別 POST** にする
    (既存の autosave / sendBeacon 直列化ロジックに業績を混ぜると壊れやすい)
  - 銘柄切替時に既存 `raw_text` を textarea に流し込む

検証: `pytest tests/test_webapp_routes.py -v` + WebApp 起動して実 URL で目視

### Step 5: 詳細ページ表示 (【四季報予】グループ)

`get_current_research_data()` は現在 `code_rank.csv` 相当の `row_dict` だけを読み、
`_CR_GROUPS` のループで整形している。ここに `research_shelve` 由来のグループを
足すため、以下の方針を取る。

**方針: `_CR_GROUPS` のループには混ぜず、ループ後に【業績】の直後へ insert する。**
(当初は末尾 append としていたが、実物を見ると【更新日】の後ろになり、
実績ベースの【業績】との対比が失われたため、隣接させる位置に変更した)

```python
# 既存の _CR_GROUPS ループ (row_dict 由来) はそのまま
...
# 四季報予想は出典が research_shelve なので、ループ外で【業績】の直後に差し込む
shikiho_items = _build_shikiho_gyoseki_items(code_s, research_record)  # 未入力なら []
if shikiho_items:
    names = [g[0] for g in groups]
    insert_at = names.index("業績") + 1 if "業績" in names else len(groups)
    groups.insert(insert_at, ("四季報予", shikiho_items))
return groups
```

- `_CR_GROUPS` / `_CR_LABEL_MAP` は **`code_rank.csv` の列マッピング表**であり、
  ここに CSV に存在しないキーを混ぜると `row_dict.get(key, "")` が常に空を返し、
  マッピング表の意味が壊れる。**混ぜない**
- `_build_shikiho_gyoseki_items(code_s)` はヘルパ内の小関数。
  `research_shelve.get_research_record(code_s)` を読み、
  `[(ラベル, 値), ...]` を返す。未登録・未入力なら `[]`
  - 例: `[("27.3予", "+36.8%/+69.0%"), ("28.3予", "+21.4%/+16.2%")]`
  - ラベルは `label` から `連`/`単` の接頭辞を落とした期表記
  - 成長率が `None` の項目は `—` (既存の空値表示に合わせる)
- **追加の DB open が1回増える**点は許容する。詳細ページは1銘柄表示で、
  既に `research_shelve` を `record` として読んでいるため、
  `detail.py` 側で取得済みの `record` を引数で渡して**二重 open を回避する**
  (`stock_data` を渡している既存の引数パターンと同じ)
  → シグネチャ: `get_current_research_data(code_s, stock_data=None,
    portfolio_status=None, research_record=None)`
- `detail.html` は **変更不要**。既存の `cr-row` ループがそのまま描画する
- `shikiho_gyoseki` が無い銘柄はグループごと出ない (既存の空グループ省略と同じ挙動)

検証: `pytest tests/test_webapp_helpers.py -v` + WebApp で入力済み/未入力の両方を目視

## テスト方針

CLAUDE.md の「1 PR で 5本以下」に従い、**新規テストは 5本以内**:

1. `test_shikiho_gyoseki.py`: パーサー正常系 + 異常系を parametrize で 2 本
2. `test_shikiho_gyoseki.py`: 成長率の分母異常 (0/負/None) を parametrize で 1 本
3. `test_mcp_shikiho_server.py`: MCP 返却契約 (入力済み/未入力) を parametrize で 1 本
4. `test_webapp_helpers.py`: `save_shikiho_gyoseki` の保存 + クリアを 1 本
5. `test_webapp_helpers.py`: 【四季報予】グループの有無 (入力済み/未入力) を
   parametrize で 1 本

`test_research_shelve.py` への**新規テスト追加は行わない**が、
既存の `test_create_research_record_minimal` は `set(rec.keys()) == RECORD_FIELDS` を
検証しているため、`create_research_record()` にキーを足せば追加変更なしで通る
(Step 2 で必ず `pytest tests/test_research_shelve.py -v` を実行して確認する)。

## 懸念・確認ポイント

### 1. 楽天証券の列構成が銘柄で変わる可能性

issue のサンプルは 7列 (売上高/営業利益/経常利益/利益/1株益/1株配)。
金融・REIT 等で列構成が違う可能性がある。

→ **列位置で固定** (期ラベルの次の2列を売上高・営業利益とする) 方式を採る。
数値でないセル (`-` 非開示) は `None` とし、**その期の成長率を出さない**。
当初は「数値としてパースできた順に2つ」方式だったが、コードレビューで
営業利益が `-` の行から経常利益を繰り上げて拾う (= もっともらしい誤値が
DB・AI 分析まで流れる) ことが実証されたため変更した。

残る制約: 「売上高」列が無い銘柄 (銀行の経常収益等) では、
列の意味が売上高でないまま取り込まれる。ラベル行を読まないと判別できず、
ウォッチ銘柄が銀行になることは稀なので **Phase 1 では対応しない**。
誤りに気付いた場合は `raw_text` が残っているので再パースで救える。

### 2. 期ラベルの表記ゆれ

楽天証券は `連22.3*` のように会計基準や変則決算にサフィックスを付ける。
`予` 以外のサフィックスは実績扱いで一律許容する。

### 3. 更新の鮮度

四季報は年4回。`updated_at` が古いレコードは予想が陳腐化している可能性がある。
MCP 返却に `updated_at` を含め、**AI 側が古さを判断できるようにする**。
`/portfolio/shikiho` 側での「業績未入力」進捗表示は Phase 1 では作らない
(コメントの `done_count` と混ざると混乱するため)。

### 4. スコアリングへの非反映を明示する

`shikiho_gyoseki` は `make_stock_db.py` のスコア計算から一切参照しない。
将来誤って参照されないよう、`shikiho_gyoseki.py` の docstring に
「AI 分析用の定性情報。スコアリングには使わない (issue #346)」と明記する。

## ユーザー確認事項 (実装完了時に提示)

- `/portfolio/shikiho` の業績フォームが既存のコメント自動保存と干渉していないか
  (銘柄切替時にコメントが保存されずに飛ばないか)
- 「現在の調査材料」の【四季報予】グループが、直上の【業績】グループ (実績ベース) と
  紛らわしくないか。グループ名だけで「予想」と分かるか、期ラベル (27.3予) の表記で足りるか
- 未入力銘柄でグループが消えることが、「データが無い」ではなく「壊れている」と
  誤解されないか (入力導線が `/portfolio/shikiho` にあることが分かるか)
- MCP 再起動後、`get_shikiho` の返却に `gyoseki` が乗り、AI が
  「2期先予想は不確実」という前提を踏まえたコメントを返すか
