# テスト方針

## テスト階層

| 層 | 目的 | 外部依存 | マーカー | CI |
|----|------|----------|----------|-----|
| 単体テスト | 純粋関数の計算検証 | なし | なし | ○ |
| 機能テスト | ビジネスフローの結合検証 | HTTP→モック、DB→tmp_path | `functional` | ○ |
| 統合テスト | stockDB ロジック変更時の 1 銘柄 E2E 検証 | 実HTTP（kabutan/yfinance） | — | × |
| Live検証 | HTMLフォーマット変更検知 | 実HTTP通信 | `live_html` | × |

```bash
# CI（単体 + 機能テスト）
pytest tests/ -v -m "not local_db and not live_html"

# 機能テストのみ
pytest tests/ -v -m "functional"

# 単体テストのみ（高速）
pytest tests/ -v -m "not local_db and not live_html and not functional"

# 特定モジュールのみ
pytest tests/test_gyoseki.py -v
```

## 機能テスト

複数モジュールを結合し、ビジネスフロー全体の正常動作を検証するテスト。HTTP通信・Google Driveはモックし、shelve DBは`tmp_path`に一時作成して実際のShelveDBクラスで読み書きする。

### テストファイル一覧

| テストファイル | 対象フロー | 備考 |
|---|---|---|
| `test_functional_market.py` | `update_market_db()` → `create_market_csv()` → HTML生成 | テーマランク差分・冪等性・日付変更 |

### 機能テスト追加の方針

- **モックするもの**: 外部I/O境界（HTTP通信、Google Drive API）
- **モックしないもの**: パース処理、計算ロジック、shelve DB操作（tmp_path使用）、CSV生成
- **冪等性テスト**: 同日複数回実行でも結果が安定することを必ず検証
- **フィクスチャ**: `tests/fixtures/` にHTML等の固定データを配置

### 既存機能の修正時

既存機能を修正する際は、**修正対象に関連する機能テストを必ず実行**して回帰がないことを確認すること。

```bash
# make_market_db 周りの修正時
pytest tests/test_functional_market.py tests/test_make_market_db.py -v
```

機能テストが存在しないモジュールを修正する場合は、修正の影響範囲に応じて機能テストの追加を検討する。

## 単体テスト

pytestで主要モジュールの純粋計算関数をテスト（DB・HTTP通信不要）。

### テストファイル一覧

| テストファイル | 対象モジュール | 備考 |
|---|---|---|
| `test_ks_util.py` | `ks_util.py` | **変更時は全テスト実行** |
| `test_rironkabuka.py` | `rironkabuka.py` | |
| `test_gyoseki.py` | `gyoseki.py` | |
| `test_price.py` | `price.py` | |
| `test_make_stock_db.py` | `make_stock_db.py` | |
| `test_db_shelve.py` | `db_shelve.py` | |
| `test_shihyou.py` | `shihyou.py` | |
| `test_master.py` | `master.py` | |
| `test_shintakane.py` | `shintakane.py` | HTMLパース関数（決算含む） |
| `test_kessan.py` | `kessan.py` | 決算判定・タグ生成 |
| `test_make_market_db.py` | `make_market_db.py` | |
| `test_functional_market.py` | `make_market_db.py` | 機能テスト（`functional`マーカー） |
| `test_live_html.py` | 全パーサー | HTMLフォーマット変更検知（`live_html`マーカー） |

### CI

GitHub Actions（`.github/workflows/test.yml`）でPR/push時に自動実行。

```bash
# CIと同じ条件（local_db, live_html除外）
pytest tests/ -v -m "not local_db and not live_html"
```

## HTMLパース変更時の検証（shintakane.py --force）

`shintakane.py` はHTML取得結果をCSVにキャッシュし、同日中は再生成をスキップする。HTMLパースのロジックを変更した場合、既存CSVが残っているとパース修正が反映されない。

```bash
cd scripts && python shintakane.py --force
```

`--force` を付けるとCSV存在チェックをスキップし、HTMLキャッシュからCSVを再生成する。パース変更後は必ず `--force` で実行して `shintakane_result.csv` に反映されることを確認すること。

## HTMLフォーマット変更検知テスト（live_html）

実際にHTTPでkabutan.jpにアクセスし、各パーサーが期待通りにデータを抽出できるかを確認するテスト。CIでは除外され、ローカルで手動実行する。

```bash
# 全パーサーの検知テスト実行
pytest tests/test_live_html.py -v
```

### いつ実行するか

- データ取得でパースエラーや空データが発生した時
- kabutan.jpのHTMLフォーマット変更が疑われる時
- `log_warning("決算ページフォーマット変更?")` 等の警告がログに出た時

### テスト対象と対応モジュール

| テストクラス | 対応モジュール | 確認内容 |
|---|---|---|
| `TestLiveHtmlPrice` | `price.py` | 日足HTML取得・パース |
| `TestLiveHtmlShihyou` | `shihyou.py` | 財務指標・時価総額抽出 |
| `TestLiveHtmlMaster` | `master.py` | 銘柄基本情報抽出 |
| `TestLiveHtmlGyoseki` | `gyoseki.py` | 業績データ抽出 |
| `TestLiveHtmlShintakane` | `shintakane.py` | 新高値銘柄パース |
| `TestLiveHtmlKessan` | `shintakane.py` | 決算速報パース |
| `TestLiveHtmlTheme` | `make_market_db.py` | テーマランクパース |

失敗したテストクラスから対応モジュールのパーサーを修正する。

## 統合テスト（1 銘柄 E2E）

stockDB ロジック（指標計算・価格取得・業績取得など）を変更したら、本番 DB から 1 銘柄だけを強制再取得して挙動を確認する。全銘柄を回さないので 1 ループが軽く、ロジック修正のイテレーションに使える。

```bash
source .venv/bin/activate
cd scripts

# 1 銘柄を強制再取得（UPD_FORCE で master/price/shihyo/gyoseki/rironkabuka を再評価）
python make_stock_db.py update 6324

# 複数銘柄
python make_stock_db.py update 6324 7203 215A

# 更新 + ウォッチ銘柄スナップショット追記までワンショット
python make_stock_db.py update 6324 --snapshot

# 更新後の DB 中身を確認（score_gyoseki / momentum_pt / funda_pt / price_log 等）
python make_stock_db.py list 6324
```

## 手動回帰確認（全銘柄 list_all_db）

統合テスト（1 銘柄 E2E）で担保しきれない大域的な動作（全銘柄ランキング、Google Drive アップロード等）はローカル DB で目視確認する。

```bash
source .venv/bin/activate
cd scripts

# ランキング全体を再生成して確認
python make_stock_db.py list_all_db
```

**注意: コンソールに出力されない。** `log_print` 経由のためすべてログファイルとCSVに出力される。確認先:
- **ログ**: `logs/make_stock_db.log`（処理経過・エラー）
- **ランキングCSV**: `data/code_rank_data/code_rank.csv`（最終結果）
- 正常終了時はGoogle Driveへの自動アップロードも実行される（`Upload Complete` ログで確認）

## テスト量・粒度の方針

テストは「**書けば書くほど良い**」ものではない。1テスト = 1つの保守単位 (メソッド名・docstring・assertion・将来のリファクタ追従) を抱えるため、増やすほどコストがかかる。書く前に「このテストは将来バグを捕まえるか?」「同じ動作を別のテストで既に担保していないか?」を自問すること。

### 数量の目安

| 指標 | 目安 | 判断ポイント |
|---|---|---|
| テスト/実装 行数比率 | **30〜50%程度** | 70% を超えたら「単純すぎるテスト」が紛れていないか棚卸し |
| 1 PR で追加するテスト数 | **5本以下** が目安 | issue 要件1つにつき 1〜3本が自然。10本を超えるなら parametrize でまとめられないか検討 |
| 1 関数あたりのテスト数 | **3〜5本** で十分 | 入力空間の網羅は parametrize で1関数に集約 |

### 「書く価値があるテスト」

- **バグ修正の回帰テスト**: 過去に発生したバグの再発防止 (issue 番号紐づけ推奨)
- **複雑なロジックの境界値**: モメンタムポイントの ±1σ、決算日跨ぎ判定など、計算式の境界が曖昧で誤りやすい部分
- **排他制御・並行性**: lost update protection (`research_shelve.sync_stock_name` 等)、flock の動作
- **後方互換性**: shelve スキーマ拡張時の旧データ読み込み (`get_xxx_record` の backfill)
- **外部I/Oの正規化**: HTML パース、CSV パース、API レスポンスの解釈
- **契約テスト**: API レスポンスの JSON 形式 (キー存在・型) など、フロント/外部と約束した形式

### 「書かない方が良い (削減候補) テスト」

- **自明な動作のテスト**: `dict["key"] = value` で値が `value` であることなど Python の基本動作の再確認
- **getter/setter の素通し**: 値を入れて出して同じことだけを確認
- **ファクトリ関数の各フィールドが入るかの個別確認**: 1つの「フル引数 roundtrip」で済むものを分割しない
- **parametrize 過剰**: 「無効な値の例」を10種類列挙して個別 ValueError 確認 → 代表3つで十分
- **上位テストで自動カバーされる下位再テスト**: `compute_cell_styles` でカバーされる private 関数の個別テストなど
- **モック過多でロジック実体を検証していないテスト**: 何を確かめたいのか不明瞭になる
- **テンプレ文字列の `in` チェック**: レイアウト変更で誤検知しやすく機能のバグでは落ちにくい
- **後方互換 backfill だけのテスト乱発**: スキーマごとに「無 → デフォルト」「有 → そのまま」を毎回2本ずつ書く → roundtrip 1本 + backfill 1本で十分

### parametrize で集約するパターン

「入力 → 期待出力の表」になっている検証は parametrize でまとめる。テスト関数数は減るが網羅性は維持できる。

```python
# Bad: 1ルール = 1テスト関数
def test_rank_lt_300_strong_yellow(self):
    assert compute_cell_styles({"rank": 299})["rank"] == "background:濃黄"
def test_rank_300_no_color(self):
    assert "rank" not in compute_cell_styles({"rank": 300})
def test_rank_none_no_color(self):
    assert "rank" not in compute_cell_styles({"rank": None})
# ... (60 個続く)

# Good: テーブル駆動
@pytest.mark.parametrize(
    "row, field, expected",
    [
        ({"rank": 299}, "rank", "background:濃黄"),
        ({"rank": 300}, "rank", None),
        ({"rank": None}, "rank", None),
        # ... 他のルールも同じ表に並べる
    ],
)
def test_simple_threshold_rules(self, row, field, expected):
    styles = compute_cell_styles(row, today=TODAY)
    if expected is None:
        assert field not in styles
    else:
        assert styles[field] == expected
```

### 棚卸しのタイミング

PR レビューや実装作業中に「**このテストは何を守っているのか?**」が即答できないものを見つけたら削減候補。比率が 70% を超えた時点で全体棚卸しを検討する。
