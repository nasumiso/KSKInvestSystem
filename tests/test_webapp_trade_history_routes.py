"""売買履歴ページ (issue #351, #357) ルートテスト。"""

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
        """種類・日付・株数・理由・振り返りメモのヘッダが表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "種類" in html
        assert "日付" in html
        assert "株数" in html
        assert "理由" in html
        assert "振り返りメモ" in html

    def test_unsold_episode_shown(self, client):
        """未売却エピソード（3496）が保有サブ行で表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "3496" in html
        assert "ブレイク確認" in html
        assert "保有" in html

    def test_sold_episode_subrows(self, client):
        """売却済みエピソード（6324）は保有・売却の2サブ行が出る。"""
        html = client.get("/trade-history").data.decode()
        assert "6324" in html
        assert "GARP確認" in html
        assert "目標達成" in html
        assert "売却" in html

    def test_all_episodes_have_review_memo_textarea(self, client):
        """売却済み・未売却どちらのエピソードにも textarea が表示される。"""
        html = client.get("/trade-history").data.decode()
        # 3496（未売却）と 6324（売却済み）の両方で data-url が出る（class は JS内にも1つ）
        assert html.count("data-url=") == 2

    def test_save_review_memo_sold(self, client):
        """売却済みエピソード（6324）の振り返りメモを POST で保存できる。"""
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

    def test_save_review_memo_unsold(self, client):
        """未売却エピソード（3496）の振り返りメモを POST で保存できる。"""
        logs = ps.list_action_logs("3496")
        hold_log = next(l for l in logs if l.get("status_to") == "1保")
        seq = hold_log["seq"]

        resp = client.post(
            f"/trade-history/3496/{seq}/review-memo",
            data={"review_memo": "保有中メモ"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        html = client.get("/trade-history").data.decode()
        assert "保有中メモ" in html

    def test_qty_changes_subrow(self, client):
        """株数変更があるエピソードは「株数変更」サブ行が出る。"""
        ps.update_qty("3496", 50)
        html = client.get("/trade-history").data.decode()
        assert "株数変更" in html

    def test_rowspan_present_when_qty_changes(self, client):
        """株数変更があるエピソードは銘柄セルに rowspan="2" が付く（保有+株数変更=2行）。"""
        ps.update_qty("3496", 50)
        html = client.get("/trade-history").data.decode()
        assert 'rowspan="2"' in html  # 保有+株数変更=2行

    def test_qty_in_row_shows_after_value(self, client):
        """保有時に 0→N の株数変更ログがあれば保有行に N が表示される。"""
        ps.update_qty("3496", 100)  # 0 → 100 (左辺が0)
        html = client.get("/trade-history").data.decode()
        assert "100" in html  # 保有行の株数列に変更後株数が出る

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
