"""
銘柄調査Webアプリ (Flask)。

research_shelve のデータをブラウザで閲覧・編集するためのWebアプリケーション。
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from flask import Flask

_JST = timezone(timedelta(hours=9))

# scripts/ を sys.path に追加して research_shelve 等をインポート可能にする
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def create_app() -> Flask:
    """Flask アプリケーションファクトリ。"""
    app = Flask(__name__)
    # 運用機 (SHINTAKANE_ENV=production) では既定値での起動を許さない。
    # Tailnet 限定とはいえ、既定のセッション鍵で公開するのは事故のもと。
    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        if os.environ.get("SHINTAKANE_ENV") == "production":
            raise RuntimeError(
                "FLASK_SECRET_KEY が未設定です。"
                "本番起動では ~/.shintakane_env に設定してください "
                "(生成: openssl rand -hex 32)"
            )
        secret = "dev-secret-key"
    app.config["SECRET_KEY"] = secret

    from webapp.routes.search import search_bp
    from webapp.routes.detail import detail_bp
    from webapp.routes.memo import memo_bp
    from webapp.routes.refresh import refresh_bp
    from webapp.routes.market import market_bp
    from webapp.routes.disclosure import disclosure_bp
    from webapp.routes.portfolio import portfolio_bp
    from webapp.routes.trade_history import trade_history_bp
    from webapp.routes.ir_docs import ir_docs_bp

    app.register_blueprint(search_bp)
    app.register_blueprint(detail_bp)
    app.register_blueprint(memo_bp)
    app.register_blueprint(refresh_bp)
    app.register_blueprint(market_bp)
    app.register_blueprint(disclosure_bp)
    app.register_blueprint(portfolio_bp)
    app.register_blueprint(trade_history_bp)
    app.register_blueprint(ir_docs_bp)

    # issue #165: /market テンプレートで theme-news markdown を HTML 化するフィルタ
    from webapp.helpers import theme_news_md_to_html
    app.jinja_env.filters["theme_news_md_to_html"] = theme_news_md_to_html

    @app.context_processor
    def _inject_today_jst():
        # issue #220: action_date input の value/max に使う実カレンダー上の JST 当日。
        # ks_util.get_price_day() は業務日 (17:00 前は前日) のため使わない。
        return {"today_jst": datetime.now(_JST).date().strftime("%Y-%m-%d")}

    return app
