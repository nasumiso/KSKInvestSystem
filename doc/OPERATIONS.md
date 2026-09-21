# OPERATIONS — 運用機 (MacMini) の構築と日常運用

運用機 (MacMini M2 Pro) で日次バッチと WebApp を常駐させ、開発機 (MBA) から分離するための手順。
issue #452 に対応する。

## 1. 構成と原則

| 観点 | MacMini (運用機) | MBA (開発機) |
|---|---|---|
| 用途 | 平日19:00 の日次バッチ / WebApp 常駐 | 機能開発・パーサー修正 |
| ソース | `git pull --ff-only` で main 追従 | feature ブランチで開発 |
| `KS_DATA_DIR` | ローカル SSD、**正本** | 開発用コピー (分離は #453) |
| WebApp | LaunchAgent で常駐、Tailscale Serve で Tailnet 公開 | 開発時のみ手動起動 |

**原則:**

- **single-writer** — メモ・レーティング・action_log 等、人が書く運用データの編集は**常に運用機の WebApp 経由**で行う。スマホからも MBA からも Tailnet 経由 (Tailscale Serve の URL) で運用機の WebApp を開く
- **データ同期は 運用機 → MBA の一方向のみ** — 開発でデータが要るときにオンデマンドで rsync する。書き戻しはしない (「どっちが新しいか」を考える場面を構造的に無くす)
- **運用機ではローカル変更をしない** — `git pull --ff-only` が conflict で止まらないようにする

## 2. MacMini 初期セットアップ

```bash
# リポジトリ (Dropbox 配下ではなく素のローカルパス)
git clone <repo> ~/dev/shintakane
cd ~/dev/shintakane
git config user.name  "K.Sohara"
git config user.email "<email>"

# Python 3.11 + venv
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

`~/.zshrc` に追記する。`KS_DATA_DIR` は `ks_util._resolve_data_dir()` が `os.path.abspath()` をかけるだけで**チルダ展開しない**ため、必ず絶対パスで書く。

```bash
export KS_DATA_DIR=/Users/<user>/shintakane_data
```

スリープを無効化する (運用機の存在理由なので必須)。

```bash
sudo pmset -a sleep 0 disksleep 0
pmset -g | grep -E '^ *(sleep|disksleep)'
```

**自動ログインと電源復帰を設定する (必須)。** LaunchAgent は per-user agent なので、**ログインセッションが成立するまで起動しない**。停電や OS アップデートで再起動したあと誰もログインしなければ、日次バッチも WebApp も止まったままになる。

- システム設定 > ユーザとグループ > 自動ログイン を運用ユーザーに設定する
- FileVault が有効だと再起動後に必ずディスク解錠が要る (自動ログインは効かない)。無人運用を優先するなら**運用機では FileVault を切る**。物理的に手元にある前提の割り切り
- 停電復帰後に自動で電源が入るようにする

```bash
sudo pmset -a autorestart 1      # 電源断からの復帰時に自動起動
pmset -g | grep -E 'autorestart|SleepDisabled'
```

LaunchDaemon (システムドメイン) にすればログイン不要にできるが、**採らない**。Google Drive アプリ・キーチェーン・Tailscale の GUI クライアントがいずれも GUI セッション前提で、システムドメインへ移すと別の問題が出る。

**設定できたら再起動して検証する。** 再起動後にログインせず放置し、`launchctl print` でジョブが読み込まれていること、当日の19時に実行されることを確認する (後述の「4. LaunchAgent の有効化」の後に行う)。

## 3. データ移行

**静止点を作ってから実施する。** 途中で書き込まれると shelve の整合が崩れる。

```bash
# --- MBA 側 ---
# 1. WebApp を停止し、書き込みプロセスが居ないことを確認
lsof -iTCP:5001 -sTCP:LISTEN -t | xargs -r kill
lsof +D "$KS_DATA_DIR" | grep -v ' DIR ' | head        # 0件であること

# 2. 退避ファイル・同期競合コピーを削除 (削除前に du -sh で記録)
du -sh "$KS_DATA_DIR"
ls -lhS "$KS_DATA_DIR"/stock_data | head
rm "$KS_DATA_DIR"/stock_data/stocks_shelve.*.before_compact_*.bak
rm "$KS_DATA_DIR"/stock_data/stocks_shelve\ \(*\).*

# 3. compact してから backup
cd scripts
python make_stock_db.py compact
python make_stock_db.py backup
```

> 2026-09-21 に実施済み: 退避 6.6GB + 競合コピーを削除し、`stocks_shelve.dat` は 1.7GB → 18MB、`stock_data` は 9.8GB → 1.5GB になった。

```bash
# 4. 転送 (キャッシュ類は再取得可能なので除外)
rsync -avh --progress \
  --exclude '*.lock' --exclude '*.dbm.lock' \
  --exclude 'html_cache' --exclude 'disclosure/cache' \
  --exclude '.DS_Store' --exclude 'portfolio_csv_import_tmp' \
  "$KS_DATA_DIR"/ <macmini>:/Users/<user>/shintakane_data/
```

```bash
# --- MacMini 側: 整合性確認 ---
du -sh "$KS_DATA_DIR"
cd ~/dev/shintakane/scripts && source ../.venv/bin/activate
python make_stock_db.py list 6324      # 複数銘柄で試す
python shintakane.py analyze           # 完走すること
```

**Google Drive 認証**: `data/googledrive/` の認証ファイルを MBA から手動コピーし、**cron を有効化する前に対話認証を一度通しておく**。token が無いと `oauth2client` が対話入力を待ち、launchd 経由では無言でハングする。

## 4. LaunchAgent の有効化

plist の置換とインストールは [deploy/README.md](../deploy/README.md) を参照。インストール後にスモークテストする。

```bash
# 手動実行で通ることを確認 (theme-news は時間がかかるので省略)
cd ~/dev/shintakane && bash shintakane_cron.sh --skip-theme-news

# launchd が意図した plist を読んでいるか
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.cron" | grep -E 'path|state'
```

## 5. Tailscale Serve (出先アクセス)

```bash
brew install --cask tailscale
tailscale up
# ローカルの 5001 を Tailnet 限定の HTTPS (443) で公開する。
# 証明書は自動発行され、URL は https://<machine>.<tailnet>.ts.net/ になる。
tailscale serve --bg localhost:5001
tailscale serve status         # 実際の公開 URL がここに出る
```

`--bg` だけでは公開先が既定の HTTPS (443) になる点に注意する。**`https://<machine>.<tailnet>.ts.net/` で開く**のであって、`http://<machine>:5001` では届かない (Flask は 127.0.0.1 にしか bind していないため、Tailnet IP の 5001 番は開いていない)。

ポート番号付きの HTTP で使いたい場合は明示する。ただし MagicDNS 名でのみ到達でき、TLS は付かない。

```bash
tailscale serve --bg --http=5001 localhost:5001   # http://<machine>:5001/
```

**`tailscale funnel` は使わない。** funnel は公開インターネットへ露出する。WebApp は認証を持たないので、Tailnet 限定が前提。

MBA・スマホで Tailscale にログインし、`tailscale serve status` が表示した URL で到達することを確認する。

止めるときは同じコマンドに `off` を付ける。

```bash
tailscale serve --bg localhost:5001 off
```

## 6. MCP tunnel の移設

四季報 MCP (`com.k_sohara.shintakane-tunnel`) は ChatGPT から運用データを参照するため、運用機側へ移す。手順は [scripts/mcp/README.md](../scripts/mcp/README.md) の「常駐起動」節。キーチェーンへの Runtime API Key 登録 (`security add-generic-password`) を MacMini で再実行する必要がある。

移設後、MBA 側の tunnel LaunchAgent は unload する。

## 7. 日常運用

| やること | コマンド |
|---|---|
| ログを見る | `tail -f ~/Library/Logs/shintakane/cron.stdout.log` |
| 個別処理のログ | `~/dev/shintakane/logs/{shintakane,make_stock_db,theme_news,compact}.log` |
| 手動で日次バッチ | `cd ~/dev/shintakane && bash shintakane_cron.sh` |
| WebApp を新コードで再起動 | `launchctl kickstart -k "gui/$(id -u)/com.k_sohara.shintakane.webapp"` |

**自動で走るもの:**

- 平日19:00 に日次バッチ (`git pull --ff-only` → 分析 → DB更新 → exposure → theme-news)
- pull が成功したら WebApp を自動で kickstart (新しいコードを反映)
- **金曜のみ** バッチ末尾で `backup` → `compact` (stocks_shelve が 100〜120MB/日 肥大するため)

pull が失敗しても**バッチは継続する**。前回のコードで走るので、ログに `❌ git pull --ff-only 失敗` が出ていたら手当てする。

## 8. トラブルシュート

**日次バッチが走らない**

```bash
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.cron" | grep -E 'path|state|runs'
```

`path` が意図したファイルか必ず見る。過去に `/Library/LaunchAgents/` (システム側) の古い plist が読まれていて、`~/Library/LaunchAgents/` の編集が反映されていなかったことがある。

**再起動後に何も動いていない**

LaunchAgent はログインセッションが無いと起動しない。自動ログインが効いているか確認する。

```bash
who                              # 運用ユーザーがログインしているか
launchctl print "gui/$(id -u)" | grep -c shintakane   # 0 ならセッションが無い
```

FileVault が有効だと自動ログインは効かず、再起動のたびに手でディスク解錠が要る。セットアップ節を参照。

**theme-news だけ失敗する (`claude CLI が見つかりません`)**

plist の `PATH` に `<home>/.local/bin` が入っているか確認する。launchd はシェルを通らないので `.zshrc` の PATH は効かない。

**WebApp に繋がらない**

```bash
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.webapp" | grep -E 'state|last exit'
tail -30 ~/Library/Logs/shintakane/webapp.stderr.log
```

`FLASK_SECRET_KEY が未設定です` で落ちている場合は `~/.shintakane_env` を確認 (chmod 600)。

**compact が中断して次回から止まる**

`<db>.compact_backup.*` が残っていると、これを「中断の痕跡」とみなして次回実行が `RuntimeError` で停止する。中身を確認してから戻すか消す。

```bash
ls -lh "$KS_DATA_DIR"/stock_data/stocks_shelve.compact_backup.*
# 退避を正本へ戻す場合は .dat/.dir/.bak の3点セットで .compact_backup を外した名前へ
```

**ロックを掴んでいるプロセスを知りたい**

```bash
lsof "$KS_DATA_DIR"/stock_data/stocks_shelve.dbm.lock
```

`ShelveDB` は open〜close で flock を保持する (#451)。compact は全工程で排他するので、WebApp を止めずに実行してよい。

## 9. 復旧

**research / portfolio shelve** — `make_stock_db.py` の各実行末尾で日付付き14世代を自動保存している。復元手順は [doc/COMMANDS.md](COMMANDS.md) の該当節を参照。

**stocks_shelve** — 再生成可能なのでバックアップ世代は持たない。壊れたら `make_stock_db.py list_all_db` で作り直す。

**運用機が全損したら** — MBA を運用機に戻す。`~/Library/LaunchAgents/` へ plist を入れ直し (`deploy/README.md`)、`launchctl enable` + `bootstrap` する。MBA の launchd は 2026-09-19 に `disable` 済みなので `enable` が要る。

```bash
launchctl enable "gui/$(id -u)/com.k_sohara.shintakane.cron"
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.k_sohara.shintakane.cron.plist
```
