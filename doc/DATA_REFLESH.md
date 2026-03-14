# data/ ディレクトリ精査結果（2026-03-14）

全体サイズ: **2.3GB**

## 削除候補（合計 約460MB削減可能）

### 1. `data/stock_data/stocks_pickle_back/` — 50MB
shelve移行時のpickleバックアップ。shelve DB（1.2GB）が本番稼働中なので不要。0バイトのファイルも多い。

### 2. `data/stock_data/stocks.pickle` — 13MB
レガシーpickle。shelveに移行済み。`make_stock_db.py` で参照パスが残っているが、`USE_SHELVE = True` なので実際には使われていない。

### 3. `data/market_data/theme_rank_*.html` — 32MB（747ファイル）
2023年〜現在まで毎日蓄積されたテーマランクHTMLキャッシュ。コードでは `theme_rank.html`（最新1件）と直前1日分しか参照していない。日付付きファイルは全て不要。

### 4. `data/market_data/market_db_py2.pickle` — 微小
Python 2時代のレガシー。

### 5. `data/market_data/market_db.pickle` + `*_backup_before_shelve_*` — 微小
shelve移行前のバックアップ。

### 6. `data/code_rank_data/code_rank_*.csv`（日付付き15ファイル） — 29MB
過去のランキングスナップショット。コードから参照されていない。Google Driveにもアップロードされているはず。

### 7. `data/stock_data/kabutan/` 直下のHTML（price以外） — 約374MB（約9,650ファイル）
株探銘柄ページHTMLキャッシュ。`rironkabuka.get_kabutan_base_html()` 等で取得されるHTTPキャッシュ。次回実行時に再取得される。

## 削除しない方がよいもの

| パス | サイズ | 理由 |
|---|---|---|
| `stocks_shelve.*` | 1.2GB | メインDB（必須） |
| `kabutan/price/` | 300MB | 株価キャッシュ（再取得コスト大） |
| `yahoo/price/` | 259MB | 株価キャッシュ（再取得コスト大） |
| `market_db_shelve.*` | 32MB | 市場DB（必須） |
