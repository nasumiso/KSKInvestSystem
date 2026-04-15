---
description: HTMLスクレイピングのパーサー変更時の対応手順
globs: scripts/shintakane.py, scripts/price.py, scripts/gyoseki.py, scripts/shihyou.py, scripts/master.py, scripts/kessan.py, scripts/make_market_db.py
---

# HTMLスクレイピング変更対応ルール

## パーサー修正時の必須手順

HTMLパース処理を変更したら、以下を順に実行すること。

1. 対応する単体テストを実行（testing.md のマッピング参照）
2. `cd scripts && python shintakane.py --force` でCSVを再生成し、`shintakane_result.csv` に反映されることを確認
   - `--force` を付けないと既存CSVキャッシュが使われ、パース修正が反映されない

## パースエラー・空データ発生時の調査手順

1. `pytest tests/test_live_html.py -v` でHTMLフォーマット変更を検知
2. 失敗したテストクラスから対応モジュールを特定し、パーサーを修正

| テストクラス | 対応モジュール | 確認内容 |
|---|---|---|
| `TestLiveHtmlPrice` | `price.py` | 日足HTML取得・パース |
| `TestLiveHtmlShihyou` | `shihyou.py` | 財務指標・時価総額抽出 |
| `TestLiveHtmlMaster` | `master.py` | 銘柄基本情報抽出 |
| `TestLiveHtmlGyoseki` | `gyoseki.py` | 業績データ抽出 |
| `TestLiveHtmlShintakane` | `shintakane.py` | 新高値銘柄パース |
| `TestLiveHtmlKessan` | `shintakane.py` | 決算速報パース |
| `TestLiveHtmlTheme` | `make_market_db.py` | テーマランクパース |

## 注意事項

- Yahoo価格データはyfinance API経由のため、HTMLフォーマット変更の影響を受けない
- Kabutan HTMLのパース処理は `shintakane.py` の `convert_kabutan_*_html()` が起点
