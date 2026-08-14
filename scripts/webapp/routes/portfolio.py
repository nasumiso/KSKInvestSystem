"""保有銘柄ダッシュボードルート (Phase 3b / issue #171, #175, #178, #186, #215)。

GET  /portfolio?status=hold&gyoutai_theme=半導体 : フィルタ (issue #215)
POST /portfolio/add                              : 3監 への新規追加 / 除外済みの復活
POST /portfolio/status                           : コード指定の保有ステータス変更
POST /portfolio/<code_s>/transition              : ステータス変更
POST /portfolio/bulk-exclude                     : 2準/3監 銘柄をユニバースから除外 (一括)
POST /portfolio/bulk-transition                  : ステータスを一括変更
POST /portfolio/<code_s>/memo                    : memo 部分更新 (issue #175)
POST /portfolio/csv-import/preview               : ポートフォリオCSV差分プレビュー (issue #397 Phase3)
POST /portfolio/csv-import/apply                 : ポートフォリオCSV反映

portfolio_shelve のレコードに stocks_shelve から指標を補完して表示する。
書き込み API は txt 関連の状態を変えるもの (add/transition/bulk-exclude/bulk-transition) のみ
末尾で sync_to_my_watch_list_txt() を呼ぶ。memo 更新は txt 内容に影響しないため同期不要。

issue #215: ページング/ソートを廃止し、status は単一選択化、業態テーマ select を追加。
業態テーマ指定時は status 無視で全銘柄から該当する業態/テーマだけを抽出する。
"""

import datetime
import os
import re
import shutil
import uuid
from typing import Optional
from urllib.parse import urlencode

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import import_portfolio_csv as csv_import
import portfolio
import portfolio_shelve as ps
import research_shelve
from ks_util import DATA_DIR
from webapp.helpers import (
    MONTHLY_TAG_DESCRIPTIONS,
    TAG_DESCRIPTIONS,
    THEME_SUMMARY_SORT_FIELDS,
    build_portfolio_theme_summary,
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
PORTFOLIO_SORT_KEYS = {
    "position", "rank", "gyoutai", "rating", "rs",
    "rs_change_1d", "rs_change_5d", "rs_change_20d",
}
DEFAULT_SORT_KEY = "position"
DEFAULT_SORT_KEY_BY_STATUS = {
    "2準": "rank",
    "3監": "rank",
}
_STAGE_SINGLE_RE = re.compile(r"^(?P<s>[1-4]S)(?:\((?P<t>[1-9])(?P<unit>[TB])\))?$")
_STAGE_TRANSITION_RE = re.compile(r"^(?P<left>[1-4]S)(?:\((?P<t>[1-9])(?P<unit>[TB])\))?~(?P<right>[1-4]S)$")


def _default_sort_key_for_status(active_status: Optional[str]) -> str:
    """status ごとの portfolio 一覧デフォルト sort key を返す。"""
    return DEFAULT_SORT_KEY_BY_STATUS.get(active_status, DEFAULT_SORT_KEY)


def _parse_status_filter(args) -> Optional[str]:
    """request.args から status フィルタを内部値 (例: "1保") に変換する (issue #215)。

    `?status=` (key あり・値空) は「すべて」を明示選択した状態として None。
    `status` key 自体が無い (= 素の /portfolio) ときは保有 ("1保") をデフォルト。
    `?status=hold,semi` のようなカンマ区切り旧 URL は寛容処理し先頭値のみ採用する
    (issue #215 で複数選択は廃止)。不正値は None (= 全件表示) にフォールバック。
    """
    if "status" not in args:
        # 素の /portfolio: 保有銘柄に絞り込むのが運用上の主な使い方
        return "1保"
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


def _parse_sort_key(args, active_status: Optional[str] = None) -> str:
    """request.args から portfolio 一覧の sort key を取り出す (issue #274)。"""
    raw = (args.get("sort") or "").strip().lower()
    if raw in PORTFOLIO_SORT_KEYS:
        return raw
    return _default_sort_key_for_status(active_status)


def _build_query_string(
    status: Optional[str],
    gyoutai_theme: Optional[str],
    sort_key: Optional[str] = None,
    include_empty_status: bool = False,
) -> str:
    """フィルタ / ソートから URL クエリ文字列を組み立てる。

    include_empty_status=True のときだけ、status None を「すべて」の
    明示選択として `status=` にする。POST 後の素の /portfolio fallback と
    画面上の「すべて」選択維持を混同しないため、呼び出し側で明示する。
    """
    params = []
    if status and status in STATUS_VALUE_TO_QUERY:
        params.append(("status", STATUS_VALUE_TO_QUERY[status]))
    elif status is None and include_empty_status:
        params.append(("status", ""))
    if gyoutai_theme:
        params.append(("gyoutai_theme", gyoutai_theme))
    if sort_key:
        params.append((
            "sort",
            sort_key if sort_key in PORTFOLIO_SORT_KEYS else DEFAULT_SORT_KEY,
        ))
    return urlencode(params)


def _allowed_transitions_from(current: str) -> list[tuple[str, str]]:
    """現在のステータスから許可される遷移先を (label, value) で返す。

    label は UI 表示用 (例: 保有→準保有 は「準保有 (売却)」のように補注を入れる)。

    issue #269: 現在 1保 のときは「1保 (株数のみ変更)」を選択肢に含める。
    送信時 transition_status() は同一ステータスで no-op となり、qty だけ更新される。
    """
    pairs: list[tuple[str, str]] = []
    for st_from, st_to in ps.ALLOWED_TRANSITIONS:
        if st_from != current:
            continue
        label = STATUS_VALUE_TO_LABEL.get(st_to, st_to)
        if current == "1保" and st_to == "2準":
            label = f"{label} (売却)"
        pairs.append((label, st_to))
    # issue #269: 1保 のとき「保有のまま (株数のみ変更)」を先頭に追加
    if current == "1保":
        pairs.insert(0, ("保有のまま (株数のみ変更)", "1保"))
    else:
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


def _flash_stock_info(code_s: str, message_suffix: str) -> None:
    """銘柄コードに詳細ページリンクを付けて info flash する。"""
    normalized = ps.normalize_code_s(code_s)
    detail_url = url_for("detail.stock_detail", code_s=normalized)
    flash(
        f'<a href="{detail_url}">{normalized}</a>{message_suffix}',
        "html_info",
    )


def _parse_stage_value(raw: str) -> dict:
    """保存済み stage 文字列を structured UI の初期値へ分解する。"""
    value = (raw or "").strip()
    parsed = {
        "raw": value,
        "s": "",
        "t": "",
        "unit": "T",
        "structured": True,
        "rescue": "",
    }
    if not value:
        return parsed
    if value in ps.STAGE_OPTIONS:
        parsed["s"] = value
        parsed["unit"] = "B" if value.split("~", 1)[0] == "2S" else "T"
        return parsed
    m = _STAGE_SINGLE_RE.match(value)
    if m:
        parsed["s"] = m.group("s") or ""
        parsed["t"] = m.group("t") or ""
        parsed["unit"] = m.group("unit") or ("B" if parsed["s"] == "2S" else "T")
        return parsed
    m = _STAGE_TRANSITION_RE.match(value)
    if m:
        parsed["s"] = f'{m.group("left")}~{m.group("right")}'
        parsed["t"] = m.group("t") or ""
        parsed["unit"] = m.group("unit") or ("B" if (m.group("left") or "") == "2S" else "T")
        return parsed
    parsed["structured"] = False
    parsed["rescue"] = value
    return parsed


def _compose_stage_value(stage_s: str, stage_t: str) -> str:
    """structured UI の値から保存用 stage 文字列を組み立てる。"""
    s_value = (stage_s or "").strip()
    t_value = (stage_t or "").strip()
    if not s_value:
        return ""
    if "or" in s_value:
        return s_value
    left_stage = s_value.split("~", 1)[0]
    unit = "B" if left_stage == "2S" else "T"
    if t_value:
        if "~" in s_value:
            left, right = s_value.split("~", 1)
            return f"{left}({t_value}{unit})~{right}"
        return f"{s_value}({t_value}{unit})"
    return s_value


def _parse_chart_value(raw: str) -> dict:
    """保存済み jukyu_chart を structured UI の 2 軸へ分解する。"""
    value = (raw or "").strip()
    parsed = {
        "raw": value,
        "style": "",
        "state": "",
        "structured": True,
        "rescue": "",
    }
    if not value:
        return parsed
    states = sorted(ps.CHART_STATE_OPTIONS, key=len, reverse=True)
    normalized = value.replace("週足月足", "月足", 1) if value.startswith("週足月足") else value
    for style in ("", *ps.CHART_STYLE_OPTIONS):
        if style and not normalized.startswith(style):
            continue
        rest = normalized[len(style):]
        for state in ("", *states):
            if state and not rest.endswith(state):
                continue
            middle = rest[: len(rest) - len(state)] if state else rest
            if middle == "":
                parsed["style"] = style
                parsed["state"] = state
                return parsed
    parsed["structured"] = False
    parsed["rescue"] = value
    return parsed


def _compose_chart_value(style: str, state: str) -> str:
    """structured UI の 2 軸から保存用 jukyu_chart を組み立てる。"""
    return "".join(((style or "").strip(), (state or "").strip()))


def _augment_stage_chart_meta(memo: dict) -> dict:
    """テンプレート用に stage / jukyu_chart の分解済みメタ情報を足す。"""
    memo["stage_struct"] = _parse_stage_value(memo.get("stage") or "")
    memo["jukyu_chart_struct"] = _parse_chart_value(memo.get("jukyu_chart") or "")
    return memo


def _redirect_with_return_query(
    default_status_query: Optional[str] = None,
    code_s: Optional[str] = None,
):
    """POST フォームに埋め込まれた hidden `return_query` を尊重してリダイレクトする (issue #178, #215)。

    return_query はクエリ文字列 (例: `status=hold&gyoutai_theme=半導体`) を
    そのまま受け取り、URL に付与する。これで POST 後も直前のフィルタが復元できる。

    空 or 未指定時の戻り先 (issue #215):
      - `default_status_query=None` (デフォルト): 素の `/portfolio` (= 保有デフォルト) に戻す。
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


# ポートフォリオCSV取込 (issue #397 Phase3): アップロードした4CSVをプレビュー→反映の
# 2ステップ間で使い回すための一時保存先。トークン (uuid) 単位でサブディレクトリを分ける。
PORTFOLIO_CSV_IMPORT_TMP_DIR = os.path.join(DATA_DIR, "portfolio_csv_import_tmp")


@portfolio_bp.route("/portfolio/csv-import/preview", methods=["POST"])
def csv_import_preview():
    """ポートフォリオCSV (楽天/SBI 現物・信用 計4ファイル) の差分プレビュー (issue #397 Phase3)。

    アップロードされたCSVを一時ディレクトリに保存し、dry-run で差分を計算して
    プレビュー画面を表示する。反映 (apply) は保存済みの一時ファイルを再利用する。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    files = [f for f in request.files.getlist("csv_files") if f and f.filename]
    if not files:
        flash("CSVファイルが選択されていません。4ファイル (楽天現物/楽天信用/SBI現物/SBI信用) を選択してください。", "error")
        return redirect(url_for("portfolio.dashboard"))

    os.makedirs(PORTFOLIO_CSV_IMPORT_TMP_DIR, exist_ok=True)
    token = uuid.uuid4().hex
    tmp_dir = os.path.join(PORTFOLIO_CSV_IMPORT_TMP_DIR, token)
    os.makedirs(tmp_dir)
    saved_paths = []
    for i, f in enumerate(files):
        # SBI 現物・信用は同名 "SaveFile*.csv" で降ってくることがあるため、
        # ファイル名衝突による上書きを避け連番接頭辞を付けて保存する。
        path = os.path.join(tmp_dir, f"{i}_{os.path.basename(f.filename)}")
        f.save(path)
        saved_paths.append(path)

    as_of = datetime.datetime.now(ps.JST).date().isoformat()

    try:
        result = csv_import.import_csvs(saved_paths, as_of, dry_run=True, allow_partial=True)
    except ValueError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        flash(f"CSV取込でエラーが発生しました: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))

    diffs = [d for d in result["diffs"] if d["judgement"] not in ("一致", "対象外")]
    for d in diffs:
        d["stock_name"] = resolve_stock_name(d["code_s"])
        # 新規IN候補は確認画面で戦略を選び直せるようにするため、現在の戦略を渡す
        # (issue #397 Phase3b)。未設定なら空文字のまま (select の初期値なし)。
        if "新規IN候補" in d["judgement"]:
            record = ps.get_record(d["code_s"])
            d["current_trade_idea"] = (record.get("memo") or {}).get("trade_idea", "") if record else ""
    out_count = sum(1 for d in diffs if "売却候補" in d["judgement"])

    ps.seed_trade_ideas()
    trade_idea_master = ps.list_trade_ideas()
    return render_template(
        "portfolio_csv_import.html",
        token=token,
        as_of=as_of,
        sources=result["sources"],
        carried_over_sources=result["carried_over_sources"],
        missing_sources=result["missing_sources"],
        diffs=diffs,
        out_count=out_count,
        trade_idea_options=[t["name"] for t in trade_idea_master],
    )


@portfolio_bp.route("/portfolio/csv-import/cancel", methods=["POST"])
def csv_import_cancel():
    """CSV差分プレビューを中止し、一時保存したアップロードファイルを削除する。"""
    token = (request.form.get("token") or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", token):
        shutil.rmtree(os.path.join(PORTFOLIO_CSV_IMPORT_TMP_DIR, token), ignore_errors=True)
    return redirect(url_for("portfolio.dashboard"))


@portfolio_bp.route("/portfolio/csv-import/apply", methods=["POST"])
def csv_import_apply():
    """プレビュー画面の「この内容で反映」実行 (issue #397 Phase3/Phase3b)。

    preview で保存した一時ファイル (token で特定) を使い、covered な銘柄の
    qty/status を実際に同期する (apply_records=True, Phase2ロジックを呼ぶだけ)。
    売却・新規IN の行に入力された戦略・振り返りメモ (フォーム: trade_idea_<code_s> /
    note_<code_s>) を overrides として渡し、_sync_records に反映させる。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    token = (request.form.get("token") or "").strip()
    as_of = (request.form.get("as_of") or "").strip()
    # token は uuid4().hex (32桁16進) 前提。パストラバーサル対策として形式検証する。
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        flash("プレビューが期限切れです。もう一度CSVを選択してください。", "error")
        return redirect(url_for("portfolio.dashboard"))
    tmp_dir = os.path.join(PORTFOLIO_CSV_IMPORT_TMP_DIR, token)
    if not as_of or not os.path.isdir(tmp_dir):
        flash("プレビューが期限切れです。もう一度CSVを選択してください。", "error")
        return redirect(url_for("portfolio.dashboard"))

    overrides: dict = {}
    for key, value in request.form.items():
        if key.startswith("trade_idea_"):
            code_s = key[len("trade_idea_"):]
            # 未送信と明示的な空選択を区別する。空選択は既存戦略を外して
            # 自動INではなく保留キューへ送る指示として扱う。
            overrides.setdefault(code_s, {})["trade_idea"] = value.strip()
        elif key.startswith("note_"):
            code_s = key[len("note_"):]
            if value.strip():
                overrides.setdefault(code_s, {})["note"] = value.strip()

    csv_paths = [os.path.join(tmp_dir, name) for name in os.listdir(tmp_dir)]
    try:
        result = csv_import.import_csvs(
            csv_paths, as_of, dry_run=False, allow_partial=True, apply_records=True,
            overrides=overrides,
        )
    except ValueError as e:
        flash(f"CSV取込でエラーが発生しました: {e}", "error")
        return redirect(url_for("portfolio.dashboard"))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    _sync_txt_safely()
    applied = result["applied"]
    if applied:
        summary = " / ".join(f"{a['code_s']} {a['action']} ({a['detail']})" for a in applied)
        flash(f"ポートフォリオCSV反映完了 ({len(applied)}件): {summary}", "success")
    else:
        flash("ポートフォリオCSV反映完了: 反映対象の変更はありませんでした。", "info")
    return redirect(url_for("portfolio.dashboard"))


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
    # issue #221: 詳細モーダルからの単一除外 (return_to=detail + return_code_s) は
    # 同じ詳細ページに戻す。除外後は未登録扱いで表示されるため、操作結果が画面に反映される。
    return_code = (request.form.get("return_code_s") or "").strip()
    return _redirect_with_return_query(code_s=return_code or None)


@portfolio_bp.route("/portfolio/bulk-transition", methods=["POST"])
def bulk_transition():
    """表示中に選択した複数銘柄のステータスを一括変更する。

    フォーム: codes=<code1>&codes=<code2>... / new_status=<1保|2準|3監> /
           reason=<任意> / action_date=<任意> / return_query=<URLクエリ>
    部分成功許容。個別 transition と同じ ALLOWED_TRANSITIONS を使い、不可な銘柄だけ
    failure として報告する。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    codes = [c.strip() for c in request.form.getlist("codes") if c and c.strip()]
    new_status = (request.form.get("new_status") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    action_date = (request.form.get("action_date") or "").strip() or None

    if not codes:
        flash("変更対象が指定されていません", "error")
        return _redirect_with_return_query()
    if new_status not in ps.VALID_STATUSES:
        flash(f"不正なステータス: {new_status!r}", "error")
        return _redirect_with_return_query()

    success: list[str] = []
    unchanged: list[str] = []
    failures: list[str] = []
    for raw in codes:
        try:
            ps.validate_code_s(raw)
            normalized = ps.normalize_code_s(raw)
        except (ValueError, TypeError) as e:
            failures.append(f"{raw}: 不正なコード ({e})")
            continue

        before = ps.get_record(normalized)
        if before is None:
            failures.append(f"{normalized}: 未登録")
            continue

        old_status = before.get("status")
        try:
            ps.transition_status(
                normalized,
                new_status,
                reason=reason,
                action_date=action_date,
            )
        except (ValueError, TypeError, KeyError) as e:
            failures.append(f"{normalized}: {e}")
            continue

        if old_status == new_status:
            unchanged.append(normalized)
        else:
            success.append(normalized)

    if success:
        _sync_txt_safely()
        new_label = STATUS_VALUE_TO_LABEL.get(new_status, new_status)
        flash(f"{len(success)} 件のステータスを {new_label} に変更しました ({', '.join(success)})", "info")
    if unchanged:
        flash(f"{len(unchanged)} 件は既に同じステータスです ({', '.join(unchanged)})", "info")
    if failures:
        flash("変更できなかったコードがあります: " + " / ".join(failures), "error")
    return _redirect_with_return_query()


@portfolio_bp.route("/portfolio/<code_s>/transition", methods=["POST"])
def transition(code_s: str):
    """ステータス変更 (1保→2準 は内部で「売却」種別として記録)。

    portfolio_shelve.transition_status のバリデーションに任せる。
    同一遷移は no-op (Phase 3a 仕様)、不正遷移は ValueError。

    issue #269: form に qty が含まれていて new_status=1保 のとき、
    ステータス遷移と合わせて保有株数 (qty) も更新する。qty 更新は
    action_log を残さない (portfolio_shelve.update_qty の仕様)。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    new_status = (request.form.get("new_status") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    action_date = (request.form.get("action_date") or "").strip() or None
    # issue #363: 新規1保遷移時のエントリー宣言 (売買戦略を必須化)
    trade_idea = (request.form.get("trade_idea") or "").strip()

    # issue #269: qty は任意。空文字/欠落は None (qty 変更なし)、
    # 整数パース失敗・負数は error flash で reject。
    qty_raw = (request.form.get("qty") or "").strip()
    qty: Optional[int] = None
    if qty_raw:
        try:
            qty = int(qty_raw)
        except ValueError:
            flash(f"不正な株数: {qty_raw!r} (整数で入力してください)", "error")
            return _redirect_with_return_query(code_s=code_s)
        if qty < 0:
            flash(f"不正な株数: {qty} (0 以上で入力してください)", "error")
            return _redirect_with_return_query(code_s=code_s)

    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return _redirect_with_return_query(code_s=code_s)

    if new_status not in ps.VALID_STATUSES:
        flash(f"不正なステータス: {new_status!r}", "error")
        return _redirect_with_return_query(code_s=code_s)

    try:
        before = ps.get_record(code_s)
        old_status = (before or {}).get("status")
        # issue #363: 新規1保遷移 (非1保→1保) は売買戦略の宣言を必須にする。
        # 未分類のまま保有にできると出口ルール未宣言のポジションが生まれるため入口で弾く。
        is_new_hold = new_status == "1保" and old_status != "1保"
        if is_new_hold:
            if not trade_idea:
                flash("売買戦略を選択してください（未分類のまま保有にはできません）", "error")
                return _redirect_with_return_query(code_s=code_s)
            # 未シード環境でも transition POST 単体でバリデーションが成立するようシード (冪等)
            ps.seed_trade_ideas()
            # 戦略を先に保存 → 成功後に遷移。この順序なら戦略保存失敗時は元ステータスのままで
            # 「1保かつ未分類」の中間状態が生まれない。マスター未登録値は update_memo が ValueError で弾く。
            ps.update_memo(code_s, {"trade_idea": trade_idea})
        ps.transition_status(code_s, new_status, reason=reason, action_date=action_date, qty=qty if new_status == "1保" else None)
        # issue #269: 1保 のときだけ qty を反映する (他ステータスでは無視)
        # log_action=False は「非1保 → 1保」の遷移時のみ（IN株数は遷移ログに記録済み）
        # 既に1保の状態で株数のみ変更する場合は log_action=True のまま株数変更ログを残す
        if new_status == "1保" and qty is not None:
            ps.update_qty(code_s, qty, reason=reason, action_date=action_date, log_action=not is_new_hold)
    except KeyError as e:
        flash(f"レコード未登録: {e}", "error")
        return _redirect_with_return_query(code_s=code_s)
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return _redirect_with_return_query(code_s=code_s)

    _sync_txt_safely()
    normalized = ps.normalize_code_s(code_s)
    old_label = STATUS_VALUE_TO_LABEL.get(old_status, old_status)
    new_label = STATUS_VALUE_TO_LABEL.get(new_status, new_status)
    if old_status == new_status:
        _flash_stock_info(normalized, f" のステータスは {new_label} のままです")
    else:
        _flash_stock_info(normalized, f" のステータスを {old_label} から {new_label} に変更しました")
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

    stage_keys = ("stage_s", "stage_t")
    if any(k in form for k in stage_keys):
        stage_s = (form.get("stage_s") or "").strip()
        stage_t = (form.get("stage_t") or "").strip()
        fields["stage"] = _compose_stage_value(stage_s, stage_t)

    chart_keys = ("chart_style", "chart_state")
    if any(k in form for k in chart_keys):
        chart_style = (form.get("chart_style") or "").strip()
        chart_state = (form.get("chart_state") or "").strip()
        fields["jukyu_chart"] = _compose_chart_value(chart_style, chart_state)

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
        # issue #205: code_s を渡し、return_to=detail なら詳細ページへ戻す
        return _redirect_with_return_query(code_s=code_s)

    fields = _extract_memo_fields_from_form(request.form)

    try:
        ps.update_memo(code_s, fields)
    except KeyError:
        msg = f"{code_s} は portfolio_shelve に未登録です"
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 404
        flash(msg, "error")
        return _redirect_with_return_query(code_s=code_s)
    except (ValueError, TypeError) as e:
        if is_ajax:
            return jsonify({"ok": False, "error": str(e)}), 400
        flash(str(e), "error")
        return _redirect_with_return_query(code_s=code_s)

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
                    "jukyu_chart": (row.get("memo") or {}).get("jukyu_chart") or "",
                    "gyoutai_theme": (row.get("memo") or {}).get("gyoutai_theme") or "",
                    "gyoutai_themes": list((row.get("memo") or {}).get("gyoutai_themes") or []),
                    # issue #327: ページ2列 (売買戦略 / IN理由 / イナゴ元 / 売買メモ) の
                    # inline 編集後の表示同期に使う
                    "trade_idea": (row.get("memo") or {}).get("trade_idea") or "",
                    "watch_in_reason": (row.get("memo") or {}).get("watch_in_reason") or "",
                    "inago_origin": (row.get("memo") or {}).get("inago_origin") or "",
                    "takaichi_sensitivity": (row.get("memo") or {}).get("takaichi_sensitivity") or "",
                }
        return jsonify(body)
    _flash_stock_info(code_s, " のメモを保存しました")
    return _redirect_with_return_query(code_s=code_s)


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
    # issue #220: アクション日付 (YYYY-MM-DD)。未指定なら現在時刻。
    action_date = (request.form.get("action_date") or "").strip() or None
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
        ps.add_to_watch(normalized, reason=reason, action_date=action_date)
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


@portfolio_bp.route("/portfolio/status", methods=["POST"])
def set_status():
    """コード指定で保有/準保有/監視へ変更する。

    未登録コードは内部的に 3監 レコードを作ってから、選択された状態へ遷移する。
    これにより画面上の操作を「ユニバース追加」ではなく状態の指定に統一する。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected

    code_s = (request.form.get("code_s") or "").strip()
    new_status = (request.form.get("new_status") or "").strip()
    reason = (request.form.get("reason") or "").strip() or "WebApp ステータス変更"
    trade_idea = (request.form.get("trade_idea") or "").strip()
    qty_raw = (request.form.get("qty") or "").strip()
    if new_status not in ps.VALID_STATUSES:
        flash(f"不正なステータス: {new_status!r}", "error")
        return _redirect_with_return_query()
    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError) as e:
        flash(f"不正な銘柄コード: {e}", "error")
        return _redirect_with_return_query()
    normalized = ps.normalize_code_s(code_s)

    qty = None
    if new_status == "1保":
        try:
            qty = int(qty_raw)
        except ValueError:
            flash("保有に変更するには株数を整数で入力してください", "error")
            return _redirect_with_return_query()
        if qty < 0:
            flash("株数は 0 以上で入力してください", "error")
            return _redirect_with_return_query()

    record = ps.get_record(normalized)
    if record is None:
        if not get_stock_data(normalized):
            flash(f"{normalized} は stocks_shelve に未登録のコードです", "error")
            return _redirect_with_return_query()
        try:
            ps.add_to_watch(normalized, reason=reason)
        except ValueError as e:
            flash(str(e), "error")
            return _redirect_with_return_query()
        record = ps.get_record(normalized)
    elif record.get("excluded"):
        try:
            ps.add_to_watch(normalized, reason=reason)
        except ValueError as e:
            flash(str(e), "error")
            return _redirect_with_return_query()
        record = ps.get_record(normalized)

    old_status = (record or {}).get("status")
    try:
        if new_status == "1保" and old_status != "1保":
            if not trade_idea:
                flash("保有に変更するには売買戦略を選択してください", "error")
                return _redirect_with_return_query()
            ps.seed_trade_ideas()
            ps.update_memo(normalized, {"trade_idea": trade_idea})
        ps.transition_status(normalized, new_status, reason=reason, qty=qty)
        if new_status == "1保" and qty is not None:
            ps.update_qty(normalized, qty, reason=reason, log_action=old_status == "1保")
    except (KeyError, ValueError, TypeError) as e:
        flash(str(e), "error")
        return _redirect_with_return_query()

    _sync_txt_safely()
    label = STATUS_VALUE_TO_LABEL[new_status]
    _flash_stock_info(normalized, f" のステータスを {label} に変更しました")
    return _redirect_with_return_query(
        default_status_query=STATUS_VALUE_TO_QUERY[new_status], code_s=normalized)


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
    """フィルタ型ダッシュボード。

    URL クエリ:
      status:        hold / semi / watch のいずれか単一 (省略時=hold、空文字=全ステータス)
      gyoutai_theme: 業態/テーマ名の完全一致 (省略時=全業態テーマ)
                     指定時は status を無視して全銘柄から該当業態/テーマを抽出。
      sort:          position / rank / gyoutai / rating
                     (省略時=hold/all は position、semi/watch は rank)
    `page` パラメータは silent に無視 (旧 URL 互換)。
    """
    active_status = _parse_status_filter(request.args)
    active_gyoutai_theme = _parse_gyoutai_theme(request.args)
    active_sort = _parse_sort_key(request.args, active_status)

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
    rows = list_portfolio_with_indicators(filtered_records, sort_key=active_sort)

    # 行ごとに transitions を埋める。fallback 中は空。
    for row in rows:
        row["transitions"] = (
            [] if fallback_mode else _allowed_transitions_from(row.get("status", ""))
        )
        row["memo"] = _augment_stage_chart_meta(row.get("memo") or {})

    # 一括変更モードの可否: fallback でなければ表示行に対して有効
    delete_mode_allowed = (not fallback_mode and bool(rows))

    # POST → リダイレクトの戻り先として使う現状クエリ (status/gyoutai_theme)
    preserve_all_status = "status" in request.args and active_status is None
    return_query = _build_query_string(
        active_status,
        active_gyoutai_theme,
        active_sort,
        include_empty_status=preserve_all_status,
    )
    # テンプレートで select の selected 判定に使う、active_status の query 形式 (例: "hold")
    active_status_query = (
        STATUS_VALUE_TO_QUERY[active_status] if active_status in STATUS_VALUE_TO_QUERY else ""
    )

    # issue #282: テーママスターから候補を取得 (フィルタ select / 入力 select 兼用)
    if fallback_mode:
        theme_master: list = []
    else:
        theme_master = ps.list_themes()

    sort_urls = {
        k: "?" + _build_query_string(
            active_status,
            active_gyoutai_theme,
            k,
            include_empty_status=preserve_all_status,
        )
        for k in (
            "position", "rank", "gyoutai", "rating", "rs",
            "rs_change_1d", "rs_change_5d", "rs_change_20d",
        )
    }
    rs_change_sort_cycle = {
        "rs_change_1d": sort_urls["rs_change_5d"],
        "rs_change_5d": sort_urls["rs_change_20d"],
        "rs_change_20d": sort_urls["rs_change_1d"],
    }
    rs_change_sort_url = rs_change_sort_cycle.get(active_sort, sort_urls["rs_change_1d"])

    # issue #397 Phase3b: CSV取込フォーム展開時に前回の各ソース取込日を出すための一覧
    # (broker/kind -> as_of)。件数はごく少数 (最大4件) なので毎回取得しても軽量。
    position_sources = [] if fallback_mode else ps.list_position_sources()
    csv_import_sources = {
        f"{s['broker']}/{s['kind']}": s["as_of"] for s in position_sources
    }

    # 保有フィルタ表示時のみ、運用総額と保有株数の基準日を集計
    hold_summary = None
    if not fallback_mode and active_status == "1保":
        hold_rows = [r for r in rows if r.get("status") == "1保"]
        if hold_rows:
            total_position_value = sum(
                (r.get("position_value") or 0) for r in hold_rows
            )
            # 市場別内訳 (日経225/TOPIX/グロース/その他) を集計
            cat_values = {"日経225": 0.0, "TOPIX": 0.0, "グロース": 0.0, "その他": 0.0}
            for r in hold_rows:
                cat = r.get("market_category", "その他")
                cat_values[cat] = cat_values.get(cat, 0.0) + (r.get("position_value") or 0)
            breakdown = [
                {
                    "category": cat,
                    "man": int(round(val / 10000)),
                    "ratio": round(val / total_position_value * 100, 1)
                    if total_position_value else 0.0,
                }
                for cat, val in cat_values.items()
            ]
            hold_summary = {
                "total_man": int(round(total_position_value / 10000)),
                # 保有株数の真実源は証券会社CSVなので、手動更新時刻ではなく
                # 取込済み position_source の最新 as_of (残高基準日) を出す
                "qty_as_of": max(
                    (s["as_of"] for s in position_sources if s.get("as_of")),
                    default=None,
                ),
                "breakdown": breakdown,
            }

    # issue #363: 遷移モーダルの戦略 select に選択肢を出すため、未シード環境ではシード (冪等)
    ps.seed_trade_ideas()
    trade_idea_master = ps.list_trade_ideas()

    return render_template(
        "portfolio_list.html",
        status_choices=STATUS_CHOICES,
        active_status_query=active_status_query,
        active_gyoutai_theme=active_gyoutai_theme or "",
        active_sort=active_sort,
        default_sort=_default_sort_key_for_status(active_status),
        sort_urls=sort_urls,
        rs_change_sort_url=rs_change_sort_url,
        counts=counts,
        rows=rows,
        total=len(rows),
        return_query=return_query,
        delete_mode_allowed=delete_mode_allowed,
        status_label=STATUS_VALUE_TO_LABEL,
        fallback_mode=fallback_mode,
        theme_master=theme_master,
        gyoutai_themes_max_slots=ps.GYOUTAI_THEMES_MAX_SLOTS,
        trade_idea_options=[t["name"] for t in trade_idea_master],
        trade_idea_descriptions={t["name"]: t["description"] for t in trade_idea_master},
        stage_options=ps.STAGE_OPTIONS,
        stage_t_options=ps.STAGE_T_OPTIONS,
        chart_style_options=ps.CHART_STYLE_OPTIONS,
        chart_state_options=ps.CHART_STATE_OPTIONS,
        monthly_tag_descriptions=MONTHLY_TAG_DESCRIPTIONS,
        tag_descriptions=TAG_DESCRIPTIONS,
        hold_summary=hold_summary,
        csv_import_sources=csv_import_sources,
    )


@portfolio_bp.route("/portfolio/charts")
def charts():
    """チャート一覧確認モード (2x2 株探 iframe、issue #231)。

    /portfolio と同じ status / gyoutai_theme フィルタで銘柄を抽出し、
    業態順に並べた全件をテンプレートへ渡す。ページ送り・足切替は
    クライアント側 JS が担当する (サーバー往復なし)。並び順は issue 仕様で
    「portfolio 一覧と同じ順番固定」= 業態順なので sort は受け取らず gyoutai 固定。
    """
    active_status = _parse_status_filter(request.args)
    active_gyoutai_theme = _parse_gyoutai_theme(request.args)

    # issue #186: 未移行環境 (portfolio_shelve 空) では dashboard() と同じく
    # my_watch_list.txt 由来の仮レコードにフォールバックする (除外含む全件で判定)。
    all_records_inc = ps.list_records(include_excluded=True)
    if not all_records_inc:
        visible_records = _build_fallback_records()
    else:
        visible_records = [r for r in all_records_inc if not r.get("excluded", False)]

    # フィルタ抽出は dashboard() と同じ規則 (issue #215): gyoutai_theme 指定時は
    # status 無視で完全一致、未指定かつ status None なら全件、それ以外は status 一致。
    if active_gyoutai_theme:
        filtered_records = [
            r for r in visible_records
            if active_gyoutai_theme in ((r.get("memo") or {}).get("gyoutai_themes") or [])
        ]
    elif active_status is None:
        filtered_records = visible_records
    else:
        filtered_records = [r for r in visible_records if r.get("status") == active_status]
    rows = list_portfolio_with_indicators(filtered_records, sort_key="gyoutai")

    # iframe 表示に必要な最小キーのみ JSON 化して渡す。
    # stage / 更新日 / 需給チャートはチャートページ上での inline 編集の初期値。
    stocks = [
        {
            "code_s": r["code_s"],
            "stock_name": r.get("stock_name") or "",
            "stage": (r.get("memo") or {}).get("stage") or "",
            "last_research_update": (r.get("memo") or {}).get("last_research_update") or "",
            "last_research_update_style": (r.get("styles") or {}).get("last_research_update") or "",
            "jukyu_chart": (r.get("memo") or {}).get("jukyu_chart") or "",
            "stage_struct": _parse_stage_value((r.get("memo") or {}).get("stage") or ""),
            "jukyu_chart_struct": _parse_chart_value((r.get("memo") or {}).get("jukyu_chart") or ""),
            "tags": r.get("tags") or "",
        }
        for r in rows
    ]
    active_status_query = (
        STATUS_VALUE_TO_QUERY[active_status] if active_status in STATUS_VALUE_TO_QUERY else ""
    )
    return render_template(
        "portfolio_charts.html",
        stocks=stocks,
        total=len(stocks),
        active_status_query=active_status_query,
        active_gyoutai_theme=active_gyoutai_theme or "",
        stage_options=ps.STAGE_OPTIONS,
        stage_t_options=ps.STAGE_T_OPTIONS,
        chart_style_options=ps.CHART_STYLE_OPTIONS,
        chart_state_options=ps.CHART_STATE_OPTIONS,
        monthly_tag_descriptions=MONTHLY_TAG_DESCRIPTIONS,
    )


@portfolio_bp.route("/portfolio/shikiho")
def shikiho_page():
    """四季報コメント順次更新ページ (issue #313)。

    /portfolio/charts と同じ status / gyoutai_theme フィルタ・業態順で銘柄を抽出し、
    四季報フォーム1セットをクライアント側で銘柄切替する。四季報本体は切替時に
    都度 AJAX 取得 (GET /portfolio/shikiho/<code_s>/data) するため、ここでは
    銘柄リストと「今号入力済みか」フラグだけを渡す。research 未登録銘柄は
    保存 (save_shikiho) が失敗するため対象から除外する。
    """
    active_status = _parse_status_filter(request.args)
    active_gyoutai_theme = _parse_gyoutai_theme(request.args)

    # 銘柄抽出は charts() と同一規則 (issue #186 fallback / issue #215 フィルタ)。
    all_records_inc = ps.list_records(include_excluded=True)
    if not all_records_inc:
        visible_records = _build_fallback_records()
    else:
        visible_records = [r for r in all_records_inc if not r.get("excluded", False)]

    if active_gyoutai_theme:
        filtered_records = [
            r for r in visible_records
            if active_gyoutai_theme in ((r.get("memo") or {}).get("gyoutai_themes") or [])
        ]
    elif active_status is None:
        filtered_records = visible_records
    else:
        filtered_records = [r for r in visible_records if r.get("status") == active_status]
    rows = list_portfolio_with_indicators(filtered_records, sort_key="gyoutai")

    current_period = research_shelve.current_shikiho_period()
    # research 全件を1回の open で dict 化 (318銘柄ぶんの open/close を回避)。
    all_research = {r["code_s"]: r for r in research_shelve.list_research_records()}

    stocks = []
    done_count = 0
    for r in rows:
        code_s = r["code_s"]
        rec = all_research.get(code_s)
        if rec is None:
            continue  # research 未登録は save_shikiho が失敗するため対象外
        # list_research_records() は正規化を通さないため、旧形式 (str要素) 混在に備えて正規化。
        comments = research_shelve._normalize_shikiho_comments(rec.get("shikiho_comments") or [])
        has_current = any(
            (c.get("period") or "").strip() == current_period
            and (c.get("comment") or "").strip()
            for c in comments
        )
        if has_current:
            done_count += 1
        stocks.append({
            "code_s": code_s,
            "stock_name": r.get("stock_name") or "",
            "has_current": has_current,
        })

    active_status_query = (
        STATUS_VALUE_TO_QUERY[active_status] if active_status in STATUS_VALUE_TO_QUERY else ""
    )
    return render_template(
        "portfolio_shikiho.html",
        stocks=stocks,
        total=len(stocks),
        done_count=done_count,
        current_period=current_period,
        active_status_query=active_status_query,
        active_gyoutai_theme=active_gyoutai_theme or "",
    )


@portfolio_bp.route("/portfolio/shikiho/<code_s>/data")
def shikiho_data(code_s):
    """四季報順次更新ページの1銘柄ぶん軽量データ (issue #313)。

    overview + shikiho_comments (降順) を JSON で返す。get_research_record() は
    正規化済みを返すので _normalize は不要。get_research_detail() は post_price_changes
    バックフィル等が走るため使わない。shikiho_page が未登録を除外するため通常
    404 には到達しないが、防御的に残す。
    """
    rec = research_shelve.get_research_record(code_s)
    if rec is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    comments = research_shelve.sort_shikiho_comments_desc(rec.get("shikiho_comments") or [])
    return jsonify({
        "ok": True,
        "overview": rec.get("overview") or "",
        "shikiho_comments": [
            {"period": c.get("period") or "", "comment": c.get("comment") or ""}
            for c in comments
        ],
    })


# ===========================================
# 業態テーマ別 RS サマリー (issue #283)
# ===========================================

# 許可 sort_key は helper のマッピング (真実の源) から導出する
THEME_SUMMARY_SORT_KEYS = set(THEME_SUMMARY_SORT_FIELDS)
DEFAULT_THEME_SUMMARY_SORT = "momentum"


@portfolio_bp.route("/portfolio/themes/summary", methods=["GET"])
def theme_summary():
    """業態テーマ別 RS サマリー (閲覧専用、issue #283)。

    手動付けした業態テーマ (memo.gyoutai_themes) でユニバースをグルーピングし、
    momentum_pt (中長期) と rs_line 移動平均乖離オシレーター (短期の勢い) を集約表示する。

    URL クエリ:
      sort: momentum / dev_1d / dev_a / dev_b (省略時=momentum)
    """
    sort_key = (request.args.get("sort") or "").strip()
    if sort_key not in THEME_SUMMARY_SORT_KEYS:
        sort_key = DEFAULT_THEME_SUMMARY_SORT

    fallback_mode = _is_fallback_mode()
    # fallback (portfolio_shelve 空) では空テーブル + 案内文。
    if fallback_mode:
        themes: list = []
        total_members = 0
    else:
        themes = build_portfolio_theme_summary(sort_key=sort_key)
        total_members = sum(t["member_count"] for t in themes)

    sort_urls = {
        k: url_for("portfolio.theme_summary", sort=k)
        for k in THEME_SUMMARY_SORT_KEYS
    }
    return render_template(
        "portfolio_theme_summary.html",
        themes=themes,
        total_members=total_members,
        active_sort=sort_key,
        sort_urls=sort_urls,
        fallback_mode=fallback_mode,
    )


# ===========================================
# テーママスター編集 (issue #282)
# ===========================================

@portfolio_bp.route("/portfolio/themes", methods=["GET"])
def themes_index():
    """テーママスター一覧・編集画面を表示する。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    themes = ps.list_themes()
    usage = ps.count_theme_usage()
    return render_template(
        "portfolio_themes.html",
        themes=themes,
        usage=usage,
        theme_name_max_len=ps.THEME_NAME_MAX_LEN,
    )


@portfolio_bp.route("/portfolio/themes/create", methods=["POST"])
def themes_create():
    """テーマを新規作成して /portfolio/themes に戻る (PRG)。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    try:
        ps.create_theme(name, description)
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.themes_index"))
    flash(f"テーマ「{name}」を作成しました", "info")
    return redirect(url_for("portfolio.themes_index"))


@portfolio_bp.route("/portfolio/themes/<name>/update", methods=["POST"])
def themes_update(name: str):
    """テーマのリネーム / 説明文編集。

    フォームは name + description の両方を常に送る (portfolio_themes.html は
    edit 開始時に両 input を同時に表示する設計)。よって description フィールドが
    POST に含まれている場合は空文字でも意図的なクリアとして上書きする。
    """
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    new_name = (request.form.get("name") or "").strip()
    description = request.form.get("description")
    try:
        ps.update_theme(
            name,
            new_name=new_name if new_name and new_name != name else None,
            description=description if description is not None else None,
        )
    except KeyError:
        flash(f"テーマ「{name}」が見つかりません", "error")
        return redirect(url_for("portfolio.themes_index"))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.themes_index"))
    if new_name and new_name != name:
        flash(f"テーマを「{name}」→「{new_name}」にリネームしました", "info")
    else:
        flash(f"テーマ「{name}」を更新しました", "info")
    return redirect(url_for("portfolio.themes_index"))


@portfolio_bp.route("/portfolio/themes/<name>/delete", methods=["POST"])
def themes_delete(name: str):
    """テーマを削除し、全銘柄から除去する。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    try:
        affected = ps.delete_theme(name)
    except KeyError:
        flash(f"テーマ「{name}」が見つかりません", "error")
        return redirect(url_for("portfolio.themes_index"))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.themes_index"))
    flash(
        f"テーマ「{name}」を削除しました (影響: {affected} 銘柄)",
        "info",
    )
    return redirect(url_for("portfolio.themes_index"))


# ===========================================
# 戦略マスター編集 (issue #335)
# ===========================================

def _exit_rule_from_form():
    """戦略マスター画面の出口ルール入力を shelve 用 dict に正規化する。"""
    ma_kind = (request.form.get("ma_kind") or "").strip() or None
    ma_window_raw = (request.form.get("ma_window") or "").strip()
    stop_loss_raw = (request.form.get("stop_loss_pct") or "").strip()
    try:
        ma_window = int(ma_window_raw) if ma_window_raw else None
        stop_loss_pct = float(stop_loss_raw) if stop_loss_raw else None
    except ValueError as e:
        raise ValueError("MA期間と損切り率は数値で入力してください") from e
    return {
        "ma_kind": ma_kind,
        "ma_window": ma_window,
        "stop_loss_pct": stop_loss_pct,
        "allow_dca_lower": request.form.get("allow_dca_lower") == "true",
    }

@portfolio_bp.route("/portfolio/strategies", methods=["GET"])
def strategies_index():
    """戦略マスター一覧・編集画面を表示する。初回アクセス時にシードを自動投入する。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    ps.seed_trade_ideas()
    strategies = ps.list_trade_ideas()
    usage = ps.count_trade_idea_usage()
    return render_template(
        "portfolio_strategies.html",
        strategies=strategies,
        usage=usage,
        strategy_name_max_len=ps.THEME_NAME_MAX_LEN,
        valid_time_horizons=ps.VALID_TIME_HORIZONS,
    )


@portfolio_bp.route("/portfolio/strategies/create", methods=["POST"])
def strategies_create():
    """戦略を新規作成して /portfolio/strategies に戻る (PRG)。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    time_horizon = (request.form.get("time_horizon") or "").strip()
    try:
        # over_earnings は既存DBとの後方互換のため保持するが、運用に未接続のためUIでは編集しない。
        ps.create_trade_idea(
            name, description, time_horizon,
            exit_rule=_exit_rule_from_form(),
        )
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.strategies_index"))
    flash(f"戦略「{name}」を作成しました", "info")
    return redirect(url_for("portfolio.strategies_index"))


@portfolio_bp.route("/portfolio/strategies/<name>/update", methods=["POST"])
def strategies_update(name: str):
    """戦略のリネーム / 説明文 / 時間軸 / 出口ルールを更新する。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    new_name = (request.form.get("name") or "").strip()
    description = request.form.get("description")
    time_horizon = request.form.get("time_horizon")
    try:
        ps.update_trade_idea(
            name,
            new_name=new_name if new_name and new_name != name else None,
            description=description if description is not None else None,
            time_horizon=time_horizon if time_horizon is not None else None,
            # over_earnings は既存値を維持する（後方互換のみ。現行UIからは編集しない）。
            exit_rule=_exit_rule_from_form(),
        )
    except KeyError:
        flash(f"戦略「{name}」が見つかりません", "error")
        return redirect(url_for("portfolio.strategies_index"))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.strategies_index"))
    if new_name and new_name != name:
        flash(f"戦略を「{name}」→「{new_name}」にリネームしました", "info")
    else:
        flash(f"戦略「{name}」を更新しました", "info")
    return redirect(url_for("portfolio.strategies_index"))


@portfolio_bp.route("/portfolio/strategies/<name>/delete", methods=["POST"])
def strategies_delete(name: str):
    """戦略を削除し、全銘柄の trade_idea を空にリセットする。"""
    rejected = _reject_when_fallback()
    if rejected is not None:
        return rejected
    try:
        affected = ps.delete_trade_idea(name)
    except KeyError:
        flash(f"戦略「{name}」が見つかりません", "error")
        return redirect(url_for("portfolio.strategies_index"))
    except (ValueError, TypeError) as e:
        flash(str(e), "error")
        return redirect(url_for("portfolio.strategies_index"))
    flash(
        f"戦略「{name}」を削除しました (影響: {affected} 銘柄)",
        "info",
    )
    return redirect(url_for("portfolio.strategies_index"))
