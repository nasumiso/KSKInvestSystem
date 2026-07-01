"""銘柄詳細ページの振り返りセクション (issue #172) ユニットテスト。"""

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

    rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
    rs.upsert_research_record(rec, db_path=research_db)

    ps.add_to_watch("3496", reason="初回監視", db_path=portfolio_db)
    ps.transition_status("3496", "1保", reason="ブレイク確認", db_path=portfolio_db)

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestDetailActionLog:
    def test_action_log_section_shown(self, client):
        """portfolio 登録済み銘柄は振り返りセクションが表示される。"""
        assert "振り返り" in client.get("/stock/3496").data.decode()

    @pytest.mark.parametrize("text", ["初回登録", "ステータス変更", "1保"])
    def test_log_entries_shown(self, client, text):
        """アクションログのエントリ内容が表示される。"""
        assert text in client.get("/stock/3496").data.decode()

    def test_unregistered_stock_no_section(self, tmp_path, monkeypatch):
        """portfolio 未登録銘柄は振り返りセクションが非表示。"""
        research_db = str(tmp_path / "research2")
        portfolio_db = str(tmp_path / "portfolio2")
        stocks_db = str(tmp_path / "stocks2")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("portfolio_shelve.DATA_DIR", str(tmp_path))
        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=research_db)
        app2 = create_app()
        app2.config["TESTING"] = True
        assert "振り返り" not in app2.test_client().get("/stock/3496").data.decode()
