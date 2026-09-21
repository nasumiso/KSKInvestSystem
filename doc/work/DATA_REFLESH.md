# データディレクトリ精査・整理の記録

`KS_DATA_DIR` (現運用: `/Users/k_sohara/Ext/GoogleDrive/shintakane_data`) の棚卸し記録。

| 実施日 | 全体サイズ | 備考 |
|---|---|---|
| 2026-03-14 | 2.3GB | 精査のみ（未実行） |
| 2026-08-30 | 2.68GB → **2.00GB** | 実行済み（約680MB削減） / #174 移行前の棚卸し |

---

## 2026-08-30 実施分

issue #174 (MacMini 移行) でローカルSSDへ移す前の整理として実施。

### 削除したもの

| 対象 | 削減 | 判断根拠 |
|---|---|---|
| `stock_data/stocks_pickle_back/` の7ファイル | 122MB | shelve 移行前のレガシー pickle。**年1個を目安に残す方針**で12→5ファイルに間引き |
| `disclosure/html_cache/` (1,939件) | 145MB | **コードからの参照ゼロの孤児**。`disclosure.py` が使うのは `disclosure/cache` のみ |
| `stock_data/kabutan/{price,finance,base}` の180日超 | 441MB | HTTPキャッシュ。mtime 判定で TTL 管理されており、必要分は次回実行時に再取得される |
| `stock_data/*.bak_* / *.before_*` (49件) | 20MB | 過去 issue 作業時の手動バックアップ残骸 |

### `stocks_pickle_back/` の保持方針（確定）

**レガシー資料として年1個を目安に残す。** 現在の保持内容:

| ファイル | サイズ | 位置づけ |
|---|---|---|
| `stocks_200810.pickle` | 16.2MB | 2020年 |
| `stocks_220808.pickle` | 29.4MB | 2022年 |
| `stocks_240106.pickle` | 32.7MB | 2024年 |
| `stocks_250705.pickle` | 12.6MB | 2025年 |
| `stocks_260221.pickle` | 12.6MB | 2026年 / **shelve 移行直前の最終状態** |

削除した7件のうち5件は内容が完全重複だった (`220618`と`220619` が同一 md5、2026-02-21 の4ファイルは2種類の内容しか持たない)。2021年・2023年はもともとファイルが無い。

### 手動バックアップ削除時の注意（重要）

`portfolio_shelve.bak` (**本番ファイル**) と `portfolio_shelve.bak.bak_issue387_...` (ゴミ) は接頭辞を共有する。
削除対象は「**shelve 拡張子の後ろにさらに接尾辞が付くもの**」に限る:

```bash
# 安全なパターン (.bak_ / .bak.bak_ / .before_ が拡張子の後ろに来るものだけにマッチ)
find "$KS_DATA_DIR/stock_data" -maxdepth 1 -type f \( -name "*.bak_*" -o -name "*.bak.bak_*" -o -name "*.before_*" \)
```

実行前に、本番DB (`portfolio_shelve.{dat,dir,bak,lock}` / `research_shelve.*` / `stocks_shelve.*`) と
日次バックアップ (`*_260830.dat` 等) が候補リストに含まれないことを必ず個別検証すること。

### 削除してはいけないもの

| パス | 理由 |
|---|---|
| `stocks_shelve.*` / `research_shelve.*` / `portfolio_shelve.*` / `market_db_shelve.*` | 本番DB |
| `research_shelve_YYMMDD.*` / `portfolio_shelve_YYMMDD.*` | **日次自動バックアップ。`make_stock_db.py` の `BACKUP_GENERATIONS = 14` でローテーション済みなので手動削除不要** |
| `disclosure/cache/` | 現役。180日超は0件 |
| `yahoo/price/` | 株価キャッシュ（再取得コスト大） |

### 実行後の確認結果

- 本番DB 全件読める: portfolio 4,148件 / stocks 3,185件 / research 895件
- 日次バックアップ 14世代維持
- 日次バッチ (`shintakane_cron.sh`) 全成功・例外0件
- キャッシュ再取得後も件数が増えず → 削除したのは死蔵分のみと確認

※ 削除直後の1回だけバッチ実行が 40秒 → 3分46秒 に伸びる（キャッシュ再取得のため）。2回目以降は元に戻る。

### 残っている削減余地（未実施）

- `shintakane_result_data/` 126MB (22件のCSV、自動ローテーションなし)
- `code_rank_data/` 34MB、`today_stocks/` 17MB
- `stocks_shelve.dat` 81MB — `python make_stock_db.py compact` で断片化解消できる可能性 (issue #194)

---

## 2026-03-14 精査分（記録）

当時の全体サイズ: 2.3GB。**この回は精査のみで削除は未実行**だが、項目3〜6 は
2026-08-30 時点で既に解消されていた（別途対応済み）。

| # | 対象 | 当時の見積 | 2026-08-30 時点 |
|---|---|---|---|
| 1 | `stock_data/stocks_pickle_back/` | 50MB | 225MB に増加していた → 年1個方針で整理 |
| 2 | `stock_data/stocks.pickle` | 13MB | 解消済み |
| 3 | `market_data/theme_rank_*.html` | 32MB (747件) | 解消済み (`theme_rank/` に整理) |
| 4 | `market_data/market_db_py2.pickle` | 微小 | 解消済み |
| 5 | `market_data/market_db.pickle` ほか | 微小 | 解消済み |
| 6 | `code_rank_data/code_rank_*.csv` | 29MB (15件) | 解消済み |
| 7 | `stock_data/kabutan/` の price 以外 | 374MB | 180日超のみ削除する方針に変更（下記） |

**方針変更点**: 当時は `kabutan/price/` を「再取得コスト大」として削除非推奨としていたが、
実際には 10,580件中 10,387件 (98%) が90日以上未アクセスだった。
**180日超に限定すれば現役銘柄の working set は保持される**ため、2026-08-30 は price も削除対象に含めた。
