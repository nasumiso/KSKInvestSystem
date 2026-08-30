# 四季報 MCP サーバー

`shikiho_server.py` は `research_shelve` の四季報コメントだけを読み取り専用で提供する stdio MCP サーバーです。HTTP ポートは開きません。

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
- `get_shikiho(code_s, limit=8)`: 事業概要と四季報コメント履歴を返します。

`period` は四季報の版情報です。正確な時点は DB に保存していないため、`as_of` は常に `null` です。

すべての DB 読み取りは `research_shelve` の書き込みと同じ flock を取得するため、WebApp や日次バッチの更新とは直列化されます。

## MBA での常駐起動

Secure MCP Tunnel の Runtime API Key は平文ファイルや LaunchAgent に書かない。最初に、Runtime API Key を設定したターミナルでキーチェーンへ保存する。

```bash
security add-generic-password -U -a "$USER" \
  -s shintakane-tunnel-control-plane -w "$CONTROL_PLANE_API_KEY"
```

`com.k_sohara.shintakane-tunnel.plist` の `REPOSITORY_PATH` を、main をチェックアウトしたリポジトリ直下のパスへ置き換え、`~/Library/LaunchAgents/` へコピーして読み込む。次は、テンプレートをそのパスで展開してから配置する。

```bash
REPOSITORY_PATH="$(pwd)"
sed "s|REPOSITORY_PATH|$REPOSITORY_PATH|g" \
  scripts/mcp/com.k_sohara.shintakane-tunnel.plist \
  > ~/Library/LaunchAgents/com.k_sohara.shintakane-tunnel.plist
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.k_sohara.shintakane-tunnel.plist
```

状態は `launchctl print "gui/$(id -u)/com.k_sohara.shintakane-tunnel"`、ログは
`~/Library/Logs/shintakane-tunnel.stderr.log` で確認する。停止する場合は
`launchctl bootout "gui/$(id -u)/com.k_sohara.shintakane-tunnel"` を使う。
