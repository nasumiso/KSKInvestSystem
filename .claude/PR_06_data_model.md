# PR #6: データモデルの明確化

## 概要
Pydantic等でデータモデルを定義し、型制約・バリデーションを導入する。

## 背景・課題

- stocks dict に多種のキーが混在
- 型ヒントが部分的
- スクレイピングデータの妥当性チェック不足
- 株価が負数、出来高がNaN等の異常データへの対処が不明確

## 実装予定

### データモデル定義
```python
from pydantic import BaseModel, validator

class StockPrice(BaseModel):
    code_s: str
    price: int
    volume: float

    @validator('price')
    def price_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError('price must be positive')
        return v
```

### 新規ファイル
- `scripts/models/stock.py`
- `scripts/models/price.py`
- `scripts/models/earnings.py`

## 工数見積もり
5日
