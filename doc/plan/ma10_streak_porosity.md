# 30日連続10MA上回り判定に porosity(浸透許容)を適用

## 背景

保有銘柄ダッシュボードのトレンド列で、10MA乖離を縦点線で表示し、
「30営業日連続で終値が10MAを上回り続けた」場合に赤太線へ切替えている
(利確基準有効の印)。出典は Gil Morales / Chris Kacher
"Trade Like an O'Neil Disciple" (邦題『株式売買スクール』)。

現状の連続判定 `_above_ma10(i)` は「その日の終値 > その日の10MA」を厳密判定し、
**1日でも終値が10MAを割れたら即座に連続を切断**している
(`price.py:461` コメント「一致・下回りで連続を切断(厳密)」)。

これは原典の **violation(違反) 定義より厳しすぎる**。原典では:

> violation = 終値が移動平均線を割り込み、**かつ翌日(以降)にその割り込んだ日の
> ザラ場安値(intraday low)を下回る**こと

の2条件 AND で初めて「本物の崩れ」と判定し、終値1回の軽い潜り(porosity)は
ダマシとして救済する。現実装は porosity を許容せず、一時的な潜りで赤太線が
点きにくくなっている。

## 確定方針 (案A: 原典 violation の否定を継続条件にする)

連続上回り判定を以下に変更する:

- **継続条件**: ある日 i の終値が10MAを割っても、その**翌営業日(時系列で次の日)の
  安値が「割れ日 i の安値」を下回らなければ**、連続を切断しない(porosity許容)。
- **切断条件**: 終値が10MAを割り、**かつ翌営業日の安値が割れ日 i の安値を下回った**
  (= violation成立) 場合に連続を切断する。
- 終値が10MAを割っていない日は従来どおり連続維持。

### violation の監視窓を「翌営業日のみ」に確定する理由

原典の文言は「終値割れ + **翌日以降**に割れ日安値を下回る」だが、本実装では
**監視窓を「割れ日 i の翌営業日 (i-1) のみ」に固定する**。理由:

1. violation は「割れた直後にフォロースルー(安値更新)があるか」で本物/ダマシを
   判定するルール。割れから2日以上経った安値更新は、新しい局面の値動きであり
   同一 violation の確認とは見なさない (原典でも実務上は割れ直後で判定)。
2. 「翌日以降・無期限」にすると、割れ日のあと一度でも安値更新があれば過去に遡って
   切断され、判定が監視窓終端の定義に依存して不安定になる。終端を「翌営業日」に
   固定することで判定を決定的にする。
3. 本指標は表示専用 (赤太線の点灯)。porosity の主目的は「1日だけの軽い潜りの救済」
   であり、それは翌営業日の安値確認で十分達成できる。

この限定は意図的な仕様であり、原典の「翌日以降」を翌営業日に絞ったものである旨を
コード/テストのコメントに明記する。

STREAK_DAYS は **30 のまま据え置き** (7週=35日化は yfinance `2mo`=41営業日では
44本必要で不足。`3mo` 化は全銘柄再取得・取得負荷1.5倍を全処理に強いるため、
表示専用・スコア非連動の本指標には費用対効果が低く今回スコープ外)。

## データ構造

`calc_ma10_kairi_indicators` は現在 `closes` (終値のみ) を受ける。
porosity判定に**安値が必要**なため、`lows` 引数を追加する。

- 入力元 `_calc_daily_indicators` の `daily_price_list` は
  `[3]安値, [4]終値` を持つ (price.py:499 docstring)。安値は取得済み。
- 両リストとも「新しい日が先頭」。closes[i]/lows[i] は同じ日 i を指す。
- 「翌営業日」= 時系列で i の次の日 = リスト上は **index i-1** (新しい側)。
  i=0 (最新日) には翌日が無いので、violation判定は i>=1 の割れ日についてのみ行う。
  最新日 i=0 が終値割れの場合、翌日未到来 → violation未確定 → 連続は切断しない
  (porosity救済側に倒す。翌日データが来た次回実行で確定判定される)。

## 変更内容

### price.py

`calc_ma10_kairi_indicators(closes)` → `calc_ma10_kairi_indicators(closes, lows)`:

```python
def calc_ma10_kairi_indicators(closes, lows):
    res = {}
    # 乖離率は従来どおり closes のみで計算
    if len(closes) >= 10:
        ma10 = sum(closes[:10]) / 10
        res["price_kairi_ma10"] = (closes[0] - ma10) * 100 / ma10 if ma10 else None
    else:
        res["price_kairi_ma10"] = None

    STREAK_DAYS = 30

    def _ma10(i):
        return sum(closes[i:i + 10]) / 10 if len(closes) >= i + 10 else None

    def _streak_holds(i):
        """日 i が連続を維持するか (porosity許容)。
        終値が10ma上なら維持。終値割れでも翌営業日(i-1)の安値が割れ日 i の
        安値を下回らなければ維持 (violation未成立)。最新日 i=0 の割れは
        翌日未到来のため未確定 → 維持側に倒す。"""
        ma = _ma10(i)
        if ma is None or ma == 0:
            return False  # 10ma算出不可 = データ不足
        if closes[i] > ma:
            return True   # 終値が10ma上 → 維持
        # 終値が10maを割った日。violation成立(翌日安値 < 割れ日安値)なら切断
        if i == 0:
            return True   # 翌日未到来 → 未確定、porosity救済側
        return lows[i - 1] >= lows[i]  # 翌日安値が割れ日安値を割らなければ維持

    had_streak = False
    if len(closes) >= STREAK_DAYS + 9:
        max_start = len(closes) - (STREAK_DAYS + 9)
        for s in range(max_start + 1):
            if all(_streak_holds(i) for i in range(s, s + STREAK_DAYS)):
                had_streak = True
                break
    res["ma10_above_streak_30"] = had_streak
    return res
```

**呼び出し元は2箇所あり、`price_list` のインデックス体系が異なる**ので
各々で正しく closes/lows を生成する。

#### 呼び出し元1: `_calc_daily_indicators` (price.py:558-562 付近)

`daily_price_list` = 8要素タプル `[3]安値, [4]終値` (price.py:499):

```python
    try:
        closes = [int(float(d[4].replace(",", ""))) for d in daily_price_list]
        lows = [int(float(d[3].replace(",", ""))) for d in daily_price_list]
    except (ValueError, IndexError):
        closes = []
        lows = []
    dic.update(calc_ma10_kairi_indicators(closes, lows))
```

#### 呼び出し元2: `parse_price_text_from_list` (price.py:1766-1769)

ここの `price_list` は**別構造** `(日付[0],始値[1],高値[2],安値[3],終値[6],出来高[5])`
で、終値が `[6]`・安値が `[3]` (price.py:1717 で `[3]`=安値, `[6]`=終値として使用)。
既に int 化済みなので `float`/`replace` 不要:

```python
    closes = [row[6] for row in price_list]
    lows = [row[3] for row in price_list]
    price.update(calc_ma10_kairi_indicators(closes, lows))
```

両呼び出し元とも「新しい日が先頭」である点は共通 (porosity判定の前提)。

コメント (price.py:461-462) を porosity仕様に合わせて更新。

### tests/test_price.py

CLAUDE.md のテスト方針 (1PR5本以下、parametrize集約) に従い、porosity の
コア挙動と「2つ目の特殊経路」回帰を最小本数でカバーする。

#### 1. `test_ma10_above_streak_30` (既存 parametrize) に porosity ケース追加

`_calc_daily_indicators` (8カラム経路) で:
- 1日だけ終値割れ + 翌日安値が割れ日安値を上回る → 連続維持 (True)
- 終値割れ + 翌日安値が割れ日安値を下回る (violation成立) → 切断 (False)

既存ケースは終値を "0"/"1" で割り込ませているが、安値カラムも併せて設定し、
porosity判定が意図通り効くようにする (安値を割れ日安値より高く/低く作り分ける)。

#### 2. `TestParsePriceTextFromList` (既存クラス) に経路回帰テスト1本追加

第2呼び出し元 `parse_price_text_from_list` は **7カラム系
(`[3]=安値, [6]=終値`)** の特殊経路で、index取り違え (`[4]`/`[6]`) や lows
渡し忘れが起きやすい。これを直接叩く porosity 回帰を parametrize 1本で追加:
- 終値1日割れ + 翌日安値が割れ日安値を上回る → `ma10_above_streak_30` 維持
- 終値割れ + 翌日安値が割れ日安値を下回る → 切断

(2ケースを1つの parametrize に集約。これにより「経路依存で赤太線判定がズレる」
回帰を検出できる)

## スコープ外

- STREAK_DAYS の 7週(35日)化 (yfinance `3mo` 化が前提、費用対効果低)
- スコアリング/ランキングへの組込み (本指標は表示専用、従来どおり非連動)
- 売りシグナル(violation)そのものの新規列・通知 (別issue候補)
- violation 監視窓を「翌営業日以降の複数日」へ拡張すること (本実装は翌営業日のみに
  固定。上記「確定方針」の理由参照)
- DB マイグレーション (次回スクレイピングで上書き再計算、後方互換不要)

## 検証

```
pytest tests/test_price.py tests/test_webapp_helpers.py -v
cd scripts && python make_stock_db.py update <赤太線が出ている保有銘柄>  # 再計算確認
```

ダッシュボードのトレンド列で、軽い1日割れで赤太線が消えなくなることを目視確認
(運用しながら確認)。
