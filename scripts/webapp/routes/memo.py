"""
メモ保存ルート。

POST /stock/<code_s>/memo           : 手動メモ保存
POST /stock/<code_s>/shikiho        : 四季報保存
POST /stock/<code_s>/ir_comment     : IR分析コメント一括保存
POST /stock/<code_s>/corporate_url  : 会社HP URL 上書き保存/クリア (issue #208)
"""

from flask import Blueprint, flash, request, redirect, url_for

from webapp.helpers import (
    save_memo,
    save_shikiho,
    save_ir_comments,
    save_corporate_url_override,
)

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


@memo_bp.route("/stock/<code_s>/corporate_url", methods=["POST"])
def post_corporate_url(code_s: str):
    """会社HP URL の手動上書き保存/クリア -> flash + 302リダイレクト (issue #208)。

    空文字なら上書きをクリア (デフォルトに戻る)。
    http/https 以外で始まる URL は flash error で拒否する。
    """
    url = request.form.get("url", "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        flash(
            f"会社HPリンクの保存に失敗しました ({code_s}): "
            "URL は http:// または https:// で始めてください",
            "error",
        )
        return redirect(url_for("detail.stock_detail", code_s=code_s))
    try:
        # save_corporate_url_override は入力値がデフォルトと一致した場合に
        # 空文字 (= override クリア) として保存し、その実値を返す
        saved = save_corporate_url_override(code_s, url)
        if saved:
            flash(f"会社HPリンクを更新しました ({code_s})", "info")
        else:
            flash(f"会社HPリンクをデフォルトに戻しました ({code_s})", "info")
    except Exception as e:  # noqa: BLE001
        flash(f"会社HPリンクの保存に失敗しました ({code_s}): {e}", "error")
    return redirect(url_for("detail.stock_detail", code_s=code_s))
