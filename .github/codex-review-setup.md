# Codex PR review setup

このリポジトリでは、GitHub Actions から `@codex review` コメントを自動投稿する方式は使いません。
その方式は OpenAI 公式の自動レビュー設定ではなく、リアクションだけ付いてレビュー本文が作成されない原因になります。

Codex の PR レビューは、OpenAI / ChatGPT 側で GitHub 連携を有効化して設定します。

## 公式設定の前提

- ChatGPT の Codex が使えるプランであること
- ChatGPT と GitHub アカウントを連携済みであること
- 対象リポジトリへのアクセスを Codex に許可していること

## 設定場所

- ChatGPT / Codex の GitHub 連携画面で対象リポジトリを有効化する
- 必要に応じて自動レビューを有効化する

## 補足

- 手動トリガーの場合は、PR 上で人間が `@codex review` をコメントする
- `github-actions[bot]` が自動投稿するコメントに依存しない
- `pull_request` workflow を追加しなくても、公式の GitHub 連携が有効なら Codex は GitHub 上でレビューできる

## 参考

- OpenAI Help: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan

