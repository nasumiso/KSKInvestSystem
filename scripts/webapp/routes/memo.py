"""
メモ保存ルート。

POST /stock/<code_s>/memo             : 手動メモ保存
POST /stock/<code_s>/shikiho          : 四季報保存
POST /stock/<code_s>/ir_comment       : IR分析コメント一括保存
POST /stock/<code_s>/corporate_url    : 会社HP URL 上書き保存/クリア (issue #208)
POST /stock/<code_s>/stock_name_prev  : 旧名/エイリアス 保存/クリア (issue #236, AJAX)
POST /stock/<code_s>/chat_link            : 外部チャットリンク追加 (issue #265, AJAX)
POST /stock/<code_s>/chat_link/<idx>      : 外部チャットリンク更新 (issue #265, AJAX)
POST /stock/<code_s>/chat_link/<idx>/delete : 外部チャットリンク削除 (issue #265, AJAX)
"""

from flask import Blueprint, flash, jsonify, request, redirect, url_for

from webapp.helpers import (
    save_memo,
    save_shikiho,
    save_ir_comments,
    save_corporate_url_override,
    save_stock_name_prev,
    add_chat_link,
    update_chat_link,
    delete_chat_link,
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


@memo_bp.route("/stock/<code_s>/stock_name_prev", methods=["POST"])
def post_stock_name_prev(code_s: str):
    """旧名/エイリアスの手動編集 -> 204/JSON (issue #236, AJAX 想定)。

    空文字保存で stock_name_prev を None にリセット (= 手動エイリアス解除)。
    成功は 204 No Content、未登録は 404、不正値は 400 (いずれも JSON エラー本文)。
    """
    value = request.form.get("stock_name_prev", "")
    try:
        save_stock_name_prev(code_s, value)
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return ("", 204)


# =======================================================
# 外部チャットリンク (chat_links) ルート (issue #265, AJAX/JSON)
# =======================================================

def _validate_chat_url(url: str):
    """chat_link の URL を検証する。不正なら (error_message) を返し、正常なら None。"""
    if not url:
        return "URL を入力してください"
    if not (url.startswith("http://") or url.startswith("https://")):
        return "URL は http:// または https:// で始めてください"
    return None


@memo_bp.route("/stock/<code_s>/chat_link", methods=["POST"])
def post_chat_link_add(code_s: str):
    """外部チャットリンク追加 -> JSON {ok, links}。"""
    label = request.form.get("label", "")
    url = request.form.get("url", "").strip()
    err = _validate_chat_url(url)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        links = add_chat_link(code_s, label, url)
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({"ok": True, "links": links}), 201


@memo_bp.route("/stock/<code_s>/chat_link/<int:idx>", methods=["POST"])
def post_chat_link_update(code_s: str, idx: int):
    """外部チャットリンク更新 -> JSON {ok, links}。範囲外 idx は 400。"""
    label = request.form.get("label", "")
    url = request.form.get("url", "").strip()
    err = _validate_chat_url(url)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        links = update_chat_link(code_s, idx, label, url)
    except IndexError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({"ok": True, "links": links})


@memo_bp.route("/stock/<code_s>/chat_link/<int:idx>/delete", methods=["POST"])
def post_chat_link_delete(code_s: str, idx: int):
    """外部チャットリンク削除 -> JSON {ok, links}。範囲外 idx は 400。"""
    try:
        links = delete_chat_link(code_s, idx)
    except IndexError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({"ok": True, "links": links})
