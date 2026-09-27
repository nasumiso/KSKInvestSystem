# [シグナル] ブレイクアウト/ポケットピボット/VCP/ピボット価格の整備（#109・#111 統合）

O'Neil / Minervini 手法に基づき、**ベース構造とピボット価格**を軸にシグナル検出を整備する。
#109（VCP/CWH/トレンドテンプレート）と #111（ブレイク改善・ピボットブレイク）を統合し、PR #338 後の残作業に絞って再定義。

**フォーカス**: ブレイクアウト / ポケットピボット / VCP / ピボット価格

> 完了済み（PR #338）: ブレイク出来高30日化・Stage4フィルタ・ポケットMA25併用・年跨ぎdate parse → 再着手不要。
> CWH は実装量が大きいため Phase 3（任意・後続）。

---

## Phase 1: トレンドテンプレート修正 + データ保持拡張

ピボット/VCP の事前フィルタ（Stage 2 判定）の前提。

- **1-1 MA40週上昇判定を1ヶ月前比較に** 🔴 — `price.py:826` は1週前比較（`ma40_b`）。4週前（`prices[4:44]`）と比較に修正。
- **1-2 欠損→シグナル無効化（表示だけでなく判定も）** 🔴 — `price.py:837` の `return []`（=完璧扱い◎で誤通過）を `None`/無効マーカーに。
  - `get_trend_template_expr()` で欠損 `—` と全miss `×` を分離（表示）。
  - **加えて判定側**: 現状 `make_signal()`/`extract_signals()`（`make_stock_db.py:1092-1107, 1184-1208`）は非listを `set()` 扱いし、週足計算失敗の銘柄でもポケ/ブを通す。欠損 = 「未評価」としてシグナル無効にする。表示だけ分離すると誤シグナルが残るため Phase 1 に含める。
- **1-3 データ保持拡張 + backfill** 🔴 — VCP/CWH の前提。
  - 週足: 取得は `period="2y"`（≈104週）で潤沢。`price_week_log[:25]` → **65週**へ拡張（52週TTも恩恵）。
  - 日足: 取得 `period="2mo"`（≈41日）が不足。**実装時に案A/Bを実データ検証して決定**:
    - 案A `period="6mo"`/`LOG_DAY=120`（VCP 3〜6ヶ月フルカバー、コスト中）
    - 案B `period="3mo"`/`LOG_DAY=60`（VCP最小限、コスト小〜中）
  - **backfill 運用**: `period` 拡張だけでは既存キャッシュ/DBの短い `price_log` が残り、新旧ロジックが銘柄ごとに混在する（`price.py:1251-1305`, `make_stock_db.py:2229-2250`）。拡張PRに「全銘柄 `price` 再取得 or 対象銘柄 backfill」の手順を含める。
- テスト: `tests/test_trend_template.py`（全通過/MA40横ばい/欠損→None・シグナル無効、parametrize集約）

## Phase 2: VCP + ピボット価格 + ピボットブレイク `[BP]`

- **VCP検出** 新規 `scripts/detect_vcp.py` — TT を事前フィルタ。`argrelextrema` でスイング抽出 → 収縮の単調縮小（例 25%→15%→8%）・回数・出来高減を評価。返り値 `{pivot_price, contractions, last_contraction_depth, base_length}`。
- **ピボット価格保存** — DBキー `vcp_state`（`vcp_state.pivot` = 最後の収縮高値、状態）。
- **`[BP]` シグナル履歴キー（必須）** — `make_signal()`/`extract_signals()` は `pocket_pivot`/`breakout` の `"mm/dd,num"` 履歴リスト前提（`make_stock_db.py:1068-1128, 1137-1210`）。`vcp_state.pivot`（状態）とは別に、`breakout` と同形式の `[BP]` 用履歴キー（例 `base_pivot_breakout`）を保存し、発生日・強度・複数回発生を持たせる。これで一覧tooltip/鮮度判定/チャートマーカーに既存経路で載る。
- **ピボットブレイク `[BP]`** — `today_close > pivot かつ 出来高 >= 1.4×平均`。記号は `[BP]`（Base Pivot）新設、既存 `[ブ]`（出来高急増陽線）は当面残し段階移行。
  - **表示レイヤー対応（必須）**: webapp は種別を実質「ポ/ブ」2種前提（tooltip 文言・強度判定・マーカー生成 = `webapp/helpers.py:1970-2055, 2667-2701`）。
    - **`extract_signals()` の返却スキーマ拡張**: 表示側は `signals[i]["kind"]` だけで記号・tooltip・マーカーを決める。`[BP]` を `"ブ"` と同 kind にすると一覧で潰れ、別 kind にすると helper の分岐を全面対応する必要がある。→ `extract_signals()` に `[BP]` を `[ブ]` と区別する新 kind 識別子を追加することまで明記。
    - helper 側（記号→種別マップ、マーカー、tooltip、強度判定）と該当テスト更新を Phase 2 の必須作業に含める。
- 出力: CSV/HTML に「VCP」列・`[BP]` 表示。
- テスト: `tests/test_detect_vcp.py`（成立/収縮非単調→非検出/ピボット未超え→非検出）

## Phase 3（任意・後続）: CWH

Phase 1/2 完了後に判断。週足カップ + 日足ハンドル/ブレイク（`scripts/detect_cup_handle.py`、DBキー `cwh_state`）。

---

## PR分割
1. Phase 1（TT修正 + 保持拡張）
2. Phase 2（VCP + ピボット + `[BP]`）
3. Phase 3（CWH, 任意）

各PhaseでE2E検証してから次へ。全完了で本issue close、#109・#111 は本issueに集約。

## 関連
`price.py`（TT 799-838 / 取得 1083,1408 / 保持 652,933 / シグナル検出）, `make_stock_db.py`（`get_trend_template_expr`/`make_signal` 1137/`extract_signals` 1068/記号 1180-1223）, `make_market_db.py`（HTML）
