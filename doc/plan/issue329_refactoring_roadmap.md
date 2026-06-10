# コードベース全体リファクタリング ロードマップ

作成日: 2026-06-10
親 issue: #329
ステータス: プラン (実装未着手)

長年の蓄積で生じたデッドコード・重複・構造的負債を、挙動を変えずに段階的に解消するための親プラン。
行番号・行数は 2026-06-10 調査時点のスナップショット。**各フェーズ着手時に必ず再確認すること**(その間のマージで位置がずれる)。

## 調査サマリー (現状の負債)

- scripts/ 直下 24,046行 + scripts/webapp/ 5,496行 + tests/ 44ファイル
- コメントアウト済みの旧コードブロック 約250行 (Python 2 構文の封印関数など6ブロック)
- 役目を終えた一回限りの移行スクリプト 10本 (+対応テスト約3,000行)
- 重複パターン 10グループ (Kabutan URL/キャッシュ判定/スキーマ定数/バルク取得など)
- 循環依存: `make_stock_db ↔ make_market_db` (双方向)、`price → make_stock_db` (トップレベル) + 遅延 import 多数
- 巨大モジュール: `webapp/helpers.py` 3,665行、`make_market_db.py` 2,602行、`make_stock_db.py` 2,343行、`price.py` 1,877行、`shintakane.py` 1,871行
- テスト: `db_path` 系フィクスチャが13ファイルで重複定義

## 進め方の原則

1. **挙動を変えない**。全フェーズで「リファクタ前後の出力一致」を検証ゲートにする (下記)。
2. **1 PR = 1 サブフェーズ**。各 PR 単独でマージ可能・ロールバック可能にし、途中で止めても価値が残る状態を保つ。
3. **Surgical Changes**: 各フェーズの目的に含まれない「ついで改善」はしない。気づいた別件は issue 化のみ。
4. 本 issue は親 issue として運用し、**子 PR では `Closes` を書かない** (Phase 1 マージで親が自動クローズされる事故防止)。各 PR は `Refs #<本issue>` で紐付ける。

## 検証ゲート (全フェーズ共通)

各 PR のマージ条件:

1. `pytest tests/ -v -m "not local_db and not live_html"` 全パス (CI と同条件)
2. **出力一致検証** (Phase 0 で整備するハーネスを使用): `KS_DATA_DIR` を本番データの**スナップショットコピー**に向け、before (main) / after (リファクタブランチ) それぞれで同一スナップショットから成果物を再生成し、diff が空であること
   - `make_stock_db.py` 系統: `code_rank.csv`
   - `shintakane.py` 系統: `shintakane_result.csv`
   - `make_market_db.py` 系統: `market_data.html`
   - **注意**: 既存 CLI をそのまま検証に使ってはならない。`shintakane.py` の main は引数に関わらず `"update analyze"` を先頭に付加するため `analyze` 指定でも更新 (スクレイピング・Google Drive アップロード) が走り、`make_stock_db.py list_all_db` も `update_market_db()`・銘柄再取得・research snapshot / PTS 追記まで実行する。これらを叩くと「リファクタの差分」ではなく「検証コマンド自身の更新副作用」を diff が拾う。
3. webapp 系統: 既存 routes/helpers テストのパス + 主要画面 (`/`, `/market`, `/portfolio`, 銘柄詳細) のブラウザ目視

## Phase 0: 出力一致検証ハーネスの整備 (リスク: 低 / 1 PR)

検証ゲート 2 を成立させるため、**既存データから成果物だけを再生成する読み取り専用経路**を確保する。

- `market_data.html`: 既存の `cd scripts && python make_market_db.py html` (DB 更新なしの HTML 再生成経路) をそのまま使う。
- `code_rank.csv` / `shintakane_result.csv`: 既存 DB・既存 CSV キャッシュから出力のみ再生成する経路が現状存在しない。既存の生成関数 (`build_code_rank_row()` 系 / CSV 出力部) を**そのまま呼ぶだけ**の薄いサブコマンド (例: `make_stock_db.py export_csv`) を追加する。スクレイピング・DB 書き込み・Drive アップロードは一切呼ばないこと。
- スナップショット手順 (データ dir コピー → `KS_DATA_DIR` 切替 → before/after 実行 → diff) を `doc/TESTING.md` に追記する。
- タイムスタンプ等どうしても非決定になる出力列があれば、比較時に除外する列として手順に明記する。

検証: ハーネス自体の検証は「main 上で2回連続実行して diff が空」であること。

## Phase 1: デッドコード除去 (リスク: 低 / 1 PR)

封印済み・未参照のコードを物理削除する。判断が要るものは含めない。

### 1-1. コメントアウト済みコードブロック削除 (約250行)

- `rironkabuka.py` — 冒頭の旧 `calc_growth()`/`calc_rironkabuka()` (Python 2 構文)、旧 `get_from_kabutan2()` ブロック
- `analyze_sisu_data.py` — 封印済み `buy_and_hold()`、`rs_macd()` ブロック
- `make_stock_db.py` — 封印済み `get_relates_rank()` (`.has_key()` 使用の Python 2 残骸)
- `shintakane.py` — 封印済み `wait_connect()`
- `# code = int(code)` のコメント残骸 (make_stock_db.py / shihyou.py / rironkabuka.py / portfolio.py の計約10箇所)
- `.has_key()` を含むコメント残骸 (gyoseki.py / shihyou.py)

### 1-2. 未使用関数の削除

- `ks_util.py`: `smart_print()`, `eprint()` (grep で呼び出しゼロを確認済み)
- `make_stock_db.py`: `print_to_file()` (同上)
- ※ `memoize()` は `memoized_load_pickle`/`memoized_load_file` (ks_util.py:665,680) で**使用中のため削除しない**

### 1-3. 未参照ファイルの削除

- `scripts/googledrivetest.py` (どこからも import/参照されていない接続テストスクリプト)

検証: 共通基盤 (`ks_util.py`) を触るため全テスト実行 + 削除した識別子の grep ゼロ確認。

## Phase 2: 役目を終えた移行スクリプトの整理 (リスク: 低 / 1 PR)

一回限りのデータ移行が完了済みのスクリプトを、対応テストごと削除する。git 履歴に残るので必要なら復元可能。

| スクリプト | 移行内容 (issue) | COMMANDS.md 記載 | 扱い |
|---|---|---|---|
| `migrate_my_watch_list_to_shelve.py` | watch list TXT→shelve (#170) | なし | 削除 |
| `migrate_portfolio_from_csv.py` | portfolio CSV→shelve (#171) | なし | 削除 |
| `migrate_portfolio_drop_stock_name.py` | stock_name 物理削除 (#171 Phase 3b) | なし | 削除 |
| `migrate_gyoutai_theme_to_list.py` | gyoutai_theme str→list (#187) | なし | 削除 |
| `migrate_theme_rank_history.py` | テーマランク履歴遡及構築 (#275) | なし | 削除 |
| `migrate_themes_to_master.py` | テーママスター化 (#282/#293) | なし | 削除 |
| `cleanup_kessan_dup_entries.py` | 決算重複エントリ除去 (#207) | なし | 削除 |
| `reimport_rich_text.py` | スプシ書式保持再インポート (#115) | なし | 削除 |
| `scripts/oneshots/clear_stock_name_prev.py` | ワンショット | なし | 削除 |
| `migrate_research_from_csv.py` | 調査スプシ CSV→shelve (#92) | **あり** | **要ユーザー判断** |
| `migrate_kessan_comments_from_log.py` | 決算メモ log→shelve (#131) | **あり** | **要ユーザー判断** |

- 対応する `tests/test_migrate_*.py` / `tests/test_cleanup_*.py` / `tests/test_reimport_rich_text.py` も同時に削除。
- **着手前にユーザー確認**: 本番 DB への移行が完了済みで再実行の予定がないこと。特に COMMANDS.md 記載の2本は「今後もスプシ/ログから再インポートする運用があるか」を確認し、削除する場合は COMMANDS.md の該当手順も同 PR で削除。
- `.claude/rules/testing.md` のマッピング表 (`migrate_research_from_csv.py` の行) も更新。

検証: テスト全パス + COMMANDS.md / testing.md に削除済みスクリプトへの参照が残っていないこと (grep)。

## Phase 3: 定数・重複パターンの集約 (リスク: 低〜中 / 3 PR 程度)

挙動を変えずに「同じ知識の二重定義」を解消する。共通化は**実際に2箇所以上で重複しているものだけ**を対象とし、投機的なユーティリティは作らない。

### 3-1. Kabutan URL・キャッシュパス・キャッシュ判定の一元化 (1 PR)

- Kabutan の URL テンプレート (`finance?code=%s&mode=k`, `stock/?code=%s` など) が rironkabuka.py / gyoseki.py / shihyou.py / disclosure.py 等で独立定義 → 1箇所 (rironkabuka.py または ks_util.py) に定数として集約
- キャッシュディレクトリ定義 (`DATA_DIR/stock_data/kabutan/finance` など) の重複も同様に集約
- `upd == UPD_CACHE / UPD_FORCE / else is_cache_latest()` の3分岐が price.py / gyoseki.py / rironkabuka.py 等4箇所以上でコピペ → 共通関数化 (変数名の揺らぎ `use_cach`/`use_cache` もここで解消)

### 3-2. shelve スキーマ定数の一元化 (1 PR)

- `CODE_S_PATTERN` が research_shelve.py:57 と portfolio_shelve.py:57 で完全一致の二重定義 → 共有モジュール (db_shelve.py 等) に1定義
- `normalize_code_s`/`validate_code_s` 系の重複実装も同じ置き場に寄せる
- ※ shelve DB のスキーマ自体・保存形式は一切変更しない

### 3-3. webapp バルク取得関数とテストフィクスチャの整理 (1 PR)

- `webapp/helpers.py` の `_bulk_resolve_stock_names` / `_bulk_resolve_stock_name_prevs` / `_bulk_resolve_overall_ratings` はフィールド名だけ違う同型コード → フィールド名をパラメータ化した1関数に統合
- tests/ の `db_path` 系フィクスチャ (13ファイルで重複) と shelve パスの monkeypatch パターンを `conftest.py` の共通フィクスチャに集約

検証: 各 PR でテスト全パス + 出力一致 (3-1 は `shintakane_result.csv` / `code_rank.csv`、3-3 は webapp テスト + 目視)。

## Phase 4: 循環依存の解消 (リスク: 中 / 1〜2 PR)

Phase 5 (モジュール分割) の前提。現状:

```
make_stock_db ←→ make_market_db   (双方向トップレベル import)
price → make_stock_db             (トップレベル)
price → make_market_db            (関数内遅延 import: momentum calib 等)
webapp/helpers → 各モジュール      (遅延 import 多数 ※Web層の宿命なので対象外)
```

### 4-1. price の依存整理

- `get_momentum_calib()` / `calc_momentum_pt_value()` が price.py にありながら market_db を遅延 import している。モメンタムキャリブレーションの読み出しを「呼び出し側 (make_stock_db) が calib を引数で渡す」形に変え、price から make_stock_db / make_market_db への import を除去する。

### 4-2. make_stock_db ↔ make_market_db の双方向解消

- 相互参照している関数を特定し、共有部分 (momentum 正規化、テーマ連携の共通データ構造) を片方向に整理する。新モジュール切り出しは「双方から使われる最小限の関数」だけに留める。

検証: `python -c "import price"` 等の単独 import 確認 + テスト全パス + `code_rank.csv` / `market_data.html` 出力一致。

## Phase 5: 巨大モジュールの分割 (リスク: 中〜高 / サブフェーズごとに 1 PR)

優先度順。**各サブフェーズは独立した PR** とし、1つ完了するごとに運用 (cron + webapp) で数日問題ないことを確認してから次へ進む。

共通方針:

- cron や手順書から直接叩かれるエントリポイント (`shintakane.py`, `make_stock_db.py`, `make_market_db.py`, `webapp.app`) の**ファイル名と CLI インターフェースは変えない**。本体は分割先モジュールへ移し、元ファイルは CLI + re-export の薄いファサードとして残す。
- 分割は「ファイル移動 + import 修正」のみ。関数のシグネチャ・ロジックには触らない (触りたくなったら別 issue)。
- `from ks_util import *` (make_stock_db.py) のワイルドカード import は、この機会に明示 import へ置換する。

### 5-1. webapp/helpers.py (3,665行 → 5分割)

最も大きく、表示層なので出力一致検証がしやすい。`webapp/` 配下に:

| 分割先 | 内容 (概算) |
|---|---|
| `chart_builders.py` | SVG チャート生成系 (~900行) |
| `html_parts.py` | market/disclosure HTML 部品生成 (~800行) |
| `formatting.py` | 表示値フォーマット系 (~600行) |
| `form_handlers.py` | memo/決算コメント等の保存処理 (~300行) |
| `helpers.py` (残) | データ取得・検索系 + re-export |

### 5-2. make_stock_db.py (2,343行 → 4分割)

`stock_db_core` (DB I/O) / `stock_db_calc` (RS・スコア・シグナル計算) / `stock_db_ranking` (code_rank 行構築・CSV) / 残りを CLI ファサードに。

### 5-3. make_market_db.py (2,602行 → 3分割)

`market_db_core` (指数 DB 構築) / `market_db_theme` (テーマランク) / `market_db_html` (market_data.html レンダリング)。

### 5-4. price.py (1,877行 → 3分割)

`price_yfinance` / `price_kabutan` / `price_indicators` (Phase 4-1 完了後に実施)。

### 5-5. shintakane.py (1,871行 → 3分割)

`shintakane_scrape` (新高値/出来高/PTS パース) / `shintakane_market_data` (信用残・VI・F&G 更新) / 残りをオーケストレーション + CLI に。

### 5-6. ks_util.py (802行 → 任意)

全モジュールが依存する共通基盤のため**最後**。logging / http / file I/O / 表示ヘルパーへの分割。費用対効果が薄ければ未実施で打ち切ってよい (802行は許容範囲)。

検証: 各 PR で対象モジュールのテスト + 出力一致 + cron 翌日運用確認。

## スコープ外 (このリファクタではやらないこと)

- **スコアリング・ランキング計算式の変更・抽象化**: momentum/RS/総合ポイントの計算は投資判断に直結し回帰リスクが最大。移動はしても式は1文字も変えない。
- **HTML パーサーの BeautifulSoup 化・基盤クラス化**: `test_live_html.py` による変更検知体制が機能しており、パーサー統一は保守性をむしろ下げるリスクがある。
- **oauth2client → google-auth 移行**: deprecated 対応は実利あるが性質が異なるため別 issue。
- **TODO コメントの個別実装**: 機能追加・改善系 TODO (約20件) は本件と無関係。必要なら個別 issue 化。
- **make_sector_data.py / make_sisu_data.py / analyze_sisu_data.py / analyze_market.py の廃止判断**: HTML 変更で機能停止中のものを含むが、削除には運用判断が要る。別 issue で扱う。
- **テストの追加・拡充**: 既存テストを検証ゲートとして使うのみ。

## 実施順序と依存関係

```
Phase 0 (検証ハーネス) ──→ 全フェーズの前提。最初に実施
Phase 1 (デッドコード) ─┐
Phase 2 (移行スクリプト) ─┼─ 独立。どこからでも着手可
Phase 3 (定数集約)      ─┘
Phase 4 (循環依存) ──→ Phase 5-4 (price 分割) の前提
Phase 5 (分割) は 5-1 → 5-2 → 5-3 → 5-4 → 5-5 → (5-6)
```

目安: Phase 0〜3 で計 6 PR、Phase 4〜5 で計 7〜8 PR。各 PR は小さく保ち、1 PR で複数フェーズをまたがない。

## リスクと対策

| リスク | 対策 |
|---|---|
| 出力 CSV/HTML の意図しない変化 | 全 PR で before/after diff を必須化 (検証ゲート 2) |
| shelve DB 後方互換の破壊 | スキーマ・保存形式は全フェーズで変更禁止 |
| cron 運用の破壊 | エントリポイントのファイル名・CLI 引数を不変に保つ。分割系 PR のマージ翌日に cron ログ確認 |
| 行番号ずれによる誤削除 | プラン記載の行番号は参考値。削除時は必ず現物のコードを確認 |
| 並行セッション・worktree との干渉 | 着手前に `git worktree list` / `git stash list` を確認 (既存 stash 2件あり: daily-cache-fix, issue248) |
