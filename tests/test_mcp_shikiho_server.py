"""四季報 MCP サーバーの整形・検索テスト。"""

from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

import research_shelve as rs

MCP_DIR = Path(__file__).resolve().parents[1] / "scripts" / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))
import shikiho_server as server


@pytest.fixture
def research_db(tmp_path, monkeypatch):
    """四季報コメント入りの一時 research_shelve を作る。"""
    db_path = str(tmp_path / "research")
    monkeypatch.setattr(rs, "RESEARCH_SHELVE", db_path)

    exact = rs.create_research_record("1301", "極洋", overview="<b>水産</b>加工")
    exact["shikiho_comments"] = [
        {"period": "", "comment": "旧コメント"},
        {"period": "26.6", "comment": "<b>新コメント</b>"},
    ]
    rs.upsert_research_record(exact, db_path=db_path)
    rs.upsert_research_record(
        rs.create_research_record("1000", "1301ホールディングス"), db_path=db_path
    )
    return db_path


@pytest.mark.parametrize(
    "code_s, found, periods",
    [("1301", True, ["26.6", ""]), ("9999", False, [])],
)
def test_get_shikiho_formats_period_and_unknown_stock(research_db, code_s, found, periods):
    """版情報・HTML除去・未登録時の契約をまとめて確認する。"""
    result = server.get_shikiho_data(code_s)

    assert result["found"] is found
    assert result["source"] == "research_shelve"
    assert [item["period"] for item in result["shikiho_comments"]] == periods
    if found:
        assert result["overview"] == "水産加工"
        assert result["shikiho_comments"][0] == {
            "period": "26.6",
            "period_label": "四季報 2026年6月号",
            "as_of": None,
            "comment": "新コメント",
        }
        assert result["shikiho_comments"][1]["period_label"] is None


def test_search_stocks_prioritizes_exact_code(research_db):
    """コード完全一致を、社名部分一致より先に返す。"""
    result = server.search_stocks_data("1301")

    assert [item["code_s"] for item in result["results"]] == ["1301", "1000"]
    assert result["results"][0]["has_shikiho"] is True
    assert result["results"][0]["comment_count"] == 2


def test_locked_reader_uses_write_lock(research_db, monkeypatch):
    """MCP 用の読取 API が書込みと同じ flock を取得する。"""
    calls = []
    original_flock = rs._flock

    @contextmanager
    def tracking_flock(*args, **kwargs):
        calls.append((args, kwargs))
        with original_flock(*args, **kwargs):
            yield

    monkeypatch.setattr(rs, "_flock", tracking_flock)
    record = rs.get_research_record_locked("1301", db_path=research_db)

    assert record["stock_name"] == "極洋"
    assert calls == [((research_db,), {})]
