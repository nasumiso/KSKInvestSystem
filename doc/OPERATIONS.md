# OPERATIONS — 運用機 (MacMini) の構築と日常運用

運用機 (MacMini M2 Pro) で日次バッチと WebApp を常駐させ、開発機 (MBA) から分離するための手順。
issue #452 に対応する。

**2026-09-27 に構築を実施済み。** 以下は実際に通した手順で、確定した値をそのまま書いている。
再構築するときはこのまま上から実行できる。

| 項目 | 値 |
|---|---|
| ホスト | `kosukemac-mini` (Tailscale) / LAN 内 192.168.11.28 / macOS 26.5 / arm64 |
| ユーザー | `k_sohara` |
| repo | `~/dev/shintakane` (GitHub deploy key で SSH clone) |
| `KS_DATA_DIR` | `/Users/k_sohara/shintakane_data` (ローカル SSD) |
| Python | 3.11.16 (`uv venv --python 3.11 .venv`) |

> **Claude Code から作業するときの制約:** Bash ツールには TTY が無いため、パスワードを
> 求めるコマンド (`ssh-copy-id`・`sudo`・`brew install --cask`・キーチェーン操作) は
> **一切通らない**。プロンプトを出す前に諦めて失敗するので、認証エラーに見えて紛らわしい。
> これらはユーザーのターミナルで実行する。

## 1. 構成と原則

| 観点 | MacMini (運用機) | MBA (開発機) |
|---|---|---|
| 用途 | 平日19:00 の日次バッチ / WebApp 常駐 | 機能開発・パーサー修正 |
| ソース | `git pull --ff-only` で main 追従 | feature ブランチで開発 |
| `KS_DATA_DIR` | ローカル SSD、**正本** (`ir_docs` と `stock_ratings_drive` のみ Drive へ symlink) | `~/shintakane_data_dev` の開発用コピー (#453) |
| WebApp | LaunchAgent で常駐、Tailscale Serve で Tailnet 公開 | 開発時のみ手動起動 |

**原則:**

- **single-writer** — メモ・レーティング・action_log 等、人が書く運用データの編集は**常に運用機の WebApp 経由**で行う。スマホからも MBA からも Tailnet 経由 (Tailscale Serve の URL) で運用機の WebApp を開く
- **データ同期は 運用機 → MBA の一方向のみ** — 開発でデータが要るときにオンデマンドで rsync する。書き戻しはしない (「どっちが新しいか」を考える場面を構造的に無くす)
- **運用機ではローカル変更をしない** — `git pull --ff-only` が conflict で止まらないようにする。
  deploy key を read_only にしてあるので push もできない
- **同期するのは同期に耐えるものだけ** — PDF (`ir_docs`) は一度書いたら変わらないので
  Drive に置く。shelve は秒単位で書き換わるので絶対に同期しない (#174 の競合コピー問題)

## 2. MacMini 初期セットアップ

**SSH で入れるようにする (MBA 側から実施)。** 以降の作業はすべて `ssh macmini` で行う。

```bash
# MacMini のシステム設定 > 一般 > 共有 > リモートログイン を ON にしておく
# ★ このコマンドはパスワード入力が要るので、必ず自分のターミナルで実行する
ssh-copy-id -o StrictHostKeyChecking=accept-new k_sohara@192.168.11.28
```

`~/.ssh/config` に追記しておくと以後 `ssh macmini` で済む。

```
# 運用機。Tailscale の MagicDNS 名なので自宅・出先どちらでも届く
Host macmini
  HostName kosukemac-mini
  User k_sohara
  IdentityFile ~/.ssh/id_ed25519
  AddKeysToAgent yes
  UseKeychain yes
```

> `kosukemac-mini` は MBA に Tailscale を入れてログインしてから引ける名前 (「5. Tailscale Serve」参照)。
> それまでは LAN 内の IP (`192.168.11.28`) で代用する。自宅でも Tailscale が LAN 内の直通経路を
> 選ぶので速度は変わらない。IP 直指定は DHCP で変わりうるうえ、出先では届かない。
> mDNS 名 (`KosukenoMac-mini.local`) は IPv6 リンクローカルを先に返すことがあり、
> その経路では認証に失敗した。

**パッケージ管理 (MacMini 上)。** `brew install --cask` は sudo を要求するので、
cask だけはユーザーのターミナルで実行する。

```bash
# ★ Homebrew 本体と cask はパスワード入力が要る
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile

brew install uv
brew install --cask google-drive tailscale-app   # ★ sudo が要る
```

**リポジトリは deploy key で clone する。** 日次バッチが無人で `git pull` するため、
トークンの期限切れやキーチェーン解錠の影響を受けない SSH 鍵にする。運用機は pull しか
しないので**読み取り専用**で十分 (原則「運用機ではローカル変更をしない」とも整合する)。

```bash
# MacMini 側で鍵を作る (無人運用なのでパスフレーズ無し)
ssh-keygen -t ed25519 -N "" -C "macmini-shintakane-deploy" -f ~/.ssh/id_ed25519
ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts

# MBA 側で GitHub に deploy key として登録する
gh api repos/nasumiso/KSKInvestSystem/keys \
  -f title="macmini-shintakane-deploy" -f key="$(ssh macmini 'cat ~/.ssh/id_ed25519.pub')" \
  -F read_only=true

# MacMini 側で clone (HTTPS ではなく SSH URL。deploy key は SSH でしか使えない)
git clone git@github.com:nasumiso/KSKInvestSystem.git ~/dev/shintakane
cd ~/dev/shintakane
git config user.name  "K.Sohara"
git config user.email "kosuke4210@gmail.com"

# Python 3.11 + venv
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

`~/.zshrc` に追記する。`KS_DATA_DIR` は `ks_util._resolve_data_dir()` が `os.path.abspath()` をかけるだけで**チルダ展開しない**ため、必ず絶対パスで書く。

```bash
export KS_DATA_DIR=/Users/k_sohara/shintakane_data
export PATH="$HOME/.local/bin:$PATH"                              # theme-news の claude CLI
export PATH="/Applications/Tailscale.app/Contents/MacOS:$PATH"    # Tailscale は .app 配下
```

スリープを無効化する (運用機の存在理由なので必須)。`disksleep` の既定は 10 なので
必ず 0 にする。

```bash
# ★ sudo なのでユーザーのターミナルで実行する
sudo pmset -a sleep 0 disksleep 0 autorestart 1
pmset -g | grep -E '^ *(sleep|disksleep|autorestart)'
```

**自動ログインと電源復帰を設定する (必須)。** LaunchAgent は per-user agent なので、**ログインセッションが成立するまで起動しない**。停電や OS アップデートで再起動したあと誰もログインしなければ、日次バッチも WebApp も止まったままになる。

- システム設定 > ユーザとグループ > 自動ログイン を運用ユーザーに設定する
- FileVault が有効だと再起動後に必ずディスク解錠が要る (自動ログインは効かない)。無人運用を優先するなら**運用機では FileVault を切る**。物理的に手元にある前提の割り切り
- 停電復帰後に自動で電源が入るようにする (上の `pmset -a autorestart 1`)

設定できたか確認する。

```bash
pmset -g | grep -E '^ *(sleep|disksleep|autorestart)'
defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser   # k_sohara が返る
fdesetup status                                                          # FileVault is Off.
```

LaunchDaemon (システムドメイン) にすればログイン不要にできるが、**採らない**。Google Drive アプリ・キーチェーン・Tailscale の GUI クライアントがいずれも GUI セッション前提で、システムドメインへ移すと別の問題が出る。

**設定できたら再起動して検証する。** 再起動後にログインせず放置し、`launchctl print` でジョブが読み込まれていること、当日の19時に実行されることを確認する (後述の「4. LaunchAgent の有効化」の後に行う)。

## 3. データ移行

**静止点を作ってから実施する。** 途中で書き込まれると shelve の整合が崩れる。

### 3-1. Google Drive を「ストリーミング」で構成する

MBA では `KS_DATA_DIR` 全体が Google Drive のマイドライブ配下にあり (`~/Ext/GoogleDrive/...`
は同一 inode)、shelve まで同期対象だった。これが `stocks_shelve (1).bak` のような
**同期競合コピーが繰り返し生まれる原因** (#174)。

運用機では**データ本体をローカル SSD に置き、`ir_docs` だけを Drive に残す**。
PDF は一度書いたら変わらないので同期しても競合しないが、shelve は秒単位で書き換わるため
同期すると必ず壊れる。両者を分けるのが要点。

`ir_docs` を切れないのは、**Drive コネクタが iPhone やブラウザ版 ChatGPT から決算説明資料の
PDF を読む唯一の経路**だから (MCP は 4.9MB の PDF を返せない。`scripts/mcp/README.md` 参照)。

1. MacMini の画面で Google Drive.app にログインし、**「ストリーミング」を選ぶ**
   (「マイドライブをこのパソコンにミラーリング」にしない)
2. `~/Library/CloudStorage/GoogleDrive-<account>/マイドライブ/shintakane_data/ir_docs` が
   見えることを確認する
3. `KS_DATA_DIR` 配下から symlink を張る

```bash
ln -s "$HOME/Library/CloudStorage/GoogleDrive-kosuke4210@gmail.com/マイドライブ/shintakane_data/ir_docs" \
      /Users/k_sohara/shintakane_data/ir_docs

ls /Users/k_sohara/shintakane_data/ir_docs | wc -l     # 118 銘柄が見える
du -sh ~/Library/CloudStorage/GoogleDrive-*            # 数MB (ストリーミングなので実体を持たない)
```

4. 評価台帳の Drive 出力先も symlink で張る。正本 (`stock_ratings/`) はローカルに置いたまま、
   `stock_ratings.py` が書き込みのたびに `stock_ratings_drive/` へ上書きコピーする。
   **正本の `stock_ratings/` 自体を Drive へ symlink にしない。** Drive 上で `os.replace` すると
   ファイル ID が変わり、ChatGPT の参照が切れる (2026-09-27 実測)

```bash
ln -s "$HOME/Library/CloudStorage/GoogleDrive-kosuke4210@gmail.com/マイドライブ/shintakane_data/stock_ratings" \
      /Users/k_sohara/shintakane_data/stock_ratings_drive
```

> `mount | grep google` は空になるが正常。最近の Drive は FileProvider 方式で動くため
> `mount` には現れない。

### 3-2. MBA 側で静止点を作る

```bash
# 1. WebApp を停止し、書き込みプロセスが居ないことを確認
#    (macOS の xargs は -r が man に無いので、PID の有無をシェルで判定する)
WEBAPP_PID=$(lsof -tiTCP:5001 -sTCP:LISTEN 2>/dev/null)
[ -n "$WEBAPP_PID" ] && kill $WEBAPP_PID
lsof +D "$KS_DATA_DIR" | grep -v ' DIR ' | head        # 0件であること

# 2. 退避ファイル・同期競合コピーを削除 (削除前に du -sh で記録)
du -sh "$KS_DATA_DIR"
ls -lhS "$KS_DATA_DIR"/stock_data | head
rm "$KS_DATA_DIR"/stock_data/stocks_shelve.*.before_compact_*.bak
rm "$KS_DATA_DIR"/stock_data/stocks_shelve\ \(*\).*

# 3. compact する (転送量が一桁変わる)
cd scripts
python make_stock_db.py compact
```

> `stocks_shelve.dat` は **6日で 18MB → 92MB** に再肥大する (dbm.dumb の追記構造)。
> 転送直前の compact は必須。#451 のロックにより **WebApp を止めずに実行してよい**。
> compact 前の `backup` は取らない (世代削除を持たないので積み上がる)。

### 3-3. 転送

再取得可能なキャッシュと、Drive に置いた `ir_docs` を除外する。3.9GB のうち
**実際に転送するのは約 490MB**。

```bash
rsync -avh --progress \
  --exclude 'ir_docs/' \
  --exclude 'stock_data/kabutan/' --exclude 'stock_data/yahoo/' \
  --exclude 'stock_data/stocks_pickle_back/' \
  --exclude 'disclosure/cache/' \
  --exclude 'html_cache/' \
  --exclude '*.lock' --exclude '*.dbm.lock' \
  --exclude '.DS_Store' --exclude 'portfolio_csv_import_tmp/' \
  "$KS_DATA_DIR"/ macmini:/Users/k_sohara/shintakane_data/
```

**除外したディレクトリは空で作り直す。** コードは親ディレクトリの存在を前提に `.tmp`
ファイルを書くため、無いと `FileNotFoundError` で日次バッチが落ちる。

```bash
ssh macmini 'D=/Users/k_sohara/shintakane_data
for p in disclosure/cache sisu_data/html_cache \
         stock_data/kabutan/base stock_data/kabutan/finance stock_data/kabutan/price \
         stock_data/yahoo/price today_stocks/html_cache todays_kessan_data/html_cache \
         portfolio_csv_import_tmp; do
  mkdir -p "$D/$p"
done'
```

### 3-4. 整合性確認

件数が MBA と一致することを確認する。

```bash
ssh macmini 'export KS_DATA_DIR=/Users/k_sohara/shintakane_data
cd ~/dev/shintakane/scripts && ../.venv/bin/python -c "
from db_shelve import ShelveDB
from ks_util import DATA_DIR
import os, research_shelve as r, portfolio_shelve as p
with ShelveDB(os.path.join(DATA_DIR,\"stock_data\",\"stocks_shelve\"), read_only=True) as db:
    print(\"stocks:\", len(list(db.keys())))
print(\"research:\", len(r.list_research_records()))
print(\"records:\", len(p.list_records()), \"positions:\", len(p.list_positions()),
      \"fills:\", len(p.list_fills()), \"action_logs:\", len(p.list_action_logs()))
"'
```

> 2026-09-27 の実績: stocks 3330 / research 896 / records 321 / positions 34 /
> fills 1693 / action_logs 1780 が MBA と完全一致。
>
> 今は `deploy/macmini.sh counts` で同じ件数を運用機と開発機で並べて比べられる。
> 個別銘柄の検証には `make_stock_db.py list <code>` を使う (`shintakane.py analyze` は存在しない)。

**Google Drive API 認証**: `googledrive/` の認証ファイルは rsync で転送済み。
**cron を有効化する前に token が有効か確かめる**。token が無いと `oauth2client` が
対話入力を待ち、launchd 経由では無言でハングする。

```bash
ssh macmini 'export KS_DATA_DIR=/Users/k_sohara/shintakane_data
cd ~/dev/shintakane/scripts && ../.venv/bin/python -c "
import signal, sys
signal.signal(signal.SIGALRM, lambda *a: sys.exit(\"TIMEOUT: 対話入力待ち\")); signal.alarm(45)
import googledrive
print(googledrive.get_drive_service().about().get(fields=\"user(emailAddress)\").execute())
"'
```

## 4. LaunchAgent の有効化

**先に `~/.shintakane_env` を作る** (WebApp の本番起動に要る)。

```bash
ssh macmini 'cat > ~/.shintakane_env <<EOF
export KS_DATA_DIR=/Users/k_sohara/shintakane_data
export FLASK_SECRET_KEY=$(openssl rand -hex 32)
EOF
chmod 600 ~/.shintakane_env'
```

plist の置換とインストールは [deploy/README.md](../deploy/README.md) を参照。
`launchctl load` はユーザードメインなので sudo は要らない。

インストール後、**置換漏れと plist の構文を必ず確認する**。

```bash
grep -c "__" ~/Library/LaunchAgents/com.k_sohara.shintakane.*.plist ~/.local/bin/shintakane-webapp  # 全て 0
for f in ~/Library/LaunchAgents/com.k_sohara.shintakane.*.plist; do plutil -lint "$f"; done
```

### スモークテスト

```bash
# 手動実行で通ることを確認 (theme-news は時間がかかるので省略)
cd ~/dev/shintakane && bash shintakane_cron.sh --skip-theme-news

# launchd が意図した plist を読んでいるか
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.cron"   | grep -E 'path|state|runs|last exit'
launchctl print "gui/$(id -u)/com.k_sohara.shintakane.webapp" | grep -E 'path|state|pid'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/     # 200
```

WebApp が **production で動いているか**も見る (debug 有効のまま Tailnet へ出すと危険)。

```bash
grep -m1 "Debug mode" ~/Library/Logs/shintakane/webapp.stdout.log    # Debug mode: off
ps eww $(lsof -tiTCP:5001 -sTCP:LISTEN) | tr ' ' '\n' | grep SHINTAKANE_ENV   # =production
```

cron を load した直後は `RunAtLoad` で1回発火する。**19時前なら
`shintakane_cron.sh` の TTY ガードでサイレントスキップされる**のが正常
(`runs = 1` / `last exit code = 0` でログが両方 0 バイト)。

> **ログが空なのは失敗ではない。** ガードが効いた証拠。実行させたい場合は
> ターミナルから手動で叩く (TTY があれば時刻に関係なく走る)。

### 初回実行で踏んだ問題

- **`Errno 24: Too many open files`** — macOS の既定 `ulimit -n` は **256**。yfinance の
  週足バッチ (`threads=True` で 418銘柄を並列取得) がソケットを開いた時点で枯渇し、
  `dbm.dumb` が `.dat` を開けずに落ちる。`shintakane_cron.sh` が冒頭で 4096 に上げる
  ようにした。**MBA のターミナルは 1048576 に上がっているため手動実行では露見せず、
  launchd 経由の運用を始めて初めて出た**
- **「webapp が起動していません」の誤検知** — `git pull` 成功時の
  `launchctl kickstart -k` で WebApp を落とした直後に `lsof` で生存確認していたため、
  必ず起動途中を観測していた。最大20秒待つようにした

## 5. Tailscale Serve (出先アクセス)

```bash
brew install --cask tailscale-app        # ★ sudo が要るのでユーザーのターミナルで
# メニューバーの Tailscale アイコン > Log in (ブラウザ認証)
```

CLI は `.app` の中にあるので PATH を通す (「2. 初期セットアップ」の `.zshrc` 参照)。

```bash
export PATH="/Applications/Tailscale.app/Contents/MacOS:$PATH"
tailscale status        # 自ノードが Online であること
```

**HTTP で公開する。**

```bash
tailscale serve --bg --http=5001 localhost:5001
tailscale serve status
```

```
http://kosukemac-mini:5001 (tailnet only)
http://kosukemac-mini.tailbe284e.ts.net:5001 (tailnet only)
|-- / proxy http://localhost:5001
```

### なぜ HTTPS にしないか

`tailscale serve --bg localhost:5001` (HTTP 指定なし) は既定で **HTTPS(443) 公開**を試み、
Tailnet で HTTPS 証明書が有効化されていないと**無言でハングする** (`CertDomains: None`)。
有効化するには管理コンソール (login.tailscale.com/admin/dns) で「Enable HTTPS」を押す
必要があり、CLI からはできない。

そのうえで HTTP を選んでいる。

- Tailnet 内の通信は **WireGuard で暗号化済み**。HTTPS は二重の暗号化にしかならない
- WebApp は認証を持たないので、守っているのは **Tailnet の境界そのもの**。TLS の有無は
  防御に寄与しない
- HTTPS を有効化すると、マシン名と tailnet 名が **Certificate Transparency の公開台帳**に載る

代償はブラウザの「保護されていない通信」表示だけ。後から HTTPS へ切り替えるのは
管理コンソールの操作1回で済む。

### 到達確認

**MagicDNS 名で開く。** Tailscale IP 直打ち (`http://100.x.x.x:5001/`) は `serve` が
ホスト名で振り分けるため **404 になる** (これは正常)。

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://kosukemac-mini.tailbe284e.ts.net:5001/   # 200
```

iPhone・MBA でも Tailscale にログインし、同じ URL で開けることを確認する。

**`tailscale funnel` は使わない。** funnel は公開インターネットへ露出する。WebApp は
認証を持たないので、Tailnet 限定が前提。

止めるときは `off` を付ける。

```bash
tailscale serve --http=5001 off
```

## 6. MCP tunnel の移設

四季報 MCP (`com.k_sohara.shintakane-tunnel`) は ChatGPT から運用データを参照するため、運用機側へ移す。手順は [scripts/mcp/README.md](../scripts/mcp/README.md) の「常駐起動」節。

**Runtime API Key の登録は MacMini の画面で行う。** SSH 経由の非対話セッションでは
キーチェーンがロックされたままで `User interaction is not allowed` になり、しかも
`security` は**エラーを返さず成功したように見える**ので気づきにくい
(`security find-generic-password` で引けないことで発覚する)。

```bash
# MBA 側: 現在の値を表示する
security find-generic-password -s shintakane-tunnel-control-plane -a "$USER" -w

# ★ MacMini の画面のターミナルで: -w の値を省くとプロンプトで安全に入力できる
security add-generic-password -U -a k_sohara -s shintakane-tunnel-control-plane -w

# 登録できたか確認する。★ これも MacMini の画面で行う
security find-generic-password -s shintakane-tunnel-control-plane -a k_sohara -w | wc -c   # 165
```

> SSH 経由では**読み出しもできない** (空が返る)。SSH から「未登録」に見えても、
> 画面のターミナルで引ければ登録できている。LaunchAgent は GUI セッション配下で動くので読める。
>
> パスワード入力のプロンプト (`-w` の値を省いた形) はペーストが効いたか見えないので、
> 値を `-w '<値>'` で渡して目視確認する方が確実。

**`tunnel-client` を入れる** (cask ではないので sudo 不要)。

```bash
brew install openai/tools/tunnel-client
```

**プロファイルを MacMini 用に作る。** MBA の `~/.config/tunnel-client/shintakane-shikiho.yaml`
をそのまま持ち込まない。`command` が MBA のパス (Dropbox 配下・旧 `KS_DATA_DIR`) を指しており、
`api_key` が**平文**で入っているため。`api_key` は `env:` 参照にでき、`run_tunnel_client.sh` が
キーチェーンから読んで `CONTROL_PLANE_API_KEY` に渡す。

```yaml
config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: "tunnel_6a9415a6be7c8191904c5c66100d2681"
  api_key: "env:CONTROL_PLANE_API_KEY"        # 平文で書かない
health:
  listen_addr: "127.0.0.1:0"
admin_ui:
  open_browser: false
log:
  level: info
  format: json
mcp:
  commands:
    - channel: main
      command: "env KS_DATA_DIR=/Users/k_sohara/shintakane_data /Users/k_sohara/dev/shintakane/.venv/bin/python /Users/k_sohara/dev/shintakane/scripts/mcp/shikiho_server.py"
```

```bash
chmod 600 ~/.config/tunnel-client/shintakane-shikiho.yaml
```

runner と plist のインストールは `scripts/mcp/README.md` の手順どおり。起動後、
stdout ログに `🟢 tunnel-client started` と `tunnel metadata fetched` が出れば
API Key 認証まで通っている。

```bash
grep -E 'tunnel-client started|metadata fetched' ~/Library/Logs/shintakane-tunnel.stdout.log | tail -2
```

**移設後、MBA 側の tunnel は必ず止める。** 同じ `tunnel_id` を2台がポーリングすると、
ChatGPT のリクエストがどちらに届くか不定になり、MBA 側の古いデータが返ることがある。
`bootout` だけだと plist が残っていて次のログインで `RunAtLoad` により復活するので、
`disable` もする。

```bash
# MBA 側
launchctl bootout "gui/$(id -u)/com.k_sohara.shintakane-tunnel"
launchctl disable "gui/$(id -u)/com.k_sohara.shintakane-tunnel"
launchctl print-disabled "gui/$(id -u)" | grep shintakane      # cron と tunnel が disabled
```

## 7. 日常運用

| やること | コマンド |
|---|---|
| ログを見る | `tail -f ~/Library/Logs/shintakane/cron.stdout.log` (定刻実行分のみ) |
| 個別処理のログ | `~/dev/shintakane/logs/{shintakane,make_stock_db,theme_news,compact}.log` |
| 手動で日次バッチ | `cd ~/dev/shintakane && bash shintakane_cron.sh` |
| ブラウザから日次バッチ | `http://kosukemac-mini:5001/dev` の「今すぐ実行」(19時前でも走る。git pull はしない) |
| 最終実行の状態・日次バッチの出力 | `~/dev/shintakane/logs/cron_status.json` / `logs/cron.log` (定刻・手動・ブラウザ共通。`/dev` に表示) |
| WebApp を新コードで再起動 | `launchctl kickstart -k "gui/$(id -u)/com.k_sohara.shintakane.webapp"` |
| WebApp を開く (出先・スマホ) | `http://kosukemac-mini:5001/` (Tailnet 内のみ) |
| 開発機から稼働状況を見る | `deploy/macmini.sh status` |
| 開発機から正本データを調べる | `deploy/macmini.sh run python -c '...'` |
| 運用機と開発機のデータ件数を比べる | `deploy/macmini.sh counts` |

### MBA (開発機) での使い分け

| 用途 | URL | データ |
|---|---|---|
| 普段使い (閲覧・メモ・売買記録・レーティング) | `http://kosukemac-mini:5001/` (運用機) | 運用機の正本 |
| 開発 (コード修正の動作確認) | `http://localhost:5001/` (MBA) | MBA の開発用コピー |

**MBA の localhost でメモや売買記録を入力しない。** MBA のデータは運用機へ書き戻らないので、
入力は正本に反映されずに消える。MBA にも Tailscale を入れ、普段使いは運用機の URL を開く。

開発用コピーは `KS_DATA_DIR=~/shintakane_data_dev` (名前の末尾 `_dev` で判定する)。
このとき次のガードが効く (#453)。

- WebApp の全画面上部に赤い **DEV** 帯とデータ時点を出す
- Google Drive / Sheets へのアップロードをスキップする (古いデータで正本の出力を上書きしない)
- `shintakane_cron.sh` は即 exit 1 (theme-news の二重課金も防ぐ)

旧 `~/Ext/GoogleDrive/shintakane_data` (MBA 時代の正本) はアーカイブとして残す。
**Drive 上の `shintakane_data` フォルダは削除・移動・改名しない。** MBA の Drive はミラーなので、
ローカルで消すとクラウドからも消える。次の2つが参照している。

- `ir_docs/` — 運用機の `ir_docs` symlink の参照先、ChatGPT の Drive コネクタが PDF を読む経路
- `stock_ratings/stock_ratings.json` — 運用機の `stock_ratings_drive` symlink の参照先、ChatGPT の Drive コネクタが読む経路 (fileId 固定)

それ以外のサブフォルダ (`stock_data` 等) は 2026-09-26 時点のコピーで、運用機の安定稼働を
1〜2週間確認したら削除してよい (約 2GB)。それまでは運用機が壊れたときの戻り先として残す。

**開発用コピーの作り方 (初回のみ):**

```bash
mkdir ~/shintakane_data_dev
# ir_docs は Drive (MBA ではミラー) からローカルへコピーする。symlink にすると、
# 開発中の WebApp の資料収集が運用機と共有の index.json を書き換えてしまう
cp -Rp ~/Ext/GoogleDrive/shintakane_data/ir_docs ~/shintakane_data_dev/
KS_DATA_DIR=~/shintakane_data_dev deploy/macmini.sh pull-data
KS_DATA_DIR=~/shintakane_data_dev deploy/macmini.sh counts   # 件数が揃ったか確認
# .zshrc の KS_DATA_DIR と ~/.claude.json の shintakane-shikiho MCP の env を切り替える
```

開発で最新データが要るときだけ、運用機から取り寄せる (一方向)。

```bash
deploy/macmini.sh pull-data        # -n でドライラン
```

中身は次の rsync で、開発機の WebApp 起動中・運用機のバッチ実行中は拒否する。

```bash
rsync -a --exclude 'ir_docs' --exclude 'stock_ratings_drive' --exclude '*.lock' --exclude '*.dbm.lock' --exclude '.DS_Store' \
  macmini:/Users/k_sohara/shintakane_data/ "$KS_DATA_DIR"/
```

**`ir_docs` は必ず除外する。** 運用機側は Drive への symlink、MBA 側はローカルの実ディレクトリで、
除外しないと実ディレクトリを symlink で上書きしようとする。開発用の `ir_docs` は古くなるが、
新しい資料が要るときは `~/Ext/GoogleDrive/shintakane_data/ir_docs` (Drive 経由で運用機と同じもの)
から該当銘柄だけコピーする。

**自動で走るもの:**

- 平日19:00 に日次バッチ (`git pull --ff-only` → 分析 → DB更新 → exposure → theme-news)
- pull が成功したら WebApp を自動で kickstart (新しいコードを反映)
- **金曜のみ** バッチ末尾で `compact` (stocks_shelve が 100〜120MB/日 肥大するため)。事前の `backup` は取らない — `compact_shelve()` が swap 前に自前で退避を作り、成功後に消す・失敗時は残して次回を止める形で保護しており、`make_stock_db.py backup` は世代削除を持たないので週1で呼ぶと数百MBのコピーが永久に積み上がる

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

**日次バッチが `Errno 24: Too many open files` で落ちる**

macOS の既定 `ulimit -n` は 256 で、yfinance の週足バッチ (400銘柄超を並列取得) が
枯渇させる。`shintakane_cron.sh` が冒頭で 4096 に上げているので、まずそれが効いて
いるか見る。launchd は `.zshrc` を読まないため、シェルの設定では解決しない。

```bash
grep -n 'ulimit -n' ~/dev/shintakane/shintakane_cron.sh
ssh macmini 'ulimit -n'          # SSH 経由だと 256 のまま (これは正常)
```

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

## 10. データディレクトリの掃除

`KS_DATA_DIR` は自動では縮まない。手で掃除するときは次を守る (2026-08-30 の棚卸しで 2.68GB → 2.00GB)。

**削除してはいけないもの**

| パス | 理由 |
|---|---|
| `stocks_shelve.*` / `research_shelve.*` / `portfolio_shelve.*` / `market_db_shelve.*` | 本番DB |
| `research_shelve_YYMMDD.*` / `portfolio_shelve_YYMMDD.*` | 日次自動バックアップ。`BACKUP_GENERATIONS` でローテーション済みなので手動削除不要 |
| `disclosure/cache/` | 現役のキャッシュ |
| `yahoo/price/` | 株価キャッシュ (再取得コスト大) |
| `stock_data/stocks_pickle_back/` | shelve 移行前のレガシー pickle。**年1個を目安に残す** |

**消してよいもの**

- `stock_data/kabutan/{price,finance,base}` の180日超 — HTTP キャッシュで、必要なら次回実行時に再取得される。削除直後の1回だけバッチが数分延びる
- 過去の作業で手動作成したバックアップ (`*.bak_*` / `*.bak.bak_*` / `*.before_*`)

**手動バックアップを消すときの注意**: 本番の `portfolio_shelve.bak` と、ゴミの `portfolio_shelve.bak.bak_issue387_...` は接頭辞を共有する。
対象は「shelve 拡張子の後ろにさらに接尾辞が付くもの」に限り、候補に本番DBと日次バックアップが含まれないことを個別に確かめてから消す。

```bash
find "$KS_DATA_DIR/stock_data" -maxdepth 1 -type f \( -name "*.bak_*" -o -name "*.bak.bak_*" -o -name "*.before_*" \)
```
