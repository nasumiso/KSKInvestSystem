"""保有銘柄ダッシュボードルート (Phase 3b / issue #171, issue #175, issue #186)。

GET  /portfolio?status=hold|semi|watch  : 3 タブ式ダッシュボード
POST /portfolio/add                     : 3監 への新規追加 / 除外済みの復活
POST /portfolio/<code_s>/transition     : ステータス変更
POST /portfolio/bulk-exclude            : 3監 銘柄をユニバースから除外 (一括)
POST /portfolio/<code_s>/memo           : memo 部分更新 (issue #175)

portfolio_shelve のレコードに stocks_shelve から指標を補完して表示する。
書き込み API は txt 関連の状態を変えるもの (add/transition/bulk-exclude) のみ
末尾で sync_to_my_watch_list_txt() を呼ぶ。memo 更新は txt 内容に影響しないため同期不要。
"""

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import portfolio
import portfolio_shelve as ps
from webapp.helpers import (
    compute_cell_styles,
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


def _is_fallback_mode() -> bool:
    """portfolio_shelve が空 = txt フォールバック中かを判定する。

    フォールバック中に書き込み POST を許すと、shelve に 1 件レコードができた
    時点で次回 dashboard が `list_records()` 非空 → フォールバック解除 →
    残りの txt 銘柄が画面上から消える、という運用事故が起きる (codex 指摘)。
    各 POST ハンドラ冒頭で本関数を見て reject する。

    issue #186: 全レコードが excluded=True の状態を fallback と誤判定しない
    ため、include_excluded=True で取得して空判定する。
    """
    return not ps.list_records(include_excluded=True)


def _reject_when_fallback(redirect_query: str = "watch"):
    """フォールバック中なら flash + redirect を返す。そうでなければ None。"""
    if _is_fallback_mode():
        flash(
            "portfolio_shelve 未移行モードのため、書き込み操作は無効です。"
            "Phase 3a 移行スクリプト (migrate_my_watch_list_to_shelve.py) を実行してください。",
            "error",
        )
        return redirect(url_for("portfolio.dashboard", status=redirect_query))
    return None


@portfolio_bp.route("/portfolio/bulk-exclude", methods=["POST"])
def bulk_exclude():
    """2準/3監 銘柄を一括でユニバースから除外する (物理削除はしない)。

    フォーム: codes=<code1>&codes=<code2>... / reason=<任意> / return_to=<hold|semi|watch>
    部分成功許容。1保 が混入していたら該当のみ flash error で報告し、2準/3監 のみ除外を実行する。
    return_to はリダイレクト先タブ。不正値は watch にフォールバック。
    """
    rejected = _reject_when_fallback(redirect_query="watch")
    if rejected is not None:
        return rejected

    codes = [c.strip() for c in request.form.getlist("codes") if c and c.strip()]
    reason = (request.form.get("reason") or "").strip()
    return_to = (request.form.get("return_to") or "watch").strip()
    if return_to not in STATUS_QUERY_TO_VALUE:
        return_to = "watch"

    if not codes:
        flash("除外対象が指定されていません", "error")
        return redirect(url_for("portfolio.dashboard", status=return_to))

    success: list[str] = []
    failures: list[str] = []
    for raw in codes:
        try:
            ps.validate_code_s(raw)
            normalized = ps.normalize_code_s(raw)
        except (ValueError, TypeError) as e:
            failures.append(f"{raw}: 不正なコード ({e})")
            continue
        try:
            ok = ps.exclude_from_universe(normalized, reason=reason)
        except (ValueError, TypeError) as e:
            failures.append(f"{normalized}: {e}")
            continue
        if ok:
            success.append(normalized)
        else:
            failures.append(f"{normalized}: 未登録または既に除外済み")

    if success:
        _sync_txt_safely()
        flash(f"{len(success)} 件をユニバースから除外しました ({', '.join(success)})", "info")
    if failures:
        flash("除外できなかったコードがあります: " + " / ".join(failures), "error")
    return redirect(url_for("portfolio.dashboard", status=return_to))


@portfolio_bp.route("/portfolio/<code_s>/transition", methods=["POST"])
def transition(code_s: str):
    """ステータス変更 (1保→2準 は内部で「売却」種別として記録)。

    portfolio_shelve.transition_status のバリデーションに任せる。
    同一遷移は no-op (Phase 3a 仕様)、不正遷移は ValueError。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

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


def _extract_memo_fields_from_form(form) -> dict:
    """request.form から MEMO_FIELDS に該当するキーのみを抽出する。

    部分更新セマンティクス (codex P1 対応):
    - キー自体が form に含まれない → 該当フィールドは fields に入れない (現行値据え置き)
    - キーは含まれるが値が "" → 該当フィールドは "" として扱う (メモ削除の意図)
    したがって `form.get(field, "")` で埋めるのは不可。

    抽出した値は textarea の改行 (\\r\\n / \\r) を \\n に正規化し、前後 strip する。
    MEMO_FIELDS 外のキーは無視 (form に紛れ込んでも reject しない)。
    """
    fields = {}
    for field in ps.MEMO_FIELDS:
        if field not in form:
            continue
        raw = form[field] or ""
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        fields[field] = normalized
    return fields


def _is_ajax_request() -> bool:
    """AJAX リクエストかを判定する (issue #177 inline 編集対応)。

    fetch() からの呼び出しは X-Requested-With ヘッダ or JSON Accept で識別する。
    """
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )


@portfolio_bp.route("/portfolio/<code_s>/memo", methods=["POST"])
def update_memo(code_s: str):
    """memo を部分更新する (issue #175 / #177 inline 編集対応)。

    フォームから送られた MEMO_FIELDS のキーのみを対象に部分更新する。
    送られなかったキーは現行値据え置き。空文字を明示送信した場合はメモ削除扱い。

    AJAX リクエスト時は JSON で結果を返す (issue #177): {ok, code_s, fields}
    通常 form 送信時は flash + redirect (既存 issue #175 挙動)。
    """
    is_ajax = _is_ajax_request()
    rejected = _reject_when_fallback()
    if rejected is not None:
        if is_ajax:
            return jsonify({"ok": False, "error": "fallback_mode"}), 409
        return rejected

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        if is_ajax:
            return jsonify({"ok": False, "error": f"不正な銘柄コード: {e}"}), 400
        flash(f"不正な銘柄コード: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))

    fields = _extract_memo_fields_from_form(request.form)

    try:
        ps.update_memo(code_s, fields)
    except KeyError:
        msg = f"{code_s} は portfolio_shelve に未登録です"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 404
        flash(msg, "error")
        return redirect(url_for("portfolio.dashboard"))
    except (ValueError, TypeError) as e:
        if is_ajax:
            return jsonify({"ok": False, "error": str(e)}), 400
        flash(str(e), "error")
        return _redirect_to_current_tab(code_s, fallback_query=DEFAULT_TAB)

    if is_ajax:
        # 保存後の row を再構築して、更新済みフィールドと styles を返す
        # (codex P2 対応: inline 編集後にクライアント側で色を即時更新するため)
        rec = ps.get_record(code_s) or {}
        body = {"ok": True, "code_s": code_s, "fields": fields}
        if rec:
            rows = list_portfolio_with_indicators([rec])
            if rows:
                row = rows[0]
                body["styles"] = row.get("styles") or {}
                # 表示文字列も返す (更新日 / ステージ等の見た目同期に使う)
                body["display"] = {
                    "last_research_update": (row.get("memo") or {}).get("last_research_update") or "—",
                    "stage": (row.get("memo") or {}).get("stage") or "—",
                    "gyoutai_theme": (row.get("memo") or {}).get("gyoutai_theme") or "",
                }
        return jsonify(body)
    flash(f"{code_s} のメモを保存しました", "info")
    return _redirect_to_current_tab(code_s, fallback_query=DEFAULT_TAB)


@portfolio_bp.route("/portfolio/add", methods=["POST"])
def add():
    """銘柄を 3監 として新規追加する。除外済みコードを再投入したら復活する。

    挙動:
    - portfolio_shelve に未登録 → stocks_shelve 存在チェック → 新規追加
    - portfolio_shelve に excluded=True で存在 → 復活 (stocks_shelve チェック skip)
    - portfolio_shelve に excluded=False で存在 → 既登録扱い、ValueError flash
    """
    rejected = _reject_when_fallback(redirect_query="watch")
    if rejected is not None:
        return rejected

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

    # 既存レコード (除外済 含む) があれば stocks_shelve チェックを skip して復活パスへ。
    # 未登録コードのときのみ未知コード防衛を適用する。
    existing = ps.get_record(normalized)
    is_revival = bool(existing and existing.get("excluded", False))
    if existing is None:
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
    if is_revival:
        flash(f"{normalized} {name_for_flash} をユニバースに復活しました".rstrip(), "info")
    else:
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

    # issue #186: fallback 判定は除外含む全件で行う (全件除外時の誤判定を避ける)。
    # 表示・件数カウントは除外を弾いた visible_records を使う。
    all_records_inc = ps.list_records(include_excluded=True)
    fallback_mode = not all_records_inc
    if fallback_mode:
        visible_records = _build_fallback_records()
    else:
        visible_records = [r for r in all_records_inc if not r.get("excluded", False)]

    counts = {q: 0 for q, _, _ in TABS}
    for r in visible_records:
        st = r.get("status")
        if st in STATUS_VALUE_TO_QUERY:
            counts[STATUS_VALUE_TO_QUERY[st]] += 1

    active_records = [r for r in visible_records if r.get("status") == active_status]
    rows = list_portfolio_with_indicators(active_records)
    # フォールバック中は書き込み UI (ステータス変更フォーム / 削除モード) を
    # 出さない。shelve が空のため transition / exclude を呼ぶと KeyError になる。
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
