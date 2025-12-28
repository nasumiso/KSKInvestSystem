# PR #7: ソース抽象化とパーサの整理

## 概要
Yahoo/Kabutanのスクレイピング処理を「ソースアダプタ」に集約し、バージョニング・回帰テストを実装する。

## 背景・課題

- HTML解析が各モジュールに散在
- フォーマット変更の影響が広範囲
- 回帰テストが不足（data/cache_data の HTML を fixtures にしていない）

## 実装予定

### ソースアダプタパターン
```python
class YahooFinanceAdapter:
    def fetch_price(self, code):
        ...

    def parse_price(self, html):
        # バージョニング対応
        ...
```

### 新規ファイル
- `scripts/adapters/yahoo.py`
- `scripts/adapters/kabutan.py`
- `tests/fixtures/` - HTMLキャッシュをfixtures化

### 変更ファイル
- `scripts/price.py`
- `scripts/master.py`
- `scripts/gyoseki.py`

## 工数見積もり
1週間
