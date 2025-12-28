# PR #9: 認証とSecrets管理の改善

## 概要
Google Drive認証を最新ライブラリに移行し、Secrets管理を環境変数化する。

## 背景・課題

- 古いライブラリ（oauth2client）を使用
- Secrets管理が不明瞭
- テストが困難

## 実装予定

### google-auth への移行
```python
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# 環境変数からCredentials取得
```

### 新規ファイル
- `.env.example` - 環境変数のサンプル
- `docs/SECRETS_SETUP.md` - 設定手順

### 変更ファイル
- `scripts/googledrive.py`
- `requirements.txt`

## 工数見積もり
2日
