# 開発コマンドリファレンス

すべてのスクリプトは `scripts/` ディレクトリから実行する。

## 環境セットアップ

```bash
source .venv/bin/activate
```

## メイン分析

```bash
# スクレイピング + 分析 + ランキング
cd scripts && python shintakane.py

# スクレイピングなしで既存データのみ分析
cd scripts && python shintakane.py analyze
```

## 銘柄DB操作 (`make_stock_db.py`)

```bash
# DB全銘柄のランキング更新 + CSV出力
cd scripts && python make_stock_db.py list_all_db

# 特定銘柄の更新 (引数で指定可。未指定時はソース内デフォルト)
cd scripts && python make_stock_db.py update 6324             # 単一銘柄
cd scripts && python make_stock_db.py update 6324 7203 215A   # 複数銘柄
cd scripts && python make_stock_db.py update 6324 --snapshot  # 更新後にスナップショットも自動追記

# 特定銘柄データの表示 (引数で指定可)
cd scripts && python make_stock_db.py list 6324

# 上場廃止銘柄のクリーンアップ
cd scripts && python make_stock_db.py reflesh

# DBバックアップ
cd scripts && python make_stock_db.py backup

# PTSランキング再取得 + 当日決算銘柄の kessan_comments['pts'] 上書き
# (list_all_db を回さず PTS だけ最新化したい時に使う)
cd scripts && python make_stock_db.py refresh_pts

# 指定銘柄の master/price/shihyo/gyoseki/rironkabuka を強制再取得 + research_shelve スナップショット上書き
# (株探で上方修正/最新業績が反映されたとき、特定銘柄だけ手早く最新化したい時に使う)
# 決算速報 (kessan_quarter / kessan_mod_date) は別経路のため、必要なら shintakane.py を別途実行
cd scripts && python make_stock_db.py refresh_stock 421A
cd scripts && python make_stock_db.py refresh_stock 421A 6324 3496  # 複数銘柄

# モメンタムポイント動的キャリブレーション (issue #104)
cd scripts && python make_stock_db.py calibrate_momentum
```

## 保有銘柄リスト取得 (`portfolio_list.py`)

保有銘柄の code_s 一覧を他コマンドへ流し込むための CLI。ログは混ぜず stdout にコードのみスペース区切りで出す。

```bash
# 全ステータス (1保/2準/3監) のコードを出力
cd scripts && python portfolio_list.py

# ステータス絞り込み (1保=保有中 / 2準=準保有 / 3監=監視)
cd scripts && python portfolio_list.py --status 1保

# 保有中銘柄をまとめて update する例
for c in $(python portfolio_list.py --status 1保); do
    python make_stock_db.py update $c
done
```

## 市場DB操作

```bash
cd scripts && python make_market_db.py        # 市場DB更新 + market_data.html 生成
cd scripts && python make_market_db.py html   # DB更新なしで market_data.html だけ再生成
```

`html` サブコマンドは既存の market_db から表示用 HTML (market_data.html / disclosure_data.html) を作り直すだけ。スクレイピング不要なので、表示確認のための再生成に使う。

## 銘柄調査DB (`research_shelve`)

```bash
cd scripts && python research_shelve.py show 3496
cd scripts && python research_shelve.py list --rating S,A --keyword 駐車場
cd scripts && python research_shelve.py backup
```

`make_stock_db.py` の各実行末尾では、不可逆データである `research_shelve` と
`portfolio_shelve` の `.dat` / `.dir` / `.bak` を日付付きで自動保存し、各14世代を保持する。
復元時は WebApp と日次バッチを停止し、復元したい同一日付の3ファイル
（例: `research_shelve_260705.dat/.dir/.bak`）を `data/stock_data/` にコピーして、
日付部分を除いた `research_shelve.dat/.dir/.bak`（portfolio も同様）へ戻す。

## Shintakane Research（銘柄調査WebApp）

```bash
cd scripts && python -m webapp.app    # http://localhost:5001 で起動
```

## データ移行スクリプト

### スプシCSV → research_shelve 移行 (issue #92)

```bash
cd scripts && python migrate_research_from_csv.py "<csv_path>" --dry-run                                   # DB を触らず検証
cd scripts && python migrate_research_from_csv.py "<csv_path>" --db-path /tmp/verify --show 3496,247A,6920 # 一時DBで目視確認
cd scripts && python migrate_research_from_csv.py "<csv_path>"                                             # 本番移行
```

### 過去決算メモ log → research_shelve.kessan_comments 移行 (issue #131)

```bash
cd scripts && python migrate_kessan_comments_from_log.py ../data/kessan_comments_log.txt --dry-run                                   # パースのみ検証
cd scripts && python migrate_kessan_comments_from_log.py ../data/kessan_comments_log.txt --db-path /tmp/verify_kessan --show 5032,9556 # 一時DBで目視確認
cd scripts && python migrate_kessan_comments_from_log.py ../data/kessan_comments_log.txt                                             # 本番移行
```

### action_log に売買日終値プロキシを付与 (issue #361)

売買履歴の概算損益・成績サマリー用。既存 DB の price_log (直近30営業日) の範囲のみ終値を埋め、土日の売買日を直前営業日に補正する。夜間 price 更新後に叩くと当日 None が埋まる。

```bash
cd scripts && python backfill_price_proxy.py             # None のみ埋める (冪等)
cd scripts && python backfill_price_proxy.py --overwrite # actual 以外を再取得
```

### 証券会社CSV → fill レイヤー取込 (issue #360, #387, #390)

楽天・SBI・マネックス証券の約定CSVの実約定価格・株数を fill として取り込む。同一 dedup キーは冪等スキップ。取り込んだ fill は `/trade-history` の「売買履歴」タブに建玉ラウンド単位のエピソードとして表示され、勝率・ペイオフレシオも fill 側で計算される (issue #387 Phase4b)。SBI は個別株のみ取込 (ETF/投信=ウォッチリスト外は自動除外)。楽天は信用返済行の建約定日・建単価と現引行 (建玉の現物化) も取込む (信用/現物のエピソード損益計算に使用)。SBI 信用返済行の決済損益は fill に保存する。既存 fill への建単価・決済損益は再取込時に後付けされる (None→非None のみ)。

```bash
# 楽天 (tradehistory(JP)_YYYYMMDD.csv, Shift-JIS, 28列)
cd scripts && python import_rakuten_fills.py "<csv_path>" --dry-run   # 読込・パース検証 (DB 非書込)
cd scripts && python import_rakuten_fills.py "<csv_path>"             # 本番取込

# SBI (SaveFile_*.csv, Shift-JIS, 冒頭メタ行あり)
cd scripts && python import_sbi_fills.py "<csv_path>" --dry-run       # 読込・パース検証 (DB 非書込)
cd scripts && python import_sbi_fills.py "<csv_path>"                 # 本番取込

# マネックス (YYYYMMDD-YYYYMMDD.csv, Shift-JIS, 冒頭メタ行あり25列)
cd scripts && python import_monex_fills.py "<csv_path>" --dry-run     # 読込・パース検証 (DB 非書込)
cd scripts && python import_monex_fills.py "<csv_path>"               # 本番取込
```

マネックスは過去データのバックフィル用途 (現在は未使用)。銘柄コードが5桁 (`54710`) なので末尾の付加桁を落として4文字 `code_s` に正規化する。信用返済行は建約定日・建単価 (楽天と同じ) と受渡金額=諸経費控除後の決済損益 (SBI と同じ) の両方を持つ。税金・入出金・入出庫・配当金の行はスキップする。過去に売買したが現在ウォッチリストに無い銘柄を欠落させないため、除外は ETF のみ (SBI のようなウォッチリスト外除外はしない)。

いずれも `/trade-history` の取込UIからアップロードすれば、ヘッダの列数で証券会社を自動判別する (楽天=28列 / SBI=14列 / マネックス=25列)。

取込後の建玉ラウンド (エピソード) 損益・保有中の含み損益・振り返りメモの紐付けをターミナルで確認する (DB 非更新)。

```bash
cd scripts && python show_fill_episodes.py            # 全エピソード (最新約定日降順)
cd scripts && python show_fill_episodes.py 6324       # 特定銘柄のみ (内訳 fill も表示)
cd scripts && python show_fill_episodes.py --open     # 保有中のみ (残株数・実現/含み損益)
cd scripts && python show_fill_episodes.py --memo     # 振り返りメモ付きのみ
```

### 証券会社ポートフォリオCSV → position レイヤー取込 (issue #397 Phase1)

保有ステータス・保有株数の手入力に代えて、証券会社の残高CSVを真実源として自動同期するための取込コマンド。fill (約定の事実) とは別の position レイヤーに残高スナップショットを保存する。Phase1 は可視化のみで、`--apply` でも `qty`/`status` (record) には一切触れない。

楽天・SBI とも現物・信用が別ファイルで降ってくる (SBI は同名 `SaveFile*.csv` で連番)。ファイル名に依存せず中身の構造から4ソース (楽天現物/楽天信用/SBI現物/SBI信用) を判別するため、順不同でまとめて渡す。4ソースが揃わない実行は既定でエラー停止する。

```bash
cd scripts && python import_portfolio_csv.py \
    "assetbalance(all)_YYYYMMDD_HHMMSS.csv" \
    "marginbalance(JP)_YYYYMMDD_HHMMSS.csv" \
    "SaveFile.csv" "SaveFile (1).csv" \
    --as-of YYYY-MM-DD --dry-run           # 読込・差分プレビューのみ (DB 非書込)

cd scripts && python import_portfolio_csv.py <同上4ファイル> \
    --as-of YYYY-MM-DD --apply             # position/position_source を保存 (record は変更しない)
```

差分プレビューは「一致」「株数変更候補」「売却候補」「新規IN候補」等を表示する (Phase2 で実際の自動反映を解禁予定)。`covered` (全4ソースが同一基準日で揃っているか) が偽の銘柄は判定不能として除外する。

## 自動実行

`shintakane_cron.sh` が `shintakane.py` → `make_stock_db.py` を逐次実行。macOS launchd（`com.k_sohara.shintakane.cron.plist`）で平日19:00に定期実行。

詳細仕様:
- 平日(月〜金)19:00 の `StartCalendarInterval` が定刻発火
- `StartInterval=1800` (30分ごと) でスリープ復帰後の最初のwakeも補完
- `RunAtLoad=true` でログイン/再起動時にも起動
- shintakane_cron.sh の冒頭ガードで「19時以降」「当日未実行」の最初の起動でのみ Python 起動 (`~/.shintakane_cron_last_run` フラグ管理)
- 両プロセス (shintakane.py / make_stock_db.py) 成功時のみフラグを更新

## テスト

テスト方針・テストファイル一覧・統合テスト手順は [TESTING.md](TESTING.md) を参照。
