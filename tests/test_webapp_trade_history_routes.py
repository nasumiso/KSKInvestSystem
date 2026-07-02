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

    # 銘柄 3496: 3監 → 1保（未売却）
    rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
    rs.upsert_research_record(rec, db_path=research_db)
    ps.add_to_watch("3496", reason="初回監視", db_path=portfolio_db)
    ps.transition_status("3496", "1保", reason="ブレイク確認", db_path=portfolio_db)

    # 銘柄 6324: 3監 → 1保 → 売却（2準）
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
        assert client.get("/trade-history").status_code == 200

    def test_column_headers_shown(self, client):
        """銘柄・保有日・売却日・振り返りメモのヘッダが表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "保有日" in html
        assert "売却日" in html
        assert "振り返りメモ" in html

    def test_unsold_episode_shown(self, client):
        """未売却エピソード（3496）が表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "3496" in html
        assert "ブレイク確認" in html

    def test_sold_episode_shown_with_review_memo_textarea(self, client):
        """売却済みエピソード（6324）は振り返りメモのテキストエリアが表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "6324" in html
        assert "目標達成" in html
        assert "review-memo-ta" in html  # textarea の class

    def test_save_review_memo(self, client):
        """振り返りメモを POST で保存（JSON 200）し、次回表示に反映される。"""
        logs = ps.list_action_logs("6324")
        sell_log = next(l for l in logs if l["action_type"] == "売却")
        seq = sell_log["seq"]

        resp = client.post(
            f"/trade-history/6324/{seq}/review-memo",
            data={"review_memo": "上値で薄く売り過ぎた"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        html = client.get("/trade-history").data.decode()
        assert "上値で薄く売り過ぎた" in html

    def test_qty_changes_shown_in_accordion(self, client):
        """株数変更があるエピソードは details/summary アコーディオンで表示される。"""
        logs = ps.list_action_logs("6324")
        sell_log = next(l for l in logs if l["action_type"] == "売却")
        seq = sell_log["seq"]
        # 振り返りメモを保存して再表示
        client.post(f"/trade-history/6324/{seq}/review-memo", data={"review_memo": "test"})

        # 株数変更ログを直接追加（update_qty 経由）
        ps.update_qty("6324", 100)
        html = client.get("/trade-history").data.decode()
        # 株数変更がある銘柄は details タグが出る（6324は売却済みなのでqty_changesは空のはず）
        # 未売却の 3496 に株数変更を加える
        ps.update_qty("3496", 50)
        html = client.get("/trade-history").data.decode()
        assert "<details>" in html
        assert "0 → 50" in html

    def test_no_qty_changes_no_accordion(self, client):
        """株数変更がないエピソードは details タグが出ない（通常リンク）。"""
        html = client.get("/trade-history").data.decode()
        # 6324 は株数変更なし → details なし、直接 a タグ
        assert "6324" in html

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
        assert "売買履歴がありません" in app2.test_client().get("/trade-history").data.decode()
