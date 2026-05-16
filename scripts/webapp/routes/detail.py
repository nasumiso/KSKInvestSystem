"""
銘柄詳細ビュールート。

GET /stock/<code_s> : 銘柄の全セクション表示
"""

from flask import Blueprint, render_template, abort

import portfolio
import portfolio_shelve as ps
from webapp.helpers import (
    get_research_detail,
    get_stock_data,
    get_disclosures,
    has_recent_disclosure,
    collect_gyoutai_theme_choices,
)
from webapp.routes.portfolio import (
    STATUS_VALUE_TO_LABEL,
    STATUS_VALUE_TO_QUERY,
    _allowed_transitions_from,
)
from research_shelve import VALID_RATINGS

detail_bp = Blueprint("detail", __name__)


@detail_bp.route("/stock/<code_s>")
def stock_detail(code_s: str):
    """銘柄詳細ビュー。

    ResearchDB に未登録だが stocks_shelve に存在する銘柄は、
    追加プロンプトを表示してワンクリックで追加できるようにする。
    """
    record = get_research_detail(code_s)
    stock = get_stock_data(code_s)
    if record is None:
        if stock:
            return render_template(
                "detail_add_prompt.html",
                code_s=code_s,
                stock_name=stock.get("stock_name", ""),
                overview=stock.get("overview", ""),
            )
        abort(404)

    disclosures = get_disclosures(code_s)
    disclosures_has_recent = has_recent_disclosure(disclosures, days=7)

    snapshots = record.get("snapshots") or []
    indicator_snaps = [
        s for s in snapshots
        if s.get("quality_indicators") or s.get("rironkabuka_kairi")
    ]

    portfolio_record = ps.get_record(code_s)
    # issue #195 (codex P2): excluded=True は未登録扱い。
    # 復活は /portfolio/add の add_to_watch() 復活パスに任せる (transition では excluded フラグが下がらず
    # 「変更したつもりがユニバースから除外されたまま」になる事故を防ぐ)
    if portfolio_record and portfolio_record.get("excluded"):
        portfolio_record = None
    portfolio_status = portfolio_record.get("status") if portfolio_record else None
    # shelve 未移行環境のフォールバック: shelve が空のとき my_watch_list.txt 経由の所属を見る
    # (portfolio.parse_my_portforio は shelve 空時に txt フォールバックする)
    # issue #186: 全レコードが excluded=True の状態を fallback と誤判定しないよう
    # include_excluded=True で取得する (portfolio.py の _is_fallback_mode と同じ判定)
    portfolio_fallback_mode = not ps.list_records(include_excluded=True)
    if portfolio_status is None and portfolio_fallback_mode:
        try:
            watch, possess = portfolio.parse_my_portforio()
        except Exception:  # noqa: BLE001
            watch, possess = ([], [])
        if code_s in possess:
            portfolio_status = "1保"
        elif code_s in watch:
            portfolio_status = "3監"
    portfolio_status_label = STATUS_VALUE_TO_LABEL.get(portfolio_status) if portfolio_status else None
    portfolio_status_query = STATUS_VALUE_TO_QUERY.get(portfolio_status) if portfolio_status else None
    # issue #195: モーダル内 select 用の遷移先 [(label, value), ...]。未登録は空リスト。
    portfolio_transitions = (
        _allowed_transitions_from(portfolio_status) if portfolio_status else []
    )

    # issue #205: 業態・テーマ inline 編集用の context
    # memo は None や非 dict (旧データ等) を許容する仕様 (_normalize_loaded_memo)
    # なので、dict 以外は空 dict として扱う
    _memo = (portfolio_record or {}).get("memo")
    if not isinstance(_memo, dict):
        _memo = {}
    gyoutai_themes = _memo.get("gyoutai_themes") or []
    gyoutai_theme_choices = (
        collect_gyoutai_theme_choices(ps.list_records(include_excluded=True))
        if not portfolio_fallback_mode else []
    )

    # issue #227: 株価 + RSライン 統合チャート (フル版)
    # market_db のロード失敗時は RS 無しの株価のみで描画 (500 にしない)
    try:
        from make_market_db import get_market_db  # 遅延 import
        from webapp.helpers import build_stock_chart_payload
        _market_db = get_market_db()
        price_rs_chart = build_stock_chart_payload(stock, _market_db, mode="full")
    except Exception:  # noqa: BLE001
        price_rs_chart = {"svg": "", "tooltip": "", "blue_dot": False}

    return render_template(
        "detail.html",
        record=record,
        stock=stock,
        price_rs_chart=price_rs_chart,
        disclosures=disclosures,
        disclosures_has_recent=disclosures_has_recent,
        indicator_snaps=indicator_snaps,
        valid_ratings=sorted(VALID_RATINGS - {""}),
        portfolio_status_label=portfolio_status_label,
        portfolio_status_query=portfolio_status_query,
        portfolio_fallback_mode=portfolio_fallback_mode,
        portfolio_transitions=portfolio_transitions,
        gyoutai_themes=gyoutai_themes,
        gyoutai_theme_choices=gyoutai_theme_choices,
        gyoutai_themes_max_slots=ps.GYOUTAI_THEMES_MAX_SLOTS,
    )
