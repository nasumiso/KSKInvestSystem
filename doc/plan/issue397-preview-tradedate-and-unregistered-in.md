# ポートフォリオCSV取込 差分プレビューの改善 (issue #397 追補)

## 背景

実運用で2点の不足が判明した。

1. 差分プレビューの表に**取引日が無い**。「いつの売買でこの差分が出たのか」が
   画面から分からず、判断の材料が足りない。
2. **未登録銘柄 (例: 6336 石井表記) が 3監 止まりになる**。CSV に出てきた
   = 実際に保有している銘柄なので、保有 (1保) にしたい。

現状の 3監 止まりは意図的な仕様で、根拠は
[issue397-portfolio-csv-import.md](issue397-portfolio-csv-import.md) §5-3b:

- `portfolio_shelve.ALLOWED_TRANSITIONS` が新規登録を `(None, "3監")` のみ
  許可しており、いきなり 1保 のレコードは作れない (技術的制約)
- 「登録」と「1保 への遷移」を2段階に分け、出口ルール (戦略 = trade_idea) の
  宣言を伴う 1保 遷移だけ人に委ねる (設計判断)

この設計判断そのものは維持する。**問題は「未登録行だけプレビューで戦略を
選べない」こと**で、2準/3監 の新規IN候補には戦略 select があるのに、未登録行
には無く、必ず保留キュー経由の2度手間になっていた。

## 方針

### A. 差分プレビューに取引日列を追加

fill 履歴 (`ps.list_fills`) から銘柄ごとの**直近約定日 (`trade_date` の最大値)**
を引いて表示する。新規IN候補なら「いつ買ったか」、売却候補なら「いつ売ったか」に
相当する。

- fill が未取込の銘柄は空欄 (`-`)。fill CSV の取込は別フロー (`import_rakuten_fills.py`
  等) なので、必ず埋まる保証は無い。埋まらないことを異常扱いしない
- **DB アクセスは1回だけ**。`ps.list_fills()` (code_s=None) を1回呼んで
  `{code_s: 最新trade_date}` の dict を作る。銘柄ごとに `list_fills(code_s)` を
  呼ぶと都度 open して全件走査する N×M になる (`_build_diff_preview` の
  docstring に同じ轍の記録あり)
- 実装位置は **webapp の route 側** (`csv_import_preview`)。`stock_name` /
  `current_trade_idea` と同じく「プレビュー表示のための後付け情報」であり、
  `import_portfolio_csv.import_csvs` の差分ロジック (CLI とも共有) は触らない

### B. 未登録行でも戦略を選べるようにし、戦略ありなら 1保 まで進める

`_judge` が返す `"未登録+保有検出 (反映すると監視へ登録)"` の行を、2準/3監 の
新規IN候補と同じく**戦略 select + 振り返りメモ入力を出す行**にする。

`_sync_records` の `status == "未登録"` 分岐を次のように変える:

```
add_to_watch()        # 3監 で登録 (現状どおり。ALLOWED_TRANSITIONS の制約を満たす)
  ├ 戦略あり → update_memo(trade_idea) → transition_status(1保) → update_qty
  │                                                 … 2準/3監 の自動IN と同じ順序
  └ 戦略なし → upsert_pending_in()                  … 現状どおり保留キュー
```

`update_memo` は必須。`add_to_watch()` 直後の memo は空で、`transition_status()`
は戦略を保存しないため、これを省くと「1保だが戦略未設定」のレコードができる。

`(None,"3監")` → `("3監","1保")` の2段遷移なので `ALLOWED_TRANSITIONS` の制約は
満たす。action_log には `add_to_watch` の「初回登録」と、`transition_status` の
**「ステータス変更」(3監→1保)** の2件が残る (`transition_status` が
`action_type="売却"` を使うのは 1保→2準 のときだけで、それ以外は
「ステータス変更」。`portfolio_shelve.py:2521`)。既存の 2準/3監 自動IN 経路と
同じ記録のされ方なので、action_type の追加・`VALID_ACTION_TYPES` の変更は不要。

判定文言も実態に合わせて変える:

| 現在 | 変更後 |
|---|---|
| `未登録+保有検出 (反映すると監視へ登録)` | `未登録+保有検出 (戦略ありで1保へ / 空欄なら監視+保留キューへ)` |

### C. テンプレートの分岐を判定文字列依存から外す

現状 `portfolio_csv_import.html` は `'新規IN候補' in d.judgement` という
**判定文字列の部分一致**で戦略 select の出し分けをしている。B で未登録行も
対象に加わるため、文字列一致を増やすのではなく、`_build_diff_preview` が
返す行に `"needs_trade_idea": True/False` のフラグを持たせ、テンプレートは
それを見る。売却行の `is_out` も同様に `"is_exit"` フラグへ寄せる。

判定文言は人間向けの表示専用にし、分岐条件には使わない。

## 実装

### 1. `scripts/import_portfolio_csv.py`

- `_build_diff_preview`: 各 diff 行に `needs_trade_idea` / `is_exit` を追加
  - `needs_trade_idea`: covered かつ merged_qty > 0 かつ status が
    2準/3監/未登録 (= 反映すると 1保 になりうる行)
  - `is_exit`: status == "1保" かつ covered かつ merged_qty == 0
- `_judge`: 未登録の文言を上記表のとおり変更
- `_sync_records`: `status == "未登録" and merged_qty > 0` の分岐に戦略判定を
  追加。`add_to_watch()` の後、`chosen_trade_idea` があれば
  `update_memo(trade_idea) + transition_status(1保) + update_qty(log_action=False)`、
  無ければ従来どおり `upsert_pending_in`
  - 戦略の解決は 2準/3監 分岐と同じロジック。未登録銘柄は `add_to_watch` 直後の
    レコードなので `memo.trade_idea` は常に空。override の値のみが効く
  - `update_memo` の呼び出しを条件付きにしない (2準/3監 分岐は
    `chosen_trade_idea != existing_trade_idea` で守っているが、未登録は
    existing が常に空なので条件は不要。素直に必ず呼ぶ)
  - `update_memo` は trade_idea マスター未登録値を `ValueError` で弾く。
    override は `_validate_overrides` で検証済みなのでそのまま渡してよい
  - `applied` (反映完了 flash のサマリー) は
    `{"action": "登録+新規IN(自動)", "detail": f"株数{merged_qty} / 戦略「{...}」"}`。
    これは flash 表示用の文字列であって action_log の action_type ではない
    (既存の「新規IN(自動)」と同じ扱い)

### 2. `scripts/webapp/routes/portfolio.py` (`csv_import_preview`)

- `ps.list_fills()` を1回呼び `{code_s: max(trade_date)}` を構築
- 各 diff 行に `d["last_trade_date"]` を設定 (無ければ `""`)
- `current_trade_idea` の取得条件を `'新規IN候補' in d["judgement"]` から
  `d["needs_trade_idea"]` に変更
- `out_count` の集計を `d["is_exit"]` ベースに変更

### 3. `scripts/webapp/templates/portfolio_csv_import.html`

- ヘッダに「取引日」列を追加 (位置: 銘柄名の次。コード/銘柄名/**取引日**/
  現ステータス/DB qty/CSV合算 qty/判定/戦略)
- `is_out` / `is_in` を `d.is_exit` / `d.needs_trade_idea` に置換
- 未登録行にも戦略 select と振り返りメモが出る (置換の結果として自動的に)

### 4. テスト (`tests/test_import_portfolio_csv.py`)

CLAUDE.md の方針に沿って**追加は3本以内**、parametrize で集約する。

1. `_judge` / `_build_diff_preview` のフラグ: 1保OUT・2準IN・未登録IN・一致 を
   parametrize し、`needs_trade_idea` / `is_exit` の期待値を確認
2. `_sync_records` の未登録+戦略あり: `add_to_watch` → 1保 遷移まで進み、
   record の status=1保 / qty=merged_qty / **memo.trade_idea に選択した戦略が
   永続化されていること**、pending_in が空になること
3. `_sync_records` の未登録+戦略なし: 従来どおり 3監 + pending_in に積まれること

取引日列は route 側の表示専用なので、テストは追加しない (fill の最大値を
引くだけの自明な処理)。

## D. 保留キュー (pending_in) の廃止

上記 B により、2準/3監/未登録 のいずれも**確認画面で戦略を選べばその場で 1保 に
確定できる**ようになった。結果として保留キューに積まれるのは「確認画面で意図的に
戦略を空欄にした」場合だけになり、存在意義がほぼ無くなった。

加えて調査の結果、**キューを消化するUIが存在しない**ことが判明した。
`list_pending_in()` を呼ぶ箇所が webapp に1つも無く、積まれたことがユーザーに
通知されず、中身を見ることすらできない。消えるのは全てCSV取込の副作用
(次回取込時に戦略が設定済みだった / qty=0 になった) だった。

実DBのキースは**0件**だったため、データ損失なく廃止できる。

- `import_portfolio_csv.py` から `upsert_pending_in` / `remove_pending_in` の
  呼び出しを削除する
- 戦略未設定のときの挙動: **既存銘柄 (2準/3監) は何もしない** (status そのまま)。
  **未登録銘柄は `add_to_watch()` で 3監 登録まで行う** (登録は情報の追加でしか
  なく破壊的でないため)。いずれも次回取込でまた新規IN候補として確認画面に出る
- `portfolio_shelve` 側の API (`upsert/list/remove_pending_in`、
  `KEY_PENDING_IN_PREFIX`、`transition_status` の 1保 遷移時の自動削除) は
  **残す**。変更を小さく保ち、必要になったら戻せるようにするため

判定文言もあわせて変える:

| 変更前 | 変更後 |
|---|---|
| `新規IN候補 (戦略ありで1保へ / 空欄なら保留キューへ)` | `新規IN候補 (戦略ありで1保へ / 空欄なら変更なし)` |
| `未登録+保有検出 (… 空欄なら監視+保留キューへ)` | `未登録+保有検出 (… 空欄なら監視へ登録)` |

## 確認ポイント (ユーザー向け)

- 未登録銘柄で戦略を選んで反映したとき、**action_log に「初回登録」と
  「ステータス変更 (3監→1保)」の2件が並ぶ**。売買履歴タブでの見え方が
  くどいと感じたら要相談 (既存の 2準/3監 自動IN も同じ2件構成)
- 取引日は fill 未取込の銘柄で空欄になる。楽天/SBI の約定CSVを先に取り込んで
  おくと埋まる
- 未登録行のデフォルトは「戦略未選択」なので、**何もしなければ従来どおり
  3監+保留キュー**。挙動が勝手に変わることはない
