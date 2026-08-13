---
name: github-review-fix
description: GitHub 上の PR レビューコメント (Codex 自動レビュー等) を取得して修正し、修正コメントと @codex 再レビュー依頼を投稿する。新規コメントが尽きるまで cron で定期確認を繰り返す。「PRのレビュー対応して」「レビューコメント直して」「codexの指摘を直して」と指示されたとき、または PR 作成後にレビューを反映したいときに使用。ローカルの codex exec によるプランレビュー (.claude/rules/codex-plan-review.md) とは別物
allowed-tools: Read, Grep, Glob, Edit, Write, Bash
---

# GitHub PR レビュー対応 (github-review-fix)

**GitHub 上の** PR レビューコメントを **取得 → 検証 → 修正 → セルフレビュー → 投稿** し、
新規コメントが尽きるまで cron ループで繰り返す。

対象は Codex (`chatgpt-codex-connector`) の自動レビューを想定しているが、人間の
レビューコメントも同じ手順で扱える。

> **ローカルの `codex exec` によるプランレビューとは別物。**
> 実装プランを提示する前のレビューは `.claude/rules/codex-plan-review.md` が担当で、
> そちらはプロンプトで粒度を指定できる。本スキルは PR がある状態で、GitHub 上に
> 投稿されたコメントを相手にする。

## 基本方針

**全部に対応しない。大事なことに絞る。**

理由は往復コスト。GitHub 経由のやり取りは1往復ごとに「修正 → テスト → push →
投稿 → 再レビュー待ち (数分)」がかかる。細かい指摘まで律儀に拾うと、本質的でない
変更のために往復が増え、時間を食う。ローカルの `codex exec` なら
`Don't nitpick trivial things. Only point out critical issues.` で絞れるが、
GitHub 上の Codex は粒度を指定できないので、**受け取り側で絞る** (手順4)。

指摘は「実害があるか」で切る。バッジ (P1/P2) は Codex の自己申告なので判断材料の
一つに留める。見送ったものは理由を添えて PR コメントに残す。

## 前提

- `gh` CLI が認証済み
- 対象 PR が OPEN (MERGED でもコメント対応自体は可能だが、その場合は別PRを立てる)

## 手順

### 1. 対象 PR を特定する

引数で PR 番号が渡されていればそれを使う。**渡されていなければ現在のブランチから特定する**:

```bash
gh pr view --json number,title,state,headRefName \
  --jq '"PR #\(.number) [\(.state)] \(.title)\nbranch: \(.headRefName)"'
```

特定した PR 番号とタイトルを**ユーザーに1行で提示してから**作業に入る。
番号の取り違えは後続の全作業を無駄にするため、ここだけは必ず声に出す。

PR が見つからない場合はユーザーに確認する。勝手に番号を推測しない。

### 2. レビューコメントを取得する

**レビュー本文 (`gh pr view --json reviews`) には要約しか入っていない。**
実際の指摘は **reviewThreads (インラインコメント)** にあるので、GraphQL で取る:

```bash
gh api graphql -f query='
{ repository(owner:"OWNER", name:"REPO") { pullRequest(number:NNN) {
  reviewThreads(first:100){ nodes {
    isResolved isOutdated path line
    comments(first:3){ nodes { author{login} createdAt body } }
  } } } } }' \
--jq '.data.repository.pullRequest.reviewThreads.nodes[]
      | select(.isResolved==false and .isOutdated==false)
      | "########## \(.path):\(.line) ##########\n\(.comments.nodes[0].body)"'
```

issue コメント (PR 全体への `@codex review` の返答等) も併せて確認する:

```bash
gh pr view NNN --json comments \
  --jq '.comments[] | "\(.createdAt) | \(.author.login) | \(.body[0:150]|gsub("\n";" "))"'
```

**2周目以降は前回確認時刻で絞る** (`select(.comments.nodes[0].createdAt > "...")`)。
そうしないと対応済みの指摘を何度も読み直すことになる。

#### フィルタの意味

| フラグ | 意味 | 扱い |
|---|---|---|
| `isResolved=true` | 解決済みマーク | スキップ |
| `isOutdated=true` | 指摘行がその後の commit で変わった | **原則スキップ**。ただし指摘の本質が残っている場合はあるので、件数が少なければ中身を見る |
| 両方 false | 現行コードへの生きた指摘 | **対応対象** |

### 3. 指摘ごとに現行コードで裏を取る (最重要)

**コメントを読んだだけで修正に入らない。** レビューは特定コミット時点のもので、
その後の修正で既に解消している場合がある。

実例: 未解決21件のうち現行コードに該当するのは10件、さらにその**9件は既に対応済み**
だったケースがある (PR #410)。裏取りを飛ばしていたら、既にあるコードを重複実装するか、
「対応しました」と嘘の報告をしていた。

各指摘について:

1. **該当箇所を実際に読む** (`sed -n`, `grep`)。指摘が言う問題が現存するか確認
2. **可能なら実データで再現する**。「理屈上ずれる」と「実際にずれている」は別物
3. 既に対応済みなら**修正せず**、その旨を記録して次へ

実データ検証の例 (このリポジトリなら shelve DB を直接読む):

```bash
source .venv/bin/activate && cd scripts && python -c "
import shelve, os
from ks_util import DATA_DIR
d = shelve.open(os.path.join(DATA_DIR,'stock_data','stocks_shelve'), flag='r')
# 指摘された条件が実データで起きているか数える
d.close()
"
```

### 4. 対応方針を決める

#### まず粒度でフィルタする

**GitHub 上の Codex はレビュー粒度を指定できない。** ローカルの `codex exec` なら
プロンプトに `Don't nitpick trivial things. Only point out critical issues.` を
入れて絞れる (`.claude/rules/codex-plan-review.md`) が、PR レビューにその手段はない。

そのため **受け取り側で同じ基準を適用する**。「Codex が挙げた = 直すべき」ではない。
各指摘に対して、まずこう自問する:

> これはローカルレビューで `Only point out critical issues` と指示していたら、
> そもそも出てきた指摘か?

`P1`/`P2` のバッジは Codex の自己申告なので鵜呑みにしない。P2 でも実害があれば
直すし、P1 でも起きえない前提の話なら見送る。判断の軸は**バッジではなく実害**。

| 粒度 | 例 | 既定の扱い |
|---|---|---|
| **critical** | 実データで再現する誤り、ユーザーに嘘を伝える表示、データ破損、セキュリティ | 対応する |
| **nitpick** | 命名の好み、コメントの言い回し、追加の防御的コード、「将来こう変わったら困る」 | 見送る |
| **判断が要る** | 起きうるが稀、直すと既存挙動が変わる、修正コストが効果に見合わない | 相談する |

#### そのうえで対応可否を決める

| 判断 | 基準 |
|---|---|
| **対応する** | 現行コードに問題が実在し、修正コストが妥当。実データで再現できればなお良い |
| **見送る** | nitpick、レアケース、費用対効果が見合わない、既存の意図的な設計 |
| **相談する** | 挙動変更を伴う、影響範囲が読めない、方針が複数ありうる |

見送る場合も**理由を必ず記録**し、後で投稿するコメントに含める。黙って無視しない。
「nitpick と判断した」も立派な理由なので、そう書く。

迷ったらユーザーに聞く。特に **既存の挙動が変わる修正** は勝手に決めない
(例: 「許容期間を本日限定にする」は既存銘柄が表示されなくなる)。

**指摘件数が多いときほどフィルタが効く。** 全部に律儀に対応すると、本質的でない
変更で差分が膨らみレビューの焦点がぼける。critical を先に片付けて投稿し、
nitpick はまとめて「見送り」に列挙する方が PR として読みやすい。

**1往復にまとめる。** 今回のバッチで対応すると決めたものは、まとめて修正して
1回で投稿する。1件直すたびに投稿すると再レビュー待ちの往復がその都度発生し、
時間だけが伸びる。逆に、判断に迷って相談が必要なものがあれば、それを待つ間に
critical だけ先に片付けておく。

### 5. 修正してセルフレビューする

修正後、**指摘がなくなるまで**自分で見直す。最低限:

- **既存慣習との整合**: 新しく書いた書式・命名が、コードベースの既存パターンと揃っているか
  (例: `strftime("%-m/%-d")` を使ったが既存は全て `"%m/%d"` だった → 揃える)
- **移植性・エッジケース**: 環境依存の書式、None/空データ、日付境界
- **指摘の横展開**: 同じ問題が他の箇所にもないか grep する
  (例: 「本日掲載」の誤記を直したら、他に同じ文言が残っていないか)

### 6. テストと動作確認

`.claude/rules/testing.md` のマッピングに従って関連テストを実行。判定ロジックを
変えたなら CI 相当の全件も回す:

```bash
source .venv/bin/activate && python -m pytest tests/ \
  -m "not local_db and not live_html and not integration" -q
```

**UI に出る変更なら実機で見る。** テストが通っても表示が壊れていることはある:

```bash
kill $(lsof -ti:5001) 2>/dev/null; sleep 1
source .venv/bin/activate && cd scripts && nohup python -m webapp.app > /tmp/webapp.log 2>&1 &
sleep 8 && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5001/
# 確認後は必ず kill
```

### 7. コミット & push

修正内容と **なぜそう直したか** をコミットメッセージに書く。実データで再現した
事実があればそれも書く (レビュアーが妥当性を判断できる)。

### 8. 修正コメントと再レビュー依頼を投稿する

**2つに分けて投稿する** (`@codex review` は単独コメントにしないと拾われないことがある):

```bash
gh pr comment NNN --body "$(cat <<'EOF'
## レビュー対応 (コミットハッシュ)

### ✅ 対応: <指摘の要約> (P1/P2)

<何が問題だったか / どう直したか>

```
変更前: ...
変更後: ...
```

<実データで再現した事実があれば書く>

### ⏭️ 見送り: <指摘の要約>

<なぜ見送ったか。nitpick と判断 / レアケース / 費用対効果 / 意図的な設計。
複数あるならまとめて箇条書きにする>

### テスト

- pytest ... → N passed
- <実機確認の内容>
EOF
)"

gh pr comment NNN --body "@codex review"
```

### 9. cron ループで再レビューを待つ

再レビューの応答は数分かかる。**cron で定期確認する** (デフォルト3分間隔):

```
CronCreate:
  cron: "*/3 * * * *"
  recurring: true
  prompt: |
    PR #NNN の新しいレビューコメント（前回確認以降のもの）を確認する。

    コメントがあれば github-review-fix スキルの手順3以降で対応する:
    - レアケース・費用対効果が見合わないものは見送りでよい
    - 迷うものはユーザーに相談する
    - 修正したらセルフレビューし、指摘がなくなるまで修正を繰り返す
    - 修正完了後、修正内容のコメントと @codex review を投稿する

    新しいコメントが無ければ「N回目: 新規コメントなし」とだけ簡潔に報告する。
    新規コメントなしが3回連続に達したら、CronDelete でジョブを削除して完了する
    （ジョブIDは CronList で確認）。
```

登録後、**1回目は待たずに即実行する**。

#### 終了時は必ず最終確認する

**再レビューの反映には遅延がある。** 「N回連続なし」で打ち切った直後に指摘が
現れたことがある (5回連続なしで終了した2分後に2件届いていた)。

そのため `CronDelete` の**直前にもう一度フルで取得**し、時刻フィルタを外した
「未解決 かつ not outdated」の全件を数える。0件でなければループを続行する。

```bash
# 終了判定の最終確認: 時刻で絞らず現存する生きた指摘を数える
gh api graphql -f query='...' \
--jq '[.data.repository.pullRequest.reviewThreads.nodes[]
       | select(.isResolved==false and .isOutdated==false)] | length'
```

終了条件に達したら `CronDelete` でジョブを削除し、対応内容を要約して報告する。

## やらないこと

- **goal (Stop hook) は使わない**。中止したいときに解除できず、
  「不要になった作業を投稿しろ」と要求し続ける事故が起きる。
  cron なら `CronDelete` で普通に止められる
- **勝手にマージしない**。マージはユーザー判断
- **PR 番号を推測しない**。特定できなければ聞く
- **裏取りせずに修正しない**。既に直っている指摘に「対応しました」と報告するのは嘘になる

## 注意

- 投稿は外部に見える取り消しにくい操作。**中止指示が出たら投稿しない**
- 並行セッションが同じリポジトリで作業していることがある。`git status` に
  身に覚えのない変更があれば、コミット前に切り分ける (hunk 単位の `git apply --cached`)
