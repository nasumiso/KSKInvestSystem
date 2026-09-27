# #465: 銘柄評価台帳の正本を JSON に移す (データレイヤー + 移行)

## Background

ChatGPT との個別銘柄分析の最後に、Google スプレッドシート「投資PJ_銘柄評価台帳」(約48銘柄、採点ルール Ver2.0) を差分更新している。
採点はファンダ40 / 未織込20 / モメンタム20 / Valuation20 = 総合100、別に Confidence (A〜C) と Status (Active/Watch/Archive) を持つ。

役割分担は次のとおり。

- ChatGPT の銘柄スレッド: 研究ノート・分析過程・議論履歴
- 評価台帳: **現在時点の投資判断のスナップショット**

## Problem

1. **更新コストが大きい**: ChatGPT からシートを更新するたびに、ファイル検索 → シート取得 → 構造解析 → 行検索 → 列特定 → セル更新 → 確認 が発生し、時間・トークン・ツール呼び出し回数を消費する。中身は「銘柄 × 属性」の構造化データであり、表計算ソフトを経由する必然性はない
2. **手入力のため値が崩れる**: 現シートに次の実例がある
   - 保有欄に `B` (4970)。「保有欄の誤記を未保有へ修正」(3496) という訂正履歴もある
   - 更新メモ列に日付だけが入っている (6479)
   - Status の右隣の列に `Active` がはみ出している (5991)
   - 総合 (= 4軸の合計) と順位を人手で維持している
3. **前回値の履歴が更新メモの文章頼み**: 「Fund32→38」のように前回値を人が文章で書いている

## Goal

- 評価台帳の正本を `KS_DATA_DIR` 上の JSON 1ファイルに移す
- Python から **1銘柄だけ部分更新**でき、他の項目・他の銘柄を壊さない
- 更新のたびに差分 (前の値 → 新しい値) と理由を自動で履歴に残す
- 既存シートを一度だけ JSON に移行する

このIssueはデータレイヤーだけを扱う。ChatGPT から書き込めるようにするのは #466 (MCP)。
**このIssueだけではまだ ChatGPT 側の更新コストは下がらない**ので、本Issueと #466 は続けて実施する前提。

## Non-goals

- MCP Tool の実装 (#466)
- スプレッドシートへの表示用同期 (#467)
- WebApp の画面追加
- research_shelve / code_rank との統合 (#456 の方針どおり統合しない)
- 分析本文の保存 (ChatGPT スレッドに残す)
- Google Drive API を使った同期 (後述のとおり不要)
- JSON とシートの双方向同期

## Proposed Design

### 保存場所と Drive 同期

`KS_DATA_DIR` (`~/Ext/GoogleDrive/shintakane_data`) は Google Drive のミラー同期フォルダ。
ここにファイルを置くだけで Drive へ同期されるため、**Drive API のコードは書かない**。
ir_docs の PDF と同じく、ChatGPT の Drive コネクタからファイル名で検索して読める (Mac が止まっていても読める)。

```
{KS_DATA_DIR}/stock_ratings/
├── stock_ratings.json            # 正本 (現在の判断状態)
└── stock_ratings_history.jsonl   # 変更履歴 (追記のみ)
```

**書き込みは Shintakane (Python) からのみ**行う。スマホの Drive アプリ等で JSON を直接編集しない。
スマホからの更新は ChatGPT → MCP (#466) の経路で行う。

### 保存形式の比較

| 観点 | JSON (1ファイル) | JSONL | research_shelve に同居 | SQLite |
|---|---|---|---|---|
| 1銘柄の部分更新 | 全体を読み直して書き戻す。100銘柄・数百KBなら問題なし | 行の置換が必要で、結局全体を書き直す | API はある | 容易 |
| Python での読み書き | `json` だけで済む | 同左 | 既存 API | 新規導入 |
| 差分の見やすさ | 1項目1行で見やすい | 長い日本語が1行に詰まり差分が読めない | バイナリ | バイナリ |
| LLM からの部分更新 | Python API 経由なので形式は無関係 | 同左 | 同左 | 同左 |
| Drive/スマホから読む | そのまま読める | 読める | **読めない** | 読めない |
| 破損時の復旧 | 原子的な置き換え + 履歴 + Drive の版履歴 | 最終行が壊れても残りは読める | compact 時の破損実績あり | WAL |
| 将来 DB へ移行 | code_s → dict の形がそのまま1行になる | 同左 | — | — |

**推奨: 現在値は JSON 1ファイル、履歴は JSONL (追記のみ)**。

- 現在値は「読み直して全体を書き戻す」ので、追記向けの JSONL の利点が効かない。一方で JSON は整形すれば Drive 上でも人が読める
- 履歴は追記しかしないので JSONL が向いている。途中で壊れても最終行だけ捨てればよい
- データは `KS_DATA_DIR` にあり Git 管理外。「Git 差分の見やすさ」は判断材料にならない
- research_shelve に同居させない理由: Drive・スマホから読めない、#456 で「台帳は code_rank とは別物として扱う」と決めている、MCP サーバーのプロセスから shelve へ書き込む経路を新たに増やしたくない

### モジュール

`scripts/stock_ratings.py` を新規作成。research_shelve と同じ書き方にする (dict + 検証関数。Pydantic は導入しない)。

```python
get_rating(code_s) -> dict | None      # total を足した dict を返す
list_ratings(status=None) -> list[dict]
update_rating(code_s, fields: dict, reason: str, source: str) -> dict   # 部分更新。未登録なら新規作成
```

CLI (research_shelve の show/list と揃える):

```bash
python stock_ratings.py show 3697
python stock_ratings.py list [--status Active]
python stock_ratings.py set 3697 --fund 36 --mispricing 17 --reason "2Q決算反映"
python stock_ratings.py migrate --csv <シートから書き出したCSV>
```

### 更新手順 (部分更新・原子的な書き込み)

```
flock を取る (research_shelve の _flock と同じ方式。書き込むのは MCP サーバーと CLI)
 → JSON を読む
 → 渡された項目だけ上書きする (scores は項目単位でマージ。渡されなかった項目は変えない)
 → 全体を検証する (失敗したら何も書かずに例外を出す)
 → 履歴に1行追記する (実際に値が変わった項目だけ)
 → 同じディレクトリの一時ファイルに書く → fsync → os.replace
 → flock を解放する
```

- 履歴を先に書く。JSON の置き換えの直前で落ちても、「履歴にはあるが反映されていない」状態になるだけで、変更が履歴から漏れることはない。2ファイルをまたぐジャーナルは作らない (対象外とする理由: 書き込みは1日数回・単一ユーザーで、数ミリ秒の間に落ちる確率は極めて低い。履歴から現在値を再構築しないため、起きても余分な履歴が1行残るだけで、その行の新しい値と現在値の食い違いとして目で見て分かる)
- 一時ファイルから `os.replace` で置き換える方式は `ir_docs._write_json` / `ks_util.file_write` と同じ
- 文字列項目を空にしたいときは `""` を渡す。渡されなかった項目は変更しない (「値なし」と「消す」を区別する)
- 未知のフィールドはエラーにする (LLM の打ち間違いで項目が黙って捨てられるのを防ぐ)
- `updated_at` は書き込み時に自動で入れる

### 検証ルール

- `code_s`: research_shelve の `CODE_S_PATTERN` を再利用
- `scores.fund` 0〜40、それ以外は 0〜20 の整数
- `confidence` ∈ {A, B, C}、`status` ∈ {Active, Watch, Archive}
- 新規作成時は `name` と4軸すべて、`confidence`、`status` を必須にする
- `total` は保存しない。読み出し時に4軸を合計して返す

## Data Schema

`stock_ratings.json`:

```json
{
  "schema_version": 1,
  "rubric_version": "2.0",
  "stocks": {
    "3697": {
      "name": "SHIFT",
      "status": "Active",
      "role": "保有・本命候補",
      "scores": {"fund": 35, "mispricing": 16, "momentum": 16, "valuation": 15},
      "confidence": "A",
      "thesis": "AI駆動開発でQA需要そのものが拡大する中、…",
      "mispricing_note": "",
      "risks": "FY27の255億円は『保守的下限』ではなく…",
      "checkpoints": "10月通期決算でFY27調整後営業利益255〜300億円レンジの…",
      "note": "四季報26.9号反映。FY26予想は…",
      "updated_at": "2026-09-22"
    }
  }
}
```

- キーは `code_s` (文字列)。辞書をコード順に並べ、`ensure_ascii=False, indent=2` で書き出す
- `rubric_version` はファイルに1つだけ持つ。採点ルールを変えるときにここを上げる
- `mispricing_note` / `risks` / `checkpoints` は**文字列**にする (決定事項を参照)

`stock_ratings_history.jsonl` (1行1更新):

```json
{"at": "2026-09-23T14:05:00+09:00", "code_s": "3697", "source": "mcp", "reason": "2Q決算反映", "changes": {"scores.fund": [35, 36], "scores.mispricing": [16, 17]}}
```

`changes` には `[前の値, 新しい値]` を入れる。これで「前回から評価がどう変わったか」が分かる。イベントソーシングはしない (履歴から現在値を組み立て直す仕組みは作らない)。

### 列の対応 (移行)

| シートの列 | JSON | 扱い |
|---|---|---|
| 順位 | — | **廃止**。総合点から導出できる |
| コード | キー | そのまま |
| 銘柄 | `name` | そのまま |
| ファンダ / 未織込 / モメンタム / Valuation | `scores.fund / mispricing / momentum / valuation` | 名前を英語化 |
| 総合 | — | **廃止**。読み出し時に計算。移行時に合計と一致しない行は報告する |
| Confidence | `confidence` | そのまま |
| 保有 | — | **廃止**。portfolio_shelve から導出する (誤記の原因だったため) |
| 役割 | `role` | そのまま |
| 投資仮説 | `thesis` | そのまま |
| 主要リスク | `risks` | 名前変更。反証条件の役割を担う |
| 次の格上げ/確認条件 | `checkpoints` | 名前変更 |
| 最終レビュー | `updated_at` | 名前変更 |
| 更新メモ | `note` + 履歴 | 最新の1件を `note` に移す。今後は更新時の理由が履歴に残る |
| Status | `status` | そのまま |
| (新規) | `mispricing_note` | 未織込と見ている点。現状は更新メモ・投資仮説の文章に埋まっている。移行時は空にし、次回レビューで埋める |

2枚目のシート (採点ルール・レンジ定義) は移行しない。#466 で ChatGPT が参照できる形にする。

## Migration

1. シート1枚目を CSV で書き出す (ファイル → ダウンロード → CSV)。一度きりなので Sheets API 読み取りのコードは書かない
2. `python stock_ratings.py migrate --csv <path>` を実行する
   - JSON が既にあれば中断する (上書きしない)
   - 全行を検証してから一括で書く。検証エラーが1行でもあれば何も書かずに中断し、エラーを一覧で出す。CSV を直して再実行する
   - 検証は通るが怪しい値は警告として一覧に出す (総合点と4軸合計の不一致、更新メモが日付だけの 6479 など)。4970 の保有欄 `B` は保有列ごと廃止するので影響なし、5991 のはみ出し列は読まない
   - 移行した全銘柄を `source: "migration"` として履歴に記録する
3. 旧シートは削除しない。1枚目の先頭に「2026-xx-xx 以降の正本は stock_ratings.json」と書いて凍結する

## Acceptance Criteria

- [ ] migrate 実行後、シートの全銘柄が JSON に入り、各銘柄の4軸の値と総合点がシートと一致する (一致しない行は警告に出ている)
- [ ] `set 3697 --fund 36` の後、3697 の他の項目と他の銘柄の内容が変わっていない
- [ ] 検証エラー (例: `--fund 41`) のとき、JSON のバイト列が変わらず、履歴にも追記されない
- [ ] 更新ごとに、値が変わった項目だけが `[前の値, 新しい値]` の形で履歴に1行追記される
- [ ] 実機の `KS_DATA_DIR` で3回続けて更新した後、Drive 上に `stock_ratings.json` が1つだけあり (競合コピーができていない)、ChatGPT の Drive コネクタからファイル名で検索して読める

## Test Plan

`tests/test_stock_ratings.py` (5本以内。parametrize でまとめる):

1. 部分更新で指定しなかった項目・他の銘柄が変わらない (scores の一部だけ / 文字列だけ / `""` で消す)
2. 検証エラーでファイルと履歴が変わらない (範囲外 / 未知のフィールド / 不正なコード / 新規作成時の必須項目不足)
3. 履歴に変わった項目だけが記録される (同じ値での更新では追記しない)
4. migrate: 検証エラーの行が1つでもあれば何も書かない。怪しい値 (総合の不一致など) は警告に出たうえで移行される。JSON が既にあれば中断する

`tmp_path` 上に JSON を作って検証する。本番の `KS_DATA_DIR` には触れない。

## 決定事項

- **凍結方針の例外として進める** (2026-09-23)。日々の手動ルーチン (銘柄分析後の台帳更新) の改善であり、自己FB運用フェーズの運用コストを直接下げるため
- **仮説まわりの項目は文字列にする**。現シートは散文で、移行時に配列へ機械的に分けると内容が崩れる。更新時は項目全体を置き換えるので、配列にしても部分更新の粒度は変わらない。配列が必要になったら `schema_version` を上げて変換する
- **`mispricing_note` は移行時は空にする**。現シートに専用列はなく、未織込の根拠は「更新メモ」や「投資仮説」の文章の中に書かれている (例: 4461「未織込の中心はAI需要そのものではなく…」、5803「未織込17はAI光通信テーマ自体ではなく…」)。機械的に抜き出すと崩れるため、各銘柄の次回レビュー時に ChatGPT が埋める

## Future Work

- #466: MCP からの取得・更新
- #467: JSON → スプレッドシートへの表示用出力
- 保有状況 (portfolio_shelve) と組み合わせた表示 (WebApp の研究ページ等)
- 正本を Shintakane 内部 DB へ移す (#450 SQLite 化と合わせて検討)。`stock_ratings.py` の API を保てば、呼び出し側は変更不要
- #448 (AI振り返り分析) で、決済エピソードと評価履歴を照合する

---

# #466: 銘柄評価台帳を MCP から取得・更新できるようにする

**前提: #465**

## Goal

ChatGPT が銘柄分析の最後に、ツール1回で評価を差分更新できるようにする。これで元の課題 (シート更新のコスト) が解消する。

## Proposed Design

既存の `scripts/mcp/shikiho_server.py` にツールを追加する。トンネルのプロファイルは `shintakane-shikiho` の1つだけなので、サーバーを分けるとトンネルの設定と常駐プロセスが倍になる。
サーバーの instructions と README にある「読み取り専用」の記述は改める。

| Tool | 内容 |
|---|---|
| `list_stock_ratings(status=None)` | 一覧。長文は返さない (code_s, name, status, scores, total, confidence, role, updated_at) |
| `get_stock_rating(code_s, history=3)` | 1銘柄の全項目 + 直近の変更履歴 |
| `update_stock_rating(code_s, reason, fund=None, mispricing=None, …)` | 部分更新 (未登録なら新規作成)。変更前後の差分を返す |

- 追加と更新は1つのツールにまとめる (LLM がどちらを呼ぶか迷わないように)
- 検証エラーは例外ではなく、どの項目がなぜ不正かを返す (ChatGPT がその場で直して再実行できるように)
- 採点ルール (各軸のレンジ定義・原則) は `update_stock_rating` の説明文に入れる。シート2枚目の内容を移す
- 書き込めるのは台帳だけ。他の DB への書き込みツールは作らない

## Acceptance Criteria

- [ ] ChatGPT から「3697 の Fund を 36 に」と頼むと、ツール呼び出し1回で更新が終わり、他の項目は変わらない
- [ ] Mac が止まっていて MCP に届かない場合でも、Drive コネクタで JSON を読んで現在の評価を答えられる (読み取りのみ)

## Test Plan

`tests/test_shikiho_server.py` にツール単位のテストを1〜2本追加する (正常な更新 / 検証エラーの返却)。

---

# #467: 銘柄評価台帳をスプレッドシートへ表示用に出力する

**前提: #465**

## Goal

JSON からシートへの一方向の出力。人がスマホや PC で一覧を眺めるための表示用。

## Proposed Design

- `stock_ratings.py export_sheet` で CSV を作り、既存の `googledrive.upload_csv_async` (Sheets API でセルの値だけを更新し、書式は保持する) で出力する。code_rank と同じ仕組みで、`FILE_DICT` / `SHEETS_CONFIG` に1件追加する
- 出力先は既存スプレッドシートの**新しいタブ**にする (凍結した旧タブは上書きしない)。`upload_csv_via_sheets` はタブを作らず、無いと `ValueError` で止まるので、タブは最初に手作業で作る (一度きりなのでタブ作成のコードは書かない)
- 列: 総合点順の順位 / コード / 銘柄 / 4軸 / 総合 / Confidence / 保有 (portfolio_shelve から導出) / 役割 / 仮説 / 未織込 / リスク / 確認条件 / 更新日 / メモ / Status
- 実行タイミング: `update_rating` のたびに非同期で出力するか、日次バッチで出力するかは実装時に決める
- シート側での編集は反映しない (次の出力で上書きされる旨をシートの先頭行に明記する)
