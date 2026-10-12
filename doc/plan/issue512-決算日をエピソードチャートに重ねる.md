# issue #512: エピソードチャートに決算発表日と業績修正日を重ねる

- 対象: 売買履歴のエピソードチャート (`/trade-history/chart`、#366)
- なぜ今: #448 の月次の振り返りで、逸脱と判定した取引の多くが決算まわりだった。チャート上で決算との前後を読めるようにする (検証性の領域)
- 範囲: チャートの表示だけ。月次の振り返り JSON (`trade_review.py`) には足さない (照合の基準になるルールがまだ無いため。傾向が見えてから別 issue)

## 調べて分かったこと (2026-10-12)

株探の業績ページ (`kabutan.jp/stock/finance?code=X&mode=k`、`gyoseki.py` が取得・キャッシュ済み) の HTML に、必要な日付が入っている。

| 欲しいもの | HTML 内の場所 | 範囲 |
|---|---|---|
| 決算発表日 | 「3ヵ月決算【実績】」の表の「発表日」列 | 直近8四半期 (約2年) |
| 業績修正の日 | 「修正履歴」の表 (`fin_year_forecast_d`) の、区分が「修」の行 | 5期ぶん程度 |
| 修正の方向 | 同じ表の矢印 | **直近分は株探プレミアム限定で取れない** (6銘柄で確認、日付だけ見える) |

- `parse_kabutan_account2` は発表日を `ldict["date"]` に読むが、`ldict_list` に入れず捨てている
- 売買した 166 銘柄のうち 151 銘柄に業績ページのキャッシュがある (開発用コピーでの数字)
- 修正日の多くは決算発表日と同じ日 (5243 は修正5件中3件)
- webapp は HTTP 取得を一切しない (`load_weekly_closes` に「ネットワークアクセスは行わない」の既存方針)
- 日次バッチの `get_gyoseki_data` は ThreadPoolExecutor (5スレッド) の中で銘柄ごとに呼ばれる

## 設計

### 1. 日付のパーサー (`gyoseki.py`)

`parse_kessan_event_dates(html) -> {"kessan": [date, ...], "shusei": [date, ...]}` を足す。

- `kessan`: 「3ヵ月決算【実績】」の表の発表日
- `shusei`: 修正履歴の表で区分が「修」の行の修正日 (「初」「実」は取らない。「実」は決算発表日と同じ)
- `YY/MM/DD` は `2000 + YY` として `date` にする。日付として読めないセル (「－」など) は捨てる。表が見つからなければ空リスト (例外にしない)
- `parse_kabutan_account2` と stocks_shelve のスキーマは変えない

### 2. 蓄積ファイル

四半期の表は8四半期ぶんしか載らないので、取れた日付を貯める。貯めないと、古いエピソードから印が消えていく。

- 場所: `$KS_DATA_DIR/stock_data/kessan_dates/<code_s>.json`
- 形式: `{"kessan": ["2024-10-09", ...], "shusei": ["2024-10-09", ...]}` (昇順・重複なし)
- 銘柄ごとに1ファイルにする。バッチが並行スレッドで銘柄ごとに書くので、1ファイルにまとめると read-modify-write が競合する
- 書き込みは `ks_util.file_write` (一時ファイル → `os.replace`)
- 既存の日付は消さない (和集合)。追加が無ければ書かない
- 置き場所は `gyoseki.py` に `load_kessan_dates(code_s)` / `merge_kessan_dates(code_s, dates)` として持つ

失っても直近2年ぶんは再取得できる。それより古い四半期の発表日は戻せないが、欠けるのはチャートの印だけなので、不可逆データとしての保全 (バックアップ対象への追加) はしない。

### 3. 貯めるタイミング

- **日次**: `gyoseki.get_gyoseki_data` の中で、取得した HTML を `parse_kessan_event_dates` に通して `merge_kessan_dates` する。業績ページを解析するたびに貯まる
- **初回投入**: `python gyoseki.py backfill_kessan_dates`
  - 売買したことのある銘柄 (`build_fill_episodes` の code_s) を対象にする
  - キャッシュ HTML があればそれを読む。無い銘柄 (15件ほど) だけ株探から取得する (`http_get_html_with_retry`、キャッシュに保存)
  - 運用機で1回実行する
- 売却してバッチの対象から外れた銘柄は、それ以降の決算日が増えない。これは仕様として割り切る (必要になったら初回投入を再実行すれば埋まる)

### 4. 修正の方向 (取れる範囲だけ)

適時開示の `disclosure_db.csv` に、その銘柄・その日付で見出しに「上方修正」「下方修正」を含む行があれば、方向を付ける (`classify_disclosure_impact` を再利用)。

- チャートを描くときに読む。CSV に残っている期間 (直近50日ほど) だけ方向が付き、それより古い修正日は方向なしの印になる
- 方向を蓄積ファイルに残すことはしない。使ってみて、古い分も方向が欲しくなったら別途検討する
- `disclosure.py` に `load_revision_directions(code_s) -> {date: "上方" | "下方"}` を足す (`load_disclosure_for_code` は年を落とした `MM/DD` を返すので使えない)

### 5. チャートへの描画 (`webapp/helpers.py` の `build_episode_chart`)

- x 座標は約定マーカーと同じく、その日付の週の月曜 (`_monday_of`) のバーに合わせる。チャートの座標系は月曜アンカーで、右端も最後の月曜なので、実日付で置くと右端の週の火〜金が枠外に出る
- 描くのは、週の月曜が表示窓 (`start_d`〜`end_d`、両端を含む) に入る日付だけ
- 決算発表日: 株価と出来高の領域を貫く細い縦線 (灰色の破線)。上端に小さく「決」
- 修正日: 上端に小さな印。方向が分かれば ▲上方 (赤) / ▼下方 (青) ではなく、売買マーカー (▲青=買 / ▼赤=売) と紛れないよう、文字で「上」「下」「修」とする
- 同じ日の決算と修正は1本にまとめ、ラベルを「決・上」のようにする
- 各印に `<title>` で「2026-07-07 決算発表 / 上方修正」のように日付を出す (同じ週の約定との前後はここで読む)
- 要素に `class="ep-kessan"` を付ける
- 縦軸の範囲は変えない (防衛線と違い、枠に収めるための拡張が要らない)
- 蓄積ファイルが無い銘柄は何も描かない (注記も出さない)

### 6. 表示の切り替え (`trade_history.html`、`style.css`)

「買→売の線」と同じ方式にする。縦軸が変わらないので取り直しは要らず、body のクラス `ep-hide-kessan` で隠す。

- チェックボックス「決算」を足す。`chartToggleDefaults` に `kessan: true` (既定で出す。1エピソードの窓に入るのは多くて数本)
- `style.css` に `.ep-hide-kessan .ep-kessan { display: none; }`

## テスト (3本)

| テスト | 内容 |
|---|---|
| `test_gyoseki.py`: `parse_kessan_event_dates` | 実 HTML を削った fixture から、決算発表日と「修」の日付だけが、4桁の年で取れる。表が無い HTML は空リスト (parametrize) |
| `test_gyoseki.py`: `merge_kessan_dates` | 既存の日付を残して和集合になる。追加が無ければ書かない |
| `test_webapp_helpers.py`: `build_episode_chart` | 窓の中の決算日だけ `ep-kessan` の要素が出る (右端の週の金曜は出る、窓の外の週は出ない)。同日の決算と修正は1本になる |

実行: `pytest tests/test_gyoseki.py tests/test_webapp_helpers.py tests/test_disclosure.py -v`

## 確認の手順

1. 開発用コピーで `python gyoseki.py backfill_kessan_dates` を実行し、5243 の JSON に 2024-10-09〜2026-07-07 の8件が入ることを見る
2. worktree の WebApp で `/trade-history` を開き、5243 のエピソードを展開して、決算の線・修正の印・ツールチップ・切り替えを確かめる
3. マージ後、運用機で pull → 初回投入を1回実行 → WebApp を再起動

## 文書の更新 (同じ PR)

- `doc/spec/コマンド一覧.md`: `gyoseki.py backfill_kessan_dates`
- `doc/アーキテクチャ.md`: データストアの一覧に `stock_data/kessan_dates/` (再取得できる蓄積、チャート表示用) を1行
- この計画書を削除する

## 対象外

- 月次の振り返り JSON への「決算の何営業日後か」の追加
- 修正の方向を過去に遡って取ること (株探プレミアム限定)
- 売買済みで監視外の銘柄を、日次バッチの取得対象に足すこと
- 全銘柄ぶんの決算日の保持 (バッチが業績ページを見た銘柄は副次的に貯まるが、それを使う機能は作らない)
