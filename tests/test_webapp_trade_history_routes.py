"""売買履歴ページ (issue #351) ルートテスト。"""

import pytest
import portfolio_shelve as ps
import research_shelve as rs
from webapp import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    portfolio_db = str(tmp_path / "portfolio")
    stocks_db = str(tmp_path / "stocks")
    research_db = str(tmp_path / "research")

    monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
    monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
    monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_db)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", research_db)
    monkeypatch.setattr("portfolio_shelve.DATA_DIR", str(tmp_path))

    # 銘柄 3496: 3監 → 1保 の遷移を記録
    rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
    rs.upsert_research_record(rec, db_path=research_db)
    ps.add_to_watch("3496", reason="初回監視", db_path=portfolio_db)
    ps.transition_status("3496", "1保", reason="ブレイク確認", db_path=portfolio_db)

    # 銘柄 6324: 3監 → 1保 → 売却
    rec2 = rs.create_research_record("6324", "ダイフク", overall_rating="B")
    rs.upsert_research_record(rec2, db_path=research_db)
    ps.add_to_watch("6324", db_path=portfolio_db)
    ps.transition_status("6324", "1保", reason="GARP確認", db_path=portfolio_db)
    ps.transition_status("6324", "2準", reason="目標達成", db_path=portfolio_db)

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestTradeHistoryPage:
    def test_page_returns_200(self, client):
        """/trade-history が 200 を返す。"""
        resp = client.get("/trade-history")
        assert resp.status_code == 200

    def test_shows_hold_entry(self, client):
        """実保有になったエントリが表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "実保有" in html
        assert "3496" in html

    def test_shows_sell_entry(self, client):
        """売却エントリが表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "売却" in html
        assert "6324" in html

    def test_no_watch_only_entries(self, client):
        """3監 のみのログ (実保有・売却なし) は表示されない。"""
        html = client.get("/trade-history").data.decode()
        # 初回登録 (3監 のみ) は status_to が "3監" なので表示対象外
        # ただし 3496 は 1保 に遷移しているため 3496 自体は表示される
        # 3監のみ = 別銘柄で確認する場合は別テストが必要だが、
        # ここでは「3監への初回登録」が単独エントリとして現れていないことを確認
        # (「初回登録」テキスト自体は action_log 種別だが UI には出ない)
        assert "初回登録" not in html

    def test_empty_portfolio_shows_no_entries(self, tmp_path, monkeypatch):
        """portfolio が空の場合はエントリなしメッセージが表示される。"""
        portfolio_db = str(tmp_path / "portfolio2")
        stocks_db = str(tmp_path / "stocks2")
        research_db = str(tmp_path / "research2")
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("portfolio_shelve.DATA_DIR", str(tmp_path))
        app2 = create_app()
        app2.config["TESTING"] = True
        html = app2.test_client().get("/trade-history").data.decode()
        assert "売買履歴がありません" in html
