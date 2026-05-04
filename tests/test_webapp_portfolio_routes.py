"""webapp /portfolio ルートのユニットテスト (Phase 3b / issue #171)。

portfolio_shelve / stocks_shelve / my_watch_list.txt を tmp_path に差し替えて
Flask テストクライアントで各エンドポイントを叩く。
"""

import os

import pytest

import portfolio_shelve as ps
from db_shelve import ShelveDB, STOCKS_SHELVE
from webapp import create_app


@pytest.fixture
def portfolio_db_path(tmp_path):
    return str(tmp_path / "test_portfolio_shelve")


@pytest.fixture
def stocks_db_path(tmp_path):
    return str(tmp_path / "test_stocks_shelve")


@pytest.fixture
def txt_path(tmp_path):
    """sync_to_my_watch_list_txt の出力先を tmp_path に逃がす"""
    return str(tmp_path / "my_watch_list.txt")


@pytest.fixture
def app(portfolio_db_path, stocks_db_path, txt_path, monkeypatch):
    """テスト用 Flask アプリ。"""
    # portfolio_shelve のパス差し替え
    monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db_path)
    monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db_path)
    # stocks_shelve のパス差し替え
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db_path)
    monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db_path)
    # my_watch_list.txt の出力先を tmp_path に
    monkeypatch.setattr("portfolio_shelve.DATA_DIR", os.path.dirname(txt_path))

    # 銘柄を 3 件 portfolio_shelve に登録 (各タブに 1 件ずつ)。
    # 銘柄名は portfolio_shelve には保存されず、表示時に stocks_shelve から引かれる。
    ps.add_to_watch("6324", reason="テスト 3監", db_path=portfolio_db_path)
    ps.add_to_watch("3496", reason="テスト 1保 用", db_path=portfolio_db_path)
    ps.transition_status("3496", "1保", reason="テスト 1保 へ昇格 (3監→1保)", db_path=portfolio_db_path)
    ps.add_to_watch("7203", reason="テスト 2準 用", db_path=portfolio_db_path)
    ps.transition_status("7203", "2準", reason="テスト 2準 へ昇格 (3監→2準)", db_path=portfolio_db_path)

    # stocks_shelve にダミーデータ
    with ShelveDB(stocks_db_path) as db:
        db["6324"] = {
            "code_s": "6324", "stock_name": "ハーモニックドライブシステムズ",
            "shihyo": {"PER": 308.0, "dividend_yield": 0.39, "jikasogaku": 4960.0},
            "market_cap": 4705.0, "rs_raw": 1.63, "trend_template": [],
            "stock_rank_log": [("2026-05-03", 612)],
            "price": 5150, "rironkabuka": 659,
        }
        db["3496"] = {
            "code_s": "3496", "stock_name": "アズーム",
            "shihyo": {"PER": 30.0, "dividend_yield": 1.5},
            "market_cap": 100.0, "rs_raw": 2.5, "trend_template": [],
            "stock_rank_log": [("2026-05-03", 50)],
            "new_high": ["新"],
        }
        db["7203"] = {
            "code_s": "7203", "stock_name": "トヨタ自動車",
            "shihyo": {"PER": 12.0, "dividend_yield": 2.5},
            "market_cap": 30000.0, "rs_raw": 0.95,
            "trend_template": ["不通過1", "不通過2", "不通過3"],
            "stock_rank_log": [("2026-05-03", 200)],
        }

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestDashboardGet:
    """GET /portfolio タブ表示"""

    def test_dashboard_default_tab_is_hold(self, client):
        resp = client.get("/portfolio")
        assert resp.status_code == 200
        html = resp.data.decode()
        # 1保 (アズーム) が表示される
        assert "アズーム" in html
        # 2準 / 3監 の銘柄は表示されない (タブで絞り込み)
        assert "ハーモニック" not in html
        assert "トヨタ" not in html

    def test_dashboard_watch_tab(self, client):
        resp = client.get("/portfolio?status=watch")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "ハーモニック" in html
        assert "アズーム" not in html

    def test_dashboard_semi_tab(self, client):
        resp = client.get("/portfolio?status=semi")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "トヨタ" in html
        assert "アズーム" not in html

    def test_dashboard_unknown_status_falls_back_to_hold(self, client):
        resp = client.get("/portfolio?status=invalid")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "アズーム" in html

    def test_dashboard_shows_tab_counts(self, client):
        resp = client.get("/portfolio")
        html = resp.data.decode()
        # 各タブの件数が出ている (3 件登録: 1 / 1 / 1)
        # タブ表示の数字を全て検証するのは見栄えに依存するので最低限件数行を確認
        assert "1 件" in html or "(1)" in html or ">1<" in html

    def test_dashboard_shows_indicators(self, client):
        """1保 タブで PER / RS / 順位等の指標が表示される"""
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        # アズームの指標
        assert "30.0" in html  # PER
        assert "2.50" in html  # RS
        assert "50" in html    # rank


class TestAddPost:
    """POST /portfolio/add"""

    def test_add_new_code_succeeds(self, client, portfolio_db_path, stocks_db_path):
        # stocks_shelve に新銘柄登録 (銘柄名は表示時に他DBから引かれる)
        with ShelveDB(stocks_db_path) as db:
            db["8035"] = {"code_s": "8035", "stock_name": "東京エレクトロン"}

        resp = client.post("/portfolio/add", data={"code_s": "8035"})
        assert resp.status_code == 302
        # shelve に登録されている (banner 名は持たない)
        rec = ps.get_record("8035", db_path=portfolio_db_path)
        assert rec is not None
        assert rec["status"] == "3監"
        assert "stock_name" not in rec  # 新スキーマでは保存しない
        # 初回登録ログが記録されている
        logs = ps.list_action_logs(code_s="8035", db_path=portfolio_db_path)
        assert any(log.get("action_type") == "初回登録" for log in logs)

    def test_add_existing_code_flash_warning(self, client, portfolio_db_path):
        # 6324 は既に登録済み (fixture)
        resp = client.post("/portfolio/add", data={"code_s": "6324"})
        assert resp.status_code == 302
        # shelve のレコードは変化なし
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec is not None
        assert rec["status"] == "3監"  # 既存のまま

    def test_add_invalid_code_flash_error(self, client, portfolio_db_path):
        before = len(ps.list_records(db_path=portfolio_db_path))
        resp = client.post("/portfolio/add", data={"code_s": "abc"})
        assert resp.status_code == 302
        # shelve のレコード数は変化なし
        assert len(ps.list_records(db_path=portfolio_db_path)) == before

    def test_add_empty_code_flash_error(self, client, portfolio_db_path):
        resp = client.post("/portfolio/add", data={"code_s": ""})
        assert resp.status_code == 302


class TestTransitionPost:
    """POST /portfolio/<code_s>/transition"""

    def test_transition_1ho_to_3kan(self, client, portfolio_db_path):
        # 3496 は 1保 (fixture)
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "3監", "reason": "格下げテスト"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("3496", db_path=portfolio_db_path)
        assert rec["status"] == "3監"
        # ステータス変更 ログ
        logs = ps.list_action_logs(code_s="3496", db_path=portfolio_db_path)
        assert any(
            log.get("action_type") == "ステータス変更"
            and log.get("status_from") == "1保"
            and log.get("status_to") == "3監"
            for log in logs
        )

    def test_transition_1ho_to_2jun_records_uri_log(self, client, portfolio_db_path):
        """1保 → 2準 は売却扱いで action_type=売却 のログが記録される"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "2準", "reason": "利確"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("3496", db_path=portfolio_db_path)
        assert rec["status"] == "2準"
        logs = ps.list_action_logs(code_s="3496", db_path=portfolio_db_path)
        assert any(
            log.get("action_type") == "売却"
            and log.get("status_from") == "1保"
            and log.get("status_to") == "2準"
            for log in logs
        )

    def test_transition_same_status_is_noop(self, client, portfolio_db_path):
        """同一ステータス遷移は no-op (Phase 3a 仕様)、ログ追記なし"""
        before_logs = ps.list_action_logs(code_s="3496", db_path=portfolio_db_path)
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "1保"},
        )
        assert resp.status_code == 302
        after_logs = ps.list_action_logs(code_s="3496", db_path=portfolio_db_path)
        # ログ件数は変化なし
        assert len(after_logs) == len(before_logs)

    def test_transition_disallowed_flash_error(self, client, portfolio_db_path):
        """許可されない遷移先 (例: 1保 → 1保 は no-op だが、不正値はエラー)"""
        # 不正な値を送る
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "INVALID"},
        )
        assert resp.status_code == 302
        # shelve は変化なし
        rec = ps.get_record("3496", db_path=portfolio_db_path)
        assert rec["status"] == "1保"

    def test_transition_unknown_code_flash_error(self, client, portfolio_db_path):
        """portfolio_shelve に未登録の銘柄に対する遷移は KeyError → flash"""
        resp = client.post(
            "/portfolio/9999/transition",
            data={"new_status": "1保"},
        )
        assert resp.status_code == 302
        # 9999 は登録されていない
        assert ps.get_record("9999", db_path=portfolio_db_path) is None


class TestDeletePost:
    """POST /portfolio/<code_s>/delete"""

    def test_delete_3kan_with_reason_succeeds(self, client, portfolio_db_path):
        # 6324 は 3監 (fixture)
        resp = client.post(
            "/portfolio/6324/delete",
            data={"reason": "監視終了"},
        )
        assert resp.status_code == 302
        # レコードは物理削除
        assert ps.get_record("6324", db_path=portfolio_db_path) is None
        # 削除アクションログは残る
        logs = ps.list_action_logs(code_s="6324", db_path=portfolio_db_path)
        assert any(log.get("action_type") == "削除" for log in logs)

    def test_delete_1ho_flash_error(self, client, portfolio_db_path):
        """1保 銘柄を削除しようとすると ValueError → flash で reject"""
        resp = client.post(
            "/portfolio/3496/delete",
            data={"reason": "誤操作テスト"},
        )
        assert resp.status_code == 302
        # レコードは残る
        rec = ps.get_record("3496", db_path=portfolio_db_path)
        assert rec is not None
        assert rec["status"] == "1保"

    def test_delete_2jun_flash_error(self, client, portfolio_db_path):
        """2準 銘柄を削除しようとすると ValueError → flash で reject"""
        resp = client.post(
            "/portfolio/7203/delete",
            data={"reason": "誤操作テスト"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("7203", db_path=portfolio_db_path)
        assert rec is not None
        assert rec["status"] == "2準"

    def test_delete_empty_reason_flash_error(self, client, portfolio_db_path):
        """理由が空の削除は flash エラー"""
        resp = client.post(
            "/portfolio/6324/delete",
            data={"reason": ""},
        )
        assert resp.status_code == 302
        # レコードは残る
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec is not None

    def test_delete_unknown_code_flash_error(self, client, portfolio_db_path):
        """未登録銘柄に対する削除は False 返却 → flash"""
        resp = client.post(
            "/portfolio/9999/delete",
            data={"reason": "テスト"},
        )
        assert resp.status_code == 302
        # 9999 はもとから未登録
        assert ps.get_record("9999", db_path=portfolio_db_path) is None
