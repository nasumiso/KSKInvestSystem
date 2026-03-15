# Issue #51 実装プラン: PTSランキング銘柄の自動取り込みとニュース材料の収集

## スコープ

### Phase A（今回実装）
- PTSランキング銘柄を「今日の銘柄」に取り込み、shintakane_result.csv に反映
- 各銘柄のニュースを disclosure.py で収集し、HTMLキャッシュ＋CSV保存

### Phase B（後段実装、今回対象外）
- ニュースの結果表示（shintakane_result.csv への統合等）

---

## 1. PTSランキング取り込み

### 1.1 データ取得: `get_todays_pts()` を `shintakane.py` に追加

- **URL**: `https://kabutan.jp/warning/pts_night_price_increase`
- **取得上位**: 20銘柄（1ページ分のみ）
- **キャッシュ**: 既存パターンに準拠。`data/cache_data/` にHTMLキャッシュ保存
- **キャッシュ判定**: `get_price_day()` ベースで当日分があればスキップ（18:00境界）。既存の `get_todays_shintakane()` / `get_todays_dekidakaup()` と同じロジック
- **CSV保存**: `data/shintakane_data/pts_YYMMDD.csv`
  - 既存の新高値CSVと同じ8カラム構成:
    `rank, "code_s name", market, sector, kabuka, zenjitsuhi, zenjitsuhi_per, dekidaga`

### 1.2 HTMLパーサー: `convert_kabutan_pts_html()` を `shintakane.py` に追加

- 既存の `convert_kabutan_dekidakaup_html()` と類似構造
- 株探PTSページのテーブルをパースし、上記CSV形式の行リストを返す
- 上位20件に制限

### 1.3 CSV読み込み・ファイル検索関数を `shintakane.py` に追加

- `search_fromcsv_pts()`: 既存の `search_fromcsv()` と同様、`origin = "pts"` を設定
- `get_pts_day_txtname(today)`: 日付からCSVファイルパスを生成
- `get_latest_pts_fname()`: 最新日付のPTSファイルを検索

### 1.4 `todays_shintakane()` への統合

- `create_today_list()` 内で PTS CSV も読み込み、`compose_list()` で合流
- **`create_already_list()` にも PTS を追加**: 過去日のPTS CSVも読み込み、既出銘柄の重複排除パスに含める。これにより、PTS銘柄が翌日以降に「未調査」扱いにならない
- PTS銘柄は新高値・出来高急増と同じフローで DB 更新・フィルタ・スコアリングされる

### 1.5 結果表示

- `puts_detail()` の `origin` 表示に PTS を追加: `"pts"` → `"P"`
- PTS銘柄は shintakane_result.csv に自然に含まれる

### 1.6 `main()` への組み込み

- `update` フェーズで `get_todays_pts()` を呼び出し
- 配置: `get_todays_shintakane()`, `get_todays_dekidakaup()` の直後

---

## 2. ニュース材料の収集

### 2.1 タイミングと対象銘柄

- **`main()` の `update` フェーズ**で実行（`get_todays_pts()` の直後）。`todays_shintakane()` 内には配置しない
- 理由: `todays_shintakane()` は `python shintakane.py analyze`（スクレイピングなし）でも呼ばれるため、ネットワークI/Oを含む処理を入れると「既存データのみ分析」の契約を壊す
- **対象銘柄**: 本日の新高値CSV + 出来高急増CSV + PTS CSV から銘柄コードリストを生成する。`get_latest_*_fname()` → `search_fromcsv*()` で各CSVを読み、コードリストを作成
- `updatelist_all_code`（過去14日分+決算銘柄）は使わない。リクエスト数を抑えるため

### 2.2 `disclosure.py` に追加する関数

#### `filter_recent_news(record_list, days=3)`
- `record_list`（`parse_disclosure_html()` の返り値）から、直近 `days` 日以内のレコードのみを返す
- 日付判定は `record["date"]`（`"YYYYMMDD"` 形式）と現在日付の比較

#### `update_disclosure_for_today(code_s_list, days=3)`
- 「今日の銘柄」用のエントリポイント
- 各銘柄に対して `update_disclosure()` を呼び出し、ニュースを収集
- `filter_recent_news()` で直近 `days` 日以内に絞り込み
- 結果を `data/disclosure/todays_disclosure.csv` に CSV 出力
- **`expoert_to_csv()` に出力先パラメータ `csv_path` を追加**（デフォルトは既存の `DISCLOSURE_CSV`）。これにより既存の `update_disclosure_all()` は影響を受けず、新関数から別パスに出力できる
- HTMLキャッシュは既存の `data/disclosure/` に保存（`disclosure.py` の既存動作）

### 2.3 `shintakane.py` での呼び出し

- `main()` の `update` フェーズ内、`get_todays_pts()` の直後に `update_todays_news()` を追加
- `update_todays_news()` は本日の各CSV（新高値・出来高急増・PTS）からコードリストを生成し、`disclosure.update_disclosure_for_today(code_s_list)` を呼ぶ
- `analyze` モードでは呼ばれない（`update` ブロック内のため）

---

## 3. 変更ファイル一覧

| ファイル | 変更内容 |
|---------|---------|
| `scripts/shintakane.py` | `get_todays_pts()`, `convert_kabutan_pts_html()`, `search_fromcsv_pts()`, `get_latest_pts_fname()`, `get_pts_day_txtname()`, `update_todays_news()` 追加。`main()`, `todays_shintakane()` 内の `create_today_list()`, `create_already_list()`, `puts_detail()` 修正 |
| `scripts/disclosure.py` | `filter_recent_news()`, `update_disclosure_for_today()` 追加。`expoert_to_csv()` に `csv_path` パラメータ追加 |
| `tests/test_shintakane.py` | `TestConvertKabutanPtsHtml`, `TestSearchFromcsvPts` 追加 |
| `tests/test_live_html.py` | `TestLiveHtmlPts` 追加 |

---

## 4. テスト計画

### レベル1: ユニットテスト（ネットワーク不要）

`tests/test_shintakane.py` に追加:

- **`TestConvertKabutanPtsHtml`**: サンプルHTMLから正しいCSV行が返るか
  - 単一銘柄パース、複数銘柄、英数字コード、空テーブル、出力カラム数(8)
- **`TestSearchFromcsvPts`**: CSV → dict 変換で `origin = "pts"` がセットされるか

`tests/test_disclosure.py`（新規）に追加:

- **`TestFilterRecentNews`**: 日付ベースで直近N日以内のレコードのみ残るか

### レベル2: ライブHTMLテスト（ネットワーク必要、`@pytest.mark.live_html`）

`tests/test_live_html.py` に追加:

- **`TestLiveHtmlPts`**: 実際に `https://kabutan.jp/warning/pts_night_price_increase` を取得して `convert_kabutan_pts_html()` が1件以上パースできるか

### レベル3: 手動統合テスト

- `cd scripts && python shintakane.py` 実行後、`shintakane_result.csv` の種類列に「P」が含まれるか目視確認

---

## 5. 実装順序

1. `convert_kabutan_pts_html()` — パーサー実装 + ユニットテスト
2. `get_todays_pts()` 関連 — データ取得 + CSV保存 + ファイル検索関数
3. `todays_shintakane()` 統合 — PTS を today_list に合流 + `puts_detail()` の origin 表示
4. `main()` 修正 — update フェーズに PTS 取得追加
5. `disclosure.py` 拡張 — `filter_recent_news()`, `update_disclosure_for_today()`, `expoert_to_csv()` のパラメータ追加
6. `main()` の `update` フェーズに `update_todays_news()` 追加
7. ライブHTMLテスト追加
