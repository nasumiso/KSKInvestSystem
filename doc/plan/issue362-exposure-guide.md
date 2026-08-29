# issue #362: 市場状態連動の運用比率(エクスポージャー)ガイド + 日次ログ

## ゴール

ポートフォリオ保有タブに「基準運用額に対する現在の運用比率 (%)」と「市場状態から導いた目標レンジ」を表示し、
毎日その乖離が目に入る状態にする。あわせて運用比率と判定材料の各指標を日次ログに記録し、
将来の行動監査・指標信頼度検証のデータを貯め始める。

## 設計方針

- 判定ロジックは**純関数モジュール** `scripts/exposure_guide.py` に集約する。
  WebApp (表示) と日次バッチ (ログ記録) の 2 箇所から同じ関数を呼ぶ。
- 既存の運用総額計算 (`list_portfolio_with_indicators` 内の `position_value`) は
  RS ライン・チャート生成を含む重い処理と一体化しており、日次バッチから呼ぶには重い。
  **運用総額と市場別内訳だけを計算する軽量関数を切り出し**、WebApp 側もそれを使うように寄せる
  (同一条件を 2 箇所で書かない = issue 記載の「webapp/helpers の保有集計と同一」を構造で担保)。
- 設定値 (基準運用額・レンジ・閾値) は `portfolio_shelve` の単一 key に dict で保存する。
  値の更新は当面 CLI (`--set-base-amount`) のみ。UI からの編集は本 issue のスコープ外。

## 1. 設定の保存 (`scripts/portfolio_shelve.py`)

新規 key `exposure_settings` に dict で保存。デフォルト値をモジュール定数で持ち、
保存値が無いキーはデフォルトで埋める (後方互換: 既存 DB に key が無くても動く)。

```python
EXPOSURE_DEFAULTS = {
    "base_amount": 26_500_000,       # 基準運用額 (中立時100%)。手入力1値
    "ranges": {                      # ステート別の目標レンジ (基準比%)
        "confirmed_uptrend":      [100, 120],
        "uptrend_under_pressure": [80, 100],
        "market_in_correction":   [65, 80],
    },
    "modifiers": {                   # 過熱時に上限を削る (非対称)
        "credit_eval_rate": {"threshold": -3.0, "penalty": 10},  # >= threshold で発動
        "fng_jp":           {"threshold": 75.0, "penalty": 10},  # >= threshold で発動
    },
}
```

API:
- `get_exposure_settings(*, db_path=None) -> dict` — デフォルトマージ済みを返す
- `set_exposure_settings(settings, *, db_path=None)` — バリデーション後に保存
  - `base_amount` は正の数、レンジは `lower <= upper` かつ 0 以上、penalty は 0 以上

## 2. 判定ロジック (`scripts/exposure_guide.py` 新規)

すべて引数から計算する純関数。DB/ファイル I/O はこのモジュールでは行わない
(呼び出し側が読んで渡す = テスト容易性のため)。

```python
STATE_SCORES = {CONFIRMED_UPTREND: 1.0, UPTREND_UNDER_PRESSURE: 0.5, MARKET_IN_CORRECTION: 0.0}

# market_category -> 参照する market_db の指数キー
CATEGORY_TO_INDEX = {"日経225": "nikkei225", "TOPIX": "topix", "グロース": "mothers", "その他": "topix"}
```

- `weighted_state(category_values, index_states)` → `(state, score)`
  - `category_values`: {"TOPIX": 1234567.0, ...} 市場別保有額
  - `index_states`: {"topix": "confirmed_uptrend", ...}
  - 保有額で加重平均したスコアを 3 段階に丸める。境界は `>= 0.75` → confirmed,
    `>= 0.25` → under_pressure, それ未満 → correction
  - ステート不明 (`index_states` に無い/None) のカテゴリは**加重から除外**する
    (残ったカテゴリだけで加重平均する)
  - **ノーポジ (合計0) / 全カテゴリのステート不明 → `(None, None)` を返し、呼び出し側がフォールバック**
- `fallback_state(index_states)` → `(state, score)`。TOPIX とグロース250 の悪い方 (スコアが低い方)
  - **片方だけ欠損ならもう片方を使う。両方欠損なら `(None, None)`**
- `apply_modifiers(range_pct, credit_eval_rate, fng_jp, modifiers)` → `(lower, upper, applied)`
  - 上限のみ削る。削った結果が下限を下回る場合は下限に丸める (レンジ反転を防ぐ)
  - `applied` は発動した modifier 名のリスト (表示・ログ用)
  - 指標値が `None` (取得失敗・鮮度切れ) の場合はその modifier を発動させない
- `evaluate_exposure(total_value, category_values, index_states, credit_eval_rate, fng_jp, settings)`
  → 表示・ログ両方が使う dict を返す:
  ```
  {"total_value", "base_amount", "ratio_pct", "state", "state_score", "state_is_fallback",
   "range_lower", "range_upper", "modifiers_applied", "deviation_pct", "position"}
  ```
  - `deviation_pct`: レンジ内なら 0、超過なら `ratio - upper` (正)、不足なら `ratio - lower` (負)
  - `position`: `"within"` / `"over"` / `"under"` (色分け用)

### 欠損時の戻り値契約 (必ず dict を返す。例外を投げない)

指標欠損でクラッシュさせない。判定不能は `state=None` で表現し、レンジ関連を全て `None` にする。

| 状況 | 戻り値 |
|---|---|
| `base_amount <= 0` | `ratio_pct=None`, `position=None`, `deviation_pct=None` (レンジは算出する) |
| 加重・フォールバックとも state 決定不可 | `state=None`, `range_lower/upper=None`, `deviation_pct=None`, `position=None` |
| `credit_eval_rate` / `fng_jp` が `None` | 該当 modifier のみ不発動。他は通常どおり |

- `state=None` または `ratio_pct=None` の場合、**表示側はガイド部分を出さず運用総額のみ表示**する。
  ログ側は当該フィールドを `null` のまま記録する (欠損した事実も記録として残す)。

## 3. 運用総額集計の切り出し (`scripts/webapp/helpers.py`)

新規関数 `summarize_hold_positions(records, stocks, *, ...)` を追加し、
**1保 かつ qty>0 かつ price>0** の条件で `total_value` と `category_values` を返す。

- 既存 `list_portfolio_with_indicators` 内の `position_value` 計算と
  `webapp/routes/portfolio.py` の `hold_summary` 集計は、この関数の結果を使うよう置き換える
  (計算条件の二重定義を解消)。
- 日次バッチはこの関数を直接呼ぶ (重い指標計算を経由しない)。

## 4. 指標値の読み出し (`scripts/exposure_guide.py` の I/O ヘルパー)

判定に渡す指標値を読むだけの薄い関数を用意する (純関数部とはファイル内で分離)。

**鮮度チェックが必須** (codex レビュー指摘): `shintakane.py` が失敗した日でも
ログ記録だけ走ると、古い指標値をその日の値として記録してしまい監査データが壊れる。
各 `read_*` は値と同時に元データの日付を見て、**許容日数を超えて古ければ `None` を返す**。
指標ごとに更新頻度が違うため許容日数は個別に持つ:

| 関数 | ソース | 許容鮮度 | 備考 |
|---|---|---|---|
| `read_credit_eval_rate()` | `credit_balance.json` `history[-1]` | **10日** | 週次公表 (実データで最新 8/21 vs 当日 8/29)。当日一致を要求すると永久に記録できない |
| `read_fng_jp()` | `fear_greed_jp.json` `history[-1].score` | **3日** | 日次 (連休を考慮) |
| `read_fng_us(market_db)` | market_db `fear_and_greed` の `access_date` | **3日** | ログのみ・判定に使わない |
| `read_index_states(market_db)` | market_db 各指数の `market_state` | **3日** | 鮮度は **market_db ファイルの mtime** で判定 (下記) |

**実装時の発見 (計画からの修正)**: market_db の指数エントリ (`topix`/`mothers`/`nikkei225`) は
**指数ごとの更新日フィールドを持たない**。`price_log` / `daily_history` / `state_history` は
更新条件の異なる append-only 履歴で、実データでも `market_state` が当日値なのに
`price_log[-1]` が 1ヶ月前という乖離があり、鮮度判定には使えない。
そこで指数ステートの鮮度は **market_db shelve ファイルの mtime** で判定する
(`make_market_db._market_db_file_sig` が既にキャッシュ世代印として使っている先例に倣う)。
mtime は DB 全体で 1 つなので、指数ごとの個別除外はできない (全指数まとめて鮮度切れ扱い)。

- 鮮度判定の基準日は `ks_util.get_price_day()` (17時前は前日扱いの既存ルール)
- ファイル欠損・パース失敗も `None` (デイリー全体を止めない既存方針に合わせる)
- 鮮度切れで `None` にした場合は `log_warning` を出す (無言でガイドが消えるのを防ぐ)

## 5. 表示 (`portfolio.py` / `portfolio_list.html`)

保有フィルタ (`1保`) 表示時のみ、既存 `hold_summary` に `exposure` を追加してヘッダに出す。

```
運用総額: 2,253 万円 (基準比 85%)  [上昇トレンド]  目標 100〜120% (2,650〜3,180万)  −15pt
```

- レンジ内=緑 / 超過・不足=赤系で `position` により着色 (既存の条件付き書式のクラス命名に合わせる)
- modifier 発動時は目標レンジに注記 (tooltip で「信用評価損益率が過熱のため上限 −10pt」等)
- 加重ステートがフォールバック (ノーポジ) の場合はその旨を tooltip に出す
- 基準運用額が未設定 (デフォルトのまま) でも表示はする。値の意味はユーザー宣言である旨を tooltip に明記

## 6. 日次ログ (`scripts/exposure_guide.py` の CLI + cron 組み込み)

保存先: `$KS_DATA_DIR/code_rank_data/exposure_log.json`
(理由: 判定材料である credit_balance.json / fear_greed_jp.json と同じ層のデータであり、
market_db・portfolio_shelve の shelve スキーマに触らずに済む)

```json
{"history": [
  {"date": "2026-08-29", "total_value": 22530000, "base_amount": 26500000, "ratio_pct": 85.0,
   "state": "confirmed_uptrend", "state_is_fallback": false,
   "range_lower": 100, "range_upper": 110, "modifiers_applied": ["credit_eval_rate"],
   "deviation_pct": -15.0,
   "index_states": {"topix": "...", "mothers": "...", "nikkei225": "..."},
   "category_values": {"日経225": 0, "TOPIX": 0, "グロース": 0, "その他": 0},
   "credit_eval_rate": -2.5, "fng_jp": 62.0, "fng_us": 55,
   "source_dates": {"credit_balance": "2026-08-21", "fng_jp": "2026-08-28",
                    "topix": "2026-08-28", "mothers": "2026-08-28"}}
]}
```

- 判定に使わない `fng_us` も記録する (後から信頼度検証をするため)
- 同一日付の再実行は**上書き** (日次バッチの再実行・手動実行で重複行を作らない)
- 日付は `ks_util.get_price_day()` を使う (17:00 前は前日扱いの既存ルールに合わせる)
- **各指標の元データ日付を `source_dates` として同時に記録する**
  (`{"credit_balance": "2026-08-21", "fng_jp": "2026-08-28", "topix": "2026-08-28", ...}`)。
  後から「この行はどの鮮度のデータで判定したか」を追跡できるようにする。
  鮮度切れで `None` になった指標も日付だけは残す
- CLI: `python exposure_guide.py log` (記録) / `show` (直近履歴表示) / `--set-base-amount N`

### cron 組み込み

`shintakane_cron.sh` の make_stock_db.py 成功後に追加実行する
(market_state・各指標 JSON が更新済みである必要があるため)。

**終了コードによる実行可否判定はしない**。理由: 判定材料の鮮度は上記 `read_*` の
鮮度チェックが個別に担保しており、`RET1`/`RET2` は「何かが失敗した」以上の粒度を持たない。
例えば shintakane.py が theme 取得だけ失敗して指標 JSON は正常更新された日に
ログを丸ごと落とすのは損失が大きい。逆に成功していても週次の信用評価損益率は
古いままなので、終了コードは鮮度の証明にならない。

- 記録自体は毎日行う (欠損指標は `null` として記録し、欠損の事実も履歴に残す)
- ただし**市場ステートが全指数とも取得できない日は記録をスキップ**する
  (ガイドの主軸が無く、`state=null` だけの行に監査価値がないため)。スキップは `log_warning`
- 失敗しても cron 全体は止めない (既存の report パターンに合わせて RET を出すのみ)

## テスト (`tests/test_exposure_guide.py` 新規、5本以内)

parametrize で集約する:

1. `weighted_state` / `fallback_state` — 加重平均の丸め境界、ステート欠損カテゴリの除外、
   ノーポジ・全欠損時の `(None, None)` (parametrize)
2. `apply_modifiers` — 未発動 / 片方 / 両方 / 下限割れ丸め / 指標 None (parametrize)
3. `evaluate_exposure` — レンジ内・超過・不足の position と deviation_pct、
   および欠損時契約 (`state=None` / `base_amount<=0` で例外を投げず None 埋め) (parametrize)
4. `read_*` の鮮度チェック — 許容内 / 許容超過で `None` (信用は10日・F&Gは3日) (parametrize)
5. `get/set_exposure_settings` のデフォルトマージと不正値バリデーション、日次ログの同一日付上書き

`webapp/helpers.py` を変更するため `pytest tests/test_webapp_helpers.py tests/test_html_sanitizer.py -v` も実行する。

## 実装順序

1. `portfolio_shelve.py` に設定 API + テスト
2. `exposure_guide.py` の純関数群 + テスト
3. `webapp/helpers.py` の集計切り出し (既存呼び出し元の置き換え含む)
4. `exposure_guide.py` の I/O ヘルパー・CLI・ログ記録
5. 表示 (`portfolio.py` / `portfolio_list.html`)
6. `shintakane_cron.sh` 組み込み
7. WebApp 起動して実データで表示確認 (スクリーンショット)

## スコープ外 (issue 記載どおり)

- 証券会社の自己資本・信用維持率連携による厳密なレバレッジ計算
- 恐怖局面の逆張り増枠の自動化 (裁量に残す)
- 日次ログの可視化 (推移チャート・パーセンタイル)
- 設定値の UI 編集 (当面 CLI のみ)
