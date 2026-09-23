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

    warnings = []
    errors = []
    monkeypatch.setattr(
        ir_docs,
        "collect_candidates",
        lambda *args, **kwargs: ([], [{"stage": "page_scan", "reason": "HTTP 500"}]),
    )
    monkeypatch.setattr(ir_docs, "log_warning", warnings.append)
    monkeypatch.setattr(ir_docs, "log_error", errors.append)
    assert ir_docs.download_ir_docs("4011", dry_run=True, output_dir=tmp_path) == []
    assert "page_scan HTTP 500" in warnings[0]
    assert "候補を取得できませんでした" in errors[0]

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


# ---- issue #457: 会社IRページからの半自動収集 ----

IR_TOP = "https://corp.example.com/ir/"
IR_PAGES = {
    "https://corp.example.com/": """
        <a href="/isSmp?">IR情報</a>
        <a href="/contact/ir.html">IRお問い合わせ</a>
        <a href="/company/">会社情報</a>
        <a href="/ir/">IR情報</a>
    """,
    IR_TOP: """
        <a href="/common/company.pdf">会社案内</a>
        <a href="/ir/news/plan_notice.pdf">中期経営計画策定に関するお知らせ</a>
        <a href="https://cdn.example.net/plan2024.pdf">中期経営計画説明資料</a>
        <a href="/ir/policy/midterm.html">中期経営計画</a>
        <a href="/ir/library/presentation.html">決算説明会</a>
        <a href="/ir/library/other.html">中長期ビジョン</a>
    """,
    "https://corp.example.com/ir/policy/midterm.html": """
        <a href="/common/company.pdf">会社案内</a>
        <a href="/common/header_guide.pdf">ご案内</a>
        <a href="pdf/Medium-term_2026.pdf">新中期経営計画（2026 年度～2028 年度）</a>
    """,
    "https://corp.example.com/ir/library/presentation.html": """
        <a href="/ir/pdf/20260515.pdf">2026年3月期 決算説明資料</a>
        <a href="/ir/pdf/script.pdf">決算説明会書き起こし</a>
        <a href="/ir/pdf/20200515.pdf">2020年3月期 決算説明資料</a>
        <a href="/ir/pdf/undated.pdf">第2四半期 決算説明資料</a>
    """,
}


def _fake_page_get(requested):
    class Response:
        def __init__(self, url):
            self.text = IR_PAGES[url]
            self.apparent_encoding = "utf-8"
            self.url = url

    def fake_get(session, url, limiter):
        requested.append(url)
        return Response(url)

    return fake_get


def _allow_public(monkeypatch):
    monkeypatch.setattr(ir_docs, "_check_public_url", lambda url: None)


def test_ir_page_candidates_follow_one_subpage_per_type(tmp_path, monkeypatch):
    requested = []
    _allow_public(monkeypatch)
    monkeypatch.setattr(ir_docs, "_get", _fake_page_get(requested))
    monkeypatch.setattr(ir_docs, "get_price_day", lambda now: ir_docs.datetime(2026, 9, 23))
    (tmp_path / "3660").mkdir()
    (tmp_path / "3660" / "index.json").write_text(json.dumps({"documents": [{
        "doc_id": "140120260515000001", "doc_type": "setsumei", "date": "20260515",
        "fiscal_period": "2026年3月期", "quarter": "FY", "url": "https://kabutan.test/x.pdf",
    }]}), encoding="utf-8")
    candidates = ir_docs.find_ir_page_candidates("3660", IR_TOP, output_dir=tmp_path)
    marks = {item["url"].rsplit("/", 1)[1]: (item["old"], item["maybe_tdnet"]) for item in candidates}
    # 期・四半期が TDnet 資料と一致すれば目印。2年より古い候補以降は同じページ内で畳む
    assert marks["20260515.pdf"] == (False, "140120260515000001")
    assert marks["20200515.pdf"] == (True, None)
    assert marks["undated.pdf"] == (True, None)
    candidates = [item for item in candidates if not item["old"]]

    by_url = {item["url"]: item["doc_type"] for item in candidates}
    assert by_url == {
        "https://cdn.example.net/plan2024.pdf": "chuki_plan",
        # 種別ページ内は文言不一致でも候補。サイト共通 (開始ページにもある) は除く
        "https://corp.example.com/ir/policy/pdf/Medium-term_2026.pdf": "chuki_plan",
        "https://corp.example.com/common/header_guide.pdf": "chuki_plan",
        "https://corp.example.com/ir/pdf/20260515.pdf": "setsumei",
    }
    # 開始 + 種別ごとに1ページのみ (「中長期ビジョン」の2ページ目は辿らない)
    assert len(requested) == 3

    # 会社トップ起点なら IR トップを1回だけ辿り、同じ候補に届く
    requested.clear()
    from_top = ir_docs.find_ir_page_candidates("3660", "https://corp.example.com/", output_dir=tmp_path)
    assert {item["url"] for item in from_top} == {item["url"] for item in candidates} | {
        "https://corp.example.com/ir/pdf/20200515.pdf", "https://corp.example.com/ir/pdf/undated.pdf",
    }
    assert len(requested) == 4


@pytest.mark.parametrize(
    "url, address",
    [
        ("http://localhost:5001/x.pdf", "127.0.0.1"),
        ("http://nas.local/x.pdf", "192.168.1.10"),
        ("file:///etc/passwd", None),
    ],
)
def test_check_public_url_rejects_non_public(monkeypatch, url, address):
    monkeypatch.setattr(
        ir_docs.socket, "getaddrinfo", lambda host, port: [(None, None, None, "", (address, port))]
    )
    with pytest.raises(ValueError):
        ir_docs._check_public_url(url)


def test_ir_page_docs_coexist_with_tdnet_and_survive_rebuild(tmp_path, monkeypatch):
    """中計は並立・日付推定・TDnet資料と網羅性情報を壊さない。手動旧版化はTDnet再収集で戻らない。"""
    _allow_public(monkeypatch)
    tdnet = {
        "doc_id": "140120260101000001", "doc_type": "setsumei", "date": "20260101",
        "heading": "2025年12月期 決算説明資料", "fiscal_period": "2025年12月期",
        "quarter": "FY", "url": "https://example.test/doc.pdf",
    }
    monkeypatch.setattr(ir_docs, "collect_candidates", lambda *args, **kwargs: ([tdnet], []))
    monkeypatch.setattr(ir_docs, "_extract_pdf", lambda content: ["日本語の説明資料です。" * 20])
    pdf_bodies = iter([b"%PDF-tdnet", b"%PDF-plan-a", b"%PDF-plan-b", b"%PDF-plan-a"])

    class Response:
        headers = {"content-type": "application/pdf"}

        def __init__(self):
            self.content = next(pdf_bodies)

    monkeypatch.setattr(ir_docs, "_get", lambda *args: Response())
    ir_docs.download_ir_docs("4011", depth="latest", output_dir=tmp_path)
    index_path = tmp_path / "4011" / "index.json"
    before = json.loads(index_path.read_text(encoding="utf-8"))

    plan_a, created_a = ir_docs.fetch_ir_page_doc(
        "4011", "https://corp.example.com/plan_2024.pdf", "chuki_plan",
        "中期経営計画（2024年5月）", output_dir=tmp_path,
    )
    plan_b, _ = ir_docs.fetch_ir_page_doc(
        "4011", "https://corp.example.com/Medium-term_20260515.pdf", "chuki_plan",
        output_dir=tmp_path,
    )
    duplicate, created_dup = ir_docs.fetch_ir_page_doc(
        "4011", "https://mirror.example.com/a.pdf", "chuki_plan", output_dir=tmp_path,
    )
    assert (created_a, created_dup) == (True, False)
    assert duplicate["doc_id"] == plan_a["doc_id"]
    assert (plan_a["date"], plan_b["date"]) == ("20240501", "20260515")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    plans = [d for d in index["documents"] if d["doc_type"] == "chuki_plan"]
    assert len(plans) == 2
    assert all(d["is_latest"] and d["date_estimated"] for d in plans)
    assert all(d["source"] == "corporate_ir_page" for d in plans)
    assert next(d for d in index["documents"] if d["doc_id"] == tdnet["doc_id"])["is_latest"]
    for key in ("last_collected_at", "collected_depth", "collection_errors"):
        assert index[key] == before[key]

    ir_docs.mark_superseded("4011", plan_a["doc_id"], output_dir=tmp_path)
    with pytest.raises(ValueError):
        ir_docs.mark_superseded("4011", tdnet["doc_id"], output_dir=tmp_path)
    ir_docs.download_ir_docs("4011", depth="latest", output_dir=tmp_path)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    latest = {d["doc_id"]: d["is_latest"] for d in index["documents"]}
    assert latest[plan_a["doc_id"]] is False
    assert latest[plan_b["doc_id"]] is True


def _candidate(heading, doc_type="chuki_plan", **marks):
    return {"heading": heading, "doc_type": doc_type, "url": "https://x/" + heading,
            "old": False, "downloaded": False, "maybe_tdnet": None, **marks}


@pytest.mark.parametrize("doc_type, expected", [
    (None, ["中期経営計画 資料", "決算説明資料"]),
    ("chuki_plan", ["中期経営計画 資料"]),
])
def test_pending_candidates_keeps_only_worth_fetching(doc_type, expected):
    """一括走査では古い・取得済み・TDnet取得済み?を除き、同じ見出しは1件にまとめる。"""
    candidates = [
        _candidate("中期経営計画 資料"),
        _candidate("中期経営計画　資料 "),  # 別URLの同じ資料 (全角空白)
        _candidate("旧中計", old=True),
        _candidate("取得済み中計", downloaded=True),
        _candidate("決算説明資料", doc_type="setsumei"),
        _candidate("TDnetにある説明資料", doc_type="setsumei", maybe_tdnet="1401"),
    ]
    result = ir_docs.pending_candidates(candidates, doc_type)
    assert [item["heading"] for item in result] == expected


@pytest.mark.parametrize("heading, url, expected", [
    ("2026.5.14 決算説明及び中期経営計画2年目振り返り", "https://x/a.pdf", "20260514"),
    ("2026年08月04日 2026年度第1四半期 決算説明会", "https://x/a.pdf", "20260804"),
    ("中期経営計画（2024年5月）", "https://x/a.pdf", "20240501"),
    ("", "https://x/20260520181351871s.pdf", "20260520"),
    # 決算期末 (公表日より未来) は捨てる
    ("2027年4月通期 第1四半期決算説明会資料", "https://x/a.pdf", None),
])
def test_estimate_date_formats(heading, url, expected):
    assert ir_docs._estimate_date(heading, url, "20260923") == expected


@pytest.mark.parametrize("depth, types, expected", [
    ("1y", ["tanshin", "hp_setsumei"], "20260807"),  # 株探に説明資料なし → 会社HP由来の最新日
    ("1y", ["tanshin"], ""),                         # 会社HPからも未取得
    ("1y", ["tanshin", "setsumei"], None),           # 株探に説明資料あり
    ("latest", ["tanshin"], None),                   # 1年分未収集なら判定しない
])
def test_tdnet_setsumei_missing(tmp_path, depth, types, expected):
    documents = [
        {"doc_id": t, "doc_type": "setsumei" if t == "hp_setsumei" else t, "date": "20260807",
         **({"source": "corporate_ir_page"} if t == "hp_setsumei" else {})}
        for t in types
    ]
    (tmp_path / "4970").mkdir()
    (tmp_path / "4970" / "index.json").write_text(
        json.dumps({"collected_depth": depth, "documents": documents}), encoding="utf-8"
    )
    assert ir_docs.tdnet_setsumei_missing("4970", output_dir=tmp_path) == expected
