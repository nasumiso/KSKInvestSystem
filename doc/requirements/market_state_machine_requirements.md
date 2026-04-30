# Market State Machine 要件定義 (issue #117 Part A)

> 既存の市場方向シグナル (`scripts/price.py:266-351 _calc_daily_indicators()` 内のDD/FTD判定 + `direction_signal`) を、O'Neil/IBD原典に準拠した3状態の State Machine に置き換える。
>
> 関連 issue: #117

---

## 1. 背景

### 現状の direction_signal 計算

`_calc_daily_indicators()` で以下を計算:

1. 直近20日の日足から DD (ディストリビューションデイ) と FTD (フォロースルーデイ) を検出
2. `direction_signal = "sell"` if `len(distribution_day) >= 5` else `"neutral"`
3. `market_db[index_name]` に `distribution_days` / `followthrough_days` / `direction_signal` として保存
4. `make_market_db.py:_html_market()` で表示

### 課題

#### 課題1: DD/FTD 判定が原典準拠でない

- DD閾値: `dr <= -0.1 and pr_pos >= 0.5` (前日比 -0.1% 以下 + 引け値が安値から半分以下)
  - O'Neil 原典は **「前日比 -0.2% 以下 + 出来高が前日より増加」** が標準
- FTD: `dr >= 1.7 and dv >= avg_vol`
  - O'Neil 原典は **「ラリーアテンプト Day 4 以降に +1.0% 以上の上昇 + 出来高増」**。ラリーアテンプトという概念自体が現状未実装
- Stalling Day (新高値付近で上昇率小・出来高増・下半分引け) 未検出

#### 課題2: DD の有効期限管理がない

原典では DD は以下のいずれかで失効する:
- 発生から 25 取引日経過
- 指数が DD の終値から 5% 以上上昇

現状は単純に直近 20 日窓でカウントしているのみで、失効ロジックがない。

#### 課題3: 2状態モデルしかない

現状 `neutral` / `sell` の 2状態のみ。原典は **3状態モデル** (Confirmed Uptrend / Uptrend Under Pressure / Market in Correction) で、各状態への遷移条件が明確に定義されている。2状態では Correction → Confirmed Uptrend の復帰判定ができない。

#### 課題4: シグナル連動の完全欠落

`make_stock_db.py:802-` の `make_signal()` は `direction_signal` を**一切参照していない**。Correction 中でもブレイクアウト/ポケットピボットのタグが出る。

### 課題4 の取り扱い

課題4 (シグナル連動) は本要件 (Part A) では対応せず、後続の Part B (issue #117 Part B) で扱う。Part A はあくまで **「market_state を計算して market_db に保存し、HTMLに表示する」** ところまで。`make_signal()` 改修は後続。

---

## 2. ゴール

1. **3状態 State Machine の導入**: confirmed_uptrend / uptrend_under_pressure / market_in_correction の3状態を計算・永続化する
2. **原典準拠の DD/FTD 判定**: O'Neil 原典に整合する通常 DD 検出 (Stalling Day はデータ拡張PRで対応) と、ラリーアテンプト追跡付き FTD 判定を実装する
3. **DD の有効期限管理**: 25取引日経過 + 5% 上昇による DD 失効を実装する
4. **後方互換**: 既存の `direction_signal` フィールドを廃止せず、新3値 (`confirmed_uptrend` / `uptrend_under_pressure` / `market_in_correction`) を入れて互換維持。HTML表示も合わせて更新する

---

## 3. アーキテクチャ方針

### モジュール分離

| モジュール | 責務 |
|---|---|
| `scripts/price.py` の `_calc_daily_indicators()` | **DD/FTD 候補の生検出**のみ。今日 + 直近の日足から「DD 候補日」「FTD 候補日」を検出する純関数 |
| `scripts/market_state.py` (新設) | **State 遷移計算と永続化用ヘルパー**。前日の state_meta を入力に、新state と新meta を返す純関数 |
| `scripts/make_market_db.py` の `make_db_common()` | 両者の**結線**。前日 state_meta を market_db から読み出し、`market_state.py` に渡し、結果を market_db に書き戻す |

`market_state.py` は I/O を持たない純関数の集まりとし、テスト容易性を確保する。

### State の永続化

各指数の `market_db[index_name]` に以下を追加:

```
market_state: str  # "confirmed_uptrend" | "uptrend_under_pressure" | "market_in_correction"
state_meta: {
    rally_attempt_start_date: str | None,
    rally_attempt_start_low: float | None,
    distribution_days_with_close: [(YYMMDD, close), ...],
    last_ftd_date: str | None,
}
state_history: [(YYMMDD, state, trigger), ...]  # 直近30件のみ保持
```

既存の `distribution_days` / `followthrough_days` フィールドは**削除せず維持** (HTML表示でも引き続き使用)。新たな (date, close) タプル形式は `state_meta.distribution_days_with_close` に持つ。

### direction_signal の取り扱い

既存の `direction_signal` フィールドは**削除せず**、新3値の文字列 + 日付の形式 (`"confirmed_uptrend,YYMMDD"` 等) で書き続ける。

- 旧値 (`"sell,YYMMDD"`, `"neutral,YYMMDD"`) → 新値 (`"market_in_correction,YYMMDD"`, `"uptrend_under_pressure,YYMMDD"` 等) に**完全置換**
- HTML表示も新値に合わせて更新 (`make_market_db.py:_html_market()` の signal 列、CSSクラスも刷新)

シグナル連動 (Part B) は本要件のスコープ外。`make_signal()` は Part A では改修しない。

---

## 4. 基本設計

### 4.1 DD 判定 (原典準拠)

`_calc_daily_indicators()` 内のDD判定ロジックを書き換える。

#### 通常 DD

```
DD成立条件:
  pct_change <= -0.2  (前日比 -0.2% 以下)
  AND
  volume > prev_volume  (出来高が前日より増加)
```

成立した DD は `distribution_days_with_close` に `(date, close)` のタプルで記録される。

#### Stalling Day (Part A では実装しない)

Stalling Day (新高値付近で上昇率小・出来高増・下半分引け) は、52週高値データを必要とする。現行のデータ取得長 (Kabutan 1ページ ≈ 60営業日 / yfinance `period="3mo"` 90日) では 52週 (250営業日) を満たせず、要件として実装不能。

→ Stalling Day は **データ拡張PR (yfinance period延長 + Kabutan ページング)** と同時に追加実装する。Part A スコープから外す。

### 4.2 DD 失効

`market_state.py:expire_distribution_days(dd_list, today_close, daily_history) -> dd_list`

- **25 取引日経過**した DD は失効
- 当日終値が DD 発生日終値の **1.05倍以上** に達した DD は失効

#### 25取引日経過の判定方法

`daily_history` (直近の日付リスト) を引数として渡し、その中で DD の日付がどの位置にあるかをカウントする。具体的には:

```
days_passed = daily_history.index(today_date) - daily_history.index(dd_date)
expired = days_passed >= 25
```

#### 境界条件: daily_history に該当 DD 日がない場合

DD の日付が `daily_history` に含まれない場合 (= データ取得窓を逸脱) は、**失効扱い**にする。これは:

- 取引日窓自体が現行 90日 (yfinance) / 60日 (Kabutan) であり、25日制限を確実に超えるため安全側
- 実装複雑度を抑える (DD自身に経過日数カウンタを持たせる代わりに、毎日 daily_history から計算するシンプル方式)
- 古いDDが永続残留してstateが張り付くリスクを避ける

将来データ拡張PR後は daily_history が長くなるので、この境界処理に当たるDDは出にくくなる。

### 4.3 ラリーアテンプト追跡

Correction 状態のとき、以下を追跡:

| メタ | 説明 |
|---|---|
| `rally_attempt_start_date` | Correction 中に最初に「前日より高引け」した日 (Day 1) |
| `rally_attempt_start_low` | Day 1 の安値 |

#### Day 1 の検出

```
Correction中で rally_attempt_start_date が None のとき:
  当日終値 > 前日終値 → Day 1 確定 (start_date, start_low を保存)
```

#### Day 1 の安値割れによるリセット

```
Day 1 設定済みの状態で:
  当日安値 < rally_attempt_start_low → ラリーアテンプト破綻、start_date を None にリセット
```

### 4.4 FTD 判定 (固定閾値)

`market_state.py:check_follow_through_day(today, prev, rally_meta) -> bool`

```
FTD成立条件:
  rally_attempt_start_date is not None  (ラリーアテンプト追跡中)
  AND
  Day 4 以降 (start_date から数えて 3 取引日経過後)
  AND
  当日安値 >= rally_attempt_start_low  (リセット条件を満たしていない)
  AND
  pct_change >= 1.0  (固定閾値、IBD中央値)
  AND
  volume > prev_volume
```

ボラティリティ連動閾値 (200日HV ベースで 0.7%〜1.245%) は本要件では**実装しない**。理由: 200日HV計算には日足データの大幅拡張が必要で、実装規模に見合う効果が得られない。固定 1.0% で運用継続。

### 4.5 状態遷移ルール (Part A)

```
[market_in_correction] ─[FTD成立]→ [confirmed_uptrend]

[confirmed_uptrend] ─[直近25取引日内の有効DD ≥ 4]→ [uptrend_under_pressure]
[confirmed_uptrend] ─[直近25取引日内の有効DD ≥ 6]→ [market_in_correction]

[uptrend_under_pressure] ─[直近25取引日内の有効DD ≥ 6]→ [market_in_correction]
[uptrend_under_pressure] ─[直近25取引日内の有効DD < 4]→ [confirmed_uptrend]
```

50日MA 割れによる遷移 (S9 など) は Part A スコープ外 (50日MA計算が前提のため、データ拡張PR後に追加)。

### 4.6 初期状態の扱い

`market_state` フィールドが market_db にない指数 (初回計算時) は、現在のDD数で初期判定:

```
DD数 ≥ 5  → market_in_correction
それ以外 → confirmed_uptrend  (デフォルト楽観)
```

初期状態判定後は通常の遷移ロジックに乗る。

---

## 5. 対象指数

TOPIX / マザーズ (東証グロース) / 日経225 / NASDAQ / SP500 の **全5指数** で State Machine を計算する。

各指数は独立に状態を持ち、独立に遷移する。Part B (シグナル連動) で参照する主指数は TOPIX / マザーズだが、Part A では全指数で計算 + 表示する (情報量を確保)。

---

## 6. HTML表示

`make_market_db.py:_html_market()` の改修:

| 列 | Part A 前 | Part A 後 |
|---|---|---|
| シグナル | `direction_signal` (`sell` / `neutral` / `buy` を `signal-sell` / `signal-buy` クラスで色分け) | `market_state` (3値) を `state-confirmed` (緑) / `state-pressure` (黄) / `state-correction` (赤) で色分け表示 |

CSSクラスは `make_market_db.py:605-` の市場テーブルCSSブロックに追加:

```
.state-confirmed { background: #eafaf1; color: #27ae60; font-weight: bold; }
.state-pressure  { background: #fffbe6; color: #b8860b; font-weight: bold; }
.state-correction { background: #fdedec; color: #c0392b; font-weight: bold; }
```

旧 `.signal-sell` / `.signal-buy` は他用途で残るため**削除しない**が、市場テーブルでは新クラスを使う。

---

## 7. テスト戦略

### 7.1 単体テスト (`tests/test_market_state.py` 新設)

最低限カバーするケース:

- **DD 判定**:
  - 通常 DD (-0.5% + 出来高増) → 検出
  - -0.1% + 出来高増 → 非検出 (閾値 -0.2% に届かない)
  - 出来高減 → 非検出
- **DD 失効**:
  - 26取引日経過 → 失効
  - 5%上昇 → 失効
  - daily_history に DD 日が含まれない (窓外) → 失効扱い
- **ラリーアテンプト**:
  - Correction中に Day 1 設定
  - Day 1 安値割れでリセット
- **FTD 判定**:
  - Day 4、+1.5% + 出来高増 → 検出
  - Day 3 (早すぎ) → 非検出
  - Day 4、+0.9% (閾値未達) → 非検出
- **状態遷移**:
  - market_in_correction → confirmed_uptrend (FTD)
  - confirmed_uptrend → uptrend_under_pressure (有効DD ≥ 4)
  - confirmed_uptrend → market_in_correction (有効DD ≥ 6)
  - uptrend_under_pressure → confirmed_uptrend (有効DD < 4 復帰)
  - uptrend_under_pressure → market_in_correction (有効DD ≥ 6)

### 7.2 既存テストの更新

- `tests/test_price.py:443-457`: `direction_signal` 形式テスト更新 (新3値の文字列を expects)
- `tests/test_make_market_db.py:358-396`: 各指数の `market_state` テスト追加。`direction_signal` の値が新3値になることを確認

### 7.3 統合テスト

- 実DB再計算: `make_market_db.update_market_db()` を実行し、5指数すべてに `market_state` が入ることを確認
- 実DB値が直感に整合するかを目視: 「直近DDが多い相場なら correction」など

---

## 8. スコープ外 (Part A で扱わないもの)

- **Stalling Day 検出**: 52週高値データが必要だが、現行データ取得長で確保できない (データ拡張PRで追加)
- ボラティリティ連動FTD閾値 (200日HV計算が必要、効果は限定的)
- 6ヶ月分日足データ取得 (Kabutanページング、yfinance period延長) — 別PR
- 日足50日MA (`pr>ma50` 遷移ルール用) — データ拡張PR後
- シグナル連動 (`make_signal()` 改修) — Part B
- Minervini Breadth (トレンドテンプレート通過銘柄数) — Part C
- 過去データバックテスト
- `make_signal()` の `direction_signal` 参照対応 (Part B)

---

## 9. 実装順序

1. **要件定義書コミット** (本ドキュメント)
2. **`scripts/market_state.py` 新設**:
   - 状態定数、`derive_state()`, `expire_distribution_days()`, `check_follow_through_day()`, ラリーアテンプト管理関数
3. **`tests/test_market_state.py` 新設** (TDD的に最低限カバー)
4. **`scripts/price.py` 改修**:
   - `_calc_daily_indicators()` の通常 DD ロジックを原典準拠 (`pct_change <= -0.2 and volume > prev_volume`) に置換
   - 戻り値に `(date, close)` タプル形式の `distribution_days_with_close` を追加 (既存 `distribution_days` も維持)
   - Stalling Day はスコープ外 (データ拡張PRで実装)
5. **`scripts/make_market_db.py` 改修**:
   - `make_db_common()` で前日 state_meta を読んで `market_state.py` に渡し、新state/meta を保存
   - `_html_market()` で signal 列を `market_state` 表示に変更、CSSクラス追加
6. **既存テスト更新**:
   - `tests/test_price.py` の direction_signal 形式テスト
   - `tests/test_make_market_db.py` の state テスト追加
7. **統合テスト**: 実DB再計算 + WebApp /market 目視確認 (Playwright)
8. **PR作成** (Closes は使わない: Part B/C 残るため)

---

## 10. 後方互換性

| 項目 | 互換 | 備考 |
|---|---|---|
| `distribution_days` フィールド | ◯ 維持 | 形式変えず、表示でも使う |
| `followthrough_days` フィールド | ◯ 維持 | 形式変えず、表示でも使う |
| `direction_signal` フィールド形式 | ◯ 維持 | `"<state>,YYMMDD"` 形式を継続 |
| `direction_signal` フィールド値 | ✗ 変更 | `sell`/`neutral`/`buy` → `confirmed_uptrend`/`uptrend_under_pressure`/`market_in_correction` |
| `make_signal()` の挙動 | ◯ 維持 | Part B で改修するため、Part A では既存挙動のまま (`direction_signal` 参照していないので影響なし) |
| `market_state` フィールド | (新規) | 既存DBに無くてもデフォルト遷移ロジックで初期化される |

`direction_signal` の値を依存している外部があれば影響を受ける。grep で確認: `grep -rn "direction_signal" scripts/ tests/`。現在の grep 結果では `make_market_db.py:_html_market()` の表示と、`tests/test_price.py`/`tests/test_make_market_db.py` のテストアサーションのみが該当。すべて Part A 内で更新する。

---

## 11. 関連

- 本要件は issue #117 の Part A の実装仕様を定義する
- Part B: シグナル連動 (`make_signal()` 改修) — `direction_signal` または `market_state` を参照する別要件
- Part C: Minervini Breadth (トレンドテンプレート通過銘柄数による市場健全性評価) — 別要件
- 関連ドキュメント: `doc/ARCHITECTURE.md` の市場DB構造 (`market_state` 追加で更新が必要)
