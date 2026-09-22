"""issue #139: IR資料収集のテスト。"""

import json

import pytest

import ir_docs


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("2026年3月期 決算短信〔日本基準〕", "tanshin"),
        ("2026年3月期 決算短信補足資料", "setsumei"),
        ("2026年3月期 決算説明資料（修正版）", "setsumei"),
        ("（訂正）「2026年3月期 決算短信」の一部訂正について", "tanshin"),
        ("「2026年3月期 決算短信」の一部訂正に関するお知らせ", None),
        ("決算説明資料の掲載について", None),
        ("決算説明会の書き起こしに関するお知らせ", None),
        ("決算説明会動画 公開について", None),
    ],
)
def test_classify_heading(heading, expected):
    assert ir_docs.classify_heading(heading) == expected


@pytest.mark.parametrize(
    "pages, expected",
    [
        (["日本語の決算説明資料です。" * 20], "ok"),
        (["썛쎱쎯쎡쎤쎚" * 30], "garbled"),
        (["abcdef" * 30], "garbled"),
        (["日本語" * 20], "image_based"),
    ],
)
def test_classify_text_quality(pages, expected):
    assert ir_docs.classify_text_quality(pages) == expected


def test_link_revisions_matches_all_keys_and_revision_marker():
    documents = [
        {"doc_id": "1", "date": "20260201", "doc_type": "setsumei", "fiscal_period": "2025年12月期", "quarter": "FY", "heading": "2025年12月期 決算短信補足資料"},
        {"doc_id": "2", "date": "20260202", "doc_type": "tanshin", "fiscal_period": "2025年12月期", "quarter": "FY", "heading": "決算短信（訂正版）"},
        {"doc_id": "3", "date": "20260302", "doc_type": "setsumei", "fiscal_period": "2025年12月期", "quarter": "FY", "heading": "2025年12月期通期決算説明資料"},
        {"doc_id": "4", "date": "20260302", "doc_type": "setsumei", "fiscal_period": "2025年12月期", "quarter": "FY", "heading": "2025年12月期通期決算短信補足資料（修正版）"},
        {"doc_id": "5", "date": "20260303", "doc_type": "setsumei", "fiscal_period": None, "quarter": "FY", "heading": "決算説明資料（訂正版）"},
    ]
    ir_docs._link_revisions(documents)
    by_id = {document["doc_id"]: document for document in documents}

    assert by_id["4"]["supersedes"] == "1"
    assert by_id["1"]["superseded_by"] == "4"
    assert by_id["1"]["is_latest"] is False
    assert by_id["2"]["is_latest"] is True
    assert by_id["3"]["is_latest"] is True
    assert by_id["5"]["supersedes"] is None


def test_depth_state_is_monotonic_and_uses_price_day_cutoff(monkeypatch):
    index = {"collected_depth": "1y", "collected_months": 12, "collection_errors": {"latest": [{}], "1y": [{}], "2y": [{}]}}
    ir_docs._update_depth_state(index, "latest", [])
    assert index["collected_depth"] == "1y"
    assert "latest" not in index["collection_errors"]

    ir_docs._update_depth_state(index, "2y", [{"stage": "pdf_download"}])
    assert index["collected_depth"] == "1y"
    assert "2y" in index["collection_errors"]

    ir_docs._update_depth_state(index, "2y", [])
    assert index["collected_depth"] == "2y"
    assert index["collected_months"] == 24
    assert index["collection_errors"] == {}

    pages = iter([
        [{
            "type": "kaiji",
            "date": "20250921",
            "heading": "2025年12月期 決算説明資料",
            "url": "https://kabutan.jp/disclosures/pdf/20250921/140120250921000001/",
        }],
        [],
    ])
    monkeypatch.setattr(ir_docs, "_get", lambda *args: type("Response", (), {"text": ""})())
    monkeypatch.setattr(ir_docs.disclosure, "parse_disclosure_html", lambda html: next(pages))
    candidates, errors = ir_docs.collect_candidates(
        "4011", depth="1y", now=ir_docs.datetime(2026, 9, 22, 10, 0)
    )
    assert [candidate["doc_id"] for candidate in candidates] == ["140120250921000001"]
    assert errors == []


def test_download_flow_cache_force_and_shared_bulk_http_state(tmp_path, monkeypatch):
    candidate = {
        "doc_id": "140120260101000001", "doc_type": "setsumei", "date": "20260101",
        "heading": "2025年12月期 決算説明資料", "fiscal_period": "2025年12月期",
        "quarter": "FY", "url": "https://example.test/doc.pdf",
    }
    monkeypatch.setattr(ir_docs, "collect_candidates", lambda *args, **kwargs: ([candidate], []))
    monkeypatch.setattr(ir_docs, "_extract_pdf", lambda content: ["日本語の説明資料です。" * 20])

    calls = []

    class Response:
        content = b"%PDF-test"
        headers = {"content-type": "application/pdf"}

    def fake_get(*args, **kwargs):
        calls.append(args[1])
        return Response()

    monkeypatch.setattr(ir_docs, "_get", fake_get)
    ir_docs.download_ir_docs("4011", depth="latest", output_dir=tmp_path)
    ir_docs.download_ir_docs("4011", depth="latest", output_dir=tmp_path)
    assert len(calls) == 1

    ir_docs.download_ir_docs("4011", depth="latest", force=True, output_dir=tmp_path)
    assert len(calls) == 2
    index = json.loads((tmp_path / "4011" / "index.json").read_text(encoding="utf-8"))
    assert index["collected_depth"] == "latest"
    assert index["documents"][0]["text_quality"] == "ok"

    bulk_calls = []
    monkeypatch.setattr(ir_docs.portfolio, "parse_my_portforio", lambda: (["4011"], ["4436"]))

    def fake_download(code_s, **kwargs):
        bulk_calls.append((code_s, kwargs["session"], kwargs["limiter"]))
        return []

    monkeypatch.setattr(ir_docs, "download_ir_docs", fake_download)
    ir_docs.download_all()
    assert [call[0] for call in bulk_calls] == ["4011", "4436"]
    assert bulk_calls[0][1] is bulk_calls[1][1]
    assert bulk_calls[0][2] is bulk_calls[1][2]
