# extended ブレイクマーカーのサイズを強度連動にするプラン

## 目的・背景

portfolio 詳細チャート（RS ラインチャート）下部のブレイクマーカーのうち、
extended（高値追い圏で正規ブレイクから弾かれた候補、中抜き◇）のサイズが
現状 **固定 4.5（×1.8=8.1）** で、実際のブレイクの大きさ（出来高超過率）を
反映していない。

ユーザー要望: extended◇のサイズも通常ブレイク◆と同じ基準（出来高超過率の
強度バケット）に従わせ、◆と◇でサイズ・強度ロジックを共通化する。塗り（filled）と
不透明度だけで両者を区別する。

### 当初設計との関係（経緯）

extended は issue #111 / PR #338（コミット d67dd96）で導入。当初は「高値追い圏 =
規律上買わない対象外」のため **強度（出来高超過率）をあえて見せず、MA10乖離率のみ保存・
固定サイズ・半透明** にする意図的設計だった（コード内コメント「強度は出さない」）。
今回の変更はこの方針を一部転換し「extended でもブレイクの強さ（出来高超過率）を
サイズで可視化する」もの。ただし **中抜き◇と半透明は『対象外』サインとして維持** し、
サイズだけ実態連動にする。半透明はサイズが大きくなると薄く見づらくなるため
opacity 0.3 → 0.5 に少し濃くする。

### 実測で確認済みの事実（5572.T）

| 日付 | 区分 | MA10乖離 | 出来高超過率 | 強度バケット |
|---|---|---|---|---|
| 06/15 | ◆通常ブ | -0.6% | +244% | 強 |
| 06/05 | ◇extended | +13.5% | +256% | 強 |
| 06/03 | ◇extended | +22.0% | +4944% | 強 |

→ extended でも出来高超過率は「強」相当が出る。固定サイズだと実際の強度を表現できていない。

## 現状の制約（重要）

- `breakout_extended` の保存形式は `"MM/DD,kairi"`（2要素）。**出来高超過率は保存されていない**。
- `breakout`（通常）は `"MM/DD,per"`（per=出来高超過率）を保存。
- `_signal_strength_bucket("ブ", num)` は num を出来高超過率前提で判定するため、
  extended の num（=MA10乖離率）をそのまま渡すと誤判定になる。

→ サイズを出来高超過率の強度に従わせるには、**extended に出来高超過率を新規保存する**
必要がある。生成側・パース側・描画側の3層に手が入る。

## 変更方針

extended の保存形式を `"MM/DD,kairi"` → `"MM/DD,kairi,per"`（3要素）に拡張する。
per は通常ブレイクと同じ計算式 `max(100*vol/avg_vol - 100, 0)`。
これにより後方互換（既存2要素データは per 欠落として読める）を保ちつつ、
描画側で per から強度バケットを引ける。

## 変更ファイルと具体的変更

### 1. `scripts/price.py`（生成側）

`make_signal`（≈1799-1806 行）の extended 分岐で per も計算して保存する。

```python
# 現状（1799-1806 付近）
if kairi <= 5:
    per = max(100 * vol / avg_vol - 100, 0)
    breaks.append("%s,%d" % (day, per))
elif kairi <= BREAKOUT_EXTENDED_KAIRI_MAX:
    breaks_ext.append("%s,%d" % (day, round(kairi)))
```

変更後:

```python
if kairi <= 5:
    per = max(100 * vol / avg_vol - 100, 0)
    breaks.append("%s,%d" % (day, per))
elif kairi <= BREAKOUT_EXTENDED_KAIRI_MAX:
    # extended も出来高超過率 per を保存し、描画側で強度バケットに使う。
    # 形式: "MM/DD,kairi,per"（既存 "MM/DD,kairi" との後方互換のため per は末尾追加）
    per = max(100 * vol / avg_vol - 100, 0)
    breaks_ext.append("%s,%d,%d" % (day, round(kairi), per))
```

### 2. `scripts/make_stock_db.py`（extract_signals パース側）

`extract_signals`（1109-1133 行）の extended パースで、3要素目（per）があれば
`extended_per` として entry に持たせる。2要素しかない旧データは per 欠落とする。

```python
# 現状: num = int(spl[1]) のみ
entry = {
    "kind": kind, "mmdd": spl[0], "num": num,
    "sig_date": sig_date, "delta": delta,
}
if extended:
    entry["extended"] = True
```

変更後（extended のときだけ 3要素目を読む）:

```python
entry = {
    "kind": kind, "mmdd": spl[0], "num": num,
    "sig_date": sig_date, "delta": delta,
}
if extended:
    entry["extended"] = True
    # 3要素目があれば出来高超過率（描画側の強度バケット用）。
    # 旧2要素データは per なし → 描画側で従来の固定サイズにフォールバック。
    if len(spl) >= 3:
        try:
            entry["extended_per"] = int(spl[2])
        except ValueError:
            pass
```

注意: 通常ブレイク（extended=False）の num はそのまま per なので変更不要。
num の意味は extended のみ MA10乖離率のまま（tooltip の乖離表示が依存）。

### 3. `scripts/webapp/helpers.py`（描画側）

#### 3-1. `_resolve_signal_markers`（2683-2697 行）

extended marker に `extended_per` を引き継ぎ、per があれば強度バケットを付与する。

```python
if s.get("extended"):
    m["extended"] = True
    per = s.get("extended_per")
    if per is not None:
        m["strength"] = _signal_strength_bucket("ブ", per)
else:
    m["strength"] = _signal_strength_bucket(s["kind"], s["num"])
```

#### 3-2. マーカー描画（3165-3175 行）

extended のサイズを、strength があれば size_map から引く（=◆と共通サイズ）。
strength が無い（旧データ）場合のみ従来の固定 EXT_SIZE にフォールバック。
塗り（filled=False, 中抜き）と不透明度は extended 識別子として維持する。

```python
if m.get("extended"):
    # extended は中抜き◇。サイズは通常ブレイク◆と共通の強度バケット基準。
    # per 未保存の旧データは strength を持たないため固定サイズにフォールバック。
    ext_size = size_map[m["strength"]] if m.get("strength") else EXT_SIZE
    title = "ブ(extended) %s 乖離+%d%% 高値追い圏・対象外" % (sig_md, m["num"])
    parts.append(_svg_diamond(
        m["x"], y_bu, ext_size * BU_SIZE_SCALE, "#f57c00", 0.5, title, filled=False))
    continue
```

→ 6/5・6/3 の extended は strength="強" → size_map["強"]=6.0 → ×1.8=10.8 となり、
6/15 の通常ブレイク◆（強, 10.8）と同サイズになる。

## 後方互換・移行

- 既存 DB の `breakout_extended` は2要素（per 欠落）。読み出し側は per 欠落を許容し、
  従来の固定サイズで描画する（壊れない）。
- per は次回の銘柄更新（`make_stock_db.py update` / `list_all_db`）で3要素形式に
  書き換わる。マイグレーションスニペットは不要（自然に上書き更新される）。
- shelve スキーマ自体は変わらない（リスト内文字列のフォーマット拡張のみ）。

## 検証ポイント（ゴール）

1. `pytest tests/test_price.py tests/test_make_stock_db.py -v` が通る。
2. extract_signals の extended エントリに、3要素データで `extended_per` が入り、
   2要素データでは入らないこと（parametrize で1〜2本）。
3. `cd scripts && python make_stock_db.py update 5572` 後、
   `breakout_extended` が `"06/05,13,256"` 形式（3要素）になること。
4. ブラウザで 5572 詳細チャートを開き、extended◇が 6/15 の◆と同サイズ（強）で
   描画されること（中抜き・半透明は維持）。スクリーンショットで確認。

## 非対象（やらないこと）

- ポケットピボット△のサイズ基準は変更しない。
- 通常ブレイク◆のサイズ・強度しきい値は変更しない。
- extended の num（MA10乖離率）の意味・tooltip 表示は変更しない。
- 強度しきい値（200/100）の調整はこのプランの範囲外。
