"""保有銘柄ダッシュボードルート (Phase 3b / issue #171)。

GET  /portfolio?status=hold|semi|watch  : 3 タブ式ダッシュボード
POST /portfolio/add                     : 3監 への新規追加
POST /portfolio/<code_s>/transition     : ステータス変更
POST /portfolio/<code_s>/delete         : 削除 (3監 のみ)

portfolio_shelve のレコードに stocks_shelve から指標を補完して表示する。
書き込み API はすべて末尾で sync_to_my_watch_list_txt() を呼び、txt 同期を行う。
"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

import portfolio
import portfolio_shelve as ps
from webapp.helpers import (
    get_stock_data,
    list_portfolio_with_indicators,
    resolve_stock_name,
)

portfolio_bp = Blueprint("portfolio", __name__)


STATUS_QUERY_TO_VALUE = {
    "hold": "1保",
    "semi": "2準",
    "watch": "3監",
}
STATUS_VALUE_TO_QUERY = {v: k for k, v in STATUS_QUERY_TO_VALUE.items()}

# 内部キー (1保/2準/3監) → ユーザー可視ラベル (保有/準保有/監視)。
# DB 内部表記は数字付きのまま、UI 表示時はこのマップで日本語ラベルに置換する。
STATUS_VALUE_TO_LABEL = {
    "1保": "保有",
    "2準": "準保有",
    "3監": "監視",
}

# タブ表示順 (左から右): (query, status_value, label)
TABS = [
    ("hold", "1保", STATUS_VALUE_TO_LABEL["1保"]),
    ("semi", "2準", STATUS_VALUE_TO_LABEL["2準"]),
    ("watch", "3監", STATUS_VALUE_TO_LABEL["3監"]),
]
DEFAULT_TAB = TABS[0][0]


def _resolve_status_query(query: str) -> tuple[str, str]:
    """クエリ文字列を (正規化クエリ, status 値) に変換。不明値は DEFAULT_TAB。"""
    q = (query or "").strip().lower()
    if q not in STATUS_QUERY_TO_VALUE:
        q = DEFAULT_TAB
    return q, STATUS_QUERY_TO_VALUE[q]


def _allowed_transitions_from(current: str) -> list[tuple[str, str]]:
    """現在のステータスから許可される遷移先を (label, value) で返す。

    label は UI 表示用 (例: 保有→準保有 は「準保有 (売却)」のように補注を入れる)。
    """
    pairs: list[tuple[str, str]] = []
    for st_from, st_to in ps.ALLOWED_TRANSITIONS:
        if st_from != current:
            continue
        label = STATUS_VALUE_TO_LABEL.get(st_to, st_to)
        if current == "1保" and st_to == "2準":
            label = f"{label} (売却)"
        pairs.append((label, st_to))
    pairs.sort(key=lambda x: x[1])
    return pairs


def _sync_txt_safely() -> None:
    """shelve→txt 同期を try/except で囲んで失敗してもハンドラを止めない。

    IO エラー時は flash で通知。shelve 更新は既に成功済みなので 200 系で返す。
    """
    try:
        ps.sync_to_my_watch_list_txt()
    except Exception as e:
        flash(f"my_watch_list.txt 同期に失敗: {e}", "error")


def _redirect_to_current_tab(code_s: str, fallback_query: str = "watch"):
    """code_s の現在ステータスのタブにリダイレクトする。

    エラー時に元タブに戻すための共通処理。レコードが取れない場合は fallback。
    """
    current = ps.get_record(code_s) or {}
    current_query = STATUS_VALUE_TO_QUERY.get(current.get("status"), fallback_query)
    return redirect(url_for("portfolio.dashboard", status=current_query))


@portfolio_bp.route("/portfolio/<code_s>/delete", methods=["POST"])
def delete(code_s: str):
    """3監 銘柄の物理削除。理由必須。

    1保 / 2準 銘柄に対する直接 POST は portfolio_shelve.delete_record 内部で
    ValueError → flash で対応。UI 側でも 3監 タブのみ削除ボタンを表示する。
    """
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("削除理由は必須です", "error")
        return redirect(url_for("portfolio.dashboard", status="watch"))

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return redirect(url_for("portfolio.dashboard", status="watch"))

    try:
        deleted = ps.delete_record(code_s, reason=reason)
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return _redirect_to_current_tab(code_s, fallback_query="watch")

    if not deleted:
        flash(f"{code_s} は portfolio_shelve に未登録です", "error")
    else:
        flash(f"{code_s} を削除しました", "info")

    _sync_txt_safely()
    return redirect(url_for("portfolio.dashboard", status="watch"))


@portfolio_bp.route("/portfolio/<code_s>/transition", methods=["POST"])
def transition(code_s: str):
    """ステータス変更 (1保→2準 は内部で「売却」種別として記録)。

    portfolio_shelve.transition_status のバリデーションに任せる。
    同一遷移は no-op (Phase 3a 仕様)、不正遷移は ValueError。
    """
    new_status = (request.form.get("new_status") or "").strip()
    reason = (request.form.get("reason") or "").strip()

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))

    if new_status not in ps.VALID_STATUSES:
        flash(f"不正なステータス: {new_status!r}", "error")
        return redirect(url_for("portfolio.dashboard"))

    try:
        ps.transition_status(code_s, new_status, reason=reason)
    except KeyError as e:
        flash(f"レコード未登録: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return _redirect_to_current_tab(code_s, fallback_query=DEFAULT_TAB)

    _sync_txt_safely()
    return redirect(url_for("portfolio.dashboard", status=STATUS_VALUE_TO_QUERY[new_status]))


@portfolio_bp.route("/portfolio/add", methods=["POST"])
def add():
    """銘柄を 3監 として新規追加する。

    既存登録済みなら ValueError → flash 警告で no-op。
    銘柄名は portfolio_shelve には保存しない (表示時に他DBから引く)。
    flash メッセージ用にだけ stocks_shelve / research_shelve から取得する。
    """
    code_s = (request.form.get("code_s") or "").strip()
    if not code_s:
        flash("銘柄コードが空です", "error")
        return redirect(url_for("portfolio.dashboard"))

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))

    normalized = ps.normalize_code_s(code_s)

    # 未知コード防衛: stocks_shelve に存在しないコードは銘柄名解決もできず、
    # txt 同期したときに識別不能な行が混ざるので reject する。
    if not get_stock_data(normalized):
        flash(
            f"{normalized} は stocks_shelve に未登録のコードです。先に銘柄DBへの登録が必要です。",
            "error",
        )
        return redirect(url_for("portfolio.dashboard", status="watch"))

    try:
        ps.add_to_watch(normalized, reason="WebApp 追加")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.dashboard", status="watch"))

    _sync_txt_safely()
    name_for_flash = resolve_stock_name(normalized)
    flash(f"{normalized} {name_for_flash} を監視に追加しました".rstrip(), "info")
    return redirect(url_for("portfolio.dashboard", status="watch"))


def _build_fallback_records() -> list[dict]:
    """portfolio_shelve が空のとき、my_watch_list.txt から仮レコードを組み立てる。

    Phase 3a で portfolio_shelve に移行したが、本ブランチを移行未実施環境で
    動かすと shelve が空 → ダッシュボードも空になり既存運用が壊れる。
    `portfolio.parse_my_portforio()` は同条件で txt にフォールバックする
    挙動を持つので、UI も同じソースを共有する。
    txt 由来レコードはメモを持たず、書き込み API も走らせない (= 表示専用)。
    """
    try:
        watch, possess = portfolio.parse_my_portforio()
    except Exception:  # noqa: BLE001 — txt 不在等は表示空でフェイルセーフ
        return []
    records: list[dict] = []
    for code_s in possess:
        records.append(ps.create_record(code_s, status="1保"))
    for code_s in watch:
        records.append(ps.create_record(code_s, status="3監"))
    return records


@portfolio_bp.route("/portfolio")
def dashboard():
    """3 タブ式ダッシュボード。"""
    active_query, active_status = _resolve_status_query(request.args.get("status", DEFAULT_TAB))

    # 全レコードを 1 度だけ取得し、件数 (全タブ) と表示行 (active タブ) を共に算出する
    all_records = ps.list_records()
    fallback_mode = not all_records
    if fallback_mode:
        all_records = _build_fallback_records()

    counts = {q: 0 for q, _, _ in TABS}
    for r in all_records:
        st = r.get("status")
        if st in STATUS_VALUE_TO_QUERY:
            counts[STATUS_VALUE_TO_QUERY[st]] += 1

    active_records = [r for r in all_records if r.get("status") == active_status]
    rows = list_portfolio_with_indicators(active_records)
    # フォールバック中は書き込み UI (ステータス変更フォーム / 削除フォーム) を
    # 出さない。shelve が空のため transition / delete を呼ぶと KeyError になる。
    transitions = [] if fallback_mode else _allowed_transitions_from(active_status)

    return render_template(
        "portfolio_list.html",
        tabs=TABS,
        active_query=active_query,
        active_status=active_status,
        counts=counts,
        rows=rows,
        transitions=transitions,
        status_label=STATUS_VALUE_TO_LABEL,
        fallback_mode=fallback_mode,
    )
