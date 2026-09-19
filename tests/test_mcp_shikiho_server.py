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
    exact["shikiho_gyoseki"] = {
        "prev_year": {"label": "連26.3", "sales": 51163, "op_profit": 2189},
        "this_year": {
            "label": "連27.3予", "sales": 70000, "op_profit": 3700,
            "sales_growth": 36.8, "op_growth": 69.0,
        },
        "next_year": {
            "label": "連28.3予", "sales": 85000, "op_profit": 4300,
            "sales_growth": 21.4, "op_growth": 16.2,
        },
        "raw_text": "連26.3\t51,163\t2,189\n連27.3予\t70,000\t3,700\n",
        "updated_at": "2026-09-17",
    }
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


@pytest.mark.parametrize(
    "code_s, has_gyoseki",
    [("1301", True), ("1000", False), ("9999", False)],
)
def test_get_shikiho_returns_gyoseki(research_db, code_s, has_gyoseki):
    """業績予想の返却契約 (入力済み/未入力/未登録) を確認する (issue #346)。"""
    result = server.get_shikiho_data(code_s)

    assert "gyoseki" in result  # 未入力・未登録でもキーは必ず存在する
    if not has_gyoseki:
        assert result["gyoseki"] is None
        return
    gyoseki = result["gyoseki"]
    assert gyoseki["unit"] == "百万円"
    assert gyoseki["this_year"]["sales_growth"] == 36.8
    assert gyoseki["next_year"]["op_growth"] == 16.2
    assert gyoseki["updated_at"] == "2026-09-17"
    assert "raw_text" not in gyoseki  # 原文は MCP では返さない


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


@pytest.fixture
def ir_qa_db(tmp_path, monkeypatch):
    """IR問い合わせ回答入りの一時 research_shelve を作る。"""
    db_path = str(tmp_path / "research_ir")
    monkeypatch.setattr(rs, "RESEARCH_SHELVE", db_path)

    rec = rs.create_research_record("1301", "極洋")
    rec["ir_qa"] = [
        {"id": "a1", "answered_at": "2026/01/05", "body": "<p>古い回答</p>"},
        {"id": "a2", "answered_at": "2026/08/20", "body": "<b>新しい回答</b>"},
    ]
    rs.upsert_research_record(rec, db_path=db_path)
    return db_path


@pytest.mark.parametrize(
    "code_s, limit, found, answered_ats",
    [
        # 降順で返る + answered_at が as_of にも入る
        ("1301", 10, True, ["2026/08/20", "2026/01/05"]),
        # limit で新しい側から絞る
        ("1301", 1, True, ["2026/08/20"]),
        # 未登録銘柄
        ("9999", 10, False, []),
    ],
)
def test_get_ir_qa_formats_and_limits(ir_qa_db, code_s, limit, found, answered_ats):
    """降順整形・HTML除去・limit・未登録時の契約をまとめて確認する。"""
    result = server.get_ir_qa_data(code_s, limit)

    assert result["found"] is found
    assert result["source"] == "research_shelve"
    assert [item["answered_at"] for item in result["ir_qa"]] == answered_ats
    if found:
        # 四季報と違い answered_at は実日付なので as_of に入る
        assert result["ir_qa"][0]["as_of"] == "2026/08/20"
        assert result["ir_qa"][0]["body"] == "新しい回答"
        assert result["total_entries"] == 2
