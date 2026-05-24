# Playwright / スクリーンショット保存ルール

Playwright MCP (`mcp__playwright__browser_*`) や `screencapture` 等でスクリーンショット・検証画像を保存する際のルール。

## 保存先

**必ず `.playwright-mcp/` 配下に保存**する。プロジェクトルートに直接 PNG を置かない。

### Playwright MCP の場合

`browser_take_screenshot` の `filename` 引数にディレクトリ込みで指定:

```
filename: .playwright-mcp/issue123-foo.png
```

`filename` を相対パス (例: `issue123-foo.png`) だけで渡すとプロジェクトルートに保存される実装があるため、必ず `.playwright-mcp/` を前置する。

### Bash 経由 (screencapture / curl 等) の場合

同様に出力先を `.playwright-mcp/` に指定:

```bash
screencapture -i .playwright-mcp/debug-screen.png
```

## 命名規則

- issue 番号があれば `issueXXX-内容.png` 形式 (例: `issue269-modal-qty-row.png`)
- 比較用は `-v2`, `-v3`, `-fixed`, `-after` などの suffix
- ad-hoc な検証は内容を短く表す名前 (例: `market-color-refresh.png`)

## 既存ファイル

`.playwright-mcp/` 自体は `.gitignore` 済み。保険として `/*.png` (ルート直下 PNG) も `.gitignore` 済みだが、本ルールに従えばそちらは発動しない。
