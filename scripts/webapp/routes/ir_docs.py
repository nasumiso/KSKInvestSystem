"""
会社IRページからの資料取得ルート (issue #457)。

POST /api/ir_page_candidates/<code_s> : 中計・決算説明資料のPDF候補を返す (AJAX)
POST /stock/<code_s>/ir_page_docs     : 選択・手入力されたPDFを取得して保存
"""

import requests
from flask import Blueprint, flash, jsonify, redirect, request, url_for

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
        return redirect(url_for("detail.stock_detail", code_s=code_s))

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
    return redirect(url_for("detail.stock_detail", code_s=code_s))
