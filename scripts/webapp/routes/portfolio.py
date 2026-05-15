"""保有銘柄ダッシュボードルート (Phase 3b / issue #171, #175, #178, #186, #215)。

GET  /portfolio?status=hold&gyoutai_theme=半導体 : フィルタ (issue #215)
POST /portfolio/add                              : 3監 への新規追加 / 除外済みの復活
POST /portfolio/<code_s>/transition              : ステータス変更
POST /portfolio/bulk-exclude                     : 2準/3監 銘柄をユニバースから除外 (一括)
POST /portfolio/<code_s>/memo                    : memo 部分更新 (issue #175)

portfolio_shelve のレコードに stocks_shelve から指標を補完して表示する。
書き込み API は txt 関連の状態を変えるもの (add/transition/bulk-exclude) のみ
末尾で sync_to_my_watch_list_txt() を呼ぶ。memo 更新は txt 内容に影響しないため同期不要。

issue #215: ページング/ソートを廃止し、status は単一選択化、業態テーマ select を追加。
業態テーマ指定時は status 無視で全銘柄から該当する業態/テーマだけを抽出する。
"""

from typing import Optional
from urllib.parse import urlencode

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import portfolio
import portfolio_shelve as ps
from webapp.helpers import (
    collect_gyoutai_theme_choices,
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

# フィルタ表示順 (左から右): (query, status_value, label)
# issue #215: select の option 順序を規定する。
STATUS_CHOICES = [
    ("hold", "1保", STATUS_VALUE_TO_LABEL["1保"]),
    ("semi", "2準", STATUS_VALUE_TO_LABEL["2準"]),
    ("watch", "3監", STATUS_VALUE_TO_LABEL["3監"]),
]
DEFAULT_STATUS_QUERY = STATUS_CHOICES[0][0]  # "hold" — 書き込み POST 後フォールバック先


def _parse_status_filter(args) -> Optional[str]:
    """request.args から status フィルタを内部値 (例: "1保") に変換する (issue #215)。

    省略・空文字・「すべて」(value="") は None を返し、ダッシュボード側で
    「全ステータス表示」として扱う。`?status=hold,semi` のようなカンマ区切り
    旧 URL は寛容処理し先頭値のみ採用する (issue #215 で複数選択は廃止)。
    不正値は None (= 全件表示) にフォールバック。
    """
    raw = (args.get("status") or "").strip().lower()
    if not raw:
        return None
    # 旧 URL `?status=hold,semi` 互換: 先頭値だけ採用
    first = raw.split(",")[0].strip()
    return STATUS_QUERY_TO_VALUE.get(first)


def _parse_gyoutai_theme(args) -> Optional[str]:
    """request.args から業態/テーマフィルタを取り出す (issue #215)。

    空文字・未指定は None を返す。値は前後空白除去のみで、後段で
    `gyoutai_themes` リストとの完全一致判定に使う。
    """
    raw = (args.get("gyoutai_theme") or "").strip()
    return raw or None


def _build_query_string(status: Optional[str], gyoutai_theme: Optional[str]) -> str:
    """フィルタから URL クエリ文字列を組み立てる (issue #215)。

    POST → リダイレクトの戻り先として使う。両方 None なら空文字を返し、
    呼び出し側 (`_redirect_with_return_query`) でデフォルトに落ちる。
    """
    params = []
    if status and status in STATUS_VALUE_TO_QUERY:
        params.append(("status", STATUS_VALUE_TO_QUERY[status]))
    if gyoutai_theme:
        params.append(("gyoutai_theme", gyoutai_theme))
    return urlencode(params)


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


def _redirect_with_return_query(
    default_status_query: Optional[str] = None,
    code_s: Optional[str] = None,
):
    """POST フォームに埋め込まれた hidden `return_query` を尊重してリダイレクトする (issue #178, #215)。

    return_query はクエリ文字列 (例: `status=hold&gyoutai_theme=半導体`) を
    そのまま受け取り、URL に付与する。これで POST 後も直前のフィルタが復元できる。

    空 or 未指定時の戻り先 (issue #215):
      - `default_status_query=None` (デフォルト): 素の `/portfolio` (= 全件表示) に戻す。
        引数なし /portfolio の全件状態からの POST を復元するためのフォールバック。
      - `default_status_query="watch"` 等: `?status=<value>` を付与。`add()` から
        追加直後の銘柄が見えるよう監視フィルタに切り替えたい場合に使う。

    安全のため return_query から先頭の `?` を取り除き、改行や `#` も弾く
    (URL injection 防止)。

    issue #195: hidden `return_to=detail` かつ呼出側が `code_s` を渡している
    ときは詳細ページ (`/stock/<code_s>`) にリダイレクトする。`code_s` 未指定
    経路では従来のダッシュボード復帰にフォールバック (url_for build error 防御)。
    """
    if code_s and (request.form.get("return_to") or "").strip() == "detail":
        return redirect(url_for("detail.stock_detail", code_s=code_s))

    raw = (request.form.get("return_query") or "").strip()
    if raw.startswith("?"):
        raw = raw[1:]
    # URL fragment / 改行は弾く (生のフォーム値をそのまま付ける防御)
    if any(ch in raw for ch in ("\n", "\r", "#", " ")):
        raw = ""
    base = url_for("portfolio.dashboard")
    if raw:
        return redirect(f"{base}?{raw}")
    if default_status_query:
        return redirect(f"{base}?status={default_status_query}")
    return redirect(base)


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


def _reject_when_fallback():
    """フォールバック中なら flash + redirect を返す。そうでなければ None。

    issue #178: redirect_query 引数を廃止し、戻り先は hidden return_query (or デフォルト)
    に統一する。
    """
    if _is_fallback_mode():
        flash(
            "portfolio_shelve 未移行モードのため、書き込み操作は無効です。"
            "Phase 3a 移行スクリプト (migrate_my_watch_list_to_shelve.py) を実行してください。",
            "error",
        )
        return _redirect_with_return_query()
    return None


@portfolio_bp.route("/portfolio/bulk-exclude", methods=["POST"])
def bulk_exclude():
    """2準/3監 銘柄を一括でユニバースから除外する (物理削除はしない)。

    フォーム: codes=<code1>&codes=<code2>... / reason=<任意> / return_query=<URLクエリ>
    部分成功許容。1保 が混入していたら該当のみ flash error で報告し、2準/3監 のみ除外を実行する。
    issue #178: 旧 return_to=hold|semi|watch を return_query (hidden) に置換。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    codes = [c.strip() for c in request.form.getlist("codes") if c and c.strip()]
    reason = (request.form.get("reason") or "").strip()

    if not codes:
        flash("除外対象が指定されていません", "error")
        return _redirect_with_return_query()

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
    return _redirect_with_return_query()


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
        return _redirect_with_return_query(code_s=code_s)

    if new_status not in ps.VALID_STATUSES:
        flash(f"不正なステータス: {new_status!r}", "error")
        return _redirect_with_return_query(code_s=code_s)

    try:
        ps.transition_status(code_s, new_status, reason=reason)
    except KeyError as e:
        flash(f"レコード未登録: {e}", "error")
        return _redirect_with_return_query(code_s=code_s)
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return _redirect_with_return_query(code_s=code_s)

    _sync_txt_safely()
    return _redirect_with_return_query(code_s=code_s)


def _extract_memo_fields_from_form(form) -> dict:
    """request.form から MEMO_FIELDS に該当するキーのみを抽出する。

    部分更新セマンティクス (codex P1 対応):
    - キー自体が form に含まれない → 該当フィールドは fields に入れない (現行値据え置き)
    - キーは含まれるが値が "" → 該当フィールドは "" として扱う (メモ削除の意図)
    したがって `form.get(field, "")` で埋めるのは不可。

    抽出した値は textarea の改行 (\\r\\n / \\r) を \\n に正規化し、前後 strip する。
    MEMO_FIELDS 外のキーは無視 (form に紛れ込んでも reject しない)。

    issue #187: gyoutai_themes は `gyoutai_themes_0`, `gyoutai_themes_1` の
    スロット形式で受信し、空文字スロットを除去した list[str] に集約する。
    """
    fields = {}
    for field in ps.MEMO_FIELDS:
        if field in ps.MEMO_LIST_FIELDS:
            continue  # list 系はスロット展開ロジックで別途処理
        if field not in form:
            continue
        raw = form[field] or ""
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        fields[field] = normalized

    # gyoutai_themes_0, gyoutai_themes_1 をスロット展開して list 化 (issue #187)
    themes_slot_keys = [
        f"gyoutai_themes_{i}" for i in range(ps.GYOUTAI_THEMES_MAX_SLOTS)
    ]
    if any(k in form for k in themes_slot_keys):
        themes: list = []
        for k in themes_slot_keys:
            if k not in form:
                continue
            v = (form[k] or "").strip()
            if v:
                themes.append(v)
        fields["gyoutai_themes"] = themes

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
        return _redirect_with_return_query()

    fields = _extract_memo_fields_from_form(request.form)

    try:
        ps.update_memo(code_s, fields)
    except KeyError:
        msg = f"{code_s} は portfolio_shelve に未登録です"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 404
        flash(msg, "error")
        return _redirect_with_return_query()
    except (ValueError, TypeError) as e:
        if is_ajax:
            return jsonify({"ok": False, "error": str(e)}), 400
        flash(str(e), "error")
        return _redirect_with_return_query()

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
                    "gyoutai_themes": list((row.get("memo") or {}).get("gyoutai_themes") or []),
                }
        return jsonify(body)
    flash(f"{code_s} のメモを保存しました", "info")
    return _redirect_with_return_query()


@portfolio_bp.route("/portfolio/add", methods=["POST"])
def add():
    """銘柄を 3監 として新規追加する。除外済みコードを再投入したら復活する。

    挙動:
    - portfolio_shelve に未登録 → stocks_shelve 存在チェック → 新規追加
    - portfolio_shelve に excluded=True で存在 → 復活 (stocks_shelve チェック skip)
    - portfolio_shelve に excluded=False で存在 → 既登録扱い、ValueError flash
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    # 追加直後は監視リストを見たいので、return_query 未指定時のデフォルトは watch。
    code_s = (request.form.get("code_s") or "").strip()
    # issue #195: 詳細モーダルから理由を受け取れるように引数化。
    # 未送信フォーム (portfolio ダッシュボード追加) では空文字 → 従来の "WebApp 追加" にフォールバック。
    reason = (request.form.get("reason") or "").strip() or "WebApp 追加"
    if not code_s:
        flash("銘柄コードが空です", "error")
        return _redirect_with_return_query(default_status_query="watch")

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return _redirect_with_return_query(default_status_query="watch")

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
            return _redirect_with_return_query(default_status_query="watch")

    try:
        ps.add_to_watch(normalized, reason=reason)
    except ValueError as e:
        flash(str(e), "error")
        return _redirect_with_return_query(default_status_query="watch", code_s=normalized)

    _sync_txt_safely()
    name_for_flash = resolve_stock_name(normalized)
    if is_revival:
        flash(f"{normalized} {name_for_flash} をユニバースに復活しました".rstrip(), "info")
    else:
        flash(f"{normalized} {name_for_flash} を監視に追加しました".rstrip(), "info")
    return _redirect_with_return_query(default_status_query="watch", code_s=normalized)


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
    """フィルタ型ダッシュボード (issue #215 でページング/ソートは廃止)。

    URL クエリ:
      status:        hold / semi / watch のいずれか単一 (省略時=全ステータス)
      gyoutai_theme: 業態/テーマ名の完全一致 (省略時=全業態テーマ)
                     指定時は status を無視して全銘柄から該当業態/テーマを抽出。
    並び順は常時業態順。`sort` / `page` パラメータは silent に無視 (旧 URL 互換)。
    """
    active_status = _parse_status_filter(request.args)
    active_gyoutai_theme = _parse_gyoutai_theme(request.args)

    # issue #186: fallback 判定は除外含む全件で行う (全件除外時の誤判定を避ける)。
    # 表示・件数カウントは除外を弾いた visible_records を使う。
    all_records_inc = ps.list_records(include_excluded=True)
    fallback_mode = not all_records_inc
    if fallback_mode:
        visible_records = _build_fallback_records()
    else:
        visible_records = [r for r in all_records_inc if not r.get("excluded", False)]

    # 件数 (フィルタ前): hold/semi/watch ごとに常に出す (status select の option ラベル用)
    counts = {q: 0 for q, _, _ in STATUS_CHOICES}
    for r in visible_records:
        st = r.get("status")
        if st in STATUS_VALUE_TO_QUERY:
            counts[STATUS_VALUE_TO_QUERY[st]] += 1

    # issue #215: gyoutai_theme 指定時は status 無視で業態/テーマ完全一致のレコードのみ。
    # 未指定時は status フィルタ (None なら全件)。
    if active_gyoutai_theme:
        filtered_records = [
            r for r in visible_records
            if active_gyoutai_theme in ((r.get("memo") or {}).get("gyoutai_themes") or [])
        ]
    elif active_status is None:
        filtered_records = visible_records
    else:
        filtered_records = [r for r in visible_records if r.get("status") == active_status]
    rows = list_portfolio_with_indicators(filtered_records)

    # 行ごとに transitions を埋める。fallback 中は空。
    for row in rows:
        row["transitions"] = (
            [] if fallback_mode else _allowed_transitions_from(row.get("status", ""))
        )

    # 削除モードの可否: 表示中の rows に 2準/3監 が含まれる時のみ
    delete_mode_allowed = (
        not fallback_mode
        and any(row.get("status") in ("2準", "3監") for row in rows)
    )

    # POST → リダイレクトの戻り先として使う現状クエリ (status/gyoutai_theme)
    return_query = _build_query_string(active_status, active_gyoutai_theme)
    # テンプレートで select の selected 判定に使う、active_status の query 形式 (例: "hold")
    active_status_query = (
        STATUS_VALUE_TO_QUERY[active_status] if active_status in STATUS_VALUE_TO_QUERY else ""
    )

    # issue #187: datalist 候補は表示フィルタ/excluded と独立、shelve 内の全レコードから集計
    if fallback_mode:
        gyoutai_theme_choices: list = []
    else:
        gyoutai_theme_choices = collect_gyoutai_theme_choices(all_records_inc)

    return render_template(
        "portfolio_list.html",
        status_choices=STATUS_CHOICES,
        active_status_query=active_status_query,
        active_gyoutai_theme=active_gyoutai_theme or "",
        counts=counts,
        rows=rows,
        total=len(rows),
        return_query=return_query,
        delete_mode_allowed=delete_mode_allowed,
        status_label=STATUS_VALUE_TO_LABEL,
        fallback_mode=fallback_mode,
        gyoutai_theme_choices=gyoutai_theme_choices,
        gyoutai_themes_max_slots=ps.GYOUTAI_THEMES_MAX_SLOTS,
    )
