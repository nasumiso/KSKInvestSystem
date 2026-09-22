"""四季報 MCP サーバーの整形・検索テスト。"""

from contextlib import contextmanager
from datetime import date
import json
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


# ==================================================
# 決算資料 (issue #433)
# ==================================================

def _write_ir_index(tmp_path, monkeypatch, index, texts=None):
    """一時 ir_docs ディレクトリに index.json とページJSONを作る。"""
    root = tmp_path / "ir_docs"
    monkeypatch.setattr(server, "IR_DOCS_DIR", root)
    stock_dir = root / "4011"
    stock_dir.mkdir(parents=True)
    (stock_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )
    for name, pages in (texts or {}).items():
        (stock_dir / name).write_text(
            json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8"
        )
    return root


def _document(doc_id="D1", date_s="20260901", **overrides):
    """index.json の documents 1件分を作る。"""
    document = {
        "doc_id": doc_id, "date": date_s, "heading": "決算説明資料",
        "doc_type": "setsumei", "fiscal_period": "2026年3月期", "quarter": "Q1",
        "pages": 2, "total_chars": 20, "text_quality": "ok",
        "is_latest": True, "supersedes": None, "superseded_by": None,
        "pdf_path": f"{date_s}_{doc_id}.pdf", "text_path": f"{date_s}_{doc_id}.json",
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize(
    "index, expect_status, expect_total, expect_listed",
    [
        # 未収集: 資料の有無は判定できない
        (None, "not_collected", 0, 0),
        # 収集済みだが期間外にしかない: total は立つが listed は0
        ({"last_collected_at": "2026-09-01T18:50:31+09:00", "collected_months": 12,
          "documents": [_document(date_s="20200101")]}, "collected", 1, 0),
        # 期間内にある
        ({"last_collected_at": "2026-09-01T18:50:31+09:00", "collected_months": 12,
          "documents": [_document(date_s="20260901")]}, "collected", 1, 1),
    ],
)
def test_coverage_status_distinguishes_uncollected_from_empty(
    tmp_path, monkeypatch, index, expect_status, expect_total, expect_listed
):
    """未収集 / 期間内に無いだけ / 期間内ありを区別する (混同すると資料なしと誤認)。"""
    if index is None:
        monkeypatch.setattr(server, "IR_DOCS_DIR", tmp_path / "ir_docs")
    else:
        _write_ir_index(tmp_path, monkeypatch, index)

    result = server.list_earnings_documents_data(
        "4011", months=12, today=date(2026, 9, 22)
    )
    assert result["coverage_status"] == expect_status
    assert result["total_documents"] == expect_total
    assert len(result["documents"]) == expect_listed


@pytest.mark.parametrize(
    "last_collected_at, collected_months, months, expect_partial",
    [
        # 収集直後: 要求区間が収集区間に収まる
        ("2026-09-22T18:50:31+09:00", 12, 12, False),
        # latest 収集は collected_months=0 で期間保証がない
        ("2026-09-22T18:50:31+09:00", 0, 12, True),
        # 要求が収集深度を超える
        ("2026-09-22T18:50:31+09:00", 12, 24, True),
        # --- 以下は「収集からの経過月数を引く」素朴な実装が false と誤答する ---
        # 3ヶ月前に1y収集し直近3ヶ月を要求: 経過0ヶ月ではないが
        # 素朴版は 3 > (12-3) が偽で「収集済み」と誤答する。
        # 実際は最新3ヶ月ぶんの資料が収集されていない
        ("2026-06-22T18:50:31+09:00", 12, 3, True),
        # 2日前の収集でも最新側に穴はある (素朴版は経過0ヶ月として false)
        ("2026-09-20T18:50:31+09:00", 12, 12, True),
    ],
)
def test_partial_coverage_uses_interval_not_month_subtraction(
    tmp_path, monkeypatch, last_collected_at, collected_months, months,
    expect_partial
):
    """収集後に経過した分は最新側が欠ける。区間の包含で判定する。

    月数の引き算 (collected_months - 経過月数) では、要求期間が収集深度より
    短いときに穴を見逃す。後半2ケースはその実装だと false になる。
    """
    _write_ir_index(tmp_path, monkeypatch, {
        "last_collected_at": last_collected_at,
        "collected_months": collected_months,
        "documents": [_document()],
    })
    result = server.list_earnings_documents_data(
        "4011", months=months, today=date(2026, 9, 22)
    )
    assert result["partial_coverage"] is expect_partial


def test_collection_errors_are_surfaced(tmp_path, monkeypatch):
    """収集エラーがあると一覧は不完全。黙って信頼できる体で返さない。"""
    _write_ir_index(tmp_path, monkeypatch, {
        "last_collected_at": "2026-09-22T18:50:31+09:00", "collected_months": 12,
        "collection_errors": {"1y": [{"doc_id": "X"}, {"doc_id": "Y"}]},
        "documents": [_document()],
    })
    result = server.list_earnings_documents_data(
        "4011", months=12, today=date(2026, 9, 22)
    )
    assert result["has_collection_errors"] is True
    assert result["collection_error_count"] == 2
    assert "不完全" in result["note"]


@pytest.mark.parametrize("quality", ["image_based", "garbled"])
def test_bad_quality_returns_null_text_with_path(tmp_path, monkeypatch, quality):
    """空文字や文字化けを返すと LLM が推測で分析を進めるため理由とパスを示す。"""
    _write_ir_index(tmp_path, monkeypatch, {
        "last_collected_at": "2026-09-22T18:50:31+09:00", "collected_months": 12,
        "documents": [_document(text_quality=quality)],
    })
    result = server.get_earnings_document_data("4011", "D1")

    assert result["text"] is None
    assert result["text_quality"] == quality
    assert result["local_path"].endswith("20260901_D1.pdf")
    assert "添付" in result["note"]


@pytest.mark.parametrize(
    "pages, max_chars, expect_page_to, expect_truncated, expect_next",
    [
        # ページ境界で切る: 1ページ目だけで打ち切り、続きを案内
        ([{"page": 1, "text": "あ" * 100}, {"page": 2, "text": "い" * 100}],
         100, 1, True, 2),
        # 全ページ収まれば truncated しない
        ([{"page": 1, "text": "あ" * 10}, {"page": 2, "text": "い" * 10}],
         100, 2, False, None),
        # 1ページが max_chars 超でもそのページを返す (返さないと無限ループする)
        ([{"page": 1, "text": "あ" * 200}], 100, 1, False, None),
    ],
)
def test_text_is_cut_at_page_boundary(
    tmp_path, monkeypatch, pages, max_chars, expect_page_to,
    expect_truncated, expect_next
):
    """切り出しはページ境界。1ページが上限超でも返さないと同じページを繰り返す。"""
    _write_ir_index(
        tmp_path, monkeypatch,
        {"last_collected_at": "2026-09-22T18:50:31+09:00", "collected_months": 12,
         "documents": [_document(pages=len(pages))]},
        texts={"20260901_D1.json": pages},
    )
    result = server.get_earnings_document_data("4011", "D1", max_chars=max_chars)

    assert result["page_to"] == expect_page_to
    assert result["truncated"] is expect_truncated
    assert result["next_page_from"] == expect_next
    assert result["text"]


@pytest.mark.parametrize(
    "last_requested_depth, expect_discontinuous, expect_partial",
    [
        # 1y 済みに latest をかけると ir_docs は last_collected_at だけ現在へ
        # 進め collected_months=12 を残す。連続収集の終端として扱うと、
        # 前回収集〜今日の開示が抜けているのに「収集済み」と偽る
        ("latest", True, True),
        # 通常の 1y 収集は当日収集なら連続しており穴はない
        ("1y", False, False),
    ],
)
def test_latest_after_deep_collection_is_not_treated_as_continuous(
    tmp_path, monkeypatch, last_requested_depth, expect_discontinuous,
    expect_partial
):
    """深い収集の後の latest 再収集で、連続カバレッジを捏造しない。"""
    _write_ir_index(tmp_path, monkeypatch, {
        "last_collected_at": "2026-09-22T18:50:31+09:00",
        "collected_depth": "1y", "collected_months": 12,
        "last_requested_depth": last_requested_depth,
        "documents": [_document()],
    })
    result = server.list_earnings_documents_data(
        "4011", months=12, today=date(2026, 9, 22)
    )
    assert result["coverage_discontinuous"] is expect_discontinuous
    assert result["partial_coverage"] is expect_partial
    if expect_discontinuous:
        assert "抜けている可能性" in result["note"]


def test_note_names_the_boundary_that_falls_short(tmp_path, monkeypatch):
    """当日に12ヶ月収集して24ヶ月を要求した場合、不足は古い側だと述べる。"""
    _write_ir_index(tmp_path, monkeypatch, {
        "last_collected_at": "2026-09-22T18:50:31+09:00",
        "collected_depth": "1y", "collected_months": 12,
        "last_requested_depth": "1y",
        "documents": [_document()],
    })
    result = server.list_earnings_documents_data(
        "4011", months=24, today=date(2026, 9, 22)
    )
    assert result["partial_coverage"] is True
    # 不足しているのは coverage_from より前。最新側の欠落と混同しない
    assert "より前は収集していません" in result["note"]
    assert "以降に開示された資料は未収集" not in result["note"]
