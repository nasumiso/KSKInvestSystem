# 実装プラン作成ルール

新機能追加や既存機能の挙動変更を伴うプランをユーザーに提示する前に、`codex` でレビューし、**指摘がなくなるまで修正→レビューのサイクルを繰り返すこと。**

プロンプトには必ず次の指示を含める: *「些細な指摘は不要。重大な問題のみ指摘してください。」*

## スキップ可能なケース

軽微な修正のみの場合はレビュー不要:

- 定数値の変更 (LOG_DAY, タイムアウト値など)
- バグ修正 (既存仕様通りに動作させる修正)
- ログ出力の追加・調整
- 既存機能の挙動を変えないリファクタリング
- コメント・docstring の修正

判断に迷う場合はレビューを通す。

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
