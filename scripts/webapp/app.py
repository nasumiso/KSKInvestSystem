#!/usr/bin/env python3
"""
Webアプリ起動エントリポイント。

使い方:
    cd scripts && python -m webapp.app                      # デフォルト port 5001
    cd scripts && SHINTAKANE_PORT=5002 python -m webapp.app  # worktree 検証用 5002
"""

import os

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("SHINTAKANE_PORT", "5001"))
    app.run(debug=True, port=port)
