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
    """GET /portfolio フィルタ表示 (issue #215: ステータス単一選択 + 全件デフォルト)"""

    def test_dashboard_default_shows_all(self, client):
        """引数なし = 全ステータス表示 (issue #215)"""
        resp = client.get("/portfolio")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "アズーム" in html       # 1保
        assert "トヨタ" in html         # 2準
        assert "ハーモニック" in html    # 3監

    def test_dashboard_watch_filter(self, client):
        resp = client.get("/portfolio?status=watch")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "ハーモニック" in html
        assert "アズーム" not in html

    def test_dashboard_semi_filter(self, client):
        resp = client.get("/portfolio?status=semi")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "トヨタ" in html
        assert "アズーム" not in html

    def test_dashboard_unknown_status_falls_back_to_all(self, client):
        """不正値は None 扱い = 全件表示 (issue #215)"""
        resp = client.get("/portfolio?status=invalid")
        assert resp.status_code == 200
        html = resp.data.decode()
        # 全件 = 1保/2準/3監 全部表示
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" in html

    def test_dashboard_shows_status_counts(self, client):
        """status select の option ラベルに件数が出ている"""
        resp = client.get("/portfolio")
        html = resp.data.decode()
        # select option の "保有 (1)" / "準保有 (1)" / "監視 (1)" のような表示
        assert "(1)" in html

    def test_dashboard_shows_indicators(self, client):
        """保有フィルタで PER / モメンタム / 順位等の指標が表示される"""
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

    def test_transition_with_action_date_form_param(self, client, portfolio_db_path):
        """issue #220: form の action_date が action_log の timestamp に伝搬する"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={
                "new_status": "2準",
                "reason": "昨日売却",
                "action_date": "2026-05-10",
            },
        )
        assert resp.status_code == 302
        logs = ps.list_action_logs(code_s="3496", db_path=portfolio_db_path)
        latest = logs[-1]
        assert latest["timestamp"] == "2026-05-10T12:00:00+09:00"
        assert latest["action_type"] == "売却"

    def test_transition_future_action_date_flashes_error(self, client, portfolio_db_path):
        """issue #220: 未来日は flash error でステータス変更されない"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "2準", "action_date": "2099-12-31"},
        )
        assert resp.status_code == 302
        rec = ps.get_record("3496", db_path=portfolio_db_path)
        assert rec["status"] == "1保"  # 変更されていない


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
        """return_query=status=semi&gyoutai_theme=半導体 なら同じクエリにリダイレクト (issue #215)"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": "7203", "return_query": "status=semi&gyoutai_theme=半導体"},
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "status=semi" in loc
        assert "gyoutai_theme=" in loc

    def test_bulk_exclude_empty_return_query_falls_back_to_all(self, client, portfolio_db_path):
        """return_query 未指定は素の /portfolio (= 全件表示) にリダイレクト (issue #215)"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": "6324"},
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        # /portfolio に戻る (status= 等のフィルタクエリは付かない)
        assert loc.endswith("/portfolio")

    def test_bulk_exclude_from_detail_returns_to_detail(self, client, portfolio_db_path):
        """issue #221: 詳細モーダルからの単一除外 (return_to=detail + return_code_s) は
        同じ銘柄の詳細ページに戻す"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={
                "codes": "6324",
                "return_to": "detail",
                "return_code_s": "6324",
            },
        )
        assert resp.status_code == 302
        assert "/stock/6324" in resp.headers["Location"]

    def test_bulk_exclude_without_return_code_falls_back(self, client, portfolio_db_path):
        """return_to=detail でも return_code_s が無ければ通常の /portfolio に戻る"""
        resp = client.post(
            "/portfolio/bulk-exclude",
            data={"codes": "6324", "return_to": "detail"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/portfolio")

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
class TestDashboardFilter:
    """GET /portfolio フィルタ仕様 (issue #215)"""

    def test_default_shows_all(self, client):
        """引数なし = 全ステータス表示"""
        resp = client.get("/portfolio")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" in html

    def test_legacy_url_status_hold_still_works(self, client):
        """既存 URL ?status=hold (単一値) は引き続き保有のみ表示"""
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "ハーモニック" not in html

    def test_legacy_csv_status_uses_first_value(self, client):
        """旧 URL ?status=hold,semi は寛容処理で先頭値 hold のみ採用 (issue #215)"""
        resp = client.get("/portfolio?status=hold,semi")
        html = resp.data.decode()
        assert "アズーム" in html
        # semi も watch も表示されない (先頭の hold のみ)
        assert "トヨタ" not in html
        assert "ハーモニック" not in html

    def test_legacy_sort_param_silently_ignored(self, client):
        """旧 URL ?sort=rank&page=2 が混ざっても 200 で全件返る (issue #215 silent 無視)"""
        resp = client.get("/portfolio?sort=rank&page=2")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" in html

    def test_invalid_status_falls_back_to_all(self, client):
        """不正値だけのフィルタは全件表示にフォールバック (None 扱い)"""
        resp = client.get("/portfolio?status=invalid")
        html = resp.data.decode()
        # 全件 = 1保/2準/3監 全部
        assert "アズーム" in html
        assert "トヨタ" in html
        assert "ハーモニック" in html

    def test_status_badge_visible_in_all_view(self, client):
        """全件表示時、ステータス badge (保有/準保有/監視) が HTML に出る"""
        resp = client.get("/portfolio")
        html = resp.data.decode()
        assert 'class="status-badge status-hold"' in html
        assert 'class="status-badge status-semi"' in html
        assert 'class="status-badge status-watch"' in html

    def test_gyoutai_theme_filter_applies(
        self, client, portfolio_db_path
    ):
        """?gyoutai_theme=<value> で memo.gyoutai_themes に該当値を持つ銘柄のみ表示"""
        # fixture: 3496 (1保) に "ロボット" テーマを付与
        ps.update_memo("3496", {"gyoutai_themes": ["ロボット"]}, db_path=portfolio_db_path)
        resp = client.get("/portfolio?gyoutai_theme=ロボット")
        html = resp.data.decode()
        assert "アズーム" in html        # 3496 該当
        assert "トヨタ" not in html       # 7203 非該当
        assert "ハーモニック" not in html  # 6324 非該当

    def test_gyoutai_theme_overrides_status(
        self, client, portfolio_db_path
    ):
        """?status=hold&gyoutai_theme=X 指定時は status 無視で X 該当銘柄全件を表示"""
        # fixture: 7203 (2準) に "ロボット" テーマを付与 (status=hold とは異なる)
        ps.update_memo("7203", {"gyoutai_themes": ["ロボット"]}, db_path=portfolio_db_path)
        resp = client.get("/portfolio?status=hold&gyoutai_theme=ロボット")
        html = resp.data.decode()
        # status=hold (1保) を指定しているが gyoutai_theme 優先で 7203 (2準) が表示される
        assert "トヨタ" in html
        # 1保 のアズーム (ロボット未付与) は出ない
        assert "アズーム" not in html


class TestReturnQueryRedirect:
    """POST 後の return_query によるリダイレクト先復元 (issue #178, #215 で形式変更)"""

    def test_transition_redirects_with_return_query(self, client):
        """transition POST 時、return_query が反映される"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={
                "new_status": "3監",
                "reason": "test",
                "return_query": "status=hold&gyoutai_theme=半導体",
            },
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "status=hold" in loc
        assert "gyoutai_theme=" in loc

    def test_memo_redirects_with_return_query(self, client):
        """memo POST 時、return_query が反映される"""
        resp = client.post(
            "/portfolio/6324/memo",
            data={"trade_idea": "X", "return_query": "status=watch"},
        )
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "status=watch" in loc

    def test_add_default_status_query_is_watch(self, client, stocks_db_path):
        """add は return_query 未指定時、デフォルトで watch にリダイレクト (追加直後の確認用)"""
        with ShelveDB(stocks_db_path) as db:
            db["8035"] = {"code_s": "8035", "stock_name": "東京エレクトロン"}
        resp = client.post("/portfolio/add", data={"code_s": "8035"})
        assert resp.status_code == 302
        assert "status=watch" in resp.headers["Location"]

    def test_add_form_does_not_emit_return_query_hidden(self, client):
        """追加フォームは hidden return_query を出さない (codex 指摘 P2)。

        追加された 3監 銘柄を見えなくしないため、現在のフィルタを引き継がず
        add() の default_status_query='watch' フォールバックを効かせる。
        """
        import re
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        # 追加フォーム部分を抽出 (action="/portfolio/add" の form タグ)
        m = re.search(r'<form[^>]*action="/portfolio/add"[^>]*>(.*?)</form>', html, re.DOTALL)
        assert m is not None, "追加フォームが見つからない"
        form_inner = m.group(1)
        assert 'name="return_query"' not in form_inner

    def test_return_query_with_unsafe_chars_falls_back(self, client):
        """改行や # を含む return_query はフォールバック (URL injection 防止)"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "1保", "return_query": "status=hold\n#evil"},
        )
        assert resp.status_code == 302
        # return_query が空相当の扱い → 素の /portfolio (= 全件表示、issue #215)
        loc = resp.headers["Location"]
        assert loc.endswith("/portfolio")
        assert "evil" not in loc
        assert "%0A" not in loc
        assert "%0A" not in loc


class TestReturnToDetail:
    """issue #195: 詳細ページからの POST は /stock/<code_s> に戻る"""

    def test_transition_with_return_to_detail_redirects_to_stock_page(self, client):
        """transition + return_to=detail → /stock/<code_s>"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "3監", "return_to": "detail"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/stock/3496")

    def test_transition_without_return_to_detail_keeps_dashboard(self, client):
        """return_to が無いときは従来通り dashboard に戻る (回帰)"""
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "3監", "return_query": "status=hold"},
        )
        assert resp.status_code == 302
        assert "/portfolio" in resp.headers["Location"]
        assert "/stock/" not in resp.headers["Location"]

    def test_add_with_return_to_detail_redirects_to_stock_page(
        self, client, stocks_db_path
    ):
        """add + return_to=detail → /stock/<normalized code_s>"""
        with ShelveDB(stocks_db_path) as db:
            db["8035"] = {"code_s": "8035", "stock_name": "東京エレクトロン"}
        resp = client.post(
            "/portfolio/add",
            data={"code_s": "8035", "return_to": "detail"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/stock/8035")

    def test_add_persists_reason_from_form(
        self, client, portfolio_db_path, stocks_db_path
    ):
        """add は form の reason を action_log に保存する (codex 指摘 #3)"""
        with ShelveDB(stocks_db_path) as db:
            db["8035"] = {"code_s": "8035", "stock_name": "東京エレクトロン"}
        client.post(
            "/portfolio/add",
            data={"code_s": "8035", "reason": "高値ブレイク候補", "return_to": "detail"},
        )
        logs = ps.list_action_logs("8035", db_path=portfolio_db_path)
        assert any(a.get("reason") == "高値ブレイク候補" for a in logs), (
            f"reason がログに残らず: logs={logs}"
        )

    def test_add_falls_back_to_default_reason_when_empty(
        self, client, portfolio_db_path, stocks_db_path
    ):
        """reason 未送信 (= 空文字 or キー無し) は従来通り 'WebApp 追加' で記録"""
        with ShelveDB(stocks_db_path) as db:
            db["8035"] = {"code_s": "8035", "stock_name": "東京エレクトロン"}
        client.post("/portfolio/add", data={"code_s": "8035"})
        logs = ps.list_action_logs("8035", db_path=portfolio_db_path)
        assert any(a.get("reason") == "WebApp 追加" for a in logs), (
            f"デフォルト reason 'WebApp 追加' が記録されていない: logs={logs}"
        )


class TestDeleteCheckboxScope:
    """削除モードのチェックボックスは 2準/3監 行のみに表示される (codex 指摘 P2)"""

    def test_1ho_row_has_no_checkbox_in_mixed_filter(self, client):
        """全件表示 (引数なし) で、1保 (3496) 行には checkbox が無く、
        2準 (7203) と 3監 (6324) 行には checkbox が出る (issue #215 で全件 = 引数なし)"""
        import re
        resp = client.get("/portfolio")
        html = resp.data.decode()
        # 各銘柄行を抽出 (data-code="XXXX" から次の </tr> まで)
        for code, has_checkbox in [
            ("3496", False),  # 1保 → checkbox 無し
            ("7203", True),   # 2準 → checkbox あり
            ("6324", True),   # 3監 → checkbox あり
        ]:
            m = re.search(
                rf'<tr data-code="{code}"[^>]*>(.*?)</tr>', html, re.DOTALL
            )
            assert m is not None, f"行 {code} が見つからない"
            row_inner = m.group(1)
            checkbox_present = 'class="bulk-cb"' in row_inner
            assert checkbox_present == has_checkbox, (
                f"行 {code} の checkbox 期待値 {has_checkbox} だが実際は {checkbox_present}"
            )

    def test_no_checkbox_when_only_hold_filter(self, client):
        """status=hold のみ (削除可能ステータスなし) なら delete_mode_allowed=False で
        bulk-col 列自体出ない"""
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        assert 'class="bulk-cb"' not in html
        # bulk-col の th も出ない (delete_mode_allowed が False)
        assert 'class="bulk-col"' not in html
