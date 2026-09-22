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

`get_earnings_document` は `text_quality` が `ok` 以外 (画像主体・文字化け) の資料で
`text: null` と `local_path` を返します。この場合は PDF を直接添付してください
(MCP で PDF バイナリは返しません)。

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
