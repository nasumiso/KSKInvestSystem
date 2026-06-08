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

import threading

from flask import Blueprint, flash, jsonify, request, redirect, url_for

import portfolio_shelve as ps
import theme_suggest
from webapp.helpers import (
    save_memo,
    save_shikiho,
    save_ir_comments,
    save_corporate_url_override,
    save_stock_name_prev,
    add_chat_link,
    update_chat_link,
    delete_chat_link,
    get_research_detail,
    get_stock_data,
)

memo_bp = Blueprint("memo", __name__)

# 業態テーマ提案 (issue #297) の同時実行ガード。
# 単一プロセス運用 (app.run) 前提のプロセス内ロック。多重押下・並行リクエストで
# claude -p が同時に複数走りワーカースレッドが枯渇するのを防ぐ。
# リクエストはロックが空くまでタイムアウト付きで待機し、順番に直列実行する。
# 待ち行列が異常に長い (大量同時押下) 場合のみタイムアウトで 429 を返す。
# ※ 将来マルチプロセス運用 (gunicorn 等) に切り替える場合は file lock 等に置換が必要。
_suggest_themes_lock = threading.Lock()
# 提案ロックの待機タイムアウト (秒)。claude -p (Haiku) 1回が数秒〜十数秒のため、
# 現実的な同時押下 (2〜3件) なら 60 秒で吸収できる。
SUGGEST_THEMES_LOCK_TIMEOUT_SEC = 60


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


@memo_bp.route("/stock/<code_s>/suggest_themes", methods=["POST"])
def post_suggest_themes(code_s: str):
    """業態テーマを事業内容から LLM 提案する (issue #297, AJAX/JSON)。

    事業テキスト (四季報特色・四季報コメント・株探概要) をマスター登録テーマと
    ともに claude -p (Haiku) に渡し、最も合うテーマを最大2件返す。
    提案は保存せず、クライアントが select にプリセットするだけ。

    サーバー側でもボタン表示条件 (テーマ未設定・事業テキストあり) を再検証し、
    直接 API を叩いても設定済み銘柄では LLM を起動しない (コスト暴発防止)。
    """
    # 同時実行ガード: 既に提案処理中ならロックが空くまで待機し、順番に直列実行する。
    # 待ちきれない (待ち行列が長すぎる) 場合のみ 429 を返す。
    if not _suggest_themes_lock.acquire(timeout=SUGGEST_THEMES_LOCK_TIMEOUT_SEC):
        return jsonify({
            "ok": False,
            "error": "提案処理が混み合っています。時間をおいて再試行してください",
        }), 429
    try:
        record = get_research_detail(code_s)
        stock = get_stock_data(code_s)
        if record is None and not stock:
            return jsonify({"ok": False, "error": "銘柄が見つかりません"}), 404

        # サーバー側ガード: 業態テーマが既に設定済みなら起動しない。
        portfolio_record = ps.get_record(code_s)
        memo = (portfolio_record or {}).get("memo")
        if not isinstance(memo, dict):
            memo = {}
        existing_themes = memo.get("gyoutai_themes") or []
        if any((t or "").strip() for t in existing_themes):
            return jsonify({"ok": False, "error": "業態テーマが既に設定済みです"}), 409

        business_text = theme_suggest.build_business_text(
            (record or {}).get("overview"),
            (record or {}).get("shikiho_comments"),
            stock.get("overview"),
        )
        if not business_text:
            return jsonify({
                "ok": True, "preset": [], "low": [], "new": [],
                "reason": "no_business_text",
            })

        theme_names = [t["name"] for t in ps.list_themes()]
        if not theme_names:
            return jsonify({
                "ok": True, "preset": [], "low": [], "new": [],
                "reason": "no_master",
            })

        result = theme_suggest.suggest_gyoutai_themes(business_text, theme_names)
        return jsonify({
            "ok": True,
            "preset": result["preset"],
            "low": result["low"],
            "new": result["new"],
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _suggest_themes_lock.release()
