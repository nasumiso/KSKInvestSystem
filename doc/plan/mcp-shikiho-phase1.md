親issue: #426 (Shintakane MCP連携対応) の Phase 1。

## 概要

四季報データだけを対象にしたローカル MCP サーバーを新設し、
**ChatGPT Web の通常チャット (銘柄分析スレッド) から**四季報コメント履歴を参照できるようにする。
同じサーバーを Claude Code からも stdio で直接利用する。

MCP 対応全体の**最小の縦串**として、サーバー起動・Tool 定義・既存 Repository 再利用・
Claude Code からの疎通までを一通り通すことを目的とする。
ここで得た型を Phase 2 以降の Tool 追加に流用する。

## なぜ四季報から切り出すか

- `research_shelve` に既に **858銘柄 / 2,506件** の四季報コメントが蓄積済み (実データあり)
- 読み取り専用で完結し、書き込み・整合性の考慮が不要
- LLM の定性分析と最も相性が良い (テキスト情報 + 時系列)
- 依存が `research_shelve` 1モジュールで済み、スコープが閉じている

## 前提: Python バージョン (解決済み)

MCP Python SDK は **Python 3.10+ が必須**。本プロジェクトの `.venv` はかつて Python 3.9.6 だったため、
当初は「MCP サーバーだけ別の 3.10+ 環境で動かす」方針だった。

**2026-08-30 に本体 `.venv` を Python 3.11.14 へ移行済み**のため、この制約は解消された。
→ **MCP サーバー専用の venv は不要**。本体 `.venv` に `mcp` を追加インストールするだけでよい。

### 移行時の検証結果 (2026-08-30)

- 全依存が `requirements.txt` のまま 3.11 にインストール可能
- テストスイート **2207 passed / 0 failed** (`-m "not local_db and not live_html"`)
- 本番 shelve DB を正常に読める (`research_shelve` 895件)
- `KS_DATA_DIR` によるパス解決も 3.11 側で正しく効く
- 日次バッチ (`shintakane_cron.sh`) / theme-news / WebApp すべて正常動作
- shelve は `dbm.dumb` (純Python形式) のためバージョン間のバイナリ互換性問題なし

なお pandas は 2.3.3 → 3.0.5 へメジャーアップしたが、利用箇所は `price.py` の
`df.loc[idx]` 行アクセス程度に限られ、yfinance 実データ取得・変換とも動作確認済み。

### 注意点1: 無言のパスフォールバック

`db_shelve.py` は `from ks_util import ...` に失敗すると
**例外を出さずリポジトリ直下の `data/` にフォールバックする** (`db_shelve.py:20-30`)。
依存不足時に「空の DB を読んで 0 件が返る」形で失敗するため、
起動時に DB パスと読み込み件数をログ出力し、無言のフォールバックを検知できるようにする。

### 注意点2: プロセス間の読み書き整合性 (必須対応)

MCP Server は WebApp / 日次バッチとは**別プロセス**から live の shelve を読む。
ここに整合性の穴がある:

- `research_shelve._flock()` は **書き込み専用のロック**で、
  `upsert_research_record()` 等の書き込み経路でのみ取得される
  (`research_shelve.py:376,595,634`)
- 一方 `get_research_record()` / `list_research_records()` は**無ロック**で
  `dbm.dumb` を開く (`db_shelve.py:77`)

`dbm.dumb` は書き込み時に `.dir` / `.dat` を追記・書き換えするため、
**WebApp の四季報保存中に MCP が読むと、不整合なレコードや読取例外が起きうる**。
単一プロセスの WebApp だけなら顕在化しなかったが、MCP を足すと現実的なリスクになる。

**Phase 1 の方針: MCP 側の読み取りでも同じ flock を取得する。**

- `_flock()` は同一ロックファイルに対する `LOCK_EX` (排他ロック)。
  読み取り側も同じロックを取れば WebApp の書き込みと排他できる
  (読み取り同士も直列化されるが、読み取りは短時間なので許容する)
- 読み取りは短時間で終わる (1銘柄取得 / 全件走査でも1秒未満) ためロック競合の実害は小さい
- `research_shelve.py` に**読み取り用の公開ヘルパを1つ追加**する。
  既存の書き込み経路・WebApp の挙動は変更しない

代替案 (採用しない): DB のスナップショットコピーを読む方式は、
鮮度が落ちるうえコピー管理が増えるため Phase 1 では過剰。
将来 Mac mini で常時稼働させ読み取り頻度が上がった段階で再検討する (#174)。

## 接続方式: OpenAI Secure MCP Tunnel

ChatGPT からカスタム MCP を使うには、通常は**公開 HTTPS エンドポイント**が必要で、
ローカル stdio サーバーには到達できない。
これを解決するのが **OpenAI Secure MCP Tunnel** (2026-05 公開)。

```text
ChatGPT (Web)
    ↓ OpenAI ホストのトンネルendpoint にリクエストをキュー
tunnel-client  (Mac 上で常時稼働 / outbound HTTPS long-polling)
    ↓ --mcp-command で stdio 起動
scripts/mcp/shikiho_server.py   (本体 .venv / Python 3.11)
    ↓ import
research_shelve → research_shelve DB
```

**重要**: `tunnel-client` は **stdio サーバーをそのままブリッジできる**
(`--mcp-command "python /path/to/server.py"`)。
つまり HTTP/SSE サーバーを別途書く必要はなく、**stdio 実装のまま ChatGPT から使える**。
Claude Code からは同じ `shikiho_server.py` を `.mcp.json` 経由で直接叩く。
→ **サーバー実装は1つで、ChatGPT と Claude Code の両方に対応できる。**

### 利点

- inbound ポートを開けない (outbound HTTPS のみ)
- Flask や DB をインターネットに公開しない → 親issue #426 のインフラ方針と一致
- 自前で OAuth を実装する必要がない

### 確認済み (2026-08-29 / ユーザーアカウント)

- ChatGPT の Developer mode: **有効化できる**
  (`Settings → Security and login → Developer mode`。2026-07 に Connectors → Plugins へ改称)
- `platform.openai.com → Settings → Organization → Tunnels`: **アクセス可能**
  (「トンネルはまだありません」+ 作成ボタンが有効)

### 制約 (実装前に確認が必要)

- **`tunnel-client` は常時起動が必要**。停止するとトンネル経由のリクエストは失敗する。
  MacBook Air を閉じると切れるため、**日常的に使うなら Mac mini 移行 (#174) が事実上の前提**。
  Phase 1 の動作確認自体は MacBook Air で完結できる
- ChatGPT の Web 版でのみ利用可能 (デスクトップ/モバイルアプリでは developer mode が出ない)
- **個人プランのカスタムコネクタは read/fetch のみ**。
  full MCP (書き込み可) は Business / Enterprise / Edu ワークスペース限定とされる。
  本 issue は完全 Read Only なので**影響しない**が、
  将来の Write Tool (親issue #426 Phase 4) は個人プランのままでは実現できない可能性が高い。
  → #426 の Phase 4 着手時に、プラン要件を再確認すること
- **利用面 (surface) の制約**: 公式ヘルプによると
  **agent mode はカスタムアプリを使わない**、deep research は read/fetch のみで使える。
  したがって Phase 1 の成功条件は **ChatGPT Web の通常チャット**に固定する
  (「銘柄分析スレッド」= 通常チャットのスレッドを指す)。
  agent mode / deep research での利用可否は Phase 1 のスコープ外とする

※ プラン・surface の制約は OpenAI 側の仕様変更が速い領域。
本 issue のアカウントでは Developer mode と Tunnels の**両方が実際に開けることを確認済み** (上記) のため、
接続自体は成立する見込みだが、最終的な可否は実機確認で確定させる。

## スコープ

### 対象

- 四季報コメント履歴 (`shikiho_comments`)
- 事業概要 (`overview`)
- 銘柄コード / 銘柄名
- 社名・コードによる銘柄検索

### 対象外

- 総合評価 / メモ / 機関投資家コメント / 決算コメント / スナップショット
  → Phase 2 以降の別 Tool で扱う
- 書き込み系 (完全 Read Only)
- #346 の業績予想 (`shikiho_gyoseki`) — 未実装のため。#346 完了後に追加する

## 実装方式

`scripts/` 配下に stdio MCP サーバーを新規追加し、`research_shelve` を直接 import する。
Flask WebApp とは独立プロセス。HTTP 公開は行わない。

```text
Claude Code
    ↓ stdio
scripts/mcp/shikiho_server.py   (本体 .venv / Python 3.11)
    ↓ import
research_shelve                 (既存モジュール / 読み取りロックのみ追加)
    ↓
research_shelve DB
```

### 想定ファイル構成

```text
scripts/mcp/
├── shikiho_server.py     # MCP サーバー本体 (Tool 定義 + stdio)
└── README.md             # セットアップ・.mcp.json 登録手順
```

依存は本体 `requirements.txt` に `mcp` を追記して管理する (専用 requirements.txt は作らない)。

既存モジュールは原則**変更しない**。例外は「注意点2」の読み取りロック用ヘルパ追加のみで、
既存の書き込み経路・WebApp の挙動は変えない。

## Tool 定義

### 1. `get_shikiho`

指定銘柄の四季報コメント履歴と事業概要を返す。

**引数**

| 名前 | 型 | 必須 | 説明 |
|---|---|---|---|
| `code_s` | string | ○ | 銘柄コード (`"7729"` / `"215A"` 形式) |
| `limit` | int | | 返すコメント件数の上限。既定 8 |

**返却**

```json
{
  "code_s": "1301",
  "stock_name": "極洋",
  "overview": "水産品の貿易、加工、買い付け主力。すしネタに強み。",
  "shikiho_comments": [
    {
      "period": "26.6",
      "period_label": "四季報 2026年6月号",
      "as_of": null,
      "comment": "【一歩前進】水産は前半鈍いが…"
    },
    {
      "period": "",
      "period_label": null,
      "as_of": null,
      "comment": "【順　調】生鮮事業はコロナ収束につれ…"
    }
  ],
  "source": "research_shelve",
  "total_comments": 4
}
```

**仕様**

- 並び順は既存の `sort_shikiho_comments_desc()` をそのまま使う (新しい順、`period` 空は末尾)
- **`period` は「版情報」であって「時点情報」ではない**。`26.6` は「四季報 2026年6月号」を指すだけで、
  「2026年6月時点の事実」を意味しない (号の内容は発行前に書かれ、対象期間も号ごとに異なる)。
  したがって **`period` を `as_of` に変換しない**。
  - `period`: 生の値 (`"26.6"`)。空なら `""`
  - `period_label`: 人間可読の版名 (`"四季報 2026年6月号"`)。`period` 空なら `null`
  - `as_of`: **常に `null`**。実際の取得日・発行日を DB が保持していないため。
    価格・業績・保有状況など他の時点データと混同されないよう、推測で埋めない
- Tool の description に「四季報コメントは版情報のみを持ち、正確な時点は不明」と明記し、
  LLM が他の as_of 付きデータと同列に扱わないようにする
- 該当銘柄が無い場合はエラーではなく「未登録」と分かる形で返す
- コメント本文に HTML が含まれる可能性があるため、既存の `strip_html_tags` 相当でプレーンテキスト化して返す

### 2. `search_stocks`

社名またはコードで銘柄を検索し、候補を返す。
LLM が「東京精密」のように社名で来た場合に `code_s` へ辿り着くための Tool。

**引数**

| 名前 | 型 | 必須 | 説明 |
|---|---|---|---|
| `query` | string | ○ | 社名の一部 または 銘柄コード |
| `limit` | int | | 既定 10 |

**返却**

```json
{
  "results": [
    { "code_s": "7729", "stock_name": "東京精密", "has_shikiho": true, "comment_count": 4 }
  ]
}
```

**仕様**

- コード完全一致を最優先、次に社名の部分一致
- 既存の検索正規化 (`normalize_for_search`) を流用する
- 四季報コメントの有無・件数を返し、LLM が `get_shikiho` を呼ぶ価値を判断できるようにする

## セットアップ手順 (README に記載)

本体 `.venv` が Python 3.11 になったため、専用 venv は作らず `mcp` を追加するだけでよい。

```bash
# 本体 venv に MCP SDK を追加
uv pip install --python .venv/bin/python mcp
```

`requirements.txt` に `mcp` を追記する (本体 venv で使うため)。

`.mcp.json` (リポジトリルート):

```json
{
  "mcpServers": {
    "shintakane-shikiho": {
      "command": ".venv/bin/python",
      "args": ["scripts/mcp/shikiho_server.py"],
      "env": { "KS_DATA_DIR": "/Users/k_sohara/Ext/GoogleDrive/shintakane_data" }
    }
  }
}
```

### ChatGPT から使う (Secure MCP Tunnel)

```bash
brew install openai/tools/tunnel-client

# platform.openai.com → Settings → Organization → Tunnels でトンネルを作成し tunnel_id を取得
tunnel-client init --profile shintakane-shikiho \
  --tunnel-id <tunnel_id> \
  --mcp-command ".venv/bin/python scripts/mcp/shikiho_server.py"

tunnel-client doctor --profile shintakane-shikiho --explain   # 設定検証
tunnel-client run    --profile shintakane-shikiho             # 常時起動
```

ChatGPT 側 (Web版):

1. `Settings → Security and login → Developer mode` を ON
2. `Settings → Plugins` でアプリを作成し、Connection に **Tunnel** を選択
3. 作成したトンネルを選ぶ

`KS_DATA_DIR` は `tunnel-client` から起動されるプロセスに引き継がれる必要がある。
プロファイル設定で env を渡すか、`shikiho_server.py` 側で既定値を持つ
(**「注意点1」の無言フォールバックを踏まないよう、解決結果を必ず起動ログに出す**)。

## 完了条件

- [ ] MCP サーバーが stdio で起動する
- [ ] 既存 `research_shelve` からデータを取得できる (追加は読み取りロック用ヘルパのみ)
- [ ] `get_shikiho` が動作する
- [ ] `search_stocks` が動作する
- [ ] 全 Tool が Read Only
- [ ] `as_of` は常に `null` で返り、`period` は版情報として `period` / `period_label` に分離されている
- [ ] `source` を返す
- [ ] 起動時に DB パスと読み込み件数をログ出力し、`data/` へのフォールバックを検知できる
- [ ] 読み取り時に `research_shelve` の flock を取得し、WebApp の書き込みと排他できている
- [ ] **Claude Code から Tool Call できる** (`.mcp.json` 登録 → 実際に 7729 等で応答確認)
- [ ] **ChatGPT Web の通常チャットから Tool Call できる** (tunnel-client 経由 → 銘柄分析スレッドで四季報コメントが引ける)
      ※ agent mode / deep research は対象外
- [ ] `tunnel-client` 停止時にサーバー側が異常終了せず、再接続で復帰する
- [ ] README にセットアップ手順を記載 (ローカル stdio / tunnel の両方)

## 動作確認

まず Claude Code (stdio 直結) で以下が通ること:

1. 「東京精密の四季報コメントを見せて」→ `search_stocks` → `get_shikiho` の順に呼ばれ、コメントが返る
2. 存在しないコードを指定 → 未登録と分かる応答が返る
3. `period` 空のコメントを含む銘柄 (例 1301) → `period: ""` / `period_label: null` / `as_of: null` で返る

次に **ChatGPT Web の通常チャット** (tunnel 経由) で以下が通ること:

4. 銘柄分析スレッドで同じ質問をして、同一内容が返る
5. `tunnel-client` を落として質問 → エラーになり、再起動後に復帰する
   (常時起動が前提であることを実地で確認しておく)

## テスト方針

CLAUDE.md のテスト方針に従い **5本以下**、parametrize で集約する。

- `get_shikiho` の整形結果 (period あり / なし / 未登録銘柄) を parametrize で1本
- `search_stocks` の一致順序 (コード完全一致優先 / 社名部分一致) を1本

Tool 定義の素通しや MCP プロトコル自体の挙動はテストしない (SDK の責務)。
`research_shelve` は既存テストでカバー済みのため重複させない。

## 関連

- 親issue: #426 Shintakane MCP連携対応
- #346 [四季報] 業績予想の取り込み — 完了後 `get_shikiho` に業績予想を追加
- #174 MacMini 移行 — **`tunnel-client` の常時稼働先。ChatGPT から日常的に使うなら事実上の前提**
