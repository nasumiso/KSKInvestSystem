"""売買履歴ページ (issue #351, #357) ルートテスト。"""

import pytest
from datetime import date, timedelta
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
    monkeypatch.setattr(
        "portfolio_shelve._fetch_price_proxy",
        lambda code_s, timestamp: {"3496": 1234, "6324": 5678}.get(code_s),
    )

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
        """種類・日付・株数・株価・理由・振り返りメモのヘッダが表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "種類" in html
        assert "日付" in html
        assert "株数" in html
        assert "株価" in html
        assert "理由" in html
        assert "振り返りメモ" in html

    def test_price_proxy_column_shown(self, client):
        """保有・売却イベント日の終値プロキシが株価列に表示される。"""
        html = client.get("/trade-history").data.decode()
        assert "1,234" in html
        assert "5,678" in html

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

    def test_post_sell_returns_are_displayed_and_saved(self, client, monkeypatch):
        """売却後5/20営業日騰落率を表示時に確定・保存する。"""
        sell_log = next(l for l in ps.list_action_logs("6324") if l["action_type"] == "売却")
        sell_day = date.fromisoformat(sell_log["timestamp"][:10])
        log = [(sell_day, 5678)] + [
            (sell_day + timedelta(days=offset), 5678 + offset * 10)
            for offset in range(1, 22)
        ]
        monkeypatch.setattr("webapp.routes.trade_history._bulk_price_logs", lambda codes: {"6324": log})

        html = client.get("/trade-history").data.decode()

        assert "後5:" in html
        assert "後20:" in html
        saved = next(l for l in ps.list_action_logs("6324") if l["action_type"] == "売却")
        assert saved["post_sell_returns"].keys() == {"5d", "20d"}

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
        """株数増加は「買増」サブ行として表示される。"""
        ps.update_qty("3496", 50)
        html = client.get("/trade-history").data.decode()
        assert "買増" in html

    def test_qty_decrease_subrow(self, client):
        """株数減少は「一部売却」サブ行として表示される。"""
        ps.update_qty("3496", 100)
        ps.update_qty("3496", 50)
        html = client.get("/trade-history").data.decode()
        assert "一部売却" in html

    def test_rowspan_present_when_qty_changes(self, client):
        """株数変更があるエピソードは銘柄セルに rowspan="2" が付く（保有+株数変更=2行）。"""
        ps.update_qty("3496", 50)
        html = client.get("/trade-history").data.decode()
        assert 'rowspan="2"' in html  # 保有+株数変更=2行

    def test_qty_in_row_shows_initial_value(self, client):
        """株数変更ログがあれば保有行に最初の変更前株数（→左辺）が表示される。"""
        ps.update_qty("3496", 100)  # 0 → 100 → 左辺 "0" がIN時株数
        html = client.get("/trade-history").data.decode()
        assert "0" in html  # 保有行の株数列に変更前株数が出る

    def test_qty_change_while_holding_generates_action_log(self, app):
        """1保のまま株数変更すると株数変更ログが残る（メモ引数ありで変更メモも保存）。"""
        with app.app_context():
            ps.update_qty("3496", 200, reason="買い増し")
        logs = ps.list_action_logs("3496")
        qty_log = next((l for l in logs if l["action_type"] == "株数変更"), None)
        assert qty_log is not None
        assert "200" in qty_log["reason"]
        assert "買い増し" in qty_log["reason"]

    def test_sell_inherits_hold_review_memo(self, app):
        """保有中に入力した振り返りメモは売却ログに引き継がれる。"""
        with app.app_context():
            # 3496: 1保ログに review_memo を保存
            logs = ps.list_action_logs("3496")
            hold_log = next(l for l in logs if l.get("status_to") == "1保")
            ps.update_action_log_review_memo("3496", hold_log["seq"], "保有中メモ")
            # 売却
            ps.transition_status("3496", "2準", reason="利確")
        sell_log = next(l for l in ps.list_action_logs("3496") if l["action_type"] == "売却")
        assert sell_log["review_memo"] == "保有中メモ"

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


class TestLastActionDate:
    """エピソードの表示順キー: 最新アクション日 (保有/株数変更/売却の最大日付)。"""

    @pytest.mark.parametrize(
        "ep,expected",
        [
            # 売却済み: 売却日が最新
            ({"hold_date": "2026-06-01", "qty_changes": [], "sell_date": "2026-07-02"},
             "2026-07-02"),
            # 保有中で一部売却あり: 株数変更日が最新
            ({"hold_date": "2026-06-01",
              "qty_changes": [{"date": "2026-07-02"}], "sell_date": ""},
             "2026-07-02"),
            # 遡り入力で株数変更日が売却日より後: max が勝つ
            ({"hold_date": "2026-06-01",
              "qty_changes": [{"date": "2026-07-05"}], "sell_date": "2026-07-02"},
             "2026-07-05"),
            # アクションなし: 保有日
            ({"hold_date": "2026-06-01", "qty_changes": [], "sell_date": ""},
             "2026-06-01"),
        ],
    )
    def test_last_action_date(self, ep, expected):
        from webapp.routes.trade_history import _last_action_date
        assert _last_action_date(ep) == expected
