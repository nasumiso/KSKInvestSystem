# 四季報 MCP サーバー

`shikiho_server.py` は `research_shelve` の四季報コメント・業績予想・IR問い合わせ回答を読み取り専用で提供する stdio MCP サーバーです。HTTP ポートは開きません。利用者向けの仕様は [doc/MCP.md](../../doc/MCP.md) を参照してください。

## ローカル起動前の準備

Python 3.11 の本体 `.venv` を使います。依存をまだ入れていない環境では、リポジトリルートから実行します。

```bash
uv pip install --python .venv/bin/python -r requirements.txt
```

MCP ホストは通常のシェル環境を引き継がないため、`KS_DATA_DIR` を必ず渡してください。未設定またはリポジトリ内 `data/` を参照した場合、サーバーは起動時に失敗します。

```json
{
  "mcpServers": {
    "shintakane-shikiho": {
      "command": "/<REPOSITORY_ROOT>/.venv/bin/python",
      "args": ["/<REPOSITORY_ROOT>/scripts/mcp/shikiho_server.py"],
      "env": {
        "KS_DATA_DIR": "/Users/k_sohara/Ext/GoogleDrive/shintakane_data"
      }
    }
  }
}
```

この内容を Claude Code の `.mcp.json` に登録するのは手順 4 で行います。`/<REPOSITORY_ROOT>` は main をチェックアウトしたリポジトリ直下の絶対パスへ置き換えます。

## 提供ツール

- `search_stocks(query, limit=10)`: 社名の一部またはコードで検索します。コード完全一致を優先します。
- `get_shikiho(code_s, limit=8)`: 事業概要、四季報コメント履歴、四季報業績予想を返します。
- `get_ir_qa(code_s, limit=10)`: IR部門への問い合わせ回答履歴を新しい順に返します。
- `list_earnings_documents(code_s, months=12, include_superseded=False)`: 収集済みの決算説明資料・決算短信の一覧を返します。テキスト本体は含みません。
- `get_earnings_document(code_s, doc_id, page_from=1, page_to=None, max_chars=15000)`: 資料の抽出済みテキストをページ範囲で返します。

`period` は四季報の版情報です。正確な時点は DB に保存していないため、`as_of` は常に `null` です。

`get_ir_qa` の `answered_at` は実際の回答日 (`YYYY/MM/DD`) なので、`as_of` にも同じ値が入ります。回答本文は公開情報として流通しない非公開の一次情報です。既定は10件、取得上限は50件です。

### 決算資料 (issue #433)

資料は `ir_docs.py` が収集したものに限られます (`{KS_DATA_DIR}/ir_docs/<code_s>/`)。
収集していない銘柄と、資料が存在しない銘柄は区別して返します。

- `coverage_status`: `collected` (収集を試みた) / `not_collected` (未収集。資料の有無は不明)
- `total_documents`: `months` の絞り込みを無視した収集済み総数。`documents` が空でも
  これが非0なら「期間内に無いだけ」で、資料が存在しないわけではありません
- `partial_coverage`: 要求期間が収集区間に収まらないとき `true`。`collected_months` は
  **収集した時点**からの深度なので、収集後に時間が経つと最新側に穴が空きます。
  `coverage_through` (= 最終収集日) 以降の資料は未収集です
- `coverage_discontinuous`: 期間を遡った収集の後に `--depth latest` で追加収集した場合 `true`。
  `ir_docs.py` は `latest` 実行でも `last_collected_at` を現在へ進める一方、`collected_months`
  は据え置くため、前回の深い収集から今日までの間に開示された資料が抜けている可能性があります
- `has_collection_errors`: 収集時にエラーがあった場合 `true`。一覧は不完全です

### 訂正版の扱い

`list_earnings_documents` は既定で訂正版に置き換えられた旧版を返さないが、
**訂正版が差分通知のときは原本も返す**。実データには2種類ある:

| 種別 | 実例 | 既定の返却 |
|---|---|---|
| 全文差し替え | 2681: 原本29,490字 → 訂正版30,324字 | 訂正版のみ |
| 一部訂正の通知 | 2780: 原本48,085字/74p → 訂正版1,180字/2p | 訂正版 + 原本 |

後者で原本を落とすと、74頁の本体が一覧から消え、宛名だけの2頁がその四半期の
決算説明資料として返ってしまう。残した原本は `superseded_by` が入っているので
訂正済みと判別できる。

`get_earnings_document` は `text_quality` が `ok` 以外 (画像主体・文字化け) の資料で
`text: null` と PDF の所在 (`relative_path` / `local_path`) を返します。

`relative_path` は `KS_DATA_DIR` からの相対 (`ir_docs/<code_s>/<ファイル名>.pdf`) で、
端末 (Mac mini / MBA) に依存しない。`local_path` は互換のため残している絶対パス。

### テキストは万能ではない (利用側が知っておくこと)

**`text_quality: ok` でも図表内の数値が抜けていることがある。** `classify_text_quality()` の
`ok` は「1ページ平均100文字以上あり文字化けしていない」という判定で、数値が取れたかは
見ていない。決算説明資料はスライド形式で図表が主体のため、見出しと要約文だけが抽出され、
業績数値がグラフ画像内に残るケースがある (実測: 決算説明資料113件中7件が数値3個/頁未満、
うち4件は数値0個)。

項目名だけあって数値が続かないテキスト (例「売上高 営業利益 （単位:百万円）」) は、
**資料に数値がないのではなく抽出できていない**。数値の裏取りが要る分析では PDF を見る。

この濃淡を資料単位の数値で渡す案 (issue #461) は priority:low。PDF を見るかは資料より
タスク (数値の裏取りが要るか) で決まり、ページ単位の欠落は LLM が読んだテキストから
判断できるため、下記の経路案内で足りるとした (#463)。

### PDF は MCP では渡せない (Google Drive コネクタで読む)

4.9MB の PDF は base64 で約6.5MB となりコンテキストに載らず、ChatGPT のコネクタも
ツール結果をテキストとして扱う。MCP が返すのは PDF の所在だけで、
**ローカルファイルを読めない環境 (通常のチャット・iPhone・ブラウザ版の ChatGPT) は
`local_path` を開けない**。サーバーと同じ Mac の ChatGPT アプリの Work モードや
Claude Code は開けるので (実機確認済み)、
その場合は `local_path` が最も確実。

代わりに **Google Drive コネクタ**で読む。`ir_docs` は Google Drive へミラー同期
されており、ファイル名は TDnet ID を含むため一意になる。`relative_path` の末尾の
ファイル名でコネクタから検索すれば、人の手を介さず PDF を開ける。コネクタ経由でも
ビジョン処理が効き、テキスト抽出で落ちたグラフ内の数値も読める
(2026-09-22 実機確認済み / #461 のコメント)。iPhone の ChatGPT からも使える。

LLM には、instructions と docstring で次の順に案内している (#463)。

| 状況 | 読み方 |
|---|---|
| PDF がチャットに添付済み / 同じ端末で `local_path` を開ける (Mac の ChatGPT アプリの Work モード、Claude Code 等) | それを使う (取りに行き直さない) |
| 全体の把握・文言の確認・ページの特定 | `get_earnings_document` のテキスト |
| `text: null`、数値の裏取り、項目名だけで数値がない | Drive コネクタで PDF を開く |
| コネクタが使えない | ユーザーに添付を依頼する (最後の手段) |

ユーザーの手動添付でもビジョン処理は効く (2026-08-30 実機確認済み)。

テキストは `max_chars` を超えないようページ境界で切り出し、続きは `truncated: true` と
`next_page_from` で案内します。

すべての DB 読み取りは `research_shelve` の書き込みと同じ flock を取得するため、WebApp や日次バッチの更新とは直列化されます。

## MBA での常駐起動

Secure MCP Tunnel の Runtime API Key は平文ファイルや LaunchAgent に書かない。最初に、Runtime API Key を設定したターミナルでキーチェーンへ保存する。

```bash
security add-generic-password -U -a "$USER" \
  -s shintakane-tunnel-control-plane -w "$CONTROL_PLANE_API_KEY"
```

launchd はこの Mac では Dropbox 内のシェルスクリプトを直接起動できないことがある。そのため、起動ラッパーだけをホーム配下へインストールする。MCP サーバー本体はトンネルのプロファイルで main の `scripts/mcp/shikiho_server.py` を参照するため、コードや DB のコピー運用にはならない。

`com.k_sohara.shintakane-tunnel.plist` の `RUNNER_PATH` を、ホーム配下にインストールした起動スクリプトの絶対パスへ置き換え、`~/Library/LaunchAgents/` へコピーして読み込む。

```bash
RUNNER_PATH="$HOME/.local/bin/shintakane-tunnel"
mkdir -p "$(dirname "$RUNNER_PATH")"
install -m 755 scripts/mcp/run_tunnel_client.sh "$RUNNER_PATH"
sed "s|RUNNER_PATH|$RUNNER_PATH|g" \
  scripts/mcp/com.k_sohara.shintakane-tunnel.plist \
  > ~/Library/LaunchAgents/com.k_sohara.shintakane-tunnel.plist
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.k_sohara.shintakane-tunnel.plist
```

状態は `launchctl print "gui/$(id -u)/com.k_sohara.shintakane-tunnel"`、ログは
`~/Library/Logs/shintakane-tunnel.stderr.log` で確認する。停止する場合は
`launchctl bootout "gui/$(id -u)/com.k_sohara.shintakane-tunnel"` を使う。
