"""一覧と詳細の往復で条件を維持し、不正な戻り先を受け付けない。"""

from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from tests.test_webapp_routes import app, client, db_path  # noqa: F401


@pytest.mark.parametrize("destination, allowed", [
    ("/?q=半導体&show_results=1", True),
    ("/portfolio?status=&gyoutai_theme=半導体&sort=rs", True),
    ("/portfolio/charts?status=watch", True),
    ("/portfolio/shikiho?status=semi", True),
    ("/portfolio/themes/summary?sort=rank", True),
    ("https://example.com/portfolio", False),
    ("//example.com/portfolio", False),
    ("///portfolio", False),
    ("/\\example.com/portfolio", False),
    ("/portfolio\n", False),
    ("/portfolio#fragment", False),
    ("/dev", False),
    ("/%70ortfolio", False),
    ("https://[", False),
    ("", False),
])
def test_return_destination(client, destination, allowed):
    html = client.get("/stock/3496", query_string={"return_to": destination}).get_data(as_text=True)
    link = BeautifulSoup(html, "html.parser").find(id="detail-return-link")
    assert bool(link) is allowed
    if allowed:
        assert link["href"] == destination


@pytest.mark.parametrize("query", ["3496", "テスト"])
def test_search_round_trip(client, query):
    response = client.get("/", query_string={"q": query})
    if response.status_code == 302:
        detail_url = response.headers["Location"]
    else:
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        detail_url = soup.select_one('td a[href^="/stock/"]')["href"]
    detail = client.get(detail_url)
    soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
    back = soup.find(id="detail-return-link")["href"]
    assert parse_qs(urlsplit(back).query) == {"q": [query], "show_results": ["1"]}
    returned = client.get(back)
    assert returned.status_code == 200
    assert 'id="filter-q"' in returned.get_data(as_text=True)


def test_unregistered_add_keeps_origin(client, monkeypatch):
    from webapp.routes import detail, search

    monkeypatch.setattr(detail, "get_stock_data", lambda code: {"stock_name": "未登録"})
    origin = "/portfolio?status=watch&sort=rank"
    response = client.get("/stock/9999", query_string={"return_to": origin})
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert soup.select_one('input[name="return_to"]')["value"] == origin
    monkeypatch.setattr(search, "add_stock", lambda code: code)
    added = client.post("/stock/add", data={"add_code_s": "9999", "return_to": origin})
    assert parse_qs(urlsplit(added.headers["Location"]).query)["return_to"] == [origin]
    rejected = client.post("/stock/add", data={"add_code_s": "9999", "return_to": "//example.com"})
    assert "return_to" not in parse_qs(urlsplit(rejected.headers["Location"]).query)


@pytest.mark.parametrize("path", [
    "/portfolio?status=&gyoutai_theme=半導体&sort=rs",
    "/portfolio/charts?status=watch&gyoutai_theme=半導体",
    "/portfolio/shikiho?status=semi",
    "/portfolio/themes/summary?sort=rank",
])
def test_listing_url_preserves_query(app, path):
    from webapp.navigation import stock_detail_url

    with app.test_request_context(path):
        detail_url = stock_detail_url("215A")
        back = parse_qs(urlsplit(detail_url).query)["return_to"][0]
        assert urlsplit(back).path == urlsplit(path).path
        assert parse_qs(urlsplit(back).query, keep_blank_values=True) == parse_qs(
            urlsplit(path).query, keep_blank_values=True,
        )


@pytest.mark.parametrize("endpoint, origin", [
    ("memo", "/portfolio?status=semi&sort=rs"),
    ("corporate_url", "/?q=3496&show_results=1"),
    ("refresh", "/portfolio/charts?status=hold"),
    ("memo", "//example.com"),
])
def test_post_keeps_valid_origin(client, monkeypatch, endpoint, origin):
    import make_stock_db
    monkeypatch.setattr(make_stock_db, "refresh_stock", lambda codes: [])
    response = client.post("/stock/3496/" + endpoint, data={
        "detail_return_to": origin, "memo": "往復確認", "url": "",
    })
    params = parse_qs(urlsplit(response.headers["Location"]).query)
    if origin.startswith("//"):
        assert "return_to" not in params
    else:
        assert params["return_to"] == [origin]
