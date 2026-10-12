"""トップページの「今日のアクション」(issue #372)。

データの手入れが要る項目だけを出す。各項目は1行に集約し、手入れを済ませると消える。
相場起点の検知 (シグナル点灯・決算接近など) は扱わない。
"""

from datetime import date, datetime
from typing import Dict, List, Optional

import portfolio_shelve as ps
import research_shelve
from ks_util import log_warning
from webapp.routes.portfolio import STATUS_VALUE_TO_LABEL, STATUS_VALUE_TO_QUERY

# 証券会社ごとの「これより古ければ催促する」日数。ここに無い証券会社は催促しない。
# SBI はめったに取引しないので長め。
BROKER_STALE_DAYS = {"楽天": 7, "SBI": 30}

# 四季報コメントを必須とするステータス (優先順)。
SHIKIHO_STATUSES = ("1保", "2準")


def _item(text: str, url: str, link_label: str) -> Dict[str, str]:
    return {"text": text, "url": url, "link_label": link_label}


def _balance_csv_item(today: date) -> Optional[Dict[str, str]]:
    """残高CSVの基準日が古い証券会社を1行にまとめる。一度も取り込んでいない口座は催促しない。"""
    latest: Dict[str, str] = {}
    for s in ps.list_position_sources():
        broker, as_of = s.get("broker"), s.get("as_of")
        if broker in BROKER_STALE_DAYS and as_of:
            latest[broker] = max(latest.get(broker, ""), as_of)
    stale = []
    for broker, as_of in latest.items():
        as_of_date = date.fromisoformat(as_of)
        days = (today - as_of_date).days
        if days > BROKER_STALE_DAYS[broker]:
            stale.append(f"{broker} ({as_of_date.month}/{as_of_date.day} 時点、{days}日前)")
    if not stale:
        return None
    return _item("残高CSVが古い: " + "・".join(stale), "/portfolio", "保有銘柄で取り込む")


def _fill_csv_item(today: date) -> Optional[Dict[str, str]]:
    """約定CSVの取込を実行してから日が経った証券会社を1行にまとめる。"""
    stamps = ps.get_fill_import_at()
    stale = []
    for broker, limit in BROKER_STALE_DAYS.items():
        stamp = stamps.get(broker)
        if not stamp:
            stale.append(f"{broker} (取込記録なし)")
            continue
        days = (today - datetime.fromisoformat(stamp).date()).days
        if days > limit:
            stale.append(f"{broker} ({days}日前)")
    if not stale:
        return None
    return _item("約定CSVが未取込: " + "・".join(stale), "/trade-history", "売買履歴で取り込む")


def _shikiho_item(today: date) -> Optional[Dict[str, str]]:
    """保有・準保有のうち、今号の四季報コメントが未入力の銘柄数を1行にまとめる。"""
    period = research_shelve.current_shikiho_period()
    research = {r["code_s"]: r for r in research_shelve.list_research_records()}
    missing = []  # [(status, 件数)]
    for status in SHIKIHO_STATUSES:
        n = sum(
            1 for r in ps.list_records(status)
            # research 未登録は入力ページの対象外なので数えない
            if r["code_s"] in research
            and not research_shelve.has_shikiho_comment(research[r["code_s"]], period)
        )
        if n:
            missing.append((status, n))
    if not missing:
        return None
    counts = "・".join(f"{STATUS_VALUE_TO_LABEL[s]} {n}銘柄" for s, n in missing)
    # 入力ページの絞り込みは単一選択なので、優先度の高いステータスへ飛ばす
    url = f"/portfolio/shikiho?status={STATUS_VALUE_TO_QUERY[missing[0][0]]}"
    return _item(f"四季報 {period} が未入力: {counts}", url, "入力する")


# (項目名, 検知関数)。表示順もこの順。
_DETECTORS: List = [
    ("残高CSV", _balance_csv_item),
    ("約定CSV", _fill_csv_item),
    ("四季報", _shikiho_item),
]


def build_action_queue(today: Optional[date] = None) -> List[Dict[str, str]]:
    """トップページに出す手入れ項目を返す。各要素は {"text", "url", "link_label"}。

    1項目の検知が失敗しても他は出す。失敗した項目は「判定できませんでした」の行にする
    (黙って消すと「手入れ不要」と見分けがつかないため)。
    """
    today = today or date.today()
    items = []
    for name, detect in _DETECTORS:
        try:
            item = detect(today)
        except Exception as e:  # noqa: BLE001 - トップページを 500 にしない
            log_warning("action_queue: 判定に失敗", name, repr(e))
            item = _item(f"{name}の状態を判定できませんでした", "", "")
        if item:
            items.append(item)
    return items
