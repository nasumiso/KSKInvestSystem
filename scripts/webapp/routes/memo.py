"""
メモ保存ルート。

POST /stock/<code_s>/memo       : 手動メモ保存
POST /stock/<code_s>/shikiho    : 四季報保存
POST /stock/<code_s>/ir_comment : IR分析コメント一括保存
"""

from flask import Blueprint, request, redirect, url_for

from webapp.helpers import save_memo, save_shikiho, save_ir_comments

memo_bp = Blueprint("memo", __name__)


@memo_bp.route("/stock/<code_s>/memo", methods=["POST"])
def post_memo(code_s: str):
    """手動メモ保存 -> 302リダイレクト。"""
    save_memo(code_s, request.form)
    return redirect(url_for("detail.stock_detail", code_s=code_s))


@memo_bp.route("/stock/<code_s>/shikiho", methods=["POST"])
def post_shikiho(code_s: str):
    """四季報保存 -> 302リダイレクト。"""
    save_shikiho(code_s, request.form)
    return redirect(url_for("detail.stock_detail", code_s=code_s))


@memo_bp.route("/stock/<code_s>/ir_comment", methods=["POST"])
def post_ir_comment(code_s: str):
    """IR分析コメント一括保存 -> 302リダイレクト。"""
    save_ir_comments(code_s, request.form)
    return redirect(url_for("detail.stock_detail", code_s=code_s))
