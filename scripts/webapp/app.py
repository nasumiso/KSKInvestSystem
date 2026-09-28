#!/usr/bin/env python3
"""
Webアプリ起動エントリポイント。

使い方:
    cd scripts && python -m webapp.app                      # デフォルト port 5001
    cd scripts && SHINTAKANE_PORT=5002 python -m webapp.app  # worktree 検証用 5002

運用機 (MacMini) では SHINTAKANE_ENV=production で起動し、Flask の
開発デバッガを無効にする (deploy/run_webapp.sh が設定する)。
"""

import os

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("SHINTAKANE_PORT", "5001"))
    # 既定は開発モード。運用機だけが production を明示する。
    debug = os.environ.get("SHINTAKANE_ENV") != "production"
    # 省略時と同値だが、Tailscale Serve 経由で公開する前提 (0.0.0.0 にはしない)
    # をコードに残すため明示する。
    app.run(debug=debug, host="127.0.0.1", port=port)
