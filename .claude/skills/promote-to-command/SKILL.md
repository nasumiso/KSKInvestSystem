---
name: promote-to-command
description: 開発中に繰り返し叩くワンショット処理を、再利用可能なCLIサブコマンド/関数/開発スクリプトに昇格させる。「この処理よく実行するな」と気づいたとき、または昇格を明示指示されたときに使用
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Edit, Write, Bash
---

# 繰り返し運用処理の昇格 (promote-to-command)

開発中に都度ワンショットで書く処理 (`python -c "..."` や複数行 Bash) を、
**再利用可能な形 (CLIサブコマンド / 関数 / 開発スクリプト) に昇格**させる。

目的は「次に同じことをするとき1コマンドで済む」状態にすること。ただし
**何でも昇格させるのではない** — less is more。下の判断基準で取捨する。

## いつ昇格を提案・実行するか

### 自動検知 (提案のトリガー)
次のすべてを満たしたら「これ、昇格しますか?」と1行で提案する:

1. 同一セッション内で**同じ系統の再現可能な処理を2回以上**実行した
2. それが**スクレイピング/DB更新そのものではない**か、または引数を変えれば
   繰り返し使える定型処理 (例: 表示用HTML再生成、特定銘柄の単体取得・確認)
3. 一度きりのデバッグ (print差し込み、その場限りの調査) ではない

提案は提案にとどめ、押し付けない。ユーザーが承認したら本スキルの手順で実行する。

### 明示トリガー
ユーザーが「この処理を昇格して」「CLI化して」等と指示したとき。

## 昇格すべきか判断する (取捨基準)

| 昇格する | 昇格しない (罠) |
|---|---|
| 表示用ファイルの再生成 (例: market_data.html) | 一度きりのデータ移行・クリーンアップ (migrate系で既に対応済) |
| 特定銘柄の単体取得・確認 (業績/指標/理論株価/価格) | スコア計算ロジック (`calc_*`) → **単体テストでカバーすべき** |
| パーサ単体の動作確認 (HTML→構造化) | その場限りのアドホック調査 |
| 引数を変えて繰り返す定型処理 | 投機的な「将来使うかも」(YAGNI) |

**迷ったら「シニアが overcomplicated と言うか?」を自問** (CLAUDE.md 行動原則2)。
`calc_*` 系を CLI 化したくなったら、まずテストで足りないか考える。

## 昇格先の標準形を選ぶ

処理の性質で昇格先を決める:

1. **既存モジュールの機能** → そのモジュールの `.py` に**CLIサブコマンド**を追加。
   - 例: `make_market_db.py html` (DB更新せず HTML だけ再生成)。
   - `if __name__ == "__main__"` の `main()` で `sys.argv` を分岐 (既存の
     make_stock_db.py / shintakane.py のスタイルに合わせる。argparse は
     既存ファイルが使っていればそれに合わせる)。
   - 既存の公開関数を**再利用**して呼ぶだけにする (ロジックを二重に書かない)。
2. **横断的な開発補助** (複数モジュールをまたぐ・検証専用) → 専用スクリプト。
   - 既存の単発スクリプト (`analyze_market.py` 等) と同じ scripts/ 直下、または
     開発補助が増えるなら `scripts/dev/` を新設して集約。
3. **純粋なデータ変換・ヘルパー** → 既存モジュールに**関数**として追加。

## 昇格の実行手順

1. **既存CLIを必ず先に確認** — 既に同等コマンドが無いか grep する
   (`grep -rn "__main__\|sys.argv\|add_parser" scripts/`)。
   shintakane は既存CLIが充実しているので**重複を作らない**。
   doc/COMMANDS.md も確認。
2. 昇格先の標準形を上記基準で選ぶ。複数解釈あればユーザーに確認。
3. **既存の公開関数を再利用**して実装。新規ロジックは最小に。
4. テスト方針 (.claude/rules/testing.md):
   - CLI引数分岐の追加だけ (既存関数を呼ぶ) なら、その関数が既存テストで
     カバーされていればテスト追加は不要。
   - 新規ロジックを足したらテストを書く (parametrize集約・1PR5本以下)。
   - testing.md のモジュール→テストのマッピングに従い回帰確認。
5. **doc/COMMANDS.md に1行追記** (CLIサブコマンドを足した場合)。使い方と
   「何をする/しない (DB更新の有無等)」を明記。
6. 反映方法 (commit/PR or main直push) はユーザーに確認 (軽微なら直push可)。

## 想定候補カタログ (棚卸し済み)

開発で繰り返し叩きそうだが現状CLIに無い、昇格価値の高い候補
(2026-06 時点の棚卸し)。これらに着手するときは本スキルの手順で。

- **特定銘柄の業績/指標/理論株価/価格だけ取得・確認**
  - 現状: `gyoseki.get_gyoseki_data()` 等を `python -c` で呼ぶしかない。
  - 案: 各 `.py <code>` で単体取得＋pretty print、または make_stock_db.py に
    `--only <table>` 等のオプション。
- **パーサ単体の動作確認** (HTMLフォーマット変更対応時)
  - 現状: `parse_master_html_kabutan` 等を手で呼ぶ。
  - 案: `<module>.py --parse <code>` でパース結果を表示。
    html-scraping.md の検証フロー (パーサ修正→単体確認) と直結。
- **市場DBの一部だけ再計算** (テーマランク等、必要が出たら)
- **portfolio_shelve の特定銘柄確認** (research_shelve.py show 相当が未CLI化)

既にCLI化済みで**やらなくてよい**もの (重複回避):
make_market_db.py html / make_stock_db.py refresh_stock|refresh_price|refresh_pts /
research_shelve.py show|list / defrag_shelve.py / 各 migrate_*。

## 参照
- 行動原則: CLAUDE.md「行動原則 (Karpathy 4原則)」「コーディング規約」
- テスト: .claude/rules/testing.md
- 全コマンド: doc/COMMANDS.md
- パーサ検証: .claude/rules/html-scraping.md
