"""webapp ルートの統合テスト (Flaskテストクライアント使用)"""

import os

import pytest

import research_shelve as rs
from webapp import create_app


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_research_shelve")


@pytest.fixture
def app(db_path, monkeypatch):
    """テスト用Flaskアプリ (DBパス差し替え済み)"""
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)

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

    def test_index_shows_records(self, client):
        resp = client.get("/")
        assert "3496" in resp.data.decode()
        assert "アズーム" in resp.data.decode()

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
        """stock_name_prev が入っていれば「新名 (旧○○)」併記される"""
        # 既存テストデータの 3496 に旧名を入れる
        rs.sync_stock_name("3496", "アズームニューネーム", db_path=db_path)
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "アズームニューネーム" in html
        assert "(旧アズーム)" in html

    def test_detail_no_paren_when_stock_name_prev_none(self, client, db_path):
        """stock_name_prev が None なら旧名併記なし (デフォルト状態)"""
        resp = client.get("/stock/3496")
        html = resp.data.decode()
        assert "アズーム" in html
        assert "(旧" not in html


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

    def test_unregistered_stock_shows_add_button(self, portfolio_client):
        """未登録銘柄 (research_shelve には存在): 「+ 監視に追加」ボタン + add POST モーダルが出る"""
        resp = portfolio_client.get("/stock/1234")
        html = resp.data.decode()
        assert "+ 監視に追加" in html
        assert 'action="/portfolio/add"' in html
        assert 'name="return_to" value="detail"' in html

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

        # 除外済みなのでバッジ button や transition モーダルは出ない
        # (CSS 定義文字列と区別するため <button タグにマッチさせる)
        import re
        assert not re.search(r'<button[^>]*class="[^"]*portfolio-badge', html), (
            "除外済み銘柄でもバッジ button が描画されている"
        )
        assert 'action="/portfolio/9999/transition"' not in html
        # 代わりに add (復活) モーダルが出る
        assert "+ 監視に追加" in html
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
        assert data["post_price_changes"] == {"1d": "", "5d": ""}
        assert "post_price_change" not in data

    def test_get_legacy_record_returns_normalized_dict(self, client):
        """旧 post_price_change のみのレコード GET → post_price_changes に正規化、旧キー無し"""
        # fixture の 2024/05/14 は post_price_change="-3.1" のみ
        resp = client.get("/api/kessan_comment/3496?kessanbi=2024/05/14")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["post_price_changes"] == {"1d": "-3.1", "5d": ""}
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
            "post_price_changes": {"1d": "+2.5", "5d": "+4.0"},
            "post_comment": "新形式",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        })
        rs.upsert_research_record(rec, db_path=db_path)
        resp = client.get("/api/kessan_comment/3496?kessanbi=2026/03/15")
        data = resp.get_json()
        assert data["post_price_changes"] == {"1d": "+2.5", "5d": "+4.0"}
        assert "post_price_change" not in data

    def test_post_response_returns_only_new_schema(self, client, monkeypatch):
        """POST 後のレスポンスにも post_price_changes のみ含まれ、旧キー無し"""
        from webapp import helpers as _helpers
        monkeypatch.setattr(
            _helpers, "calc_price_reactions",
            lambda c, k: {"1d": "+1.0", "5d": "+2.0"},
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
        assert data["post_price_changes"] == {"1d": "+1.0", "5d": "+2.0"}
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
        """グローバルナビに /disclosure へのリンクがある"""
        resp = client.get("/")
        html = resp.data.decode()
        assert 'href="/disclosure"' in html
        assert "適宜開示" in html


class TestMarketRouteKessanCard:
    """GET /market の決算日カード表示テスト

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
            "post_price_changes": {"1d": "", "5d": ""},
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

        from webapp.routes import market as _market_route
        monkeypatch.setattr(
            _market_route, "get_market_kessan_data",
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

        resp = client.get("/market")
        html = resp.data.decode()
        assert resp.status_code == 200

        # 当日カード: "(済)" なし、past クラスなし
        assert f'<div class="card-date">{today_md}</div>' in html
        # 前日カード: "(済)" あり、past クラスあり
        assert '<div class="card-date">04/26 (済)</div>' in html
        assert 'kessan-card past' in html

    def test_today_card_appears_between_recent_past_and_future(
        self, client, monkeypatch
    ):
        """カード表示順は recent_past → today → future"""
        from datetime import datetime as _dt
        today_str = _dt.today().strftime("%Y/%m/%d")
        today_md = _dt.today().strftime("%m/%d")
        yesterday_str = "2026/04/26"
        tomorrow_str = "2026/04/30"

        from webapp.routes import market as _market_route
        monkeypatch.setattr(
            _market_route, "get_market_kessan_data",
            lambda: {
                "base_day": _dt.today().date(),
                "future_entries": [
                    (tomorrow_str, [self._make_stub_entry("9984", tomorrow_str)]),
                ],
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

        resp = client.get("/market")
        html = resp.data.decode()
        idx_yesterday = html.find("04/26 (済)")
        idx_today = html.find(f'<div class="card-date">{today_md}</div>')
        idx_tomorrow = html.find("04/30")
        assert 0 < idx_yesterday < idx_today < idx_tomorrow

    def test_today_card_still_renders_post_fields(self, client, monkeypatch):
        """当日カードでも中身は past (is_past=True) として render され、反応コメ・株価変動率枠が出る"""
        from datetime import datetime as _dt
        today_str = _dt.today().strftime("%Y/%m/%d")

        from webapp.routes import market as _market_route
        monkeypatch.setattr(
            _market_route, "get_market_kessan_data",
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

        resp = client.get("/market")
        html = resp.data.decode()
        # data-is-past="1" で past 扱い (反応コメ枠が出る)
        assert 'data-is-past="1"' in html


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

        app = create_app()
        app.config["TESTING"] = True
        return app, portfolio_db

    @pytest.fixture
    def portfolio_client(self, portfolio_app):
        app, _ = portfolio_app
        return app.test_client()

    def test_registered_stock_renders_inline_inputs(self, portfolio_client):
        """登録済銘柄: 業態・テーマ inline edit (data-code 付き wrapper + 2 input) が出る"""
        html = portfolio_client.get("/stock/3496").data.decode()
        assert 'class="gyoutai-themes-inline" data-code="3496"' in html
        assert 'name="gyoutai_themes_0"' in html
        assert 'name="gyoutai_themes_1"' in html
        # AJAX 化したので保存ボタンは無い、form タグも無い
        assert "action=\"/portfolio/3496/memo\"" not in html

    def test_registered_stock_renders_datalist(self, portfolio_client):
        """datalist#gyoutai-theme-choices が出る (候補は空でもタグは描画)"""
        html = portfolio_client.get("/stock/3496").data.decode()
        assert '<datalist id="gyoutai-theme-choices">' in html

    def test_existing_themes_prefilled(self, portfolio_app):
        """事前に保存したテーマが input value にプリフィルされる"""
        import portfolio_shelve as ps
        app, portfolio_db = portfolio_app
        ps.update_memo("3496", {"gyoutai_themes": ["AI", "半導体"]}, db_path=portfolio_db)

        html = app.test_client().get("/stock/3496").data.decode()
        assert 'value="AI"' in html
        assert 'value="半導体"' in html

    def test_unregistered_stock_hides_inline_inputs(self, portfolio_client):
        """未登録銘柄では inline edit (gyoutai_themes_0 input) が出ない"""
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
