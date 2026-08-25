# 売買履歴 総合成績の年単位化

## 背景

`/trade-history` の売買履歴タブに出ている成績サマリー (勝率 / ペイオフレシオ /
期待値 / 実現損益 + 勝ち負け内訳) は現在「全期間の通算」1つだけ。
年ごとの成績を見たい。年はリストボックス (`<select>`) で選ぶ。

現状の実装:
- `scripts/webapp/routes/trade_history.py` `trade_history()` が
  `fill_summary` / `stock_summary` などを計算し `render_template` に渡す
- `scripts/webapp/templates/trade_history.html` の
  `fill_summary_block(...)` マクロが2箇所 (エピソード単位ビュー l.117 /
  銘柄単位ビュー l.317) で呼ばれる

## 決定事項 (ユーザー確認済み)

1. 銘柄単位ビューも年で絞る。**年でエピソードを絞ってから銘柄集約**する
   (`build_stock_rollups(その年のエピソード)`)。エピソード単位と実現損益・
   期待値が一致する性質を保つ。
2. リストボックスの選択肢は**各年のみ**(全期間は出さない)。初期値は**今年**。

## 年の定義

エピソードの `last_trade_date`[:4] を年とする。クローズ済みエピソードでは
最終約定日 = 手仕舞い日なので「その年に決済した取引の成績」になる。
サマリーの母数は現状どおり `ep["closed"] and ep["pl"] and not ep["split_suspect"]`
なので、保有中エピソードはどの年にも入らない。

## 実装方針

データ量が小さい (数百エピソード) ので、**全年分のサマリーをサーバで計算して
すべて描画し、`<select>` の change で CSS display を切り替える**。
既存のビュー切替 (`th-view-btn`) と同じクライアント側切替パターンで、
再読込もクエリパラメータも不要。

### routes/trade_history.py

`fill_summary` / `fill_total_pl` / `fill_priced_count` / `fill_closed_count` と
`stock_summary` / `stock_priced_count` / `stock_closed_count` を計算している
ブロックを、年ごとにループする形へ置き換える。

```python
def _summarize_fill_episodes(episodes):
    """エピソード群から (エピソード単位サマリー, 銘柄単位サマリー) を返す。

    現行の trade_history() の集計ロジックを関数化したもの。年別サマリーで
    年ごとに呼ぶため切り出す。

    銘柄単位は split_suspect (分割・併合の疑いだが未換算) を除いたエピソードだけを
    集約する。build_stock_rollups() 自体は split_suspect を除外しないので、
    素の rollups を使うとエピソード単位と母数がずれる (codex レビュー指摘)。
    """
    valid = [ep for ep in episodes if not ep.get("split_suspect")]
    pls = [ep["pl"] for ep in valid if ep["closed"] and ep["pl"]]
    total_pl = sum(p["profit_amount"] for p in pls
                   if p["profit_amount"] is not None)
    episode_part = {
        "summary": calc_trade_summary(pls),
        "total_pl": total_pl,
        "priced_count": len(pls),
        "closed_count": sum(1 for ep in valid if ep["closed"]),
    }
    # split_suspect 除外後のエピソードだけで銘柄集約 → 実現損益・期待値が
    # エピソード単位と厳密に一致する (金額加重なのでグループ化に依存しない)
    rollups = build_stock_rollups(valid)
    stock_pls = [r["pl"] for r in rollups if r["pl"]]
    stock_part = {
        "summary": calc_trade_summary(stock_pls),
        "total_pl": total_pl,        # 実現損益はエピソード単位と一致する
        "priced_count": len(stock_pls),
        "closed_count": sum(1 for r in rollups
                            if any(ep["closed"] for ep in r["episodes"])),
    }
    return episode_part, stock_part
```

なお現行コードは銘柄単位サマリーで `split_suspect` を除外していない
(`build_stock_rollups(fill_episodes)` をそのまま使っている)。上記により
エピソード単位と定義が揃うので、銘柄単位の勝率・期待値が現在の値から
わずかに変わりうる (該当銘柄がある場合)。これは issue #398 の意図に沿った是正。

`trade_history()` 側:

```python
fill_episodes = build_fill_episodes()

# 年別サマリー (last_trade_date の年)。サマリー母数はクローズ済みのみなので
# 保有中エピソードは年集計に入らない。
eps_by_year = {}
for ep in fill_episodes:
    if not ep["closed"]:
        continue
    year = (ep["last_trade_date"] or "")[:4]
    if year:
        eps_by_year.setdefault(year, []).append(ep)

summary_years = []   # [(year, episode_part, stock_part), ...] 年降順
for year in sorted(eps_by_year, reverse=True):
    ep_part, st_part = _summarize_fill_episodes(eps_by_year[year])
    summary_years.append((year, ep_part, st_part))

# 初期選択年 = 今年。今年の決済がまだ無ければ最新年にフォールバック。
current_year = datetime.datetime.now(ps.JST).strftime("%Y")
years = [y for y, _, _ in summary_years]
selected_year = current_year if current_year in years else (years[0] if years else "")
```

既存の `stock_rollups` (テーブル描画用、全期間) はそのまま残す —
一覧テーブルは年アコーディオン (issue #406) で既に全期間分を出しており、
今回変えるのはサマリーだけ。

`render_template` へは `summary_years` / `selected_year` を追加し、
`fill_summary` / `fill_total_pl` / `fill_priced_count` / `fill_closed_count` /
`stock_summary` / `stock_priced_count` / `stock_closed_count` は削除する
(テンプレートで使わなくなるため)。

### templates/trade_history.html

`fill_summary_block` マクロ本体は変更しない。呼び出し側を差し替える。

年セレクタ + 年ごとのサマリーブロックを出すマクロを追加:

```jinja
{% macro year_summary_group(view_key, part_index, count_label, hold_label) %}
{% if summary_years %}
<div style="display:flex;align-items:center;gap:0.5em;margin-bottom:0.5em;">
  <span style="color:#888;font-size:0.82em;">成績年:</span>
  <select class="th-year-select" data-view="{{ view_key }}"
          style="background:#1c1c1c;color:#ddd;border:1px solid #444;border-radius:4px;
                 font-size:0.85em;padding:0.25em 0.5em;">
    {% for year, ep_part, st_part in summary_years %}
    <option value="{{ year }}" {% if year == selected_year %}selected{% endif %}>{{ year }}年</option>
    {% endfor %}
  </select>
</div>
{% for year, ep_part, st_part in summary_years %}
{% set part = ep_part if part_index == 'episode' else st_part %}
<div class="th-year-summary" data-view="{{ view_key }}" data-year="{{ year }}"
     style="display:{{ '' if year == selected_year else 'none' }};">
  {{ fill_summary_block(part.summary, part.total_pl, part.priced_count,
                        part.closed_count, count_label, hold_label) }}
</div>
{% endfor %}
{% endif %}
{% endmacro %}
```

呼び出し (l.117 / l.317 の置き換え):

```jinja
{{ year_summary_group('episode', 'episode', "件", "平均保有") }}
{{ year_summary_group('stock', 'stock', "銘柄", "平均通算保有") }}
```

### JS (l.542〜 の `<script>` 内)

2つの `<select>` (エピソード単位ビュー用 / 銘柄単位ビュー用) は独立に置くが、
選択年は**両ビューで同期**する (ビューを切り替えたときに年が食い違うと混乱するため)。

```js
// 成績サマリーの年切替。エピソード単位/銘柄単位の2つの select を同期させる。
var yearSelects = document.querySelectorAll('.th-year-select');
function selectSummaryYear(year) {
  yearSelects.forEach(function (s) { s.value = year; });
  document.querySelectorAll('.th-year-summary').forEach(function (el) {
    el.style.display = (el.dataset.year === year) ? '' : 'none';
  });
}
yearSelects.forEach(function (s) {
  s.addEventListener('change', function () { selectSummaryYear(s.value); });
});
```

localStorage への記憶はしない (年は毎回意識して選ぶものなので、
古い年が固定されて残るほうが害が大きい)。

## 検証

1. `pytest tests/test_webapp_trade_history_routes.py -v`
   - 既存テスト `勝率`/`ペイオフレシオ` の存在チェックは、年別ブロックでも
     文字列は出るので通るはず。落ちたら期待値を年別前提に直す。
2. テスト追加 (1本): `summary_years` が年降順で、各年の
   `episode_part.total_pl` の合計が全期間の実現損益と一致すること。
3. 銘柄単位サマリーの母数が split_suspect 除外後になっていること
   (エピソード単位と実現損益・期待値が一致するかで確認)
4. WebApp を起動して目視:
   - 初期表示が今年 (2026年) のサマリーになっている
   - 年を切り替えると勝率・ペイオフ・期待値・実現損益が変わる
   - エピソード単位 ⇄ 銘柄単位でビューを切り替えても年が保たれる
   - 銘柄単位の実現損益がエピソード単位と一致する

## 影響範囲

- `scripts/webapp/routes/trade_history.py` (集計部の関数化 + 年ループ)
- `scripts/webapp/templates/trade_history.html` (サマリー呼び出し + select + JS)
- 一覧テーブル・アクションログタブ・CSV取込は変更なし
