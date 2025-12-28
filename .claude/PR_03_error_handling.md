# PR #3: エラーハンドリングとリトライ戦略の強化

## 概要
スクレイピング処理のエラーハンドリングを統一し、リトライ・フォールバック機能を実装する。

## 背景・課題

- スクレイピング失敗時の処理が統一されていない
- HTMLフォーマット変更への対応が各パーサー内にハードコード
- 429エラー等への動的対応がない

## 実装予定

### 1. リトライライブラリ導入
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def fetch_stock_data(code):
    ...
```

### 2. スクレイパー基底クラス
```python
class BaseScraper:
    def fetch_with_fallback(self, url):
        # リトライ、バックオフ、フォールバック
        pass
```

### 3. Circuit Breaker パターン
サービス障害時の連鎖防止

### 新規ファイル
- `scripts/scrapers/base.py` - スクレイパー基底クラス

### 変更ファイル
- `scripts/price.py`
- `scripts/master.py`
- `scripts/gyoseki.py`
- `scripts/shihyou.py`

## 工数見積もり
2日
