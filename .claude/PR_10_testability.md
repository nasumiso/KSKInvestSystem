# PR #10: テスタビリティの向上

## 概要
リポジトリパターン導入とHTTPクライアントの抽象化により、ユニットテストを容易にする。

## 背景・課題

- HTTPリクエストが実外部サービスに依存（モック化困難）
- ユニットテストが test_functional.py のみ
- 副作用を持つ関数が多い（DBへの直接書き込み）

## 実装予定

### リポジトリパターン
```python
class StockRepository:
    def get(self, code_s):
        ...

    def save(self, stock):
        ...

# テスト時はモックに差し替え可能
```

### HTTPクライアント抽象化
```python
# requests_mock, vcr.py の活用
```

### 新規ファイル
- `scripts/repositories/stock_repository.py`
- `tests/test_stock_repository.py`
- `tests/fixtures/http_responses/`

## 工数見積もり
1週間
