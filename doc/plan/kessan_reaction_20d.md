# 決算反応に 20営業日後 を追加 + 表示桁数ルール変更プラン

## 背景・ゴール

disclosure 決算日セクションの反応率表示 (例: `[2Q] +16.2% / +51.3%`) は現状「翌営業日 / 5営業日後」の2つ。
ここに **20営業日後** を追加する。あわせて表示桁数ルールを以下に変更:

- `|x| >= 10` のとき: 整数表示 (例: `+16%`, `+51%`)
- `|x| < 10` のとき: 小数1桁 (例: `+2.7%`, `-0.3%`)

## 前提確認 (既に検証済み)

- `KESSAN_REACTION_PERIODS = (("1d", 1), ("5d", 5))` (`scripts/research_shelve.py:94`) が唯一の期間定義 (の SoT)
- 計算ロジック `_price_reactions_from_log` は periods をループするので汎用
- 永続化先 `post_price_changes` は dict (`{"1d": "+3.2", "5d": "+5.1"}` 形式) でスキーマ変更不要
- `price_log` 保持件数は `LOG_DAY=30` (`scripts/price.py:546`) で 20営業日後は理論上計算可能
- ただしテンプレ `scripts/webapp/templates/disclosure.html:40,42,73-76` は `'1d'/'5d'/'pts'` を**ハードコード**しているため、20d 表示には手当てが必要

## 実装内容

### 1. `scripts/research_shelve.py:94`

```python
KESSAN_REACTION_PERIODS = (("1d", 1), ("5d", 5), ("20d", 20))
```

これだけで:
- 計算: `_price_reactions_from_log` が `20d` キーを自動算出
- 保存: `update_pts_reactions` / `kessan_comments` 書込時に `20d` キーが入る
- backfill: `_backfill_post_price_changes_for_entries` (`webapp/helpers.py:57`) が次回ページ表示時に既存決算の `20d` を `price_log` から補完 (補完可否は LOG_DAY マージン次第)

### 2. `scripts/webapp/helpers.py:_format_reaction` (654行)

桁数ルールを変更:

```python
def _format_reaction(before_price, after_price):
    if before_price is None or before_price == 0:
        return ""
    try:
        change = (float(after_price) / float(before_price) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return ""
    sign = "+" if change >= 0 else ""
    # |x| >= 10 → 整数 / |x| < 10 → 小数1桁
    if abs(change) >= 10:
        return f"{sign}{change:.0f}"
    return f"{sign}{change:.1f}"
```

### 3. テンプレート / JS の 20d 対応 (画面間で揃える)

ハードコードしている全箇所に `'20d'` を追加する:

**a. `scripts/webapp/templates/disclosure.html`**
- 40,42行付近: `'1d'/'5d'` の隣に `'20d'` を追加
- 73-76行: `{% set pc20 = s.post_price_changes['20d'] %}` を追加し、出力ブロックも併記

**b. `scripts/webapp/templates/detail.html` (codex指摘1)**
- 408-409行: `pc1/pc5` 周辺に `pc20 = entry.post_price_changes['20d']` を追加し、表示も併記
  - disclosure と detail で表示行が揃うように

**c. JS 側の `updatePostPriceChangeDisplay` (codex 出力 298-309行付近)**
- `kessan-pc-20d` クラスを増設し、`changes['20d']` を反映するブロックを追加
- editor 内のスパン (templates 側) にも `kessan-pc-20d` クラスのスパンを追加

CSS で `.kessan-pc-20d` のスタイルが必要なら最小限あわせる (既存 1d/5d と同等で十分)。

### 4. `scripts/make_market_db.py` の決算日カード

`_html_kessan` (1632行〜) は反応率を描画していないことを再確認。変更なし。

### 5. 既存決算データへの反映 (codex指摘2を反映)

- **新規決算 (これから発表される分)**: 次回 `update_pts_reactions` / 決算コメント保存時に `20d` が自動付与され、20営業日経過後の表示更新で値が埋まる
- **既存決算**: backfill (`_backfill_post_price_changes_for_entries`, `webapp/helpers.py:57`) は `price_log` (LOG_DAY=30) を参照するため、決算日が `price_log[-1]` (最古) より古いと補完不能
  - 結果: 過去の決算カードの `20d` は多くが恒久的に空表示 (`-`) になる
  - **これは要件「これから先の決算で20日後も見たい」と合致する範囲**であり、過去履歴を遡って埋める追加対応 (yfinance 再取得など) は今回スコープ外とする
  - 「過去履歴の `20d` を全部埋めたい」という追加要件が出たら別 PR で yfinance による単発再取得スクリプトを書く方針

## テスト

`tests/test_webapp_helpers.py` に既存の `_price_reactions_from_log` / `_format_reaction` テストがあるはず。そこに次の3本を追加 (parametrize で集約):

1. `_format_reaction` の桁数ルール: `|x| >= 10` は小数なし、`|x| < 10` は小数1桁、`|x| == 10.0` ぴったりも整数
2. `_price_reactions_from_log` が `20d` キーを返す (price_log が十分長いケース)
3. `price_log` が20件未満の場合 `20d` が "" になる

## 検証手順

1. `pytest tests/test_webapp_helpers.py tests/test_research_shelve.py -v`
2. 開発サーバを再起動 (= `KESSAN_REACTION_PERIODS` 反映)
3. `/disclosure` で過去決算カードに 1d/5d/20d の3値が並び、桁数ルールが守られていることを目視

## 影響範囲

- `KESSAN_REACTION_PERIODS` 変更1行: 計算/保存/backfill が自動追従
- `_format_reaction` 変更: 桁数ルールが全反応率表示で変わる (disclosure 決算日カードに限らず portfolio 等の決算反応列も対象)
- テンプレ disclosure.html: 20d 表示用の固定キー追加 (kessan-pc-20d クラスを CSS で揃える必要あれば対応)
- DB スキーマ変更なし
- 既存 `post_price_changes` dict には `20d` キーが順次追加されていく
