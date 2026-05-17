"""
銘柄調査Webアプリ (Flask)。

research_shelve のデータをブラウザで閲覧・編集するためのWebアプリケーション。
"""

import os
import sys

from flask import Flask

# scripts/ を sys.path に追加して research_shelve 等をインポート可能にする
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def create_app() -> Flask:
    """Flask アプリケーションファクトリ。"""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

    from webapp.routes.search import search_bp
    from webapp.routes.detail import detail_bp
    from webapp.routes.memo import memo_bp
    from webapp.routes.refresh import refresh_bp
    from webapp.routes.market import market_bp
    from webapp.routes.disclosure import disclosure_bp
    from webapp.routes.portfolio import portfolio_bp

    app.register_blueprint(search_bp)
    app.register_blueprint(detail_bp)
    app.register_blueprint(memo_bp)
    app.register_blueprint(refresh_bp)
    app.register_blueprint(market_bp)
    app.register_blueprint(disclosure_bp)
    app.register_blueprint(portfolio_bp)

    return app
