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
    monkeypatch.setattr("webapp.helpers.RESEARCH_SHELVE", db_path)
    monkeypatch.setattr("webapp.helpers._LOCK_PATH", db_path + ".lock")

    # テストデータ投入
    rec = rs.create_research_record(
        "3496", "アズーム",
        overall_rating="A",
        memo="テストメモ",
        overview="駐車場サブリース",
        shikiho_comments=["最高益"],
    )
    rs.upsert_research_record(rec, db_path=db_path)
    snap = rs.create_snapshot("26.4", ir_quant="[A]28%", ir_comment="好調", data_source="auto")
    rs.upsert_snapshot("3496", snap, db_path=db_path)

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
        })
        assert resp.status_code == 302

    def test_shikiho_post_persists(self, client):
        client.post("/stock/3496/shikiho", data={
            "overview": "更新概要",
            "shikiho_comments_0": "更新コメント",
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
