#!/usr/bin/env python3
"""
Webアプリ起動エントリポイント。

使い方:
    cd scripts && python -m webapp.app
"""

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
