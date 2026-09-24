"""
会社IRページからの資料取得ルート (issue #457)。

POST /api/ir_page_candidates/<code_s> : 中計・決算説明資料のPDF候補を返す (AJAX)
POST /stock/<code_s>/ir_page_docs     : 選択・手入力されたPDFを取得して保存
POST /api/ir_top_url/<code_s>         : 会社HPから IR トップの URL を推測して返す (AJAX)
GET  /ir_docs/<code_s>/<doc_id>       : 保存済みPDFを返す (issue #473)
POST /stock/<code_s>/ir_docs/tdnet    : 株探 (TDnet) から直近1年の短信・説明資料を収集 (issue #473)
"""

import re

import requests
from flask import Blueprint, abort, flash, jsonify, redirect, request, send_file, url_for

ir_docs_bp = Blueprint("ir_docs", __name__)


@ir_docs_bp.route("/api/ir_page_candidates/<code_s>", methods=["POST"])
def post_ir_page_candidates(code_s: str):
    """会社HP (上書き優先) を起点に候補を探して JSON で返す。外部GETを伴うため POST。"""
    # ir_docs は pypdf 等を読み込むため、refresh.py と同様にルート内で遅延 import する
    import ir_docs

    try:
        start_url = ir_docs.resolve_ir_start_url(code_s)
        if not start_url:
            return jsonify({"ok": False, "error": "会社HPのURLがありません"}), 400
        candidates = ir_docs.find_ir_page_candidates(code_s, start_url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "start_url": start_url, "candidates": candidates})


@ir_docs_bp.route("/api/ir_top_url/<code_s>", methods=["POST"])
def post_ir_top_url(code_s: str):
    """会社HP上書きの編集用に IR トップの URL を推測する。保存はしない。"""
    import ir_docs

    try:
        url = ir_docs.guess_ir_top_url(code_s)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502
    if not url:
        return jsonify({"ok": False, "error": "IRページが見つかりませんでした"}), 404
    return jsonify({"ok": True, "url": url})


@ir_docs_bp.route("/stock/<code_s>/ir_page_docs", methods=["POST"])
def post_ir_page_docs(code_s: str):
    """チェックされた候補と手入力URLを順に取得 -> flash + 302リダイレクト。"""
    import ir_docs

    targets = []
    for i in request.form.getlist("pick"):
        targets.append((
            request.form.get(f"url_{i}", ""),
            request.form.get(f"doc_type_{i}", ""),
            request.form.get(f"heading_{i}", ""),
            request.form.get(f"source_page_{i}") or None,
        ))
    manual_url = request.form.get("manual_url", "").strip()
    if manual_url:
        targets.append((
            manual_url,
            request.form.get("manual_doc_type", ""),
            request.form.get("manual_heading", ""),
            None,
        ))
    if not targets:
        flash(f"取得するIR資料が選択されていません ({code_s})", "error")
        return redirect(url_for("detail.stock_detail", code_s=code_s, _anchor="ir-docs"))

    # 複数件を続けて取得しても1秒1リクエストを守るため、セッションと待機を共有する
    session = requests.Session()
    limiter = ir_docs._RateLimiter()
    for url, doc_type, heading, source_page in targets:
        try:
            document, created = ir_docs.fetch_ir_page_doc(
                code_s, url, doc_type, heading, source_page,
                session=session, limiter=limiter,
            )
            if created:
                flash(f"IR資料を保存しました ({code_s}): {document['heading']}", "info")
            else:
                flash(f"同じIR資料が保存済みです ({code_s}): {document['heading']}", "info")
        except Exception as e:  # noqa: BLE001
            flash(f"IR資料の取得に失敗しました ({code_s}): {url} {e}", "error")
    return redirect(url_for("detail.stock_detail", code_s=code_s, _anchor="ir-docs"))


@ir_docs_bp.route("/ir_docs/<code_s>/<doc_id>")
def get_ir_doc_pdf(code_s: str, doc_id: str):
    """保存済みPDFを返す。パスは URL から受け取らず、index.json を doc_id で引いて決める。"""
    import ir_docs

    code_s = code_s.upper()
    if not re.fullmatch(r"\d[0-9A-Z]\d[0-9A-Z]", code_s):
        abort(404)
    stock_dir = ir_docs.IR_DOCS_DIR / code_s
    index = ir_docs._load_index(stock_dir / "index.json")
    document = next((item for item in index["documents"] if item["doc_id"] == doc_id), None)
    if document is None or not (stock_dir / document["pdf_path"]).is_file():
        abort(404)
    return send_file(stock_dir / document["pdf_path"], mimetype="application/pdf")


@ir_docs_bp.route("/stock/<code_s>/ir_docs/tdnet", methods=["POST"])
def post_ir_docs_tdnet(code_s: str):
    """株探 (TDnet) から直近1年の資料を収集する。取得済みはスキップされるので2回目以降は速い。"""
    import ir_docs

    # 訂正版が入ると原本が旧版になり is_latest の件数は増えないので、全件数の差で数える
    index_path = ir_docs.IR_DOCS_DIR / code_s.upper() / "index.json"
    before = len(ir_docs._load_index(index_path)["documents"])
    try:
        ir_docs.download_ir_docs(code_s, depth="1y")
        index = ir_docs._load_index(index_path)
        added = len(index["documents"]) - before
        # 通信障害などは例外にならず collection_errors に残る (成功すると消える) ので、成功と区別して知らせる
        errors = index["collection_errors"].get("1y") or []
        if errors:
            flash(
                f"株探からのIR資料収集で取りこぼしがあります ({code_s}): {added}件追加 / "
                f"失敗{len(errors)}件 ({errors[0]['stage']}: {errors[0]['reason']})",
                "error",
            )
        else:
            flash(f"株探からIR資料を収集しました ({code_s}): {added}件追加", "info")
    except Exception as e:  # noqa: BLE001
        flash(f"株探からのIR資料収集に失敗しました ({code_s}): {e}", "error")
    return redirect(url_for("detail.stock_detail", code_s=code_s, _anchor="ir-docs"))
