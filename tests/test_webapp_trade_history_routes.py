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

    def test_two_tabs_present(self, client):
        """売買履歴タブとアクションログタブの両方が1ページに存在する (issue #387)。"""
        html = client.get("/trade-history").data.decode()
        assert 'data-tab="tab-fills"' in html
        assert 'data-tab="tab-actions"' in html
        assert 'id="tab-fills"' in html
        assert 'id="tab-actions"' in html

    def test_fill_does_not_override_action_log_price(self, app, client):
        """fill があってもアクションログ側は price_proxy のまま (突合撤去、issue #387)。

        6324 に実約定 fill (6,990/7,810) を足しても、アクションログタブのエピソードは
        proxy (5,678) を表示し続ける。fill による上書きは行われない。
        """
        with app.app_context():
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-10", side="buy", qty=300,
                                          price=6990.0, amount=-2097000, trade_kind="信用新規",
                                          dedup_key="th-buy"))
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-20", side="sell", qty=300,
                                          price=7810.0, amount=2343000, trade_kind="信用返済",
                                          dedup_key="th-sell"))

        html = client.get("/trade-history").data.decode()
        actions = html.split('id="tab-actions"')[1]
        # アクションログタブでは proxy が維持される
        assert "5,678" in actions

    def test_trade_fills_listed_in_fills_tab(self, app, client):
        """全 fill が売買履歴タブに一覧され、同日同side分割は加重平均で集約される (issue #387)。

        matched_seq の有無を問わず全 fill を表示する (突合廃止)。同日同side2件は加重平均単価・
        合計株数で1行に畳み「2件集約」バッジが出る。
        """
        with app.app_context():
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-15", side="buy", qty=100,
                                          price=1234.0, amount=-123400, trade_kind="現物",
                                          dedup_key="tf-a"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-18", side="sell", qty=50,
                                          price=1500.0, amount=75000, trade_kind="現物",
                                          dedup_key="tf-b1"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-18", side="sell", qty=50,
                                          price=1510.0, amount=75500, trade_kind="現物",
                                          dedup_key="tf-b2"))

        html = client.get("/trade-history").data.decode()
        fills_tab = html.split('id="tab-fills"')[1].split('id="tab-actions"')[0]
        assert "1,234" in fills_tab       # 単発 buy の単価
        assert "2件集約" in fills_tab      # 分割約定を集約したバッジ
        assert "1,505" in fills_tab       # 集約後の加重平均単価 (50@1500 + 50@1510)

    def test_different_trade_kind_not_aggregated(self, app, client):
        """同一銘柄・同日・同 side でも取引区分が違えば集約せず別行にする (PR #388 レビュー)。

        現物買 (@1000) と信用新規買 (@2000) を同日に足す。誤って集約されると加重平均で
        混ざるが、区分別に別行なら両単価がそのまま出る。
        """
        with app.app_context():
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-15", side="buy", qty=100,
                                          price=1000.0, amount=-100000, trade_kind="現物",
                                          dedup_key="tk-genbutsu"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-15", side="buy", qty=100,
                                          price=2000.0, amount=-200000, trade_kind="信用新規",
                                          dedup_key="tk-shinyo"))

        html = client.get("/trade-history").data.decode()
        fills_tab = html.split('id="tab-fills"')[1].split('id="tab-actions"')[0]
        assert "1,000" in fills_tab       # 現物の単価
        assert "2,000" in fills_tab       # 信用新規の単価 (加重平均 1,500 に潰れない)
        assert "1,500" not in fills_tab   # 混ぜた加重平均は出ない
        # 別区分なので集約バッジ (>N件集約<) は付かない (説明文の「N件集約」は除く)
        assert "件集約</span>" not in fills_tab

    def test_fills_tab_shown_without_episodes(self, tmp_path, monkeypatch):
        """action_log が空でも売買履歴タブに fill が表示される (issue #387)。"""
        portfolio_db = str(tmp_path / "pf_um")
        stocks_db = str(tmp_path / "st_um")
        research_db = str(tmp_path / "rs_um")
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("portfolio_shelve.DATA_DIR", str(tmp_path))
        ps.append_fill(ps.create_fill("6324", trade_date="2026-06-10", side="buy", qty=100,
                                      price=5000.0, amount=-500000, trade_kind="信用新規",
                                      dedup_key="tf-noep"), db_path=portfolio_db)
        app2 = create_app()
        app2.config["TESTING"] = True
        html = app2.test_client().get("/trade-history").data.decode()
        assert "アクションログがありません" in html
        assert "5,000" in html

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
        """action_log が空の場合はアクションログなしメッセージが表示される (issue #387)。"""
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
        assert "アクションログがありません" in app2.test_client().get("/trade-history").data.decode()


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
