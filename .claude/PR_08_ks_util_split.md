# PR #8: ks_util.py の分割と依存関係整理

## 概要
ゴッドオブジェクト化している ks_util.py を機能別モジュールに分割し、依存関係を整理する。

## 背景・課題

- ks_util.py が肥大化（HTTP、ログ、ファイルIO、日付処理等）
- 各モジュールが多数の関数に依存
- モジュール間の責任範囲が不明確

## 実装予定

### 分割後の構造
```
scripts/
├── utils/
│   ├── http_client.py      # HTTP関連
│   ├── cache_manager.py    # キャッシュ
│   ├── logger.py           # ログ
│   ├── date_utils.py       # 日付処理
│   └── file_utils.py       # ファイルIO
```

### 変更ファイル
- `scripts/ks_util.py` → 分割
- 全モジュール（import文の修正）

## 工数見積もり
3日
