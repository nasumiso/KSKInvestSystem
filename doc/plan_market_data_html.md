# market_data HTML出力機能の追加

## Context

market_data.csv をGoogleスプレッドシートにアップロードして閲覧しているが、CSVの可読性が低い（色分けなし、リンクが`=HYPERLINK()`関数表示、全データがフラットに並ぶ）。ブラウザで開くだけで見やすいHTMLファイルを並行生成する。プロトタイプ（`/tmp/market_data_prototype.html`）の方向性はユーザー承認済み。

## 実装方針

1. `create_market_csv()` のCSV生成処理をコメントアウトし、代わりに `create_market_html()` を呼ぶ形に変更する。kessan/disclosure データ取得ロジックは `create_market_csv()` 内に残し、取得済みデータをHTML生成に渡す。
2. GoogleドライブアップロードはCSV→スプレッドシートではなく、HTMLファイルを「投資データ」フォルダにアップロードする形に変更。`googledrive.py` に `upload_html()` を追加。
3. 新高値テーマ分布の「当日」「過去」行は不要になったため、生成コードをコメントアウトする。
4. 既存コードは削除せずコメントアウトで残す（将来CSV復活が必要になった場合に備える）。

## 変更ファイル

### `scripts/make_market_db.py`（主要変更）

#### 1. `create_market_csv()` の変更（HTML出力に置き換え）

`create_market_csv()` 内のデータ取得ロジック（theme_rank, 市場データ, kessan, disclosure）はそのまま残す。
以下をコメントアウト:
- 行371-399: CSV `rows` 構築（テーマランクセクション）
- 行427-456: CSV `rows` 構築（市場・決算・適宜開示セクション）
- 行458-460: CSV書き出し（`open(csv_path, "w")` + `csv_w.writerows(rows)`）
- 行462-467: 旧GoogleDriveアップロード（`threading.Thread(target=googledrive.upload_csv, ...)`）

代わりにHTML生成 + Googleドライブアップロードを呼び出す:
```python
# HTML版を生成（CSV版に置き換え）
theme_rank_data = (theme_rank_list, prev_theme_rank_list, None, prev_day)
html_path = create_market_html(market_db,
                   kessan_csv=kessan_csv, disc_csv=disc_csv,
                   theme_rank_data=theme_rank_data)

# GoogleDriveに非同期アップロード（HTMLファイルとして）
import threading
threading.Thread(
    target=googledrive.upload_html, args=(html_path,), daemon=False
).start()
```

※ データ取得部分（`get_theme_rank_list()`, `kessan.make_kessan_csv()`, `disclosure.update_disclosure_all()` 等）は引き続き `create_market_csv()` 内で実行し、取得結果をHTML関数に渡す。関数名は変えず互換性を保つ。

#### 1b. `update_shintakane_theme_csv()` のコメントアウト

`scripts/make_market_db.py` 行492-503: 関数本体をコメントアウト

#### 1c. 呼び出し元の変更

`scripts/shintakane.py` 行470-473: `update_shintakane_theme_csv()` 呼び出しをコメントアウト
```python
# shintakane_theme_csv = make_market_db.update_shintakane_theme_csv(...)
# make_market_db.create_market_csv(None, shintakane_theme_csv)
make_market_db.create_market_csv()  # shintakane_theme_csv は不要になった
```

#### 2. `create_market_html()` 関数の追加

```python
def create_market_html(market_db,
                       kessan_csv=None, disc_csv=None,
                       theme_rank_data=None):
    """市場DBから表示用HTMLファイルを生成する

    Args:
        market_db: 市場DB（必須）
        kessan_csv: 決算CSVデータ（Noneの場合はセクション省略）
        disc_csv: 適宜開示CSVデータ（Noneの場合はセクション省略）
        theme_rank_data: get_theme_rank_list()の返り値タプル（Noneの場合は履歴テーブル省略）
    """
```

**重要: 全引数はレンダリングに必要なデータを受け取るだけ。Noneの場合はそのセクションを省略する（ネットワーク再取得しない）。** これにより、HTML生成関数は完全にネットワーク非依存の純粋なレンダリング関数となる。`shintakane_theme_csv`（新高値テーマ分布）は不要になったため引数から除外。

処理フロー:
1. html_path = `{DATA_DIR}/code_rank_data/market_data.html`
2. 各セクションのHTMLを個別ヘルパーで生成
3. HTMLテンプレート（f-string）にパーツを埋め込み、ファイル書き出し
4. `log_print()` で生成完了を通知

#### 3. HTMLエスケープ方針

全てのスクレイピング由来テキスト（テーマ名、銘柄名、開示見出し等）は `html.escape()` でエスケープしてからHTMLに埋め込む。`import html` を追加。

- テーマ名: `html.escape(theme)`
- 銘柄名: `html.escape(stock_name)`
- 開示タイトル: `html.escape(title)`
- URL: `=HYPERLINK` からパースしたURLはエスケープせず `<a href>` に使用（株探URL固定パターンのため）

#### 4. ヘルパー関数の設計

**テーマランク (`_html_theme_rank`)**:
- `market_db["theme_rank"]` と `market_db["theme_rank_diff"]` を使用
- diff が None → `theme-new`、正 → `theme-up`、負 → `theme-down`、0 → `theme-flat`
- `market_db["theme_momentum"]` から騰落率を取得し、正負で `rate-pos`/`rate-neg`
- `theme_rank_data` 引数からKabutan履歴テーブル（Noneなら省略）
- ※ 新高値テーマ分布の「当日」「過去」行は廃止

**市場指標 (`_html_market`)**:
- `create_market_csv()` 内の `get_db_row()` と同じデータ取得ロジック
- `direction_signal` に "sell" 含む → `signal-sell`、"buy" → `signal-buy`
- トレンドの `◯`/`◎` → `trend-good`

**決算 (`_html_kessan`)**:

`kessan_csv` は `make_kessan_csv()` の返り値で、3種類の構造が混在する:
- `write_to_csv(before_list)` → 2行セット: [日付リスト行, 銘柄リスト行]（過去分をまとめて）
- `write_to_csv_current(current_list)` → 日付ごと1行: [日付, 銘柄1, 銘柄2, ...]（直近14日）
- `write_to_csv(future_list)` → 2行セット: [日付リスト行, 銘柄リスト行]（14日以降をまとめて）

パース方針:
1. `kessan_csv` を先頭から走査
2. 行の最初の要素が日付パターン（`MM/DD`）かどうかで判定:
   - 日付パターンが複数カラムに並ぶ行 → `write_to_csv` 形式。次の行が銘柄リスト行
   - 日付パターンが最初の1カラムだけ → `write_to_csv_current` 形式。同一行に銘柄も含む
3. 各行をパースして `(date_str, [銘柄文字列リスト])` のタプルリストに正規化
4. 正規化後のリストからカードHTMLを生成

**適宜開示 (`_html_disclosure`)**:
- `disc_csv` の各行から `=HYPERLINK("url","text")` を正規表現でパース → `html.escape(text)` を `<a href="url">` に
- 直近3日分は `<details open>`、それ以前は `<details>`（折りたたみ）

#### 5. CSSテンプレート

プロトタイプ（`/tmp/market_data_prototype.html`）のCSSをそのまま埋め込む。HTMLテンプレート文字列の先頭に定数として定義。

```python
_HTML_CSS = """..."""
_HTML_TEMPLATE = """<!DOCTYPE html>..."""
```

### `scripts/googledrive.py`（HTMLアップロード追加）

`upload_html()` 関数を追加。「投資データ」フォルダにHTMLファイルをアップロードする。

```python
def upload_html(html_path):
    """HTMLファイルをGoogleDriveの「投資データ」フォルダにアップロードする"""
    log_print("%sをGoogleDriveにアップロードします" % html_path)
    drive_service = get_drive_service()
    folder_id = FOLDER_DICT["投資データ"]
    fname = os.path.basename(html_path)

    # 既存ファイルを検索（同名ファイルがあれば上書き更新）
    results = drive_service.files().list(
        q="name='%s' and '%s' in parents and trashed=false" % (fname, folder_id),
        fields="files(id)"
    ).execute()
    files = results.get("files", [])

    media = MediaFileUpload(html_path, mimetype="text/html", resumable=True)

    if files:
        # 既存ファイルを更新
        file_id = files[0]["id"]
        drive_service.files().update(fileId=file_id, media_body=media).execute()
        log_print("Upload(更新) Complete: %s" % fname)
    else:
        # 新規作成
        file_metadata = {"name": fname, "parents": [folder_id]}
        drive_service.files().create(
            body=file_metadata, media_body=media, fields="id"
        ).execute()
        log_print("Upload(新規) Complete: %s" % fname)
```

ポイント:
- CSVの `upload_csv()` と違い、スプレッドシート変換（`mimeType: application/vnd.google-apps.spreadsheet`）は行わない
- 同名ファイルがあれば上書き更新、なければ新規作成
- 「投資データ」フォルダ（ID: `1CvpiB0bV4mK8DLR_LBQmeCXKgrYHOJZr`）にアップロード

### `tests/test_make_market_db.py`（テスト追加）

- `_html_theme_rank()`: モックDB で NEW/UP/DOWN/FLAT の各クラスが出力されることを検証
- `_html_market()`: モックDBで signal-sell/signal-buy クラスの検証
- `_html_disclosure()`: `=HYPERLINK(...)` → `<a>` 変換の正規表現テスト + HTMLエスケープの検証
- `_html_kessan()`: 3種類の混在構造を含むモックデータで正しくパースされることを検証
- `create_market_html()`: モックデータで HTML ファイルが生成され、全セクションヘッダーを含むことを検証

### `scripts/shintakane.py`（呼び出し元変更）

行470-473: `update_shintakane_theme_csv()` 呼び出しをコメントアウトし、`create_market_csv()` を引数なしで呼ぶ

## 実装順序

1. 新高値テーマ分布の廃止:
   - `make_market_db.py`: `create_market_csv()` 内の `shintakane_theme_csv` 行追加をコメントアウト
   - `make_market_db.py`: `update_shintakane_theme_csv()` をコメントアウト
   - `shintakane.py`: 呼び出し箇所をコメントアウトし `create_market_csv()` を引数なしに
2. `googledrive.py`: `upload_html()` 関数を追加
3. `make_market_db.py`: `import html` と `_HTML_CSS`, `_HTML_TEMPLATE` 定数を追加
4. `_html_theme_rank()` ヘルパー追加
5. `_html_market()` ヘルパー追加
6. `_html_kessan()` ヘルパー追加（3種類の混在構造対応）
7. `_html_disclosure()` ヘルパー追加（HTMLエスケープ付き）
8. `create_market_html()` 関数追加（html_pathを返す）
9. `create_market_csv()` のCSV生成・旧アップロードをコメントアウトし、HTML生成 + 新アップロードに置き換え
10. テスト追加

## 検証方法

1. `cd scripts && python -c "import make_market_db; db=make_market_db.get_market_db(); make_market_db.create_market_html(db)"` で単体実行（kessan/disclosure セクションは省略される）
2. 生成された `{DATA_DIR}/code_rank_data/market_data.html` をブラウザで開いて目視確認
3. `pytest tests/test_make_market_db.py -v -k html` でHTML関連テスト実行
4. 既存テスト全体: `pytest tests/ -v` で回帰テストなし確認
5. パイプライン統合テスト: `cd scripts && python shintakane.py analyze` でHTMLが生成され、Googleドライブにアップロードされることを確認
