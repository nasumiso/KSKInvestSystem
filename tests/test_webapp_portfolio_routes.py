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
            "market_cap": 4705.0, "momentum_pt": 90, "trend_template": [],
            "stock_rank_log": [("2026-05-03", 612)],
            "price": 5150, "rironkabuka": 659,
        }
        db["3496"] = {
            "code_s": "3496", "stock_name": "アズーム",
            "shihyo": {"PER": 30.0, "dividend_yield": 1.5},
            "market_cap": 100.0, "momentum_pt": 85, "trend_template": [],
            "stock_rank_log": [("2026-05-03", 50)],
            "new_high": ["新"],
            # 年度成長率テスト用 (calc_annual_growth は tbl[-2], tbl[-3] を比較)
            # gyoseki_current[i] = [年度, 売上, 営業益, 経常益, 純利益, EPS, BPS]
            # -3=2023, -2=2024 (基準), -1=2025予 → 売上 50→100 (+100%), 営利 5→10 (+100%)
            "gyoseki_current": [
                ["2023.03", 50, 5, 5, 3, 0, 0],
                ["2024.03", 100, 10, 11, 6, 0, 0],
                ["2025.03", 138, 22, 22, 12, 0, 0],
            ],
        }
        db["7203"] = {
            "code_s": "7203", "stock_name": "トヨタ自動車",
            "shihyo": {"PER": 12.0, "dividend_yield": 2.5},
            "market_cap": 30000.0, "momentum_pt": 40,
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
        """1保 タブで PER / モメンタム / 順位等の指標が表示される"""
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        # アズームの指標 (PER は二桁なので整数表記: 30)
        assert ">30<" in html  # PER (二桁以上は整数)
        assert ">85<" in html  # モメンタム
        assert "50" in html    # rank
        # 売上成長%・利益成長% (アズーム fixture の gyoseki_current から計算: 50→100 は +100%)
        # 値の "%" は列ヘッダ側 ("利益成長(%)") に集約 (issue #177)
        assert ">100<" in html


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


class TestBulkExclude:
    """POST /portfolio/bulk-exclude (issue #186)"""

    def test_bulk_exclude_single_3kan(self, client, portfolio_db_path):
        resp = client.post("/portfolio/bulk-exclude", data={"codes": "6324"})
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec is not None
        assert rec["excluded"] is True
        # アクションログに「ユニバース除外」
        logs = ps.list_action_logs("6324", db_path=portfolio_db_path)
        assert any(log.get("action_type") == "ユニバース除外" for log in logs)

    def test_bulk_exclude_multiple(self, client, portfolio_db_path):
        # 追加で 3監 をもう 1 件登録
        ps.add_to_watch("4377", reason="テスト追加", db_path=portfolio_db_path)
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": ["6324", "4377"]},
        )
        assert resp.status_code == 302
        for code in ("6324", "4377"):
            rec = ps.get_record(code, db_path=portfolio_db_path)
            assert rec is not None
            assert rec["excluded"] is True

    def test_bulk_exclude_1ho_mixed_partial_success(self, client, portfolio_db_path):
        """3監 + 1保 混入時、3監 のみ除外され 1保 はそのまま残る"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": ["6324", "3496"]},
        )
        assert resp.status_code == 302
        # 3監 6324 は除外
        rec_watch = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec_watch["excluded"] is True
        # 1保 3496 は除外されない
        rec_hold = ps.get_record("3496", db_path=portfolio_db_path)
        assert rec_hold["excluded"] is False
        assert rec_hold["status"] == "1保"

    def test_bulk_exclude_2jun_allowed(self, client, portfolio_db_path):
        """2準 銘柄もユニバース除外可能"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": "7203"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("7203", db_path=portfolio_db_path)
        assert rec is not None
        assert rec["excluded"] is True
        assert rec["status"] == "2準"  # status は 2準 のまま、excluded フラグのみ立つ
        logs = ps.list_action_logs("7203", db_path=portfolio_db_path)
        assert any(log.get("action_type") == "ユニバース除外" for log in logs)

    def test_bulk_exclude_2jun_3kan_mixed(self, client, portfolio_db_path):
        """2準 と 3監 を混ぜても両方除外される"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": ["6324", "7203"]},
        )
        assert resp.status_code == 302
        for code in ("6324", "7203"):
            rec = ps.get_record(code, db_path=portfolio_db_path)
            assert rec is not None
            assert rec["excluded"] is True

    def test_bulk_exclude_redirects_to_return_query(self, client, portfolio_db_path):
        """return_query=status=hold,semi&sort=rank なら同じクエリにリダイレクト (issue #178)"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": "7203", "return_query": "status=hold,semi&sort=rank"},
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "status=hold,semi" in loc
        assert "sort=rank" in loc

    def test_bulk_exclude_empty_return_query_falls_back_to_default(self, client, portfolio_db_path):
        """return_query 未指定はデフォルト ?status=hold にリダイレクト (issue #178)"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": "6324"},
        )
        assert resp.status_code == 302
        assert "status=hold" in resp.headers["Location"]

    def test_bulk_exclude_empty_codes_flash_error(self, client, portfolio_db_path):
        resp = client.post("/portfolio/bulk-exclude", data={})
        assert resp.status_code == 302
        # 6324 は除外されない
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["excluded"] is False

    def test_bulk_exclude_unknown_code_no_change(self, client, portfolio_db_path):
        """未登録コードのみ送信された場合、何も変更されない"""
        resp = client.post("/portfolio/bulk-exclude", data={"codes": "9999"})
        assert resp.status_code == 302
        assert ps.get_record("9999", db_path=portfolio_db_path) is None
        # 既存の 6324 は影響なし
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["excluded"] is False

    def test_bulk_exclude_invalid_code_in_list(self, client, portfolio_db_path):
        """不正なコードが混じっても他のコードは正常に処理される"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": ["INVALID!", "6324"]},
        )
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["excluded"] is True


class TestExcludedHidden:
    """除外済みレコードはダッシュボードから消える (issue #186)"""

    def test_excluded_record_not_in_watch_tab(self, client, portfolio_db_path):
        ps.exclude_from_universe("6324", db_path=portfolio_db_path)
        resp = client.get("/portfolio?status=watch")
        assert resp.status_code == 200
        # データテーブル行に 6324 が出ない (data-code 属性で判定)
        assert b'data-code="6324"' not in resp.data

    def test_excluded_record_not_in_any_tab(self, client, portfolio_db_path):
        # 1保 を除外できないので一旦 3監 へ戻して除外
        ps.transition_status("3496", "2準", db_path=portfolio_db_path)
        ps.transition_status("3496", "3監", db_path=portfolio_db_path)
        ps.exclude_from_universe("3496", db_path=portfolio_db_path)
        for q in ("hold", "semi", "watch"):
            resp = client.get(f"/portfolio?status={q}")
            assert b'data-code="3496"' not in resp.data, f"3496 はタブ {q} で非表示のはず"


class TestAddRevival:
    """除外済みコードを再投入すると復活する (issue #186)"""

    def test_revive_via_add_post(self, client, portfolio_db_path):
        ps.exclude_from_universe("6324", db_path=portfolio_db_path)
        resp = client.post("/portfolio/add", data={"code_s": "6324"})
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["excluded"] is False
        # 「ユニバース除外」action_type の reason="復活" ログが追記される
        logs = ps.list_action_logs("6324", db_path=portfolio_db_path)
        revive_logs = [
            log for log in logs
            if log.get("action_type") == "ユニバース除外" and log.get("reason") == "復活"
        ]
        assert len(revive_logs) == 1

    def test_revive_does_not_require_stocks_shelve(
        self, client, portfolio_db_path, stocks_db_path, monkeypatch
    ):
        """除外済みレコードは stocks_shelve に登録が無くても復活できる"""
        ps.exclude_from_universe("6324", db_path=portfolio_db_path)
        # stocks_shelve 上の 6324 を削除して未登録状態にする
        with ShelveDB(stocks_db_path) as db:
            del db["6324"]
        resp = client.post("/portfolio/add", data={"code_s": "6324"})
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["excluded"] is False


class TestFallbackJudgmentWithAllExcluded:
    """全レコードが excluded=True でも fallback モードと誤判定されない (issue #186)"""

    def test_dashboard_not_in_fallback_when_all_excluded(self, client, portfolio_db_path):
        # 全レコードを除外可能な状態 (3監) にしてから除外
        ps.transition_status("3496", "2準", db_path=portfolio_db_path)
        ps.transition_status("3496", "3監", db_path=portfolio_db_path)
        ps.transition_status("7203", "3監", db_path=portfolio_db_path)
        for code in ("6324", "3496", "7203"):
            ps.exclude_from_universe(code, db_path=portfolio_db_path)
        # この状態で /portfolio を開いても fallback バナーが出ないこと
        resp = client.get("/portfolio?status=watch")
        assert resp.status_code == 200
        # fallback バナー文言の一部 ("portfolio_shelve 未移行" 等) が出ていない
        assert "portfolio_shelve 未移行".encode("utf-8") not in resp.data

    def test_revive_works_when_all_excluded(self, client, portfolio_db_path):
        # 全レコードを除外
        ps.transition_status("3496", "2準", db_path=portfolio_db_path)
        ps.transition_status("3496", "3監", db_path=portfolio_db_path)
        ps.transition_status("7203", "3監", db_path=portfolio_db_path)
        for code in ("6324", "3496", "7203"):
            ps.exclude_from_universe(code, db_path=portfolio_db_path)
        # この状態で復活が許可されること
        resp = client.post("/portfolio/add", data={"code_s": "6324"})
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["excluded"] is False


# ==================================================
# POST /portfolio/<code_s>/memo (issue #175 部分更新)
# ==================================================
class TestUpdateMemoPost:

    def test_memo_full_eight_fields_persist(self, client, portfolio_db_path):
        """ブラウザ form と同じく str 系の全項目を送ったら全部反映される"""
        # list 系フィールド (gyoutai_themes) は別 form name (gyoutai_themes_0/1) のため除外
        str_fields = ps.MEMO_FIELDS - ps.MEMO_LIST_FIELDS
        form_data = {field: f"val_{field}" for field in str_fields}
        resp = client.post("/portfolio/6324/memo", data=form_data)
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        for field, expected in form_data.items():
            assert rec["memo"][field] == expected
        # action_log に "メモ更新" が 1 件追加 (初回登録 + メモ更新 = 2 件)
        logs = ps.list_action_logs("6324", db_path=portfolio_db_path)
        assert len([log for log in logs if log["action_type"] == "メモ更新"]) == 1

    def test_memo_partial_three_fields_keeps_others(self, client, portfolio_db_path):
        """部分送信: 送られたキーだけ更新、未送信フィールドは現行値据え置き (codex P1)"""
        # 事前に 5 項目をセット
        prefilled = {
            "trade_idea": "X",
            "watch_in_reason": "Y",
            "stage": "1S",
            "inago_origin": "twitter",
            "jukyu_chart": "CWH",
        }
        ps.update_memo("6324", prefilled, db_path=portfolio_db_path)

        # 3 項目だけ送信 (form に他のキーを含めない)
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "X2", "watch_in_reason": "Y2", "stage": "2S"},
        )
        assert resp.status_code == 302

        rec = ps.get_record("6324", db_path=portfolio_db_path)
        # 送られた 3 項目は更新
        assert rec["memo"]["trade_idea"] == "X2"
        assert rec["memo"]["watch_in_reason"] == "Y2"
        assert rec["memo"]["stage"] == "2S"
        # 送られなかった 2 項目は据え置き
        assert rec["memo"]["inago_origin"] == "twitter"
        assert rec["memo"]["jukyu_chart"] == "CWH"

    def test_memo_empty_string_overwrites(self, client, portfolio_db_path):
        """空文字を明示送信したら "" に上書き (メモ削除扱い)"""
        ps.update_memo("6324", {"trade_idea": "X"}, db_path=portfolio_db_path)
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": ""},
        )
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["trade_idea"] == ""

    def test_memo_no_diff_no_action_log(self, client, portfolio_db_path):
        """全項目を現行値そのまま送ったら action_log は増えない"""
        ps.update_memo("6324", {"trade_idea": "X"}, db_path=portfolio_db_path)
        before_logs = ps.list_action_logs("6324", db_path=portfolio_db_path)

        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "X"},  # 現行値と同じ
        )
        assert resp.status_code == 302
        after_logs = ps.list_action_logs("6324", db_path=portfolio_db_path)
        assert len(after_logs) == len(before_logs)

    def test_memo_unregistered_code_flash_error(self, client, portfolio_db_path):
        resp = client.post(
            "/portfolio/9999/memo",
            data={"trade_idea": "X"},
        )
        assert resp.status_code == 302
        assert ps.get_record("9999", db_path=portfolio_db_path) is None

    def test_memo_invalid_code_flash_error(self, client):
        resp = client.post(
            "/portfolio/abc/memo",
            data={"trade_idea": "X"},
        )
        assert resp.status_code == 302

    def test_memo_normalizes_crlf_in_textarea(self, client, portfolio_db_path):
        """textarea の改行 \\r\\n は \\n に正規化される"""
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "上値追い\r\n決算待ち\r\n"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        # 前後 strip + \r\n → \n 正規化
        assert rec["memo"]["trade_idea"] == "上値追い\n決算待ち"

    def test_memo_unknown_form_field_ignored(self, client, portfolio_db_path):
        """MEMO_FIELDS 外のフォームキーは無視され、エラーにならない"""
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "X", "csrf_token": "dummy", "garbage": "ignored"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["trade_idea"] == "X"
        assert "csrf_token" not in rec["memo"]
        assert "garbage" not in rec["memo"]


# ==================================================
# AJAX inline 編集 (issue #177): JSON レスポンス
# ==================================================
class TestUpdateMemoAjax:
    """X-Requested-With ヘッダ付き POST で JSON 応答が返ること"""

    def test_ajax_partial_update_returns_json(self, client, portfolio_db_path):
        resp = client.post(
            "/portfolio/6324/memo",
            data={"stage": "2S", "last_research_update": "5/10"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert resp.is_json
        body = resp.get_json()
        assert body["ok"] is True
        assert body["code_s"] == "6324"
        assert body["fields"]["stage"] == "2S"
        assert body["fields"]["last_research_update"] == "5/10"
        # shelve に反映されている
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["stage"] == "2S"
        assert rec["memo"]["last_research_update"] == "5/10"

    def test_ajax_response_includes_styles_and_display(self, client, portfolio_db_path):
        # codex P2 対応: 保存後にクライアント側で色を即時更新するため、
        # サーバは styles と display を AJAX レスポンスに含める。
        resp = client.post(
            "/portfolio/6324/memo",
            data={"stage": "2S", "last_research_update": "5/10"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "styles" in body
        # ステージ "2S" は薄赤色付け対象 (ルール 13)
        assert body["styles"].get("stage") == "background:#f4c7c3"
        # display フィールドに保存後の表示値が入っている
        assert body["display"]["stage"] == "2S"
        assert body["display"]["last_research_update"] == "5/10"

    def test_ajax_unknown_code_returns_404_json(self, client):
        resp = client.post(
            "/portfolio/9999/memo",
            data={"stage": "1S"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["ok"] is False
        assert "未登録" in body["error"]

    def test_ajax_invalid_code_returns_400_json(self, client):
        resp = client.post(
            "/portfolio/abc/memo",
            data={"stage": "1S"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False

    def test_non_ajax_still_redirects(self, client, portfolio_db_path):
        # 既存挙動: ヘッダなしの form POST はリダイレクト維持
        resp = client.post(
            "/portfolio/6324/memo",
            data={"stage": "2S"},
        )
        assert resp.status_code == 302
        assert not resp.is_json


# ==================================================
# issue #187: gyoutai_themes スロット POST + dashboard 連動
# ==================================================
class TestGyoutaiThemesPost:
    """gyoutai_themes_0/1 をスロット形式で受信し list に集約して保存される。"""

    def test_post_two_slots_saves_as_list(self, client, portfolio_db_path):
        resp = client.post(
            "/portfolio/6324/memo",
            data={"gyoutai_themes_0": "半導体", "gyoutai_themes_1": "AI"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["gyoutai_themes"] == ["半導体", "AI"]
        # AJAX レスポンスの display にも list が含まれる
        assert body["display"]["gyoutai_themes"] == ["半導体", "AI"]

    def test_post_empty_slot_removed_order_preserved(self, client, portfolio_db_path):
        # スロット 0 が空、1 にのみ値 → 順序維持で 1 件のみ保存
        resp = client.post(
            "/portfolio/6324/memo",
            data={"gyoutai_themes_0": "", "gyoutai_themes_1": "AI"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["gyoutai_themes"] == ["AI"]

    def test_post_both_empty_saves_empty_list(self, client, portfolio_db_path):
        # 先に値をセットしておく
        ps.update_memo(
            "6324", {"gyoutai_themes": ["X"]}, db_path=portfolio_db_path
        )
        resp = client.post(
            "/portfolio/6324/memo",
            data={"gyoutai_themes_0": "", "gyoutai_themes_1": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["gyoutai_themes"] == []

    def test_post_without_slot_keys_keeps_existing(self, client, portfolio_db_path):
        # 部分更新セマンティクス: gyoutai_themes_* キーが POST に含まれない → 据え置き
        ps.update_memo(
            "6324", {"gyoutai_themes": ["既存"]}, db_path=portfolio_db_path
        )
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "X"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        rec = ps.get_record("6324", db_path=portfolio_db_path)
        assert rec["memo"]["gyoutai_themes"] == ["既存"]
        assert rec["memo"]["trade_idea"] == "X"

    def test_dashboard_renders_datalist(self, client, portfolio_db_path):
        # 候補集計が dashboard のテンプレに渡って HTML に含まれる
        ps.update_memo(
            "6324",
            {"gyoutai_themes": ["半導体", "AI"]},
            db_path=portfolio_db_path,
        )
        resp = client.get("/portfolio?status=hold")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="gyoutai-theme-choices"' in html
        assert "半導体" in html
        assert "AI" in html


# ==================================================
# P2 (codex 指摘): 未知コードの監視追加 reject
# ==================================================
class TestAddUnknownCodeRejected:
    """stocks_shelve に未登録のコードは reject されるべき (codex P2)"""

    def test_add_unknown_code_does_not_persist(
        self, client, portfolio_db_path, stocks_db_path
    ):
        before = len(ps.list_records(db_path=portfolio_db_path))
        # 9999 は stocks_shelve に存在しない (fixture 未登録)
        resp = client.post("/portfolio/add", data={"code_s": "9999"})
        assert resp.status_code == 302
        # shelve のレコードは変わらない
        assert len(ps.list_records(db_path=portfolio_db_path)) == before
        assert ps.get_record("9999", db_path=portfolio_db_path) is None


# ==================================================
# P1 (codex 指摘): portfolio_shelve 未移行時 txt フォールバック
# ==================================================
@pytest.fixture
def fallback_app(tmp_path, monkeypatch):
    """portfolio_shelve が空 + my_watch_list.txt にデータあり、の状態を再現する。"""
    portfolio_db_path = str(tmp_path / "test_portfolio_shelve")
    stocks_db_path = str(tmp_path / "test_stocks_shelve")
    txt_path = tmp_path / "my_watch_list.txt"

    monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db_path)
    monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db_path)
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db_path)
    monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db_path)
    monkeypatch.setattr("portfolio_shelve.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("portfolio.DATA_DIR", str(tmp_path))

    # shelve は空のまま、txt に保有 (H...) と監視を書き込む
    txt_path.write_text(
        "# kabutan\n"
        "H6324\n"   # 保有
        "3496\n"    # 監視
        "7203\n",   # 監視
        encoding="utf-8",
    )

    # 表示時に銘柄名解決するため stocks_shelve にも入れる
    with ShelveDB(stocks_db_path) as db:
        db["6324"] = {"code_s": "6324", "stock_name": "ハーモニックドライブシステムズ",
                      "shihyo": {"PER": 308.0}}
        db["3496"] = {"code_s": "3496", "stock_name": "アズーム",
                      "shihyo": {"PER": 30.0}}
        db["7203"] = {"code_s": "7203", "stock_name": "トヨタ自動車",
                      "shihyo": {"PER": 12.0}}

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def fallback_client(fallback_app):
    return fallback_app.test_client()


class TestFallbackFromTxt:

    def test_fallback_dashboard_shows_txt_records(self, fallback_client):
        # 1保 タブ (デフォルト) にハーモニック (H プレフィクス) が出る
        resp = fallback_client.get("/portfolio")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "ハーモニック" in html
        # 監視タブには 3496/7203 が出る
        resp = fallback_client.get("/portfolio?status=watch")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "トヨタ" in html

    def test_fallback_shows_banner(self, fallback_client):
        resp = fallback_client.get("/portfolio")
        html = resp.data.decode()
        assert "未移行モード" in html

    def test_fallback_disables_transition_form(self, fallback_client):
        # 書き込み UI (ステータス変更フォーム) は出ない
        resp = fallback_client.get("/portfolio")
        html = resp.data.decode()
        assert "/transition" not in html
        assert "/delete" not in html

    def test_fallback_rejects_add_post(self, fallback_client, tmp_path):
        """フォールバック中は /portfolio/add も reject (= shelve に書き込まれない)。

        codex 指摘: バナーで「無効」と表示しても POST が通ると、shelve に 1 件
        書かれた瞬間にフォールバック解除 → 残りの txt 銘柄が画面から消える。
        """
        portfolio_db_path = str(tmp_path / "test_portfolio_shelve")
        before = ps.list_records(db_path=portfolio_db_path)
        resp = fallback_client.post("/portfolio/add", data={"code_s": "8035"})
        assert resp.status_code == 302
        # shelve にレコードは増えていない
        after = ps.list_records(db_path=portfolio_db_path)
        assert len(after) == len(before)
        assert ps.get_record("8035", db_path=portfolio_db_path) is None

    def test_fallback_rejects_transition_post(self, fallback_client, tmp_path):
        portfolio_db_path = str(tmp_path / "test_portfolio_shelve")
        resp = fallback_client.post(
            "/portfolio/3496/transition", data={"new_status": "1保"}
        )
        assert resp.status_code == 302
        assert ps.list_records(db_path=portfolio_db_path) == []

    def test_fallback_rejects_bulk_exclude_post(self, fallback_client, tmp_path):
        """フォールバック中は /portfolio/bulk-exclude も reject (issue #186)"""
        portfolio_db_path = str(tmp_path / "test_portfolio_shelve")
        resp = fallback_client.post(
            "/portfolio/bulk-exclude", data={"codes": "3496"}
        )
        assert resp.status_code == 302
        assert ps.list_records(include_excluded=True, db_path=portfolio_db_path) == []

    def test_fallback_rejects_memo_post(self, fallback_client, tmp_path):
        """フォールバック中は /portfolio/<code>/memo も reject (issue #175)"""
        portfolio_db_path = str(tmp_path / "test_portfolio_shelve")
        resp = fallback_client.post(
            "/portfolio/3496/memo", data={"trade_idea": "X"}
        )
        assert resp.status_code == 302
        # shelve は空のまま (memo 更新で 1 件作られたら fallback 解除事故が起きる)
        assert ps.list_records(db_path=portfolio_db_path) == []


# ==================================================
# issue #178: フィルタ / ソート / ページング / status badge / return_query
# ==================================================
class TestDashboardFilterSortPaging:
    """GET /portfolio?status=...&sort=...&page=... の検証 (issue #178)"""

    def test_default_shows_hold_only(self, client):
        """引数なし = 保有のみ (1保) を表示"""
        resp = client.get("/portfolio")
        html = resp.data.decode()
        assert "アズーム" in html      # 1保
        assert "ハーモニック" not in html  # 3監
        assert "トヨタ" not in html      # 2準

    def test_multi_status_filter_csv(self, client):
        """?status=hold,semi (カンマ区切り) で 1保 + 2準 を表示"""
        resp = client.get("/portfolio?status=hold,semi")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" not in html

    def test_multi_status_filter_repeated(self, client):
        """?status=hold&status=semi (HTML form 送信形式) で 1保 + 2準 を表示"""
        resp = client.get("/portfolio?status=hold&status=semi")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" not in html

    def test_multi_status_filter_mixed(self, client):
        """カンマ区切りと同名複数キーが混在しても merge される (issue #178 codex 指摘)"""
        resp = client.get("/portfolio?status=hold,xxx&status=watch")
        html = resp.data.decode()
        assert "アズーム" in html      # hold
        assert "ハーモニック" in html   # watch
        assert "トヨタ" not in html      # semi は含まれていない

    def test_all_status_filter(self, client):
        """全件指定で 3 銘柄全部表示"""
        resp = client.get("/portfolio?status=hold,semi,watch")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" in html

    def test_legacy_url_status_hold_still_works(self, client):
        """既存 URL ?status=hold (単一値) が引き続き動作 (issue #178 互換性)"""
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "ハーモニック" not in html

    def test_invalid_status_falls_back_to_default(self, client):
        """不正値だけのフィルタはデフォルト = hold にフォールバック"""
        resp = client.get("/portfolio?status=invalid")
        html = resp.data.decode()
        assert "アズーム" in html

    def test_status_badge_visible_in_all_filter(self, client):
        """全件表示時、ステータス badge (保有/準保有/監視) が HTML に出る"""
        resp = client.get("/portfolio?status=hold,semi,watch")
        html = resp.data.decode()
        assert 'class="status-badge status-hold"' in html
        assert 'class="status-badge status-semi"' in html
        assert 'class="status-badge status-watch"' in html

    def test_sort_rank_orders_by_rank(self, client):
        """?sort=rank で順位昇順 (業態無視)。fixture: 3496=50, 7203=200, 6324=612"""
        resp = client.get("/portfolio?status=hold,semi,watch&sort=rank")
        html = resp.data.decode()
        # 表示位置で並び順を判定 (3496 が 7203 より前、7203 が 6324 より前)
        i_3496 = html.find('data-code="3496"')
        i_7203 = html.find('data-code="7203"')
        i_6324 = html.find('data-code="6324"')
        assert i_3496 != -1 and i_7203 != -1 and i_6324 != -1
        assert i_3496 < i_7203 < i_6324

    def test_sort_gyoutai_default(self, client):
        """sort 未指定はデフォルト gyoutai (radio が checked になっている)"""
        import re
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        # gyoutai radio タグ内に checked が含まれ、rank radio タグ内には含まれないこと
        gyoutai_radio = re.search(r'<input[^>]*name="sort"[^>]*value="gyoutai"[^>]*>', html)
        rank_radio = re.search(r'<input[^>]*name="sort"[^>]*value="rank"[^>]*>', html)
        assert gyoutai_radio is not None and "checked" in gyoutai_radio.group(0)
        assert rank_radio is not None and "checked" not in rank_radio.group(0)

    def test_pagination_disabled_when_under_page_size(self, client):
        """総件数がページサイズ以内ならページネーション nav は出ない"""
        resp = client.get("/portfolio?status=hold,semi,watch")
        html = resp.data.decode()
        # 全 3 件 <= 50 なので pagination nav は出ない
        assert 'class="pagination"' not in html

    def test_pagination_invalid_page_falls_back(self, client):
        """?page=abc は 1 にフォールバック (例外を投げない)"""
        resp = client.get("/portfolio?status=hold&page=abc")
        assert resp.status_code == 200
        assert "アズーム" in resp.data.decode()

    def test_pagination_zero_page_falls_back(self, client):
        """?page=0 は 1 にフォールバック"""
        resp = client.get("/portfolio?status=hold&page=0")
        assert resp.status_code == 200

    def test_pagination_overflow_page_clamped(self, client, monkeypatch):
        """総ページ数を超えた page 指定は最終ページにクランプ"""
        # ページサイズを 1 に下げて 3 銘柄を 3 ページに分割
        from webapp.routes import portfolio as portfolio_routes
        monkeypatch.setattr(portfolio_routes, "PORTFOLIO_PAGE_SIZE", 1)
        resp = client.get("/portfolio?status=hold,semi,watch&sort=rank&page=999")
        html = resp.data.decode()
        # 最終ページ (page=3) には 6324 (rank=612) のみ
        assert 'data-code="6324"' in html
        assert 'data-code="3496"' not in html
        assert 'data-code="7203"' not in html

    def test_pagination_actually_paginates(self, client, monkeypatch):
        """ページサイズを下げた状態で 1 ページ目が 1 件、2 ページ目で違う 1 件"""
        from webapp.routes import portfolio as portfolio_routes
        monkeypatch.setattr(portfolio_routes, "PORTFOLIO_PAGE_SIZE", 1)
        # rank 順: 3496 (50) → 7203 (200) → 6324 (612)
        resp1 = client.get("/portfolio?status=hold,semi,watch&sort=rank&page=1")
        html1 = resp1.data.decode()
        assert 'data-code="3496"' in html1
        assert 'data-code="7203"' not in html1

        resp2 = client.get("/portfolio?status=hold,semi,watch&sort=rank&page=2")
        html2 = resp2.data.decode()
        assert 'data-code="7203"' in html2
        assert 'data-code="3496"' not in html2

        # ページネーション nav が出ている (total_pages=3)
        assert 'class="pagination"' in html2

    def test_filter_form_does_not_emit_page_hidden(self, client):
        """フィルタ form 内に page の hidden が無い (= submit 時に page=1 にリセット)"""
        resp = client.get("/portfolio?status=hold&page=2")
        html = resp.data.decode()
        # フィルタ form 部分を抽出して page hidden が無いことを確認
        form_start = html.find('id="portfolio-filter-form"')
        form_end = html.find('</form>', form_start)
        form_html = html[form_start:form_end]
        assert 'name="page"' not in form_html


class TestReturnQueryRedirect:
    """POST 後の return_query によるリダイレクト先復元 (issue #178)"""

    def test_transition_redirects_with_return_query(self, client):
        """transition POST 時、return_query が反映される"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={
                "new_status": "3監",
                "reason": "test",
                "return_query": "status=hold,semi&sort=rank",
            },
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "status=hold,semi" in loc
        assert "sort=rank" in loc

    def test_memo_redirects_with_return_query(self, client):
        """memo POST 時、return_query が反映される"""
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "X", "return_query": "status=watch&sort=rank&page=2"},
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "status=watch" in loc
        assert "sort=rank" in loc
        assert "page=2" in loc

    def test_add_default_status_query_is_watch(self, client, stocks_db_path):
        """add は return_query 未指定時、デフォルトで watch にリダイレクト (追加直後の確認用)"""
        with ShelveDB(stocks_db_path) as db:
            db["8035"] = {"code_s": "8035", "stock_name": "東京エレクトロン"}
        resp = client.post("/portfolio/add", data={"code_s": "8035"})
        assert resp.status_code == 302
        assert "status=watch" in resp.headers["Location"]

    def test_return_query_with_unsafe_chars_falls_back(self, client):
        """改行や # を含む return_query はフォールバック (URL injection 防止)"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "1保", "return_query": "status=hold\n#evil"},
        )
        assert resp.status_code == 302
        # return_query が空相当の扱い → デフォルト ?status=hold
        loc = resp.headers["Location"]
        assert "status=hold" in loc
        assert "evil" not in loc
        assert "%0A" not in loc
