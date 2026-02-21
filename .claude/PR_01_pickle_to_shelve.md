# PR #1: pickle → shelve への移行

## 概要
データ永続化をpickleからshelveへ移行し、パフォーマンスとデータ安全性を向上させる。

## 背景・課題

現在のpickle実装には以下の問題があります：

1. **全体読み込みの非効率性**
   - 1件更新するだけで全銘柄（数千件）を読み書き必要
   - メモリ使用量が大きい（全データを常時メモリに展開）

2. **データ破損リスク**
   - 書き込み中にプロセスが終了すると全データ破損の可能性

3. **パフォーマンス**
   - `make_stock_db.py`で1銘柄更新するたびに全DBを読み書き

## 実装予定

### 新規ファイル
- `scripts/db_shelve.py` - shelve用抽象化層
- `scripts/migrate_pickle_to_shelve.py` - 移行スクリプト

### 変更ファイル
- `scripts/make_stock_db.py`
- `scripts/make_market_db.py`
- `scripts/kessan.py`
- `scripts/make_sector_data.py`

## パフォーマンス改善見込み

| 操作 | pickle | shelve | 改善率 |
|------|--------|--------|--------|
| 1銘柄更新 | 5秒 | 0.01秒 | **500倍** |
| メモリ使用量 | 200MB | 10MB | **20分の1** |
