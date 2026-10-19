"""トップページの「今日のアクション」(issue #372)。

相場起点の項目 (出口ライン「防」・決算接近・ブレイク) と、データの手入れが要る項目
(ステージ・残高CSV・約定CSV・四季報) を出す。各項目は1行に集約し、期限が過ぎるか
手入れを済ませると消える。順位・RS の急落と業態テーマの悪化は、過去の値を残しておらず
新しく悪化したことを判定できないので扱わない。
"""

from datetime import date, datetime
from functools import cached_property
from typing import Any, Dict, List, Optional

import numpy as np

import portfolio_shelve as ps
import research_shelve
from ks_util import get_price_day, log_warning
from webapp.helpers import (
    _bulk_get_stock_data,
    _parse_kessanbi,
    _parse_research_update_md,
)
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

# 「防」の初点灯とブレイクの発生を何営業日出すか (発生日を含む。土日だけ除き、祝日は考慮しない)。
# トップを開かない日があっても見逃さず、点きっぱなしの銘柄で毎日埋まらない長さ。
SIGNAL_SHOW_BUSDAYS = 3
# 出すブレイクのシグナル種別 (make_stock_db.extract_signals の kind)。
BREAKOUT_KINDS = ("ブ", "週ブ")
# 決算・「防」のあと、何日までステージの更新を促すか (暦日)。
# 過ぎたら出さない (更新しないまま古くなった銘柄で点きっぱなしにしないため)。
STAGE_AFTER_EVENT_DAYS = 14


class _Material:
    """防・決算・ブレイク・ステージの行が共有する材料。初めて使うときに1回だけ読む。"""

    @cached_property
    def records(self) -> List[Dict[str, Any]]:
        return ps.list_records()

    @cached_property
    def stocks(self) -> Dict[str, Dict[str, Any]]:
        return _bulk_get_stock_data([r["code_s"] for r in self.records])

    @cached_property
    def exits(self) -> Dict[str, Dict[str, Any]]:
        return _recorded_exits(self.records, self.stocks)

    def name(self, code_s: str) -> str:
        return (self.stocks.get(code_s) or {}).get("stock_name") or code_s


def _recorded_exits(records: List[Dict[str, Any]],
                    stock_map: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """1保の銘柄ごとに、記録済みの「防」から今のシグナルと初点灯日を返す。

    {code_s: {"signal": dict|None, "first_defense": "YYYY-MM-DD"}}。判定と記録は日次バッチ
    (webapp.helpers.record_hold_exit_alerts) が行い、ここでは読むだけにする (トップを速く保つため)。
    最後の記録が銘柄の最新の株価日と同じなら、今も「防」とみなす。
    戦略を変えたり出口ルールを外したりした銘柄の古い記録は読まない (バッチは出口ルールの無い銘柄の
    記録を消さないため)。記録の cycle_id の末尾が今の「戦略|出口ルール」と一致するものだけ使う。
    """
    rules = {i.get("name"): i.get("exit_rule") for i in ps.list_trade_ideas() if i.get("name")}
    suffixes = {}  # code_s -> 今の「|戦略|ルールID」
    for r in records:
        strategy = (r.get("memo") or {}).get("trade_idea")
        if r.get("status") == "1保" and isinstance(rules.get(strategy), dict):
            suffixes[r["code_s"]] = f"|{strategy}|{ps.exit_rule_id(rules[strategy])}"
    result = {}
    for code_s, state in ps.list_exit_alert_states().items():
        if (code_s not in suffixes or not isinstance(state, dict)
                or not str(state.get("cycle_id", "")).endswith(suffixes[code_s])):
            continue
        events = sorted((e for e in state.get("events") or [] if isinstance(e, dict) and e.get("date")),
                        key=lambda e: str(e["date"]))
        if not events:
            continue
        price_log = (stock_map.get(code_s) or {}).get("price_log") or []
        latest = str(price_log[0][0]) if price_log else None
        result[code_s] = {
            "signal": events[-1] if str(events[-1]["date"]) == latest else None,
            "first_defense": str(events[0]["date"]),
        }
    return result


def _real_today() -> date:
    """実際の今日 (テストで差し替えるため関数にしている)。"""
    return date.today()


def _busdays_since(start: date, today: date) -> int:
    """start から today までの営業日数 (start 当日は 0)。"""
    return int(np.busday_count(start, today))


def _names(entries: List[str]) -> str:
    """銘柄の表示を並べる。上限を超えた分は「他N銘柄」にまとめる。"""
    text = "・".join(entries[:KESSAN_NAME_LIMIT])
    if len(entries) > KESSAN_NAME_LIMIT:
        text += f" 他{len(entries) - KESSAN_NAME_LIMIT}銘柄"
    return text


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


def _exit_items(today: date, mat: Optional[_Material] = None) -> List[Dict[str, str]]:
    """保有で出口ライン「防」が今の保有サイクルで初めて点いてから3営業日の銘柄を、1銘柄1行で出す。

    点きっぱなしの銘柄 (持ち続けると決めたもの) で毎日埋まらないよう、初点灯からの日数で切る。
    今日も「防」のままの銘柄だけを出す (割れから戻った銘柄は急ぎの判断が要らない)。
    """
    mat = mat or _Material()
    rows = []
    for code_s, ev in mat.exits.items():
        signal, first = ev["signal"], ev["first_defense"]
        if not (signal and signal["level"] == "防" and first):
            continue
        first_day = date.fromisoformat(first)
        if 0 <= _busdays_since(first_day, today) < SIGNAL_SHOW_BUSDAYS:
            rows.append((first_day, code_s, "・".join(signal.get("reasons") or [])))
    return [
        _item(f"出口ライン: {code_s} {mat.name(code_s)} {reasons} ({_md(day)}〜)",
              "/portfolio?status=hold", "保有銘柄で手仕舞いを決める")
        for day, code_s, reasons in sorted(rows)
    ]


def _breakout_items(today: date, mat: Optional[_Material] = None) -> List[Dict[str, str]]:
    """ブ・週ブが出てから3営業日の銘柄を、保有・準保有は銘柄名で、監視は件数で各1行にまとめる。"""
    from make_stock_db import extract_signals  # 遅延 import (webapp 起動時に読まない)

    mat = mat or _Material()
    found: Dict[str, Dict[str, Dict[str, Any]]] = {}  # status -> code_s -> {kinds, day}
    for r in mat.records:
        status = r.get("status")
        if status not in ("1保", "2準", "3監"):
            continue
        for sig in extract_signals(mat.stocks.get(r["code_s"]) or {}):
            day = sig.get("sig_date")
            if sig["kind"] not in BREAKOUT_KINDS or not isinstance(day, date):
                continue
            if not 0 <= _busdays_since(day, today) < SIGNAL_SHOW_BUSDAYS:
                continue
            hit = found.setdefault(status, {}).setdefault(r["code_s"], {"kinds": set(), "day": day})
            hit["kinds"].add(sig["kind"])
            hit["day"] = max(hit["day"], day)

    def kinds_label(kinds: set) -> str:
        return "・".join(k for k in BREAKOUT_KINDS if k in kinds)

    items = []
    for status, action in (("1保", "増し玉を検討"), ("2準", "入り直しを検討")):
        hits = found.get(status)
        if not hits:
            continue
        # 発生日の新しい順 (同日はコード順)
        order = sorted(hits, key=lambda c: (-hits[c]["day"].toordinal(), c))
        names = _names([f"{mat.name(c)} ({kinds_label(hits[c]['kinds'])})" for c in order])
        items.append(_item(
            f"ブレイク点灯: {STATUS_VALUE_TO_LABEL[status]} {names}",
            _charts_url(STATUS_VALUE_TO_QUERY[status], order), f"チャート一覧で{action}",
        ))
    watch = found.get("3監")
    if watch:
        counts = "・".join(
            f"{k}{n}" for k in BREAKOUT_KINDS
            if (n := sum(1 for h in watch.values() if k in h["kinds"]))
        )
        order = sorted(watch, key=lambda c: (-watch[c]["day"].toordinal(), c))
        items.append(_item(
            f"監視のブレイク: {len(watch)}銘柄 ({counts})", _charts_url("watch", order), "チャート一覧で見る"
        ))
    return items


def _charts_url(status_query: str, codes: List[str]) -> str:
    """行に出ている銘柄だけを、その順でチャート一覧に出すリンク。status はチャート一覧の戻り先"""
    return f"/portfolio/charts?status={status_query}&codes={','.join(codes)}"


def _stage_item(today: date, mat: Optional[_Material] = None) -> Optional[Dict[str, str]]:
    """決算・「防」のあと14日以内で、ステージをまだ更新していない保有・準保有の数を1行にまとめる。

    チャートが変わりやすい出来事の後だけ促す (更新日の古さだけで数えると、ほぼ全銘柄が常に該当する)。
    チャート一覧でステージを保存すると更新日が今日になり、消える。
    両方に当たる銘柄は「防の後」として1回だけ数える。
    """
    mat = mat or _Material()
    counts: Dict[str, Dict[str, int]] = {"防の後": {}, "決算後": {}}
    hits: Dict[str, date] = {}  # code_s -> 出来事の日 (チャート一覧に新しい順で渡す)
    for r in mat.records:
        status = r.get("status")
        if status not in ("1保", "2準"):
            continue
        code_s = r["code_s"]
        # 更新日はブラウザが保存した実際の日付。株価の基準日 (17時前は前日) で年を補うと、
        # 当日の保存を去年と読んでしまう
        updated = _parse_research_update_md((r.get("memo") or {}).get("last_research_update"), _real_today())
        events = []
        first = (mat.exits.get(code_s) or {}).get("first_defense") if status == "1保" else None
        if first:
            events.append(("防の後", date.fromisoformat(first)))
        kessan_day = _parse_kessanbi((mat.stocks.get(code_s) or {}).get("kessan_jisseki_date", ""))
        if kessan_day:
            events.append(("決算後", kessan_day))
        for label, day in events:
            if 0 <= (today - day).days <= STAGE_AFTER_EVENT_DAYS and not (updated and updated >= day):
                counts[label][status] = counts[label].get(status, 0) + 1
                hits[code_s] = day
                break
    parts = [
        f"{label} " + "・".join(f"{STATUS_VALUE_TO_LABEL[st]} {n}" for st, n in sorted(c.items()))
        for label, c in (("決算後", counts["決算後"]), ("防の後", counts["防の後"])) if c
    ]
    if not parts:
        return None
    has_hold = any("1保" in c for c in counts.values())
    order = sorted(hits, key=lambda c: (-hits[c].toordinal(), c))
    return _item(
        "ステージ未更新: " + "、".join(parts),
        _charts_url("hold" if has_hold else "semi", order), "チャート一覧で更新",
    )


def _kessan_items(today: date, mat: Optional[_Material] = None) -> List[Dict[str, str]]:
    """決算が近い銘柄を、保有・準保有・監視の各1行にまとめる。

    保有・準保有は事前見通しが未記入の銘柄だけを数え、書くと消える。
    監視は記入を求めず、決算日を過ぎるまで件数だけ出す。

    予定日は銘柄DBから直接読む。決算カレンダーの元データ (pf_kessan_shelve) は
    日次バッチの終わりまで前日のままなので、そこを経由すると出し遅れる。
    予定日が無い・過去日の銘柄は、株探に次回予定日がまだ載っていないので出さない。
    """
    mat = mat or _Material()
    records, stocks = mat.records, mat.stocks
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
        names = _names([f"{name or code_s} ({_md(day)})" for day, code_s, name in hold])
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


# (項目名, 検知関数, 共有の材料を使うか)。表示順もこの順。
# 検知関数は1行 (dict) か複数行 (list) か None を返す。
_DETECTORS: List = [
    ("出口ライン", _exit_items, True),
    ("決算予定", _kessan_items, True),
    ("ブレイク", _breakout_items, True),
    ("ステージ", _stage_item, True),
    ("残高CSV", _balance_csv_item, False),
    ("約定CSV", _fill_csv_item, False),
    ("四季報", _shikiho_item, False),
]


def build_action_queue(today: Optional[date] = None) -> List[Dict[str, str]]:
    """トップページに出す項目を返す。各要素は {"text", "url", "link_label"}。

    1項目の検知が失敗しても他は出す。失敗した項目は「判定できませんでした」の行にする
    (黙って消すと「手入れ不要」と見分けがつかないため)。

    決算の行のリンク先 (決算・開示ページの get_market_kessan_data) が
    get_price_day (17時境界) で基準日を決めているため、ここも揃える。
    単純な date.today() だと17時前はカレンダー側と1日ずれる。
    """
    today = today or get_price_day(datetime.today())
    mat = _Material()
    items = []
    for name, detect, uses_material in _DETECTORS:
        try:
            found = detect(today, mat) if uses_material else detect(today)
        except Exception as e:  # noqa: BLE001 - トップページを 500 にしない
            log_warning("action_queue: 判定に失敗", name, repr(e))
            found = _item(f"{name}の状態を判定できませんでした", "", "")
        if found:
            items.extend(found if isinstance(found, list) else [found])
    return items
