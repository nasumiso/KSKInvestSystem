# PR #4: CI/CDパイプラインの導入

## 概要
GitHub Actionsで自動テスト・リント・フォーマットチェックを実施し、コード品質を担保する。

## 背景・課題

- 自動化テストが無く、変更の安全性が担保されていない
- コードフォーマット・リントの統一なし
- デプロイ前の品質チェックが手動

## 実装予定

### .github/workflows/ci.yml
```yaml
name: CI

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run black
        run: black --check scripts/
      - name: Run flake8
        run: flake8 scripts/
      - name: Run pytest
        run: pytest tests/
```

### 新規ファイル
- `.github/workflows/ci.yml`
- `.github/pull_request_template.md`

## メリット
- PR時に自動チェック
- コード品質の統一
- バグの早期発見

## 工数見積もり
1日
