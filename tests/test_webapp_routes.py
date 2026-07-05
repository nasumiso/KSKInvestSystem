"""webapp ルートの統合テスト (Flaskテストクライアント使用)"""

import os

import pytest

import research_shelve as rs
from webapp import create_app


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_research_shelve")


@pytest.fixture
def app(db_path, tmp_path, monkeypatch):
    """テスト用Flaskアプリ (DBパス差し替え済み)"""
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
    # トップページの Spreadsheet ポータルは CSV の mtime を表示に使う。
    # CI には実データが無いため、テスト用 DATA_DIR にダミー CSV を置く。
    portal_data_dir = tmp_path / "data"
    for rel in (
        "shintakane_result_data/shintakane_result.csv",
        "code_rank_data/code_rank.csv",
    ):
        path = portal_data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dummy\n")
    monkeypatch.setattr("webapp.routes.search.DATA_DIR", str(portal_data_dir))

    # テストデータ投入
    rec = rs.create_research_record(
        "3496", "アズーム",
        overall_rating="A",
        memo="テストメモ",
        overview="駐車場サブリース",
        shikiho_comments=["最高益"],
        kessan_comments=[
            # 昇順で保存 (本番 _sort_kessan_comments は ASC)
            {
                "kessanbi": "2024/02/14", "quarter": 2, "pre_expectation": "△",
                "pre_outlook": "", "post_price_change": "",
                "post_comment": "[S] S高 期待上振れ",  # post_price_change 空パターン
            },
            {
                "kessanbi": "2024/05/14", "quarter": 4, "pre_expectation": "◎",
                "pre_outlook": "通期上振れ期待",
                "post_price_change": "-3.1",
                "post_comment": "[E] -3.1% ガイダンス弱気で売られる",
            },
            {
                "kessanbi": "2024/08/14", "quarter": 1, "pre_expectation": "○",
                "pre_outlook": "順調に推移見込み",
                "post_price_change": "+5.2",
                "post_comment": "[B] +5.2% 好決算で反応良好",
            },
        ],
    )
    rs.upsert_research_record(rec, db_path=db_path)
    snap = rs.create_snapshot("26.4", ir_quant="[A]28%", ir_comment="好調", data_source="auto")
    rs.upsert_snapshot("3496", snap, db_path=db_path)

    # kessan_comments 空の銘柄 (空状態テスト用)
    rec_empty = rs.create_research_record(
        "1234", "空テスト",
        overall_rating="B",
    )
    rs.upsert_research_record(rec_empty, db_path=db_path)

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestSearchRoute:
    """GET / のテスト"""

    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_no_query_hides_records_and_shows_portal(self, client, monkeypatch):
        """issue #98: クエリなしトップでは銘柄一覧を出さず Spreadsheet ポータルを表示"""
        # 「最終更新」はローカル CSV の mtime 依存 (search._portal_spreadsheets) のため
        # CI 環境 (CSV 無し) では updated_at が "—" になり表示されない。ポータル表示
        # 自体の検証が目的なので updated_at を固定値にモックして環境非依存にする。
        import webapp.routes.search as search_route
        monkeypatch.setattr(
            search_route,
            "_portal_spreadsheets",
            lambda: [
                {"title": "Shintakane Result", "url": "https://example.com/r", "updated_at": "2026-05-29 12:00"},
                {"title": "Code Rank", "url": "https://example.com/c", "updated_at": "2026-05-29 12:00"},
            ],
        )
        resp = client.get("/")
        html = resp.data.decode()
        # 銘柄一覧テーブルは描画されない
        assert "アズーム" not in html
        assert '<table' not in html
        # Spreadsheet ポータルカードが存在する
        assert 'id="portal-spreadsheets"' in html
        assert "Shintakane Result" in html
        assert "Code Rank" in html
        assert "最終更新" in html

    def test_index_with_keyword_shows_records(self, client):
        """issue #244: 検索クエリ付き (keyword) では従来通り一覧を表示"""
        resp = client.get("/?keyword=アズーム")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "3496" in html
        assert "アズーム" in html

    def test_index_filter_by_rating(self, client):
        resp = client.get("/?rating=A")
        html = resp.data.decode()
        assert "アズーム" in html

        resp = client.get("/?rating=S")
        html = resp.data.decode()
        assert "アズーム" not in html
        assert "0 件" in html

    def test_index_filter_by_code(self, client):
        # 1件のみヒットする場合は詳細ページへリダイレクト
        resp = client.get("/?code_s=3496")
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]

        resp = client.get("/?code_s=9999")
        assert "アズーム" not in resp.data.decode()

    def test_index_single_hit_redirects_to_detail(self, client):
        """汎用検索 q で1件のみヒットしたら詳細ページへ直接ジャンプ"""
        resp = client.get("/?q=3496")
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]

        resp = client.get("/?q=アズーム")
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]

    def test_index_multiple_hits_shows_list(self, client):
        """複数ヒットする検索は一覧ページのまま"""
        resp = client.get("/?q=テスト")  # "アズーム" の "テストメモ" と "空テスト" 両方にヒット
        assert resp.status_code == 200

    def test_index_no_hit_with_code_q_shows_add_button(self, client):
        """issue #216: ?q=未登録コード 0件時は追加フォームを表示"""
        resp = client.get("/?q=9999")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "該当する銘柄がありません" in html
        assert 'name="add_code_s"' in html
        assert 'value="9999"' in html

    def test_index_no_hit_with_keyword_q_hides_add_button(self, client):
        """issue #216: 銘柄名検索でヒット0件でも追加フォームは出さない"""
        resp = client.get("/?q=存在しない銘柄名")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "該当する銘柄がありません" in html
        assert 'name="add_code_s"' not in html

    def test_index_no_hit_with_invalid_code_hides_add_button(self, client):
        """issue #216: 5桁などコード形式でない場合は追加フォーム非表示"""
        resp = client.get("/?q=99999")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "該当する銘柄がありません" in html
        assert 'name="add_code_s"' not in html

    def test_index_no_hit_with_3digit_plus_letter_shows_add_button(self, client):
        """issue #216: 215A 形式 (3桁+大文字) も追加対象"""
        resp = client.get("/?q=215A")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'value="215A"' in html


class TestStockAddRoute:
    """POST /stock/add のテスト"""

    def test_add_valid_code_redirects_to_detail(self, client, monkeypatch):
        """issue #216: 検索0件ページからの追加 → 詳細ページへリダイレクト"""
        # add_stock は stocks_shelve 参照するためスタブ化
        monkeypatch.setattr(
            "webapp.routes.search.add_stock",
            lambda code: code.upper(),
        )
        resp = client.post("/stock/add", data={"add_code_s": "9999"})
        assert resp.status_code == 302
        assert "/stock/9999" in resp.headers["Location"]

    def test_add_invalid_code_redirects_to_index(self, client, monkeypatch):
        """不正コードは flash + index へ"""
        def _raise(code):
            raise ValueError("invalid code")
        monkeypatch.setattr("webapp.routes.search.add_stock", _raise)
        resp = client.post("/stock/add", data={"add_code_s": "bad"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")


class TestDetailRoute:
    """GET /stock/<code_s> のテスト"""

    def test_detail_returns_200(self, client):
        resp = client.get("/stock/3496")
        assert resp.status_code == 200

    def test_detail_shows_record_info(self, client):
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "3496" in html
        assert "テストメモ" in html

    def test_detail_404_for_unknown(self, client):
        resp = client.get("/stock/9999")
        assert resp.status_code == 404


class TestDetailStockNamePrev:
    """issue #183: stock_name_prev の併記表示"""

    def test_detail_displays_stock_name_prev(self, client, db_path):
        """stock_name_prev が入っていれば「銘柄名 (エイリアス)」併記される"""
        # 既存テストデータの 3496 にエイリアスを入れる
        rs.sync_stock_name("3496", "アズームニューネーム", db_path=db_path)
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "アズームニューネーム" in html
        assert "(アズーム)" in html

    def test_detail_no_paren_when_stock_name_prev_none(self, client, db_path):
        """stock_name_prev が None ならエイリアス併記なし (デフォルト状態)"""
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "アズーム" in html
        # stock_name_prev 由来の編集 span (class 属性に stock-name-prev-edit) は出ない
        # (CSS 定義の `.stock-name-prev-edit { ... }` には引っかからないよう class 属性形で見る)
        assert 'class="stock-name-prev-edit"' not in html
        assert 'class="stock-name-prev-add"' in html


class TestStockNamePrevRoute:
    """issue #236: stock_name_prev の手動編集 + エイリアス検索"""

    def test_save_and_search_alias(self, client, db_path):
        """POST で旧名/エイリアスを保存 → 検索でヒット → 単一ヒット時は detail へリダイレクト"""
        resp = client.post(
            "/stock/3496/stock_name_prev",
            data={"stock_name_prev": "テストエイリアス"},
        )
        assert resp.status_code == 204
        # research_shelve に反映確認
        rec = rs.get_research_record("3496", db_path=db_path)
        assert rec["stock_name_prev"] == "テストエイリアス"
        # 検索でヒット → 単一ヒットなので /stock/3496 へリダイレクト
        resp_search = client.get("/?q=テストエイリアス")
        assert resp_search.status_code == 302
        assert "/stock/3496" in resp_search.headers["Location"]

    def test_clear_prev_with_empty_string(self, client, db_path):
        """空文字保存で stock_name_prev を None にリセット"""
        client.post("/stock/3496/stock_name_prev", data={"stock_name_prev": "tmp"})
        rec = rs.get_research_record("3496", db_path=db_path)
        assert rec["stock_name_prev"] == "tmp"
        # 空文字で再保存 → None
        resp = client.post("/stock/3496/stock_name_prev", data={"stock_name_prev": ""})
        assert resp.status_code == 204
        rec = rs.get_research_record("3496", db_path=db_path)
        assert rec["stock_name_prev"] is None


class TestDetailPortfolioModal:
    """issue #195: 詳細ページにポートフォリオステータス変更モーダルを描画する。

    portfolio_shelve を tmp_path に差し替えた fixture で、登録済/未登録両ケースを検証。
    本テストは module 内の app fixture を共有しつつ、必要な portfolio_shelve も差し替える。
    """

    @pytest.fixture
    def portfolio_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        # research_shelve: 3496 (登録済 1保 用) と 1234 (未登録 = portfolio に入れない) を登録
        rec_3496 = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec_3496, db_path=db_path)
        rec_1234 = rs.create_research_record("1234", "テスト未登録", overall_rating="B")
        rs.upsert_research_record(rec_1234, db_path=db_path)

        # portfolio_shelve: 3496 を 1保 として登録
        ps.add_to_watch("3496", reason="テスト用", db_path=portfolio_db)
        ps.transition_status("3496", "1保", reason="テスト用 1保 へ", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app

    @pytest.fixture
    def portfolio_client(self, portfolio_app):
        return portfolio_app.test_client()

    def test_registered_stock_renders_modal_form(self, portfolio_client):
        """登録済銘柄: モーダル DOM (transition POST フォーム + return_to=detail hidden) が出る"""
        resp = portfolio_client.get("/stock/3496")
        html = resp.data.decode()
        assert 'id="portfolio-modal"' in html
        assert 'action="/portfolio/3496/transition"' in html
        assert 'name="return_to" value="detail"' in html

    def test_registered_stock_has_transition_options(self, portfolio_client):
        """登録済銘柄 (1保): _allowed_transitions_from('1保') の遷移先 (2準) が select に含まれる"""
        resp = portfolio_client.get("/stock/3496")
        html = resp.data.decode()
        # 1保 からは 2準 (売却) への遷移が許可されている
        assert 'value="2準"' in html

    def test_registered_badge_is_clickable_button(self, portfolio_client):
        """登録済バッジは button 要素になっている (onclick=openPortfolioModal)"""
        resp = portfolio_client.get("/stock/3496")
        html = resp.data.decode()
        assert "portfolio-badge-button" in html
        assert "openPortfolioModal()" in html

    def test_hold_status_no_exclude_button(self, portfolio_client):
        """issue #221: 1保 銘柄の詳細では「ユニバース除外」ボタンが出ない"""
        resp = portfolio_client.get("/stock/3496")  # 1保
        html = resp.data.decode()
        # JS 関数定義 / CSS クラス定義 / confirm() メッセージは常に含まれるので、
        # ボタンの onclick="excludeFromUniverse(...)" 呼び出しが無いことで判定
        assert 'onclick="excludeFromUniverse' not in html

    def test_semi_status_shows_exclude_button(self, db_path, tmp_path, monkeypatch):
        """issue #221: 2準 銘柄の詳細では「ユニバース除外」ボタンが出る"""
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec, db_path=db_path)
        # 3監 → 2準 に遷移させる
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)
        ps.transition_status("3496", "2準", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "excludeFromUniverse('3496')" in html
        assert "ユニバースから除外" in html

    def test_watch_status_shows_exclude_button(self, db_path, tmp_path, monkeypatch):
        """issue #221: 3監 銘柄の詳細では「ユニバース除外」ボタンが出る"""
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec, db_path=db_path)
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)  # 3監 のまま

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "excludeFromUniverse('3496')" in html

    def test_excluded_record_treated_as_unregistered(self, db_path, tmp_path, monkeypatch):
        """除外済み (excluded=True) は未登録扱い: バッジ/transition モーダルは出さず、
        「+ 監視に追加」ボタン + /portfolio/add モーダルを出す (codex P2)。

        理由: transition_status() は excluded フラグを下げないため、
        除外済み銘柄に対して遷移を実行しても portfolio 一覧 / txt 同期から
        除外されたまま = ユーザーは「変更したつもり」になる事故を防ぐ。
        復活は /portfolio/add の add_to_watch() 復活パスに任せる。
        """
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec = rs.create_research_record("9999", "除外テスト", overall_rating="C")
        rs.upsert_research_record(rec, db_path=db_path)

        # 9999 を 3監 として追加 → ユニバース除外する
        ps.add_to_watch("9999", reason="テスト用", db_path=portfolio_db)
        ps.exclude_from_universe("9999", reason="テスト用 除外", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.get("/stock/9999")
        html = resp.data.decode()

        # 除外済み = 未登録扱い: transition モーダルは出ず、add (復活) モーダルが出る。
        # 既登録/未登録のバッジ button 自体は両方とも <button class="portfolio-badge..."> で
        # 描画されるため、画面表示の差分は portfolio-badge-add (「+」アイコン) の有無 と
        # transition vs add フォームの action URL で判定する。
        assert 'action="/portfolio/9999/transition"' not in html
        assert "portfolio-badge-add" in html
        assert 'action="/portfolio/add"' in html


class TestDetailKessanHistory:
    """GET /stock/<code_s> の決算コメント履歴セクションのテスト (issue #131)"""

    def test_section_rendered(self, client):
        """セクション見出し・件数・テーブルクラスが HTML に出現"""
        html = client.get("/stock/3496").data.decode()
        assert "決算コメント履歴" in html
        assert "(3件)" in html
        assert "kessan-history-table" in html

    def test_descending_order(self, client):
        """kessanbi 降順 (新しい順) で表示される"""
        html = client.get("/stock/3496").data.decode()
        idx_latest = html.index("2024/08/14")
        idx_middle = html.index("2024/05/14")
        idx_oldest = html.index("2024/02/14")
        assert idx_latest < idx_middle < idx_oldest

    def test_expectation_badges(self, client):
        """各 pre_expectation に対応する CSS クラスが出力される"""
        html = client.get("/stock/3496").data.decode()
        assert "exp-◎" in html
        assert "exp-○" in html
        assert "exp-△" in html

    def test_price_change_rate_class_positive(self, client):
        """+5.2% 行に rate-pos クラスが付く"""
        html = client.get("/stock/3496").data.decode()
        assert "rate-pos" in html
        assert "+5.2%" in html

    def test_price_change_rate_class_negative(self, client):
        """-3.1% 行に rate-neg クラスが付く"""
        html = client.get("/stock/3496").data.decode()
        assert "rate-neg" in html
        assert "-3.1%" in html

    def test_empty_post_price_change_shows_dash(self, client):
        """post_price_change 空のエントリでも post_comment 先頭の [S] S高 文字列は残る"""
        html = client.get("/stock/3496").data.decode()
        assert "S高" in html  # post_comment に保持されている

    def test_outlook_ellipsis_with_title_attr(self, client):
        """見通し列に title 属性 (ホバー全文) が付与される"""
        html = client.get("/stock/3496").data.decode()
        # pre_outlook が title 属性として含まれる
        assert 'title="通期上振れ期待"' in html
        assert 'title="順調に推移見込み"' in html

    def test_empty_state_message(self, client):
        """kessan_comments=[] の銘柄では「決算コメントなし」メッセージ"""
        html = client.get("/stock/1234").data.decode()
        assert "決算コメント履歴" in html  # セクションは表示
        assert "(0件)" in html
        assert "決算コメントなし" in html


class TestMemoPostRoutes:
    """POST /stock/<code_s>/memo のテスト"""

    def test_memo_post_redirects(self, client):
        resp = client.post("/stock/3496/memo", data={
            "overall_rating": "S",
            "institutional_comment": "",
            "memo": "新メモ",
            "openwork": "",
            "cramer": "",
        })
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]

    def test_memo_post_persists(self, client):
        client.post("/stock/3496/memo", data={
            "overall_rating": "B",
            "institutional_comment": "更新",
            "memo": "更新メモ",
            "openwork": "4.0",
            "cramer": "Sell",
        })
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "更新メモ" in html


class TestShikihoPostRoutes:
    """POST /stock/<code_s>/shikiho のテスト"""

    def test_shikiho_post_redirects(self, client):
        resp = client.post("/stock/3496/shikiho", data={
            "overview": "新概要",
            "shikiho_comments_0": "コメント1",
            "shikiho_periods_0": "26.3",
        })
        assert resp.status_code == 302

    def test_shikiho_post_persists(self, client):
        client.post("/stock/3496/shikiho", data={
            "overview": "更新概要",
            "shikiho_comments_0": "更新コメント",
            "shikiho_periods_0": "26.3",
        })
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "更新概要" in html


class TestIrCommentPostRoutes:
    """POST /stock/<code_s>/ir_comment のテスト"""

    def test_ir_comment_post_redirects(self, client):
        resp = client.post("/stock/3496/ir_comment", data={
            "ir_comment_26.4": "更新IR",
        })
        assert resp.status_code == 302

    def test_ir_comment_post_persists(self, client):
        client.post("/stock/3496/ir_comment", data={
            "ir_comment_26.4": "IR更新済み",
        })
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "IR更新済み" in html


class TestRefreshPostRoutes:
    """POST /stock/<code_s>/refresh のテスト (issue #203)

    本体は `from make_stock_db import refresh_stock` を関数内で行うため、
    sys.modules に make_stock_db スタブを差し込んで実体呼び出しを避ける。
    """

    def test_refresh_post_redirects_with_info_flash(self, client, monkeypatch):
        import sys
        import types

        calls = []
        stub = types.ModuleType("make_stock_db")
        stub.refresh_stock = lambda codes: calls.append(list(codes))
        monkeypatch.setitem(sys.modules, "make_stock_db", stub)

        resp = client.post("/stock/3496/refresh")
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]
        assert calls == [["3496"]]

        # follow redirect で flash 文言を検証 (info: 緑系背景)
        follow = client.get("/stock/3496")
        html = follow.data.decode()
        assert "再取得しました (3496)" in html
        assert "background:#eaffea" in html

    def test_refresh_post_handles_exception_with_error_flash(self, client, monkeypatch):
        import sys
        import types

        def raise_boom(_codes):
            raise RuntimeError("boom")

        stub = types.ModuleType("make_stock_db")
        stub.refresh_stock = raise_boom
        monkeypatch.setitem(sys.modules, "make_stock_db", stub)

        resp = client.post("/stock/3496/refresh")
        # 500 にならず 302 でリダイレクトされること
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]

        follow = client.get("/stock/3496")
        html = follow.data.decode()
        assert "再取得に失敗しました (3496)" in html
        assert "boom" in html
        assert "background:#ffeaea" in html


class TestCorporateUrlPostRoutes:
    """POST /stock/<code_s>/corporate_url のテスト (issue #208)"""

    def test_corporate_url_post_saves_override(self, client, db_path):
        """正常 URL を渡すと record.corporate_url_override が更新され info flash が出る"""
        resp = client.post("/stock/3496/corporate_url", data={
            "url": "https://example.com/ir",
        })
        assert resp.status_code == 302
        assert "/stock/3496" in resp.headers["Location"]
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded["corporate_url_override"] == "https://example.com/ir"

        follow = client.get("/stock/3496")
        html = follow.data.decode()
        assert "会社HPリンクを更新しました (3496)" in html
        assert "background:#eaffea" in html

    def test_corporate_url_post_empty_clears_override(self, client, db_path):
        """空文字を渡すと上書きがクリアされ「デフォルトに戻しました」flash が出る"""
        # 事前に上書きを入れておく
        rec = rs.get_research_record("3496", db_path=db_path)
        rec["corporate_url_override"] = "https://old.example.com"
        rs.upsert_research_record(rec, db_path=db_path)

        resp = client.post("/stock/3496/corporate_url", data={"url": ""})
        assert resp.status_code == 302
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded["corporate_url_override"] == ""

        follow = client.get("/stock/3496")
        html = follow.data.decode()
        assert "会社HPリンクをデフォルトに戻しました (3496)" in html

    def test_corporate_url_post_rejects_invalid_scheme(self, client, db_path):
        """http/https 以外で始まる URL は error flash で拒否し、record は変更されない"""
        resp = client.post("/stock/3496/corporate_url", data={
            "url": "javascript:alert(1)",
        })
        assert resp.status_code == 302
        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded["corporate_url_override"] == ""  # 変更されない

        follow = client.get("/stock/3496")
        html = follow.data.decode()
        assert "会社HPリンクの保存に失敗しました (3496)" in html
        assert "http://" in html or "https://" in html  # エラーメッセージ本文
        assert "background:#ffeaea" in html

    def test_corporate_url_post_handles_unregistered_record(self, client):
        """research_shelve 未登録の銘柄コードでは error flash で 302 リダイレクトする"""
        # 9999 は populated_db で登録していない
        resp = client.post("/stock/9999/corporate_url", data={
            "url": "https://example.com",
        })
        assert resp.status_code == 302
        assert "/stock/9999" in resp.headers["Location"]
        # 9999 は detail 取得時に 404 になるためリダイレクト先で flash 確認は省略
        # (flash は session に保存されるので次のリクエストで消費される)

    def test_detail_button_data_current_uses_override_not_display_url(self, client, db_path):
        """✎ ボタンの data-current は override のみを参照する (既定URLは入れない)

        prompt のデフォルト表示に既定URLが流れ込むと、ユーザーが ✎ を開いて
        そのまま OK するだけで既定URLが override として固定化されてしまう。
        data-current は実際の override 値のみを渡す必要がある。
        """
        # 上書き無し: data-current は空文字
        html = client.get("/stock/3496").data.decode()
        assert 'data-current=""' in html

        # 上書き有り: data-current は override 値
        rec = rs.get_research_record("3496", db_path=db_path)
        rec["corporate_url_override"] = "https://example.com/ir"
        rs.upsert_research_record(rec, db_path=db_path)
        html = client.get("/stock/3496").data.decode()
        assert 'data-current="https://example.com/ir"' in html

    def test_corporate_url_post_does_not_pin_default_url(self, client, db_path, monkeypatch):
        """既定URLと同じ値を保存しても override に固定化されない (codex review対応)

        ✎ を開いてそのまま OK しただけで stocks_shelve 側の corporate_url が
        自動更新で変わっても銘柄詳細に反映されなくなる事故を防ぐ。
        """
        # 3496 の stocks_shelve.corporate_url をテスト用に固定
        from webapp import helpers as _h
        original = _h.get_stock_data
        def patched(code_s):
            data = dict(original(code_s) or {})
            data["corporate_url"] = "https://example.com/default"
            return data
        monkeypatch.setattr(_h, "get_stock_data", patched)

        resp = client.post("/stock/3496/corporate_url", data={
            "url": "https://example.com/default",  # 既定値と同一
        })
        assert resp.status_code == 302
        loaded = rs.get_research_record("3496", db_path=db_path)
        # override は空のまま (デフォルト継続)
        assert loaded["corporate_url_override"] == ""

        follow = client.get("/stock/3496")
        html = follow.data.decode()
        assert "会社HPリンクをデフォルトに戻しました (3496)" in html

    def test_detail_hides_corporate_link_when_no_url(self, client, db_path, monkeypatch):
        """corporate_url も corporate_url_override も空のときリンク要素自体が出ない"""
        # 3496 の override は空のまま、stock の corporate_url を空に置き換え
        from webapp import helpers as _h
        original = _h.get_stock_data
        def patched(code_s):
            data = dict(original(code_s) or {})
            data["corporate_url"] = ""
            return data
        monkeypatch.setattr(_h, "get_stock_data", patched)
        # detail.py 側も同じ helpers を import 経由で参照しているか確認のため
        # routes.detail の名前空間にも patch を当てる
        from webapp.routes import detail as _d
        monkeypatch.setattr(_d, "get_stock_data", patched)

        resp = client.get("/stock/3496")
        html = resp.data.decode()
        # 「会社HP」ラベルが出ていない (リンク要素ごと非表示)
        assert ">会社HP<" not in html
        assert ">会社HP✎<" not in html
        # ✎ ボタン (上書き入口) は常に表示される
        assert 'action="/stock/3496/corporate_url"' in html


class TestKessanCommentApiContract:
    """GET/POST /api/kessan_comment のレスポンス契約 (issue #133)

    旧 post_price_change を返さず、新 post_price_changes のみ返すことを保証する。
    """

    def test_get_unregistered_returns_only_new_schema(self, client):
        """未登録銘柄 GET → デフォルトレスポンスは post_price_changes のみで旧キー無し"""
        resp = client.get("/api/kessan_comment/9999?kessanbi=2026/04/01")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "post_price_changes" in data
        assert data["post_price_changes"] == {"1d": "", "5d": "", "20d": ""}
        assert "post_price_change" not in data

    def test_get_legacy_record_returns_normalized_dict(self, client):
        """旧 post_price_change のみのレコード GET → post_price_changes に正規化、旧キー無し"""
        # fixture の 2024/05/14 は post_price_change="-3.1" のみ
        resp = client.get("/api/kessan_comment/3496?kessanbi=2024/05/14")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["post_price_changes"] == {"1d": "-3.1", "5d": "", "20d": ""}
        assert "post_price_change" not in data

    def test_get_new_format_record_passthrough(self, client, db_path):
        """新形式 post_price_changes 持ちレコード GET → そのまま、旧キー無し"""
        # 直接 shelve に新形式を書き込む
        rec = rs.get_research_record("3496", db_path=db_path)
        rec["kessan_comments"].append({
            "kessanbi": "2026/03/15",
            "quarter": 4,
            "pre_expectation": "○",
            "pre_outlook": "テスト",
            "post_price_changes": {"1d": "+2.5", "5d": "+4.0", "20d": "+8.0"},
            "post_comment": "新形式",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        })
        rs.upsert_research_record(rec, db_path=db_path)
        resp = client.get("/api/kessan_comment/3496?kessanbi=2026/03/15")
        data = resp.get_json()
        assert data["post_price_changes"] == {"1d": "+2.5", "5d": "+4.0", "20d": "+8.0"}
        assert "post_price_change" not in data

    def test_post_response_returns_only_new_schema(self, client, monkeypatch):
        """POST 後のレスポンスにも post_price_changes のみ含まれ、旧キー無し"""
        from webapp import helpers as _helpers
        monkeypatch.setattr(
            _helpers, "calc_price_reactions",
            lambda c, k: {"1d": "+1.0", "5d": "+2.0", "20d": "+3.0"},
        )
        monkeypatch.setattr(_helpers, "_is_possess_now", lambda c: False)
        resp = client.post("/api/kessan_comment/3496", data={
            "kessanbi": "2026/04/22",
            "quarter": "1",
            "pre_expectation": "○",
            "pre_outlook": "API契約テスト",
            "post_comment": "",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["post_price_changes"] == {"1d": "+1.0", "5d": "+2.0", "20d": "+3.0"}
        assert "post_price_change" not in data


class TestDisclosureRoute:
    """GET /disclosure のテスト (issue #148 関連で新設)"""

    def test_disclosure_returns_200(self, client):
        """ファイル未生成でも 200 を返し、未生成案内を表示"""
        resp = client.get("/disclosure")
        assert resp.status_code == 200

    def test_disclosure_shows_unavailable_message_when_html_missing(self, client, monkeypatch):
        """disclosure_data.html 未生成時は未生成案内が表示される"""
        from webapp.routes import disclosure as _disc_route
        monkeypatch.setattr(_disc_route, "get_disclosure_html_parts", lambda: {"available": ""})
        resp = client.get("/disclosure")
        html = resp.data.decode()
        assert "disclosure_data.html" in html
        assert "未生成" in html

    def test_disclosure_renders_html_when_available(self, client, monkeypatch):
        """disclosure_data.html が利用可能なら body / header / footer が描画される"""
        from webapp.routes import disclosure as _disc_route
        monkeypatch.setattr(_disc_route, "get_disclosure_html_parts", lambda: {
            "available": "1",
            "css": ".disc-table { color: red; }",
            "header": "<h1>適宜開示 <span>2026-04-26</span></h1>",
            "body": '<table class="disc-table"><tr><td>テストデータ</td></tr></table>',
            "footer": "<footer>fin</footer>",
        })
        resp = client.get("/disclosure")
        html = resp.data.decode()
        assert "テストデータ" in html
        assert "適宜開示" in html
        assert "fin" in html

    def test_global_nav_has_disclosure_link(self, client):
        """グローバルナビに /disclosure へのリンクがある (タブ名は「決算・開示」)"""
        resp = client.get("/")
        html = resp.data.decode()
        assert 'href="/disclosure"' in html
        assert "決算・開示" in html


class TestDisclosureRouteKessanCard:
    """GET /disclosure の決算日カード表示テスト (issue #213 で /market から移設)

    当日決算 (kessanbi == today) は中身は past 扱いで反応コメ枠を出すが、
    カードの見た目 ("(済)" ラベル / past クラス) は通常表示にする。
    """

    def _make_stub_entry(self, code_s, kessanbi, **overrides):
        base = {
            "code_s": code_s,
            "stock_name": f"銘柄{code_s}",
            "kessanbi": kessanbi,
            "quarter": 4,
            "pre_expectation": "",
            "pre_outlook": "",
            "post_price_changes": {"1d": "", "5d": "", "20d": ""},
            "post_comment": "",
            "has_comment": False,
            "is_possess": False,
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }
        base.update(overrides)
        return base

    def test_today_card_has_no_done_label(self, client, monkeypatch):
        """当日決算カード (today_entries) は "(済)" ラベルなし、past クラスなしで描画される"""
        from datetime import datetime as _dt
        today_str = _dt.today().strftime("%Y/%m/%d")
        today_md = _dt.today().strftime("%m/%d")
        yesterday_str = "2026/04/26"

        from webapp.routes import disclosure as _disc_route
        monkeypatch.setattr(
            _disc_route, "get_market_kessan_data",
            lambda: {
                "base_day": _dt.today().date(),
                "future_entries": [],
                "past_entries": [],
                "recent_past_entries": [
                    (yesterday_str, [self._make_stub_entry("7203", yesterday_str)]),
                ],
                "today_entries": [
                    (today_str, [self._make_stub_entry("6501", today_str)]),
                ],
                "older_past_entries": [],
            },
        )

        resp = client.get("/disclosure")
        html = resp.data.decode()
        assert resp.status_code == 200

        # 当日カード: "(済)" なし、past クラスなし
        assert f'<div class="card-date">{today_md}</div>' in html
        # 前日カード: "(済)" あり、past クラスあり
        assert '<div class="card-date">04/26 (済)</div>' in html
        assert 'kessan-card past' in html

    def test_today_card_still_renders_post_fields(self, client, monkeypatch):
        """当日カードでも中身は past (is_past=True) として render され、反応コメ・株価変動率枠が出る"""
        from datetime import datetime as _dt
        today_str = _dt.today().strftime("%Y/%m/%d")

        from webapp.routes import disclosure as _disc_route
        monkeypatch.setattr(
            _disc_route, "get_market_kessan_data",
            lambda: {
                "base_day": _dt.today().date(),
                "future_entries": [],
                "past_entries": [],
                "recent_past_entries": [],
                "today_entries": [
                    (today_str, [self._make_stub_entry("6501", today_str)]),
                ],
                "older_past_entries": [],
            },
        )

        resp = client.get("/disclosure")
        html = resp.data.decode()
        # data-is-past="1" で past 扱い (反応コメ枠が出る)
        assert 'data-is-past="1"' in html


class TestMarketRouteThemeNews:
    """issue #165: /market に theme-news 調査結果と手動実行ボタンを表示する。

    - 当日 (.md + .md.done) があれば当日表示
    - 当日無ければ前回分 (最新 .md.done 付き) を「前回分」ラベルで表示
    - 当日 .md.running があれば「実行中...」+ ボタン disabled
    - meta.json があれば「💰 約$X」コストバッジ
    POST /market/theme_news/run / GET /market/theme_news/status の挙動も検証
    """

    def _setup_history_dir(self, tmp_path, monkeypatch, fixed_today):
        from webapp.routes import market as _market_route
        monkeypatch.setattr(_market_route, "get_price_day", lambda _now: fixed_today)
        monkeypatch.setattr(_market_route, "_THEME_NEWS_HISTORY_DIR", tmp_path)
        return _market_route

    @pytest.mark.parametrize("today_md,today_done,prev_md,prev_done,expect_label,expect_btn_disabled", [
        # 当日両揃い → 当日表示 + 再実行ボタン (disabled なし)
        (True, True, False, False, "2026-05-21", False),
        # 当日 done 無し + 前日両揃い → 前日表示 + 「前回分」ラベル
        (False, False, True, True, "2026-05-20", False),
        # 何も無し → 「未実施」表示
        (False, False, False, False, "未実施", False),
        # 当日途中ファイル (md だけで done 無し) + 前日両揃い → 前日表示 (途中 md は公開しない)
        (True, False, True, True, "2026-05-20", False),
    ])
    def test_display_fallback_and_button(
        self, client, tmp_path, monkeypatch,
        today_md, today_done, prev_md, prev_done, expect_label, expect_btn_disabled,
    ):
        from datetime import date as _date
        fixed_today = _date(2026, 5, 21)
        self._setup_history_dir(tmp_path, monkeypatch, fixed_today)

        if today_md:
            (tmp_path / "2026-05-21.md").write_text("## 当日見出し\n", encoding="utf-8")
        if today_done:
            (tmp_path / "2026-05-21.md.done").touch()
        if prev_md:
            (tmp_path / "2026-05-20.md").write_text("## 前日見出し\n", encoding="utf-8")
        if prev_done:
            (tmp_path / "2026-05-20.md.done").touch()

        resp = client.get("/market")
        html = resp.data.decode()
        assert "📰 テーマニュース調査" in html
        assert expect_label in html
        # 「前回分」ラベルは today_done 無しで前日があるときだけ出る
        if not today_done and prev_done:
            assert "前回分" in html
        # 実行ボタンは常に存在
        assert 'id="theme-news-run-btn"' in html

    def test_running_state_disables_button_and_shows_message(self, client, tmp_path, monkeypatch):
        from datetime import date as _date
        fixed_today = _date(2026, 5, 21)
        self._setup_history_dir(tmp_path, monkeypatch, fixed_today)
        (tmp_path / "2026-05-21.md.running").touch()

        resp = client.get("/market")
        html = resp.data.decode()
        assert "実行中..." in html
        # button[disabled] (空属性 or `disabled="disabled"` のいずれか)
        assert "theme-news-run-btn" in html and "disabled" in html

    def test_meta_cost_badge(self, client, tmp_path, monkeypatch):
        import json
        from datetime import date as _date
        fixed_today = _date(2026, 5, 21)
        self._setup_history_dir(tmp_path, monkeypatch, fixed_today)
        (tmp_path / "2026-05-21.md").write_text("# 本文", encoding="utf-8")
        (tmp_path / "2026-05-21.md.done").touch()
        (tmp_path / "2026-05-21.md.meta.json").write_text(
            json.dumps({"estimated_cost_usd": 28.53, "usage": {}}), encoding="utf-8"
        )
        resp = client.get("/market")
        html = resp.data.decode()
        assert "💰 約 $28.53" in html

    def test_run_endpoint_409_when_already_running(self, client, tmp_path, monkeypatch):
        from datetime import date as _date
        fixed_today = _date(2026, 5, 21)
        self._setup_history_dir(tmp_path, monkeypatch, fixed_today)
        (tmp_path / "2026-05-21.md.running").touch()

        resp = client.post("/market/theme_news/run")
        assert resp.status_code == 409
        assert resp.get_json()["status"] == "already_running"

    def test_status_endpoint_reports_marker_states(self, client, tmp_path, monkeypatch):
        from datetime import date as _date
        fixed_today = _date(2026, 5, 21)
        self._setup_history_dir(tmp_path, monkeypatch, fixed_today)
        # 完了状態
        (tmp_path / "2026-05-21.md").write_text("# 本文", encoding="utf-8")
        (tmp_path / "2026-05-21.md.done").touch()

        resp = client.get("/market/theme_news/status")
        assert resp.status_code == 200
        s = resp.get_json()
        assert s["date"] == "2026-05-21"
        assert s["done"] is True
        assert s["running"] is False
        assert s["has_today_history"] is True


class TestMarketRouteCalendar:
    """issue #165: /market に株カレンダーを表示する。

    events.json (theme-news skill が更新) を読んで Jinja に渡し、
    インラインで JS レンダリングする。3 ケース:
      - 正常: 配列で 2 件 → available=True, count=2, summary に "(2件)" 表示
      - ファイル無し: available=False, summary に "(未登録)" 表示
      - 壊れた JSON: 同上 (例外を出さない)
    """

    @pytest.mark.parametrize("scenario,content,expect_available,expect_count,expect_summary", [
        ("valid_2",
         '[{"id":"a","title":"イベントA","start":"2026-05-20","end":"2026-05-20","importance":"high","themes":["半導体"],"body":"本文A"},'
         ' {"id":"b","title":"イベントB","start":"2026-05-22","end":"2026-05-22","importance":"mid","themes":[],"body":"本文B"}]',
         True, 2, "(2件)"),
        ("missing", None,   False, 0, "(未登録)"),
        ("broken",  "{not valid json", False, 0, "(未登録)"),
    ])
    def test_load_calendar_payload_and_render(
        self, client, tmp_path, monkeypatch, scenario, content, expect_available, expect_count, expect_summary,
    ):
        from datetime import date as _date
        from webapp.routes import market as _market_route

        fixed_today = _date(2026, 5, 21)
        monkeypatch.setattr(_market_route, "get_price_day", lambda _now: fixed_today)
        events_path = tmp_path / "events.json"
        if content is not None:
            events_path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(_market_route, "_CALENDAR_EVENTS_JSON", events_path)

        payload = _market_route._load_calendar_payload()
        assert payload["available"] is expect_available
        assert payload["events_count"] == expect_count
        assert payload["today"] == "2026-05-21"

        resp = client.get("/market")
        html = resp.data.decode()
        assert "📅 株カレンダー" in html
        assert expect_summary in html
        if expect_available and expect_count > 0:
            # インラインテンプレートが include され、events が JSON で埋め込まれる
            assert 'class="theme-news-calendar"' in html
            assert "イベントA" in html


class TestDetailGyoutaiThemes:
    """issue #205: 銘柄詳細ページの業態・テーマ inline 編集 (AJAX 即時保存)。

    既存 portfolio.update_memo (POST /portfolio/<code_s>/memo) を AJAX で再利用。
    保存ボタンは無く、change/blur で即送信する portfolio_list.html と同方式。
    """

    @pytest.fixture
    def portfolio_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec_3496 = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec_3496, db_path=db_path)
        rec_1234 = rs.create_research_record("1234", "テスト未登録", overall_rating="B")
        rs.upsert_research_record(rec_1234, db_path=db_path)

        ps.add_to_watch("3496", reason="テスト用", db_path=portfolio_db)
        ps.transition_status("3496", "1保", reason="テスト用 1保 へ", db_path=portfolio_db)
        # issue #282: テーママスター必須化に伴い、テストで使う name を登録
        ps.create_theme("AI", db_path=portfolio_db)
        ps.create_theme("半導体", db_path=portfolio_db)
        ps.create_theme("新規テーマ", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app, portfolio_db

    @pytest.fixture
    def portfolio_client(self, portfolio_app):
        app, _ = portfolio_app
        return app.test_client()

    def test_registered_stock_renders_inline_inputs(self, portfolio_client):
        """登録済銘柄: 業態・テーマ inline edit (data-code 付き wrapper + 2 select) が出る"""
        html = portfolio_client.get("/stock/3496").data.decode()
        assert 'class="gyoutai-themes-inline" data-code="3496"' in html
        assert 'name="gyoutai_themes_0"' in html
        assert 'name="gyoutai_themes_1"' in html
        # AJAX 化したので保存ボタンは無い、form タグも無い
        assert "action=\"/portfolio/3496/memo\"" not in html

    def test_existing_themes_prefilled(self, portfolio_app):
        """事前に保存したテーマが select の selected 属性として出力される"""
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        ps.update_memo("3496", {"gyoutai_themes": ["AI", "半導体"]}, db_path=portfolio_db)

        html = app.test_client().get("/stock/3496").data.decode()
        # select 化されたので value="X" selected の形式で確認
        assert 'value="AI"' in html
        assert 'value="半導体"' in html
        assert 'selected' in html

    def test_unregistered_stock_hides_inline_inputs(self, portfolio_client):
        """未登録銘柄では inline edit (gyoutai_themes_0 入力) が出ない"""
        html = portfolio_client.get("/stock/1234").data.decode()
        assert 'name="gyoutai_themes_0"' not in html
        assert 'class="gyoutai-themes-inline"' not in html

    def test_ajax_post_returns_json_with_themes(self, portfolio_app):
        """X-Requested-With 付き POST → 200 + JSON、display.gyoutai_themes に保存値が返る"""
        app, _ = portfolio_app
        client = app.test_client()
        resp = client.post(
            "/portfolio/3496/memo",
            data={"gyoutai_themes_0": "新規テーマ", "gyoutai_themes_1": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["display"]["gyoutai_themes"] == ["新規テーマ"]

    def test_ajax_post_persists(self, portfolio_app):
        """AJAX POST 後、portfolio_shelve.memo.gyoutai_themes に反映される"""
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        client = app.test_client()
        client.post(
            "/portfolio/3496/memo",
            data={"gyoutai_themes_0": "新規テーマ", "gyoutai_themes_1": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        rec = ps.get_record("3496", db_path=portfolio_db)
        assert (rec.get("memo") or {}).get("gyoutai_themes") == ["新規テーマ"]

    def test_ajax_post_clears_when_all_empty(self, portfolio_app):
        """既存テーマあり → 全スロット空文字 AJAX POST → gyoutai_themes が空 list に"""
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        ps.update_memo("3496", {"gyoutai_themes": ["AI", "半導体"]}, db_path=portfolio_db)

        client = app.test_client()
        client.post(
            "/portfolio/3496/memo",
            data={"gyoutai_themes_0": "", "gyoutai_themes_1": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        rec = ps.get_record("3496", db_path=portfolio_db)
        assert (rec.get("memo") or {}).get("gyoutai_themes") == []

    def test_detail_renders_when_memo_is_not_dict(self, portfolio_app, monkeypatch):
        """memo が None / 非 dict の旧データでも詳細ページが 500 にならない (codex P2)。

        _normalize_loaded_memo は非 dict の memo を素通しするので、
        portfolio_record["memo"] が None になり得る。`get("memo", {}).get(...)` は
        memo キーが存在し値が None だと AttributeError を投げるため、
        詳細ページ側で dict ガードが必要。

        ps.get_record をモンキーパッチして memo=None のレコードを返させる。
        """
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app

        original_get = ps.get_record

        def patched_get(code_s, db_path=None):
            rec = original_get(code_s, db_path=db_path)
            if rec and code_s == "3496":
                rec = dict(rec)
                rec["memo"] = None
            return rec

        monkeypatch.setattr(ps, "get_record", patched_get)

        # GET /stock/3496 で 500 にならないこと
        resp = app.test_client().get("/stock/3496")
        assert resp.status_code == 200
        # 業態・テーマ inline edit (input) は出る (空値で)
        html = resp.data.decode()
        assert 'name="gyoutai_themes_0"' in html


class TestTransitionWithQty:
    """issue #269: POST /portfolio/<code>/transition に qty を同時送信したときの挙動"""

    @pytest.fixture
    def portfolio_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec, db_path=db_path)
        # 3496 を 1保 で登録
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)
        ps.transition_status("3496", "1保", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app, portfolio_db

    @pytest.mark.parametrize(
        "new_status, qty_form, expected_qty, expected_status",
        [
            # 同一ステータス (1保 → 1保) + qty 更新 → status 変わらず、qty だけ反映
            ("1保", "250", 250, "1保"),
            # 異ステータス遷移 (1保 → 2準) + qty 送信 → status 変更、qty は無視 (元のまま)
            ("2準", "999", 0, "2準"),  # add_to_watch 直後 qty=0、2準 では update_qty が呼ばれない
            # 1保 のまま qty="" (空) → qty 変更なし、status も 1保 のまま (no-op)
            ("1保", "", 0, "1保"),
        ],
        ids=["same-1ho-update-qty", "to-2jun-ignore-qty", "1ho-empty-noop"],
    )
    def test_transition_qty_combinations(
        self, portfolio_app, new_status, qty_form, expected_qty, expected_status
    ):
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        client = app.test_client()

        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": new_status, "qty": qty_form, "reason": ""},
        )
        # redirect (302) または 200 を許容 (実装は redirect)
        assert resp.status_code in (200, 302)

        rec = ps.get_record("3496", db_path=portfolio_db)
        assert rec["status"] == expected_status
        assert rec["qty"] == expected_qty

    def test_transition_rejects_invalid_qty(self, portfolio_app):
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        client = app.test_client()

        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "1保", "qty": "-5", "reason": ""},
        )
        # error flash + redirect、status/qty は変更されない (1保 / qty=0 のまま)
        assert resp.status_code in (200, 302)
        rec = ps.get_record("3496", db_path=portfolio_db)
        assert rec["status"] == "1保"
        assert rec["qty"] == 0


class TestTransitionRequiresStrategy:
    """issue #363: 新規1保遷移 (2準/3監→1保) は売買戦略 (trade_idea) を必須化する"""

    @pytest.fixture
    def portfolio_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec, db_path=db_path)
        # 3496 を 3監 で登録 (add_to_watch は 3監 で登録)
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app, portfolio_db

    @pytest.mark.parametrize(
        "trade_idea_form, expect_status, expect_idea",
        [
            # 空 (未分類) のまま 1保 → 弾かれて 3監 のまま、戦略も空
            ("", "3監", ""),
            # 有効な戦略 (シードされる "GARP") → 1保 に遷移し戦略も保存
            ("GARP", "1保", "GARP"),
            # マスター未登録の戦略名 → update_memo が ValueError で弾き、3監 のまま
            ("存在しない戦略", "3監", ""),
        ],
        ids=["empty-rejected", "valid-strategy-ok", "invalid-strategy-rejected"],
    )
    def test_new_hold_requires_strategy(
        self, portfolio_app, trade_idea_form, expect_status, expect_idea
    ):
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        client = app.test_client()

        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "1保", "trade_idea": trade_idea_form, "reason": ""},
        )
        assert resp.status_code in (200, 302)

        rec = ps.get_record("3496", db_path=portfolio_db)
        assert rec["status"] == expect_status
        assert (rec["memo"].get("trade_idea") or "") == expect_idea

    def test_existing_hold_qty_change_no_strategy_required(self, portfolio_app):
        """既に1保の株数のみ変更は戦略必須化の対象外 (trade_idea なしでも成功)"""
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        # 先に戦略マスターをシードし、戦略付きで 1保 にしておく
        ps.seed_trade_ideas(db_path=portfolio_db)
        ps.update_memo("3496", {"trade_idea": "GARP"}, db_path=portfolio_db)
        ps.transition_status("3496", "1保", db_path=portfolio_db)
        client = app.test_client()

        # 1保 のまま株数のみ変更 (trade_idea 送信なし) → 成功、戦略は保持
        resp = client.post(
            "/portfolio/3496/transition",
            data={"new_status": "1保", "qty": "300", "reason": ""},
        )
        assert resp.status_code in (200, 302)
        rec = ps.get_record("3496", db_path=portfolio_db)
        assert rec["status"] == "1保"
        assert rec["qty"] == 300
        assert rec["memo"].get("trade_idea") == "GARP"


class TestPortfolioHoldSummary:
    """保有 (status=hold) フィルタ表示時の運用総額 / 保有株数更新日サマリーの統合テスト"""

    @pytest.fixture
    def portfolio_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        rec = rs.create_research_record("3496", "アズーム", overall_rating="A")
        rs.upsert_research_record(rec, db_path=db_path)
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)
        ps.transition_status("3496", "1保", db_path=portfolio_db)
        ps.update_qty("3496", 100, db_path=portfolio_db)

        # 一覧画面は _bulk_get_stock_data 経由で stocks_shelve を読むので、
        # こちらを差し替えて price を固定 (position_value 計算用)
        from webapp import helpers as _h

        def patched_bulk(code_list):
            return {c: ({"price": 2500} if c == "3496" else {}) for c in code_list}

        monkeypatch.setattr(_h, "_bulk_get_stock_data", patched_bulk)

        app = create_app()
        app.config["TESTING"] = True
        return app

    def test_hold_filter_shows_summary(self, portfolio_app):
        client = portfolio_app.test_client()
        resp = client.get("/portfolio?status=hold")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "運用総額:" in html
        assert "25 万円" in html  # 2500 × 100 / 10000 = 25
        assert "保有株数更新日:" in html
        # update_qty を呼んでいるので「未記録」ではない
        assert "未記録" not in html

    def test_other_filters_hide_summary(self, portfolio_app):
        client = portfolio_app.test_client()
        for status in ("semi", "watch", ""):
            resp = client.get(f"/portfolio?status={status}")
            html = resp.data.decode()
            assert "運用総額:" not in html, f"status={status} でサマリーが漏れている"
            assert "保有株数更新日:" not in html, f"status={status} でサマリーが漏れている"


# ==================================================
# /portfolio/themes (issue #282)
# ==================================================
class TestPortfolioThemes:
    """テーママスター編集画面の smoke テスト"""

    @pytest.fixture
    def themes_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
        # fallback_mode を外すため最低 1 件 record を入れる
        ps.add_to_watch("3496", db_path=portfolio_db)
        ps.create_theme("半導体", "test", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app, portfolio_db

    def test_index_returns_200(self, themes_app):
        app, _ = themes_app
        client = app.test_client()
        resp = client.get("/portfolio/themes")
        assert resp.status_code == 200
        assert "半導体" in resp.data.decode()

    def test_create_and_delete_roundtrip(self, themes_app):
        import portfolio_shelve as ps
        app, portfolio_db = themes_app
        client = app.test_client()
        # 作成
        resp = client.post(
            "/portfolio/themes/create",
            data={"name": "防衛", "description": "防衛関連"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert ps.get_theme("防衛", db_path=portfolio_db) is not None
        # 削除
        resp = client.post(
            "/portfolio/themes/防衛/delete", follow_redirects=False
        )
        assert resp.status_code == 302
        assert ps.get_theme("防衛", db_path=portfolio_db) is None

    def test_update_renames(self, themes_app):
        import portfolio_shelve as ps
        app, portfolio_db = themes_app
        client = app.test_client()
        resp = client.post(
            "/portfolio/themes/半導体/update",
            data={"name": "セミコン", "description": "renamed"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert ps.get_theme("半導体", db_path=portfolio_db) is None
        assert ps.get_theme("セミコン", db_path=portfolio_db)["description"] == "renamed"


class TestPortfolioThemeSummary:
    """業態テーマ別 RS サマリー画面の smoke テスト (issue #283)"""

    @pytest.fixture
    def summary_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)
        # 業態テーマ付きで 1 件登録 (集計対象になる)
        memo = ps.create_memo(gyoutai_themes=["半導体"])
        ps.add_to_watch("3496", memo=memo, db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app

    @pytest.mark.parametrize("url", [
        "/portfolio/themes/summary",
        "/portfolio/themes/summary?sort=dev_1d",
        "/portfolio/themes/summary?sort=dev_a",
        "/portfolio/themes/summary?sort=dev_b",
    ])
    def test_summary_returns_200(self, summary_app, url):
        client = summary_app.test_client()
        resp = client.get(url)
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "業態テーマ別 RS サマリー" in html
        assert "半導体" in html


class TestChatLinkRoutes:
    """issue #265: 外部チャットリンク AJAX ルート"""

    def test_add_update_delete_flow(self, client, db_path):
        """追加 → 更新 → 削除の一連と永続化を検証"""
        # 追加 (201, links に1件)
        resp = client.post("/stock/3496/chat_link",
                           data={"label": "ChatGPT", "url": "https://chat.example/a"})
        assert resp.status_code == 201
        assert resp.get_json()["links"] == [
            {"label": "ChatGPT", "url": "https://chat.example/a"}
        ]
        # 永続化確認
        assert rs.get_research_record("3496", db_path=db_path)["chat_links"] == [
            {"label": "ChatGPT", "url": "https://chat.example/a"}
        ]
        # 更新 (200, index 0 を上書き)
        resp = client.post("/stock/3496/chat_link/0",
                           data={"label": "Claude", "url": "https://claude.example/b"})
        assert resp.status_code == 200
        assert resp.get_json()["links"] == [
            {"label": "Claude", "url": "https://claude.example/b"}
        ]
        # 削除 (200, 空に戻る)
        resp = client.post("/stock/3496/chat_link/0/delete")
        assert resp.status_code == 200
        assert resp.get_json()["links"] == []
        assert rs.get_research_record("3496", db_path=db_path)["chat_links"] == []

    @pytest.mark.parametrize("path, data, status", [
        # 不正 URL (http/https 以外) → 400
        ("/stock/3496/chat_link", {"label": "x", "url": "ftp://x"}, 400),
        ("/stock/3496/chat_link", {"label": "x", "url": ""}, 400),
        # 未登録銘柄 → 404
        ("/stock/9999/chat_link", {"label": "x", "url": "https://x.example"}, 404),
        # index 範囲外 (chat_links 空なので 0 も範囲外) → 400
        ("/stock/3496/chat_link/5", {"label": "x", "url": "https://x.example"}, 400),
        ("/stock/3496/chat_link/5/delete", {}, 400),
    ])
    def test_error_cases(self, client, path, data, status):
        resp = client.post(path, data=data)
        assert resp.status_code == status
        assert resp.get_json()["ok"] is False


class TestSuggestThemes:
    """issue #297: POST /stock/<code_s>/suggest_themes (LLM 業態テーマ提案)。

    claude -p は呼ばず theme_suggest.suggest_gyoutai_themes をモックして、
    エンドポイントのガード分岐 (設定済み409・事業テキスト空・正常提案) を検証する。
    """

    @pytest.fixture
    def suggest_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        # 事業テキストあり銘柄 (overview + shikiho_comments)
        rec = rs.create_research_record(
            "3496", "アズーム", overall_rating="A",
            overview="駐車場サブリース", shikiho_comments=["最高益"],
        )
        rs.upsert_research_record(rec, db_path=db_path)
        # 事業テキスト空銘柄
        rec_empty = rs.create_research_record("1234", "空テスト", overall_rating="B")
        rs.upsert_research_record(rec_empty, db_path=db_path)
        # テーママスター登録
        ps.create_theme("不動産", db_path=portfolio_db)
        ps.create_theme("AI", db_path=portfolio_db)
        # 3496 を 3監 で登録 (テーマ未設定)
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)
        ps.add_to_watch("1234", reason="テスト", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app

    def test_suggest_returns_confidence_buckets(self, suggest_app, monkeypatch):
        """事業テキスト・マスターありで {preset, low, new} 形式が返る"""
        monkeypatch.setattr(
            "webapp.routes.memo.theme_suggest.suggest_gyoutai_themes",
            lambda business_text, theme_names: {
                "preset": [{"name": "不動産", "confidence": 80}],
                "low": [{"name": "AI", "confidence": 40}],
                "new": [{"name": "認証ソリューション", "confidence": 75, "reason": "..."}],
            },
        )
        resp = suggest_app.test_client().post("/stock/3496/suggest_themes")
        assert resp.status_code == 200
        json = resp.get_json()
        assert json["ok"] is True
        assert json["preset"] == [{"name": "不動産", "confidence": 80}]
        assert json["low"] == [{"name": "AI", "confidence": 40}]
        assert json["new"][0]["name"] == "認証ソリューション"

    def test_suggest_empty_business_text(self, suggest_app):
        """事業テキスト空銘柄は LLM を呼ばず空の buckets + reason を返す"""
        resp = suggest_app.test_client().post("/stock/1234/suggest_themes")
        assert resp.status_code == 200
        assert resp.get_json() == {
            "ok": True, "preset": [], "low": [], "new": [],
            "reason": "no_business_text",
        }

    def test_suggest_rejects_already_set(self, suggest_app):
        """業態テーマ設定済み銘柄は 409 で拒否 (サーバー側ガード)"""
        import portfolio_shelve as ps
        ps.update_memo("3496", {"gyoutai_themes": ["不動産"]})
        resp = suggest_app.test_client().post("/stock/3496/suggest_themes")
        assert resp.status_code == 409


# ==================================================
# /portfolio/shikiho (issue #313)
# ==================================================
class TestPortfolioShikihoRoute:
    """四季報順次更新ページ (一覧ルート + 軽量 data エンドポイント) の統合テスト"""

    @pytest.fixture
    def shikiho_app(self, db_path, tmp_path, monkeypatch):
        import portfolio_shelve as ps
        portfolio_db = str(tmp_path / "test_portfolio_shelve")
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", portfolio_db)
        monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", portfolio_db)

        period = rs.current_shikiho_period()
        # 3496: 今号コメントあり (=入力済み)。9999: research 未登録 (portfolio のみ)。
        rec = rs.create_research_record(
            "3496", "アズーム", overall_rating="A",
            overview="駐車場サブリース",
            shikiho_comments=[
                {"period": "25.12", "comment": "旧号コメント"},
                {"period": period, "comment": "今号コメント"},
            ],
        )
        rs.upsert_research_record(rec, db_path=db_path)
        # _parse_status_filter は status 未指定時に保有 (1保) をデフォルトにするため、
        # charts() と同じく ?status= (空="すべて") を付けてテストする。両銘柄は 3監 のまま。
        ps.add_to_watch("3496", reason="テスト", db_path=portfolio_db)
        ps.add_to_watch("9999", reason="未登録テスト", db_path=portfolio_db)

        app = create_app()
        app.config["TESTING"] = True
        return app

    def test_shikiho_page_renders_with_progress(self, shikiho_app):
        period = rs.current_shikiho_period()
        resp = shikiho_app.test_client().get("/portfolio/shikiho?status=")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert period in html  # 今号 period 表示
        # research 登録済みの 3496 は対象、未登録の 9999 は除外される
        assert "3496" in html
        # done_count=1 (3496 が今号入力済み)、total=1 (9999 除外)
        assert '<span id="done-count">1</span> / 1' in html

    def test_shikiho_data_returns_overview_and_comments(self, shikiho_app):
        period = rs.current_shikiho_period()
        resp = shikiho_app.test_client().get("/portfolio/shikiho/3496/data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["overview"] == "駐車場サブリース"
        # 降順 (新しい period が先頭)
        assert body["shikiho_comments"][0]["period"] == period

    def test_shikiho_data_404_for_unregistered(self, shikiho_app):
        resp = shikiho_app.test_client().get("/portfolio/shikiho/9999/data")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False
