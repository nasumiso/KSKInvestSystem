"""トップページの「今日のアクション」(issue #372)。

データの手入れが要る項目と、決算が近い銘柄を出す。各項目は1行に集約し、手入れを済ませると消える。
相場起点の検知のうち扱うのは決算接近だけ (シグナル点灯・順位急落などは扱わない)。
"""

from datetime import date, datetime
from typing import Dict, List, Optional

import portfolio_shelve as ps
import research_shelve
from ks_util import log_warning
from webapp.helpers import _bulk_get_stock_data, _parse_kessanbi
from webapp.routes.portfolio import STATUS_VALUE_TO_LABEL, STATUS_VALUE_TO_QUERY

# 証券会社ごとの「これより古ければ催促する」日数。ここに無い証券会社は催促しない。
# SBI はめったに取引しないので長め。
BROKER_STALE_DAYS = {"楽天": 7, "SBI": 30}

# 四季報コメントを必須とするステータス (優先順)。
SHIKIHO_STATUSES = ("1保", "2準")

# 決算予定日の何日前から出すか (暦日、当日を含む)。
# 保有・準保有は事前見通しを書く時間を取るため長め。監視は直前に知らせるだけ。
KESSAN_NEAR_DAYS = 7
KESSAN_NEAR_DAYS_WATCH = 3
# 保有の行に銘柄名を並べる上限。超えた分は「他N銘柄」にまとめる。
KESSAN_NAME_LIMIT = 3


def _item(text: str, url: str, link_label: str) -> Dict[str, str]:
    return {"text": text, "url": url, "link_label": link_label}


def _balance_csv_item(today: date) -> Optional[Dict[str, str]]:
    """残高CSVの基準日が古い証券会社を1行にまとめる。一度も取り込んでいない口座は催促しない。

    現物・信用の片方だけ取り込むと、もう片方は古い基準日のまま残る。
    古い側を見逃さないよう、証券会社ごとに最も古い基準日で判定する。
    """
    oldest: Dict[str, str] = {}
    for s in ps.list_position_sources():
        broker, as_of = s.get("broker"), s.get("as_of")
        if broker in BROKER_STALE_DAYS and as_of:
            oldest[broker] = min(oldest.get(broker, as_of), as_of)
    stale = []
    for broker, as_of in oldest.items():
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


def _md(day: date) -> str:
    return f"{day.month}/{day.day}"


def _has_outlook(record: Optional[Dict], day: date) -> bool:
    """その決算日の事前見通し (期待度か見通し文) が書かれているか。空白だけは未記入。"""
    kessanbi = day.strftime("%Y/%m/%d")
    return any(
        e.get("kessanbi") == kessanbi
        and ((e.get("pre_expectation") or "").strip() or (e.get("pre_outlook") or "").strip())
        for e in (record or {}).get("kessan_comments") or []
    )


def _kessan_items(today: date) -> List[Dict[str, str]]:
    """決算が近い銘柄を、保有・準保有・監視の各1行にまとめる。

    保有・準保有は事前見通しが未記入の銘柄だけを数え、書くと消える。
    監視は記入を求めず、決算日を過ぎるまで件数だけ出す。

    予定日は銘柄DBから直接読む。決算カレンダーの元データ (pf_kessan_shelve) は
    日次バッチの終わりまで前日のままなので、そこを経由すると出し遅れる。
    予定日が無い・過去日の銘柄は、株探に次回予定日がまだ載っていないので出さない。
    """
    records = ps.list_records()
    stocks = _bulk_get_stock_data([r["code_s"] for r in records])
    near: Dict[str, List] = {}  # status -> [(予定日, code_s, 銘柄名)]
    for r in records:
        stock = stocks.get(r["code_s"]) or {}
        day = _parse_kessanbi(stock.get("kessanbi", ""))
        limit = KESSAN_NEAR_DAYS_WATCH if r.get("status") == "3監" else KESSAN_NEAR_DAYS
        if day and 0 <= (day - today).days <= limit:
            near.setdefault(r.get("status"), []).append((day, r["code_s"], stock.get("stock_name", "")))
    if not near:
        return []

    research = {r["code_s"]: r for r in research_shelve.list_research_records()}

    def pending(status: str) -> List:
        return sorted(n for n in near.get(status, []) if not _has_outlook(research.get(n[1]), n[0]))

    items = []
    hold = pending("1保")
    if hold:
        names = "・".join(f"{name or code_s} ({_md(day)})" for day, code_s, name in hold[:KESSAN_NAME_LIMIT])
        if len(hold) > KESSAN_NAME_LIMIT:
            names += f" 他{len(hold) - KESSAN_NAME_LIMIT}銘柄"
        items.append(_item(f"決算が近い: 保有 {names}", "/disclosure", "決算・開示で見通しを書く"))
    semi = pending("2準")
    if semi:
        first, last = semi[0][0], semi[-1][0]
        span = _md(first) if first == last else f"{_md(first)}〜{_md(last)}"
        items.append(_item(
            f"決算が近い: 準保有 {len(semi)}銘柄 ({span})", "/disclosure", "決算・開示で見通しを書く"
        ))
    watch = near.get("3監")
    if watch:
        items.append(_item(
            f"監視の決算: {KESSAN_NEAR_DAYS_WATCH}日以内に {len(watch)}銘柄", "/disclosure", "決算・開示で見る"
        ))
    return items


# (項目名, 検知関数)。表示順もこの順。検知関数は1行 (dict) か複数行 (list) か None を返す。
_DETECTORS: List = [
    ("決算予定", _kessan_items),
    ("残高CSV", _balance_csv_item),
    ("約定CSV", _fill_csv_item),
    ("四季報", _shikiho_item),
]


def build_action_queue(today: Optional[date] = None) -> List[Dict[str, str]]:
    """トップページに出す項目を返す。各要素は {"text", "url", "link_label"}。

    1項目の検知が失敗しても他は出す。失敗した項目は「判定できませんでした」の行にする
    (黙って消すと「手入れ不要」と見分けがつかないため)。
    """
    today = today or date.today()
    items = []
    for name, detect in _DETECTORS:
        try:
            found = detect(today)
        except Exception as e:  # noqa: BLE001 - トップページを 500 にしない
            log_warning("action_queue: 判定に失敗", name, repr(e))
            found = _item(f"{name}の状態を判定できませんでした", "", "")
        if found:
            items.extend(found if isinstance(found, list) else [found])
    return items
