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
        resp = client.get("/?code_s=3496")
        assert "アズーム" in resp.data.decode()

        resp = client.get("/?code_s=9999")
        assert "アズーム" not in resp.data.decode()


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
