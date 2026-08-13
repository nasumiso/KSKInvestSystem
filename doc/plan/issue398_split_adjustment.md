# issue #398: 売買履歴の株式分割・併合対応

## 背景 (issue #398 より)

fill 基準のエピソード再構成は、約定CSVに記録された当時の数量・単価をそのまま集計している。株式分割・併合をまたいで保有した銘柄は、保有数量・平均取得単価・損益・騰落率が不正確になる。

### 実例: 1491 中外鉱業

- 2025-09-29 権利落ち、20株→1株の株式併合 (yfinance `Ticker('1491.T').splits` で確認: `2025-09-29: 0.05`)
- 併合前の買売差分は +4,000株 (併合後基準では +200株)
- 併合後の買売差分は -200株
- 実際の残高は0株だが、現行は併合前の4,000株を未換算のまま合算し「保有中3,800株・含み益+1,858,094円」と誤表示する
- **実現損益も誤っている**: 現行 +996,206円 → 換算後 +27,100円 (キャッシュフロー検算: 総売却1,662,600円 - 総買付1,635,500円 = +27,100円 で一致)

### 発生頻度

現物エピソード133件中、単価が隣接約定間で3倍以上/1/3以下に飛ぶもの (=分割・併合の痕跡) は **1件のみ** (1491)。信用ラウンド (277件) は影響なし (下記スコープ参照)。保有中の現物13銘柄が今後踏みうる。

## スコープ判断: 信用は対象外

信用返済 fill は証券会社が計算した決済損益・建単価を直接使っている:

- SBI: `settle_pl` (決済損益、`受渡金額/決済損益` 列由来)
- 楽天/マネックス: `tate_price` (建単価、建約定日と同一ペア行)

建玉と返済が1トランザクションでペアになるため、分割・併合があっても証券会社側で調整済みの数字が来る。**換算すると証券会社の確定損益と二重にズレるため、信用ラウンドは換算しない。**

現物のみ対象。理由: 買い fill と売り fill が独立した約定事実で、損益は当方の平均取得単価法で計算しており、企業アクションを挟むと単位が揺れる。

## 方針

手動メンテナンスする企業アクションマスターは持たない。yfinance の `Ticker(code).splits` を使い、検知は自動、比率適用はユーザー確認を挟む。

### 全体フロー

```
fill (永続, 不変)
  → [対象銘柄の企業アクション取得: yfinance, キャッシュ]
  → [現物 fill のみ、権利落ち日より前を換算した「作業用コピー」を生成]
  → _build_code_episodes (既存ロジック、そのまま)
  → エピソード (残高・損益とも正しい基準)
```

元の fill (CSV由来の約定事実) は一切変更しない。エピソード再構成の入力直前でコピーを換算する。

### 1. 検知 (2系統。単価ジャンプ検知だけでは「片側にfillが無い」保有継続ケースを取り逃すため)

**(a) 単価ジャンプ検知** — 銘柄ごとに現物 fill を約定日順に見て、隣接する fill 間で単価が3倍以上/1÷3以下に飛ぶ箇所を検出する。133件の実データで検証済み、誤検出0件・1491のみ検出。分割前後の両方に売買があるケースをノーコストで拾える。

```python
def _detect_price_jumps(fills: List[Dict]) -> List[Dict]:
    """現物 fill を約定日順に見て、隣接単価が3倍以上/1/3以下に飛ぶ箇所を検出する。
    Returns: [{"code_s", "before_date", "before_price", "after_date", "after_price"}]
    """
```

**(b) 保有中現物銘柄の総当たりチェック** — 単価ジャンプが無くても、「分割前に買って以降売買していない」保有継続銘柄は fill 間の比較ができず (a) では検知できない。これが今回の主目的 (保有中13銘柄の将来リスク) の本体なので、`show_fill_episodes.py --check-splits` は **保有中の現物銘柄すべて**に対して yfinance `Ticker(f"{code_s}.T").splits` を呼び、`open_date` 以降の split イベントが無いか確認する。件数は保有中の現物銘柄数 (現状13件) 程度で、CLI診断コマンドのみが叩くため webapp のリクエストパスへの影響はない。

```python
def _check_holding_splits(open_genbutsu_episodes: List[Dict]) -> List[Dict]:
    """保有中の現物エピソードについて、open_date 以降の split イベントの有無を yfinance で確認する。
    単価ジャンプが無くても検知できる (保有継続中で売買が発生していないケース)。
    """
```

### 2. 比率取得・保存 (yfinance、キャッシュ付き、銘柄ごとに複数イベント)

(a)(b) いずれかで検知された銘柄について、yfinance の corporate actions を取得し、**銘柄ごとに複数の企業アクションイベントをリストで**キャッシュする (`Ticker.splits` は時系列を返すため、同一銘柄で将来複数回の分割・併合が起きても破綻しない構造にする)。

```python
# portfolio_shelve.py に追加
KEY_SPLIT_ADJ_PREFIX = "split_adj:"  # split_adj:{code_s} -> {"events": [{"ex_date": "YYYY-MM-DD", "ratio": 0.05}, ...], "fetched_at": iso, "source": "yfinance"}

def get_split_adjustments(code_s, *, db_path=None) -> List[dict]:
    """登録済みイベントのリストを ex_date 昇順で返す (無ければ空リスト)。"""

def add_split_adjustment(code_s, ex_date, ratio, *, db_path=None) -> dict:
    """イベントを1件追加する (同一 ex_date は上書き、dedup)。"""
```

- `ratio`: 新株数/旧株数 (0.05 = 20:1併合、2.0 = 1:2分割)
- 取得は `show_fill_episodes.py --check-splits` 実行時のみ。webapp では yfinance 呼び出しを避け、事前にキャッシュされた値のみ参照する

### 3. 換算 (エピソード再構成の直前、現物のみ)

`build_fill_episodes` 内、`_build_code_episodes` に渡す fill リストを作る際、該当銘柄に `split_adj` イベントがあれば、**現物 fill のみ**を対象に、各イベントの権利落ち日より前の fill を比率で換算したコピーに差し替える。イベントが複数ある場合は ex_date 昇順に、古いイベントから順に適用する (各 fill は自分より後の全イベントの累積比率を受ける)。

```python
def _apply_split_adjustments(fills: List[Dict], events: List[dict]) -> List[Dict]:
    """現物 fill のうち trade_date < ex_date のものを比率で換算したコピーを返す。
    events は ex_date 昇順。各 fill には、自身より後の ex_date を持つ全イベントの
    比率を掛け合わせた累積比率を適用する。数量 = qty * cum_ratio、単価 = price / cum_ratio、
    amount は不変。信用 fill は素通し。
    """
```

- 数量は分割後に小数になり得る (併合で端数が出るケースは実際の約定にも起こりうるため、四捨五入せず float のまま `_episode_pl_from_round` の既存の加重平均ロジックに渡す。既存ロジックは int 前提の箇所がないか確認し、必要なら qty の型ヒントを float 許容に広げる)
- **ゼロ判定のフロート誤差対策 (codexレビュー指摘)**: `_build_code_episodes` 内の建玉クローズ判定は `qty <= 0` の単純比較だが、float 換算後は浮動小数演算の誤差で `qty` が厳密に0にならず `5.55e-17` のような残差が残ってクローズ判定を取り逃す恐れがある。`genbutsu_qty <= 0` 相当の判定箇所 (`helpers.py:4612` 付近) に、換算 fill を含む処理経路でのみ `abs(qty) < 1e-6` 程度の許容誤差判定を追加する (通常の整数 fill のみのケースは既存の厳密な `<= 0` を変えない)
- 元の `ps.list_fills()` の返り値は変更しない (コピー後に差し替え)

### 4. 診断コマンド (読み取り専用)

`show_fill_episodes.py --check-splits` を追加。(a) 単価ジャンプ検知 + (b) 保有中現物銘柄の総当たりチェックの両方を実行し、`split_adj` に登録済みかどうか、登録済みなら換算後の残高・損益を並べて表示する。DB は変更しない (登録は別コマンド)。

```
$ python show_fill_episodes.py --check-splits
 1491 中外鉱業  単価ジャンプ検出: 2025-09-03 @62 -> 2025-09-30 @925 (x14.9)
      split_adj 未登録。yfinance suggests: 2025-09-29 ratio=0.05 (20株->1株)
      登録: python show_fill_episodes.py --register-split 1491 2025-09-29 0.05
 6501 日立製作所  保有中チェック: split イベント無し (yfinance)
```

登録用の `--register-split CODE EX_DATE RATIO` も同コマンドに追加し、`add_split_adjustment` を呼ぶ (複数回実行すれば同一銘柄に複数イベントを積める)。yfinance 呼び出しはこの診断コマンド実行時のみで、webapp のリクエストパスには含めない (パフォーマンス・失敗時の表示崩れを避ける)。

### 5. 未登録時の表示

検知されたが `split_adj` が未登録のエピソードは、**残高・含み損益だけでなく実現損益・損益率も含めて**数値を一切出さず「⚠ 分割・併合の疑い、要確認」を表示する (既存の誤った数値を出し続けない)。

1491 の実例で確認した通り、分割・併合をまたぐ現物エピソードは残高だけでなく `pl.profit_amount` / `pl.return_pct` (クローズ済みの場合) も誤っている。`trade_history.html` は `ep.pl.return_pct` / `ep.pl.profit_amount` をクローズ済みエピソードの列にそのまま表示するため (`trade_history.html:144,156`)、`split_suspect=True` の場合は closed/open 問わずエピソード全体の損益列を隠す。`_episode_pl_from_round` / `_episode_open_pl` の計算自体は行うが (内部値として保持)、テンプレート側の表示分岐で `split_suspect` を見て隠す。

**成績サマリー集計からの除外 (codexレビュー指摘)**: 行表示を隠すだけでは不十分で、`scripts/webapp/routes/trade_history.py` の `fill_pls = [ep["pl"] for ep in fill_episodes if ep["closed"] and ep["pl"]]` (line 245 付近) はテンプレートを経由せず `fill_episodes` から直接集計しており、ここで `split_suspect` を除外しないと `fill_summary`（勝率・ペイオフレシオ）・`fill_total_pl`・`fill_priced_count` に誤った実現損益が混入する。`fill_pls` のリスト内包表記に `and not ep.get("split_suspect")` を追加し、`fill_closed_count` / `fill_priced_count` も同じ基準で suspect エピソードを一貫して除外する (`fill_priced_count = len(fill_pls)` なので `fill_pls` から除外すれば自動的に揃う)。

## 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `scripts/portfolio_shelve.py` | `split_adj:` キー追加。`get_split_adjustment` / `set_split_adjustment` |
| `scripts/webapp/helpers.py` | `_detect_price_jumps`, `_apply_split_adjustment` 追加。`build_fill_episodes` で現物 fill に適用してから `_build_code_episodes` へ渡す。未登録検知エピソードに `split_suspect: bool` フラグを付与 |
| `scripts/webapp/templates/trade_history.html` | `split_suspect` エピソードの警告表示 (残高・含み損益・実現損益・損益率の全列を隠す、closed/open 問わず) |
| `scripts/webapp/routes/trade_history.py` | `fill_pls` / `fill_closed_count` 等の集計から `split_suspect` エピソードを除外 |
| `scripts/show_fill_episodes.py` | `--check-splits`, `--register-split` サブコマンド追加 |
| `tests/test_fill_episodes.py` | 分割・併合まわりのテスト追加 (下記) |
| `tests/test_portfolio_shelve.py` | `split_adj` get/set のテスト (存在すれば既存ファイルに追加) |
| `doc/COMMANDS.md` | `--check-splits` / `--register-split` を追記 |

## テスト計画 (5本以内)

`tests/test_fill_episodes.py` に追加:

1. `test_split_adjustment_closes_episode_at_zero` — 1491 相当の合成 fill (併合前5件+併合後11件) に `split_adj` イベントを1件登録した状態で `build_fill_episodes` を呼び、残高0株・クローズ済み・実現損益 +27,100円 になることを確認
2. `test_multiple_split_events_apply_cumulative_ratio` — 同一銘柄に2回の分割・併合イベントを登録し、最古の fill に両方の累積比率が適用されること・中間の fill に1回分だけ適用されることを確認 (codex指摘の複数イベント対応の検証)
3. `test_shinyo_round_not_affected_by_split_adjustment` — 同一銘柄に信用ラウンドが混在するケースで、信用側の損益 (settle_pl/tate_price 由来) が換算の影響を受けないことを確認
4. `test_price_jump_detected_without_registration_marks_suspect` — `split_adj` 未登録の状態で単価ジャンプのある fill (1491相当) を渡すと、クローズ済み・保有中の両方で `split_suspect=True` が一貫して付与されることを確認 (codexレビュー指摘: closed/open 双方への伝播漏れがないかの検証)。同テストで `calc_trade_summary` 相当の集計対象からも除外されることを合わせて確認する
5. `test_merger_ratio_closes_without_residual_qty` — 20:1 併合相当の比率で換算した際、浮動小数の残差 (`5.55e-17` 相当) がクローズ判定を妨げないことを確認 (codexレビュー指摘: floatゼロ判定の許容誤差の検証)

`portfolio_shelve.py` 側の get/add は単純な shelve get/set なので、既存の `fill_memo` 系テストパターンに準拠する1本に留める。

`_check_holding_splits` (yfinance 同期呼び出しを含む) はユニットテスト対象外とし、`--check-splits` 実行による手動確認で見る (既存の `test_live_html.py` 系と同様、外部依存のため自動テストに組み込まない)。

## 除外・非対応

- 手動の企業アクションマスターファイルは持たない
- webapp のリクエストパス内での yfinance 同期呼び出しは行わない (診断CLIでのみ取得)
- 出来高分割型でない特殊イベント (合併に伴う交換比率など) は対象外。単価ジャンプ検知にヒットしても `--check-splits` で「yfinance splits に該当データなし」と出た場合は要手動判断とし、その旨をログに残す
