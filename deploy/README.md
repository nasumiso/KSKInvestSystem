# deploy/ — 運用機 (MacMini) の LaunchAgent テンプレート

運用機で日次バッチと WebApp を常駐させるための plist テンプレートと起動ラッパー。
`scripts/mcp/` と同じくプレースホルダを sed 置換してから `~/Library/LaunchAgents/` へ配置する。
ホスト別のファイルは作らない。

セットアップ全体の手順は [doc/OPERATIONS.md](../doc/OPERATIONS.md) を参照。ここは plist まわりだけを扱う。

| ファイル | 用途 |
|---|---|
| `com.k_sohara.shintakane.cron.plist` | 平日19:00 の日次バッチ |
| `com.k_sohara.shintakane.webapp.plist` | WebApp の常駐 (KeepAlive) |
| `run_webapp.sh` | WebApp 起動ラッパー (secret の読み込みと venv 指定)。これもテンプレートで、`__REPO__` を置換してからインストールする |

## プレースホルダ

| 置換対象 | 例 |
|---|---|
| `__REPO__` | `/Users/<user>/dev/shintakane` |
| `__USER_HOME__` | `/Users/<user>` |
| `__KS_DATA_DIR__` | `/Users/<user>/shintakane_data` |
| `__RUNNER__` | `/Users/<user>/.local/bin/shintakane-webapp` |

`launchd` の `EnvironmentVariables` は**シェル展開しない**ので `$HOME` とは書けない。必ず実パスに置換する。

## 事前準備

```bash
# ログ出力先 (launchd は親ディレクトリを作らない)
mkdir -p "$HOME/Library/Logs/shintakane"

# 環境変数ファイル (リポジトリ外・パーミッション 600)
cat > "$HOME/.shintakane_env" <<EOF
KS_DATA_DIR=/Users/$USER/shintakane_data
FLASK_SECRET_KEY=$(openssl rand -hex 32)
EOF
chmod 600 "$HOME/.shintakane_env"
```

`FLASK_SECRET_KEY` を plist に書かないのは、plist がリポジトリ管理下で git に入ってしまうため。

## インストール

```bash
REPO="$HOME/dev/shintakane"
KS_DATA_DIR="$HOME/shintakane_data"
RUNNER="$HOME/.local/bin/shintakane-webapp"

# WebApp 起動ラッパーをホーム配下へ置く
# (launchd が Dropbox 配下のシェルスクリプトを直接起動できないことがあるため、
#  scripts/mcp/ と同じ方式に揃える)
# インストール後は repo からの相対位置を失うので、__REPO__ を置換してから入れる
mkdir -p "$(dirname "$RUNNER")"
sed "s|__REPO__|$REPO|g" "$REPO/deploy/run_webapp.sh" > "$RUNNER"
chmod 755 "$RUNNER"

# cron
sed -e "s|__REPO__|$REPO|g" \
    -e "s|__USER_HOME__|$HOME|g" \
    -e "s|__KS_DATA_DIR__|$KS_DATA_DIR|g" \
    "$REPO/deploy/com.k_sohara.shintakane.cron.plist" \
    > "$HOME/Library/LaunchAgents/com.k_sohara.shintakane.cron.plist"

# webapp
sed -e "s|__RUNNER__|$RUNNER|g" \
    -e "s|__USER_HOME__|$HOME|g" \
    "$REPO/deploy/com.k_sohara.shintakane.webapp.plist" \
    > "$HOME/Library/LaunchAgents/com.k_sohara.shintakane.webapp.plist"

launchctl load "$HOME/Library/LaunchAgents/com.k_sohara.shintakane.cron.plist"
launchctl load "$HOME/Library/LaunchAgents/com.k_sohara.shintakane.webapp.plist"
```

どちらも **per-user の LaunchAgent** なので、**ログインセッションが無いと起動しない**。
再起動後に無人で動かすには自動ログインの設定が要る (FileVault は無効にする)。
手順は [doc/OPERATIONS.md](../doc/OPERATIONS.md) の「MacMini 初期セットアップ」を参照。

## 確認

```bash
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.cron"   | head -20
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.webapp" | head -20
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/
```

`launchctl print` に `path = ...` が出るので、**意図したファイルが読まれているか**を必ず見る。
過去に `/Library/LaunchAgents/` (システム側) の古い plist が読まれていて、
`~/Library/LaunchAgents/` の編集が一切反映されていなかったことがある。

## 更新するとき

plist を編集したら `unload` → `load` し直す。`launchctl load` だけでは反映されない。

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.k_sohara.shintakane.webapp.plist"
launchctl load   "$HOME/Library/LaunchAgents/com.k_sohara.shintakane.webapp.plist"
```

コードだけ更新した場合 (WebApp を新しいコードで動かし直したい場合) は kickstart でよい。
日次バッチの `git pull` 成功時にはこれが自動で走る。

```bash
launchctl kickstart -k "gui/$(id -u)/com.k_sohara.shintakane.webapp"
```

## 開発機 (MBA) について

MBA の launchd は 2026-09-19 に `bootout` + `disable` 済みで、日次バッチは手動運用。
**このテンプレートを MBA へ入れる必要はない。**
`SHINTAKANE_AUTO_PULL` / `SHINTAKANE_ENV` はどちらも未設定なら従来どおり動くので、
`bash shintakane_cron.sh` の手動実行はこれまでと変わらない。
