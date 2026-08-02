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
        """アクションログタブの種類・日付・株数・株価・理由ヘッダが表示される。

        振り返りメモ列はアクションログ側から撤去し売買履歴タブへ一本化した
        (issue #387 Phase2)。
        """
        html = client.get("/trade-history").data.decode()
        actions = html.split('id="tab-actions"')[1]
        assert "種類" in actions
        assert "日付" in actions
        assert "株数" in actions
        assert "株価" in actions
        assert "理由" in actions
        assert "<th>振り返りメモ</th>" not in actions  # アクションログの列は撤去

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

    def test_fill_episode_listed_in_fills_tab(self, app, client):
        """fill が建玉ラウンドのエピソードとして売買履歴タブに表示され、実現損益が出る (Phase4b)。

        現物買100@1234 → 売50@1500 + 売50@1510 で 1 現物ラウンドが閉じる。
        平均取得単価1234, 実現 = (1500-1234)*50 + (1510-1234)*50 = 13300 + 13800 = 27100。
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
        assert "+27,100円" in fills_tab   # 現物ラウンドの実現損益
        assert "1,234" in fills_tab       # 内訳展開の取得単価

    def test_genbutsu_and_shinyo_are_separate_episodes(self, app, client):
        """現物と信用は同一銘柄でも別エピソードになる (口座種別で分離、Phase4b)。"""
        with app.app_context():
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-15", side="buy", qty=100,
                                          price=1000.0, amount=-100000, trade_kind="現物",
                                          dedup_key="tk-genbutsu-b"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-20", side="sell", qty=100,
                                          price=1100.0, amount=110000, trade_kind="現物",
                                          dedup_key="tk-genbutsu-s"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-16", side="buy", qty=100,
                                          price=2000.0, amount=-200000, trade_kind="信用新規",
                                          dedup_key="tk-shinyo-b"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-21", side="sell", qty=100,
                                          price=2200.0, amount=220000, trade_kind="信用返済",
                                          tate_price=2000.0, dedup_key="tk-shinyo-s"))

        html = client.get("/trade-history").data.decode()
        fills_tab = html.split('id="tab-fills"')[1].split('id="tab-actions"')[0]
        # 現物ラウンド: (1100-1000)*100 = 10000、信用ラウンド: (2200-2000)*100 = 20000
        assert "+10,000円" in fills_tab
        assert "+20,000円" in fills_tab
        # 現物・信用の区分バッジが両方出る
        assert "現物" in fills_tab
        assert "信用" in fills_tab

    def test_fill_summary_on_fills_tab(self, app, client):
        """成績サマリー (勝率/ペイオフ) が売買履歴タブに表示される (Phase4b で fill 側へ一本化)。"""
        with app.app_context():
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-15", side="buy", qty=100,
                                          price=1000.0, amount=-100000, trade_kind="現物",
                                          dedup_key="sm-b"))
            ps.append_fill(ps.create_fill("3496", trade_date="2026-06-20", side="sell", qty=100,
                                          price=1200.0, amount=120000, trade_kind="現物",
                                          dedup_key="sm-s"))
        html = client.get("/trade-history").data.decode()
        fills_tab = html.split('id="tab-fills"')[1].split('id="tab-actions"')[0]
        assert "勝率" in fills_tab
        assert "ペイオフレシオ" in fills_tab

    def test_action_log_summary_removed(self, client):
        """旧 price_proxy 成績サマリーはアクションログタブから撤去された (Phase4b)。"""
        html = client.get("/trade-history").data.decode()
        actions = html.split('id="tab-actions"')[1]
        # 成績サマリー・実現損益は売買履歴タブへ一本化。アクションログ側は判断の記録のみ
        assert "終値プロキシによる概算" not in actions
        assert "一本化" in actions  # 導線の説明文

    def test_fills_tab_shown_without_episodes(self, tmp_path, monkeypatch):
        """action_log が空でも売買履歴タブに fill エピソードが表示される (issue #387)。"""
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
        assert "5,000" in html  # 保有中エピソードの内訳に建単価が出る

    def test_fill_episode_has_review_memo_cell(self, app, client):
        """売買履歴タブの fill エピソード展開に振り返りメモ入力欄が出る (Phase2)。"""
        with app.app_context():
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-10", side="buy", qty=300,
                                          price=6990.0, amount=-2097000, trade_kind="信用新規",
                                          dedup_key="rm-buy"))
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-20", side="sell", qty=300,
                                          price=7810.0, amount=2343000, trade_kind="信用返済",
                                          tate_price=6990.0, dedup_key="rm-sell"))
        html = client.get("/trade-history").data.decode()
        fills = html.split('id="tab-fills"')[1].split('id="tab-actions"')[0]
        assert "data-episode-key=" in fills
        assert 'save_fill_memo' not in fills  # url_for は解決済みのURLになる
        assert "/trade-history/fill-memo" in fills

    def test_save_fill_memo(self, app, client):
        """fill エピソードの振り返りメモをエピソードキーで保存・表示できる (Phase2)。"""
        with app.app_context():
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-10", side="buy", qty=300,
                                          price=6990.0, amount=-2097000, trade_kind="信用新規",
                                          dedup_key="fm-buy"))
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-20", side="sell", qty=300,
                                          price=7810.0, amount=2343000, trade_kind="信用返済",
                                          tate_price=6990.0, dedup_key="fm-sell"))
            from webapp.helpers import build_fill_episodes
            key = next(e["episode_key"] for e in build_fill_episodes() if e["code_s"] == "6324")

        resp = client.post(
            "/trade-history/fill-memo",
            data={"episode_key": key, "review_memo": "上値で薄く売り過ぎた"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        html = client.get("/trade-history").data.decode()
        assert "上値で薄く売り過ぎた" in html

    def test_save_fill_memo_empty_deletes(self, app, client):
        """空文字を送るとメモが削除される (Phase2)。"""
        with app.app_context():
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-10", side="buy", qty=300,
                                          price=6990.0, amount=-2097000, trade_kind="信用新規",
                                          dedup_key="fd-buy"))
            ps.append_fill(ps.create_fill("6324", trade_date="2026-06-20", side="sell", qty=300,
                                          price=7810.0, amount=2343000, trade_kind="信用返済",
                                          tate_price=6990.0, dedup_key="fd-sell"))
            from webapp.helpers import build_fill_episodes
            key = next(e["episode_key"] for e in build_fill_episodes() if e["code_s"] == "6324")
            ps.set_fill_memo(key, "消す前")
        client.post("/trade-history/fill-memo", data={"episode_key": key, "review_memo": ""})
        with app.app_context():
            assert ps.get_fill_memo(key) == ""

    def test_save_fill_memo_requires_key(self, client):
        """episode_key が空なら 400 (Phase2)。"""
        resp = client.post("/trade-history/fill-memo", data={"review_memo": "x"})
        assert resp.status_code == 400

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


class TestImportTradeCsv:
    """CSVアップロード取込 (issue #387 4a): 楽天/SBI 自動判定・原本コピー・不正弾き。"""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        portfolio_db = str(tmp_path / "portfolio")
        stocks_db = str(tmp_path / "stocks")
        research_db = str(tmp_path / "research")
        save_dir = tmp_path / "trade_history"
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", research_db)
        monkeypatch.setattr("portfolio_shelve.DATA_DIR", str(tmp_path))
        # 原本コピー先を tmp に差し替え (本番 trade_history を汚さない)
        monkeypatch.setattr("webapp.routes.trade_history.TRADE_HISTORY_DIR", str(save_dir))
        # 個別株を1件ウォッチリスト登録 (SBI ETF除外の対比用)
        rec = rs.create_research_record("6324", "ダイフク", overall_rating="B")
        rs.upsert_research_record(rec, db_path=research_db)
        app = create_app()
        app.config["TESTING"] = True
        return app, save_dir

    def _rakuten_csv(self):
        header = ",".join(["約定日"] + ["c%d" % i for i in range(1, 28)])
        # COL: 0約定日 2コード 6区分 7売買 10数量 11単価 16金額
        row = ["0"] * 28
        row[0], row[2], row[6], row[7], row[10], row[11], row[16] = \
            "2026/6/22", "6324", "現物", "買付", "100", "5678", "-567800"
        return (header + "\n" + ",".join(row) + "\n").encode("shift_jis")

    def _sbi_csv(self):
        lines = [
            "", "約定履歴照会 ", "",
            "約定日,銘柄,銘柄コード,市場,取引,期限,預り,課税,約定数量,約定単価,手数料/諸経費等,税額,受渡日,受渡金額/決済損益",
            '"2026/07/16","ダイフク","6324","東証",株式現物買,"--"," 特定 ","--",100,5678,"--","--","2026/07/21",-567800',
        ]
        return ("\n".join(lines) + "\n").encode("shift_jis")

    def test_rakuten_upload(self, env):
        import io
        app, save_dir = env
        client = app.test_client()
        data = {"csv_file": (io.BytesIO(self._rakuten_csv()), "tradehistory(JP)_20260622.csv")}
        resp = client.post("/trade-history/import", data=data,
                           content_type="multipart/form-data", follow_redirects=True)
        html = resp.data.decode()
        assert "楽天 CSV 取込完了" in html
        assert "新規 1 件" in html
        assert len(ps.list_fills("6324")) == 1
        # 原本が保存先へコピーされている
        assert (save_dir / "tradehistory(JP)_20260622.csv").exists()

    def test_sbi_upload(self, env):
        import io
        app, save_dir = env
        client = app.test_client()
        data = {"csv_file": (io.BytesIO(self._sbi_csv()), "SaveFile_0001.csv")}
        resp = client.post("/trade-history/import", data=data,
                           content_type="multipart/form-data", follow_redirects=True)
        html = resp.data.decode()
        assert "SBI CSV 取込完了" in html
        f = ps.list_fills("6324")
        assert len(f) == 1 and f[0]["broker"] == "SBI"
        assert (save_dir / "SaveFile_0001.csv").exists()

    def test_unknown_csv_rejected(self, env):
        import io
        app, save_dir = env
        client = app.test_client()
        data = {"csv_file": (io.BytesIO("foo,bar\n1,2\n".encode("shift_jis")), "unknown.csv")}
        resp = client.post("/trade-history/import", data=data,
                           content_type="multipart/form-data", follow_redirects=True)
        assert "認識できませんでした" in resp.data.decode()
        # 不正ファイルは保存先へコピーしない
        assert not (save_dir / "unknown.csv").exists()

    def test_no_file_selected(self, env):
        app, _ = env
        resp = app.test_client().post("/trade-history/import", data={},
                                      content_type="multipart/form-data",
                                      follow_redirects=True)
        assert "選択されていません" in resp.data.decode()
