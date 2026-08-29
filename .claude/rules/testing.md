---
description: Pythonコードを変更した場合のテスト実行ルール
globs: scripts/**/*.py, tests/**/*.py
---

# テスト実行ルール

Pythonコードを変更したら、以下のマッピングに従って関連テストを実行すること。
テンプレート(HTML)・CSS・JSのみの変更はブラウザ確認のみでよい。

## モジュール → テストのマッピング

### 共通基盤（変更時は全テスト実行）

| 変更対象 | テストコマンド |
|---|---|
| `ks_util.py` | `pytest tests/ -v -m "not local_db and not live_html"` |
| `db_shelve.py` | `pytest tests/ -v -m "not local_db and not live_html"` |

### データ取得・パース系

| 変更対象 | テストコマンド |
|---|---|
| `price.py` | `pytest tests/test_price.py -v` |
| `gyoseki.py` | `pytest tests/test_gyoseki.py -v` |
| `shihyou.py` | `pytest tests/test_shihyou.py -v` |
| `master.py` | `pytest tests/test_master.py -v` |
| `rironkabuka.py` | `pytest tests/test_rironkabuka.py -v` |
| `kessan.py` | `pytest tests/test_kessan.py -v` |
| `shintakane.py` | `pytest tests/test_shintakane.py -v` |

HTMLパーサー変更時は追加で `cd scripts && python shintakane.py --force` を実行し、CSV再生成を確認。

### DB・ランキング系

| 変更対象 | テストコマンド |
|---|---|
| `make_stock_db.py` | `pytest tests/test_make_stock_db.py tests/test_append_research_snapshots.py -v` |
| `make_market_db.py` | `pytest tests/test_make_market_db.py tests/test_functional_market.py -v` |
| `research_shelve.py` | `pytest tests/test_research_shelve.py -v` |
| `migrate_research_from_csv.py` | `pytest tests/test_migrate_research_from_csv.py -v` |
| `googledrive.py` | `pytest tests/test_googledrive.py -v` |
| `disclosure.py` | `pytest tests/test_disclosure.py -v` |
| `portfolio_shelve.py` | `pytest tests/test_portfolio_shelve.py -v` |
| `exposure_guide.py` | `pytest tests/test_exposure_guide.py -v` |
| `import_portfolio_csv.py` | `pytest tests/test_import_portfolio_csv.py -v` |

スコアリング・ランキングのロジック変更時は追加で `cd scripts && python make_stock_db.py list_all_db` で統合テスト。

### WebApp系

| 変更対象 | テストコマンド |
|---|---|
| `webapp/helpers.py` | `pytest tests/test_webapp_helpers.py tests/test_html_sanitizer.py -v` |
| `webapp/routes/` | `pytest tests/test_webapp_routes.py -v` |
| `webapp/` の HTML サニタイズ関連 | `pytest tests/test_html_sanitizer.py -v` |

## テスト不要なケース

- テンプレート（HTML）、CSS、JSのみの変更 → ブラウザで目視確認
- ドキュメント（`doc/`、`CLAUDE.md`等）のみの変更
- 設定ファイル（`.claude/`）のみの変更
