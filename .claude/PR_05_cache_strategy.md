# PR #5: キャッシュ戦略の一元化

## 概要
分散しているキャッシュロジックを一元化し、TTL・無効化ポリシーを統一管理する。

## 背景・課題

- UPD_*定数による制御が各モジュールに分散
- キャッシュ有効期限ロジックが関数ごとにハードコード
- ファイルキャッシュとメモリキャッシュの整合性管理が不明確

## 実装予定

### キャッシュマネージャー
```python
@cache_with_ttl(days=7, invalidate_on_earnings=True)
def get_stock_master_data(code_s):
    ...
```

### 新規ファイル
- `scripts/cache_manager.py`

### 変更ファイル
- `scripts/ks_util.py`
- `scripts/price.py`

## 工数見積もり
3日
