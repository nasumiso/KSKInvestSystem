# 実装プラン作成ルール

ユーザーにプランを提示する前に、必ず `codex` でレビューし、**指摘がなくなるまで修正→レビューのサイクルを繰り返すこと。**

プロンプトには必ず次の指示を含める: *「些細な指摘は不要。重大な問題のみ指摘してください。」*

## コマンド

### 初回レビュー

※ モデルは `-m` で必ず指定（gpt-5.3-codex 推奨）

```bash
codex exec -m gpt-5.3-codex "Review this plan. Don't nitpick trivial things. Only point out critical issues: {plan_full_path} (ref: {CLAUDE.md full_path})"
```

### 修正後の再レビュー

※ 初回レビューのコンテキストを保持するため `resume --last` が必須

```bash
codex exec resume --last -m gpt-5.3-codex "I've updated the plan, please review again. Don't nitpick trivial things. Only point out critical issues: {plan_full_path} (ref: {CLAUDE.md full_path})"
```
