"""#145: 固定DBでランキング・決算・除外フローの不変条件を守る。"""

import csv
from datetime import datetime
import math
from pathlib import Path
import re
import shutil

import pytest

import db_shelve
import kessan
import make_market_db
import make_stock_db
import portfolio
import shintakane

pytestmark = pytest.mark.integration
FIXTURE_DIR = Path(__file__).parent / "fixtures/integration_stocks_shelve"
CODES = {"5032", "3496", "135A", "247A", "218A"}


@pytest.fixture
def stock_environment(tmp_path, monkeypatch):
    """実DB・実HTTP・Drive・個人のポートフォリオに触れない。"""
    for path in FIXTURE_DIR.glob("stocks_shelve.*"):
        if path.suffix in {".dat", ".dir"}:
            shutil.copy(path, tmp_path / path.name)
    monkeypatch.setattr(db_shelve, "STOCKS_SHELVE", str(tmp_path / "stocks_shelve"))
    monkeypatch.setattr(db_shelve, "KESSAN_SHELVE", str(tmp_path / "kessan_shelve"))
    for module in (make_stock_db, make_market_db, shintakane):
        monkeypatch.setattr(module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (sorted(CODES), []))
    market = {"theme_rank": []}
    monkeypatch.setattr(make_market_db, "update_market_db", lambda: market)
    monkeypatch.setattr(make_market_db, "get_market_db", lambda: market)
    monkeypatch.setattr(db_shelve, "MARKET_SHELVE", str(tmp_path / "market_shelve"))
    # 市場CSVは別フロー。ランキング本体のCSV・DB保存は実処理を通す。
    monkeypatch.setattr(make_market_db, "create_market_csv", lambda: None)
    (tmp_path / "code_rank_data").mkdir()
    def reject_network(*args, **kwargs):
        pytest.fail("統合スモークテストから外部通信が発生")
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", reject_network)
    return tmp_path


def test_ranking_csv_and_earnings_complete(stock_environment):
    """5銘柄が決算DB・ランキングCSVまで欠落せず、スコアと順位が有効。"""
    stocks = make_stock_db.load_stock_db()
    assert set(stocks) == CODES
    shintakane.update_pf_kessan_db(stocks)
    earnings = kessan.load_pf_kessan_db()
    assert earnings
    assert set(earnings) == {code_s for code_s, stock in stocks.items() if stock.get("kessanbi")}
    assert all(0 <= row["kessan_quarter"] <= 4 for row in earnings.values())

    make_stock_db.list_all_db(upload_csv=False, update_portforio=False)
    with (stock_environment / "code_rank_data/code_rank.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert {"コード", "銘柄名", "順位", "総合PT", "決算日", "プロフィット/クォリティ", "バリュー/サイズ", "ファンダメンタル"} <= set(reader.fieldnames)
        rows = list(reader)
    assert len(rows) == len(CODES)
    assert {re.search(r'"([0-9]{3}[0-9A-Z])"\)$', row["コード"])[1] for row in rows} == CODES
    for row in rows:
        for column in ("総合PT", "プロフィット/クォリティ", "バリュー/サイズ", "ファンダメンタル"):
            assert math.isfinite(float(row[column]))
        assert math.isfinite(float(row["モメンタム(現在.20日比/5日比)"].split(".")[0]))
    persisted = make_stock_db.load_stock_db()
    assert set(persisted) == CODES
    assert sorted(stock["stock_rank_log"][0][1] for stock in persisted.values()) == list(range(1, 6))


def test_refresh_removes_etf_and_delisted(stock_environment):
    """実ファイルのETFコードと価格ゼロ銘柄が refresh 後のDBから消える。"""
    stocks = make_stock_db.load_stock_db()
    for stock in stocks.values():
        stock["access_date_price"] = datetime.today()
    stocks["1306"] = dict(stocks["5032"], code_s="1306")
    stocks["9999"] = dict(stocks["5032"], code_s="9999", price=0)
    make_stock_db.save_stock_db(stocks)
    (stock_environment / "ETF_code.txt").write_text("1306\tTOPIX ETF\n", encoding="utf-8")
    make_stock_db.reflesh_db()
    assert set(make_stock_db.load_stock_db()) == CODES
