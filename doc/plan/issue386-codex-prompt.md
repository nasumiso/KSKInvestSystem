# issue #386 Phase 1 実装指示

## 依頼

`/Users/k_sohara/Library/CloudStorage/Dropbox/document/shintakane` リポジトリで、issue #386「戦略別の防衛線を算出し、割れを最優先アラート化する」の **Phase 1 のみ**を実装してください。

### 最初に読むもの (この順で)

1. **`doc/plan/issue386-exit-line-alert.md`** — 確定済みの実装プラン。**これが仕様の真実源です。**
2. `CLAUDE.md` — コーディング規約 (日本語コメント、`log_print`/`log_debug` の使い分け、`code_s` は文字列、テストは5本以下 等)
3. `gh issue view 386` — 背景と動機。**ただし本文の仕様は一部古い**(下記「issue 本文と異なる点」参照)

### 重要: issue 本文をそのまま実装しないこと

実データ検証の結果、issue 本文の仕様には実態と合わない箇所が複数あり、ユーザー合意のうえで変更済みです。**プランファイルが優先**します。主な差分:

| issue 本文 | 実際に実装するもの | 理由 |
|---|---|---|
| MA判定 = 終値で2営業日連続割れ | **既存「早売」の violation ルール** (porosity 救済 + A日安値割れ確定) | ユーザー要望。既存の判断基準と統一し、shakeout に強い |
| 対象は現物 (暗黙) | **現物 + 信用買建を数量加重で統合** | 1保29銘柄中18銘柄(62%)が信用のみ。現物限定だと6割で機能しない |
| 「中長期ファンダ」を新設する | **既に存在する** (4銘柄で使用中)。新規作成しない | 実DB確認済み |
| 戦略は9個 | **実DBは10戦略** (「中期底値リバ」があり、「底値リバ」は「短期底値リバ」) | 実DB確認済み |
| 損切り線 = 平均取得単価 × (1-率) | **ラチェット** = max(過去の各買い時点の候補線) | issue 本文の計算式セクションに明記あり。現在の avg_cost だけでは出せない |

## Phase 1 のスコープ

**やること**:
- 損切り線 (ラチェット計算) + **日足50MA の violation** → シグナル「防」「防歴」を portfolio 一覧のシグナル列に表示
- 現物 + 信用買建を統合した銘柄単位の平均取得単価
- 戦略マスター (`trade_idea:*`) への `exit_rule` 追加 + 既存10戦略への差分投入

**やらないこと (Phase 2以降)**:
- 週足MA (30WMA/40WMA) — Phase 2
- 「防予」(MA割れ初日の予告表示) — Phase 2
- 戦略マスター編集画面の exit_rule 編集UI — Phase 3
- issue の「非スコープ」セクション全般 (証券会社連携、場中価格、売却後追跡 等)

## 実装すれば確実に壊れる罠 (実データで確認済み)

プランに詳細があります。**すべて実際のDBで誤りを確認した実績のあるものです。**

1. **平均取得単価を自前で再計算しない**
   `list_fills()` の生 fill を素朴に加重平均すると、取込期間より前に買った現物を売った銘柄で残高がマイナスになり `avg_cost = -5,226` のような無意味な値になる (4377, 402A で実測)。**必ず `build_fill_episodes()` (helpers.py:4942) の `open_pl.avg_cost` を使う** (issue #398 の分割・併合換算済み)。

2. **損切り線の計算に売り fill を必ず含める**
   加重平均法では売却時に avg_cost 自体は変わらないが**保有数量が減る**ため、その後の買い増しで計算される avg_cost が変わる。買い fill だけを並べると 285A で **1,849円**低い線が出て割れを見逃す。

3. **複数エピソード (現物+信用) の fill をマージしない**
   売り/返済が「どのエピソードの建玉を減らしたか」の情報が失われる。4970 は現物と信用で建値が倍近く違い、マージすると実在しない中間値 (誤差 **1,523円**) になる。**エピソードごとに損切り線を計算し、`held_qty` で加重平均する。**

4. **MA基準日を取り違えない**
   過去日の割れ判定にはその日時点のMAを使う。当日基準MAで前日を判定すると 2,415銘柄中 **44銘柄(1.8%)** で結論が逆転する。既存 `calc_ma10_kairi_indicators` の `_ma10(i)` は正しく実装されているので、窓を引数化して流用すれば自動的に回避される。

5. **`exit_alert` の dedup は `(cycle_id, date)` の組で行う**
   `date` だけで判定すると、同日中に全売却→再IN した場合に新サイクルの発動を取りこぼす。cycle 不一致時は state ごと置換する。

## 設計の要点

### MA violation は price.py 側で計算する

**日足の安値 (`lows`) は DB に保存されていない** (`price_log` は `(date, close)` のみ、実DB確認済み)。violation 判定には安値が必須なので、**表示層では計算できません**。

- 判定は `price.py` の `_calc_daily_indicators` 内で行い、結果を stocks_shelve に保存する (既存 `ma10_break_confirmed` と同じ流儀)
- 保存フィールド例: `ma50_violation = {"breached": bool, "confirmed": bool, "pending": bool, "ma_value": float, "a_day_low": float}`
- 表示層は保存済みの判定結果を読むだけ
- 副次効果として `LOG_DAY` / `price_week_log` の拡張は**不要** (生データ上で計算が完結するため)

既存 `calc_ma10_kairi_indicators` (price.py:460-585) を**窓を引数化して一般化**し、window=10 の既存呼び出しの挙動は変えないこと (デフォルト引数で現状維持)。

### 状態の永続化

- `exit_alert:{code_s}` (新設、`code_s` 単位。**episode_key 単位にしない** — 信用のみ保有だとキーを組めない)
- `cycle_id` を state に添えて保存し、読み出し時に一致しなければ別サイクルの残骸として無視
- リセットは `transition_status` (portfolio_shelve.py:1639) の **1保→2準 遷移時に関数内部で**呼ぶ (route 層に散らすと呼び出し口が3箇所あり漏れる)

### 損切り線は永続化しない

fill 履歴から決定論的に再計算できるため。保存するのは「防が発動した事実 (日付・理由)」だけ。

## 作業の進め方

1. **ブランチを切ってから実装を始めてください** (main に直接コミットしない)
2. プランの「受入条件チェックリスト」を上から潰す
3. テストは CLAUDE.md の方針に従い **1 PR あたり5本以下**、`@pytest.mark.parametrize` で集約。特に以下3つは必ず含める:
   - 一般化した violation 関数が日足10MA相当で既存 `ma10_break_confirmed` と同じ結果を返すこと (非回帰)
   - 「買い→売り→買い増し」で、売りを含む全fill再生と買いのみの結果が**異なる**ことを固定
   - 信用建玉のみの銘柄で防衛線が算出されること
4. テスト実行 (`.claude/rules/testing.md` のマッピングに従う):
   ```
   source .venv/bin/activate
   cd scripts && python -m pytest ../tests/test_price.py ../tests/test_portfolio_shelve.py ../tests/test_webapp_helpers.py -v
   ```
   ※ `python` 実行前に毎回 `source .venv/bin/activate` が必要 (Bash呼び出しごとにリセットされる)

   **着手前のベースライン (2026-08-11 実測)**: `test_price.py` 145 passed、
   `test_portfolio_shelve.py` + `test_webapp_helpers.py` 487 passed。
   既存テストを1件も壊さないこと (特に `calc_ma10_kairi_indicators` の一般化は非回帰が最重要)。
5. UI確認は `cd scripts && python -m webapp.app` (http://localhost:5001/portfolio)
6. **PR を作る前に、実装内容をユーザーに報告して確認を取ってください**

## 環境メモ

- Python 3.11 / `.venv`
- `KS_DATA_DIR=/Users/k_sohara/Ext/GoogleDrive/shintakane_data` (`.zshrc` 設定済み)
- shelve DB への並行書き込み禁止。提供API経由で操作すること
- スクリーンショットを撮る場合は `.playwright-mcp/` 配下に保存 (`.claude/rules/playwright.md`)

## 不明点があれば

プランファイルに「残りの要確認ポイント」セクションがあります。それ以外で仕様が曖昧な場合は、**推測で実装せずユーザーに質問してください**。
