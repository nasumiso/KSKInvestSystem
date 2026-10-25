"""webapp/action_queue.py のテスト (issue #372)"""

from datetime import date

import pytest

import portfolio_shelve as ps
import research_shelve as rs
from webapp import action_queue as aq

TODAY = date(2026, 10, 12)


@pytest.mark.parametrize("broker, days_ago, fires", [
    ("楽天", 7, False),   # 閾値ちょうどは出さない
    ("楽天", 8, True),
    ("SBI", 30, False),   # SBI は取引が少ないので長め
    ("SBI", 31, True),
    ("マネックス", 400, False),  # 閾値を持たない証券会社は催促しない
])
def test_csv_items_fire_only_past_broker_threshold(monkeypatch, broker, days_ago, fires):
    """残高CSV・約定CSV とも、証券会社ごとの日数を超えたときだけ出る"""
    day = date.fromordinal(TODAY.toordinal() - days_ago).isoformat()
    fresh = TODAY.isoformat()
    monkeypatch.setattr(ps, "list_position_sources", lambda: [
        {"broker": broker, "kind": "現物", "as_of": fresh},  # 片方だけ新しくても、古い方で判定
        {"broker": broker, "kind": "信用", "as_of": day},
    ])
    # 対象の証券会社以外は取込済みにして、記録なしの催促と混ざらないようにする
    stamps = {"楽天": f"{fresh}T09:00:00+09:00", "SBI": f"{fresh}T09:00:00+09:00"}
    stamps[broker] = f"{day}T22:00:00+09:00"
    monkeypatch.setattr(ps, "get_fill_import_at", lambda: stamps)

    balance, fill = aq._balance_csv_item(TODAY), aq._fill_csv_item(TODAY)

    assert (balance is not None) == fires
    assert (fill is not None) == fires
    if fires:
        assert broker in balance["text"] and f"{days_ago}日前" in balance["text"]
        assert broker in fill["text"] and f"{days_ago}日前" in fill["text"]


def test_fill_csv_item_clears_after_import_is_recorded(tmp_path, monkeypatch):
    """反映直後は取込記録が無く催促する。各社1回取り込めば消え、他社の記録は消さない"""
    db_path = str(tmp_path / "test_portfolio_shelve")
    monkeypatch.setattr("db_shelve.PORTFOLIO_SHELVE", db_path)
    monkeypatch.setattr("portfolio_shelve.PORTFOLIO_SHELVE", db_path)

    assert "楽天 (取込記録なし)・SBI (取込記録なし)" in aq._fill_csv_item(date.today())["text"]
    ps.record_fill_import("楽天")
    assert "楽天" not in aq._fill_csv_item(date.today())["text"]
    ps.record_fill_import("SBI")
    assert aq._fill_csv_item(date.today()) is None


@pytest.mark.parametrize("entries, real_today, expected", [
    ([], date(2026, 10, 23), "感情スコア: 未入力"),
    ([{"date": "2026-10-16", "score": -1}], date(2026, 10, 23), None),  # 入力日から5営業日までは有効
    ([{"date": "2026-10-16", "score": -1}], date(2026, 10, 26), "10/16 の入力から5営業日を過ぎた"),
])
def test_emotion_item_after_expiry(monkeypatch, entries, real_today, expected):
    """感情スコアが未入力か期限切れなら、中立で計算していることを出して入力を促す (issue #480)"""
    monkeypatch.setattr(ps, "list_emotion_scores", lambda **k: entries)
    monkeypatch.setattr(aq, "_real_today", lambda: real_today)
    item = aq._emotion_item(TODAY)
    assert (item is None) if expected is None else (expected in item["text"])


def test_shikiho_item_counts_missing_in_hold_and_semi(monkeypatch):
    """今号が未入力の 1保・2準 を数える。research 未登録は数えず、行き先は優先度の高い側"""
    monkeypatch.setattr(rs, "current_shikiho_period", lambda: "26.9")
    monkeypatch.setattr(rs, "list_research_records", lambda: [
        {"code_s": "1001", "shikiho_comments": [{"period": "26.9", "comment": "最高益"}]},
        {"code_s": "1002", "shikiho_comments": [{"period": "26.6", "comment": "前号のみ"}]},
        {"code_s": "2001", "shikiho_comments": []},
        {"code_s": "2002", "shikiho_comments": [{"period": "26.9", "comment": "  "}]},
    ])
    records = {
        "1保": [{"code_s": "1001"}, {"code_s": "1002"}, {"code_s": "1999"}],  # 1999 は research 未登録
        "2準": [{"code_s": "2001"}, {"code_s": "2002"}],
    }
    monkeypatch.setattr(ps, "list_records", lambda status: records[status])

    item = aq._shikiho_item(TODAY)

    assert item["text"] == "四季報 26.9 が未入力: 保有 1銘柄・準保有 2銘柄"
    assert item["url"] == "/portfolio/shikiho?status=hold"


def test_build_action_queue_keeps_other_items_when_one_detector_fails(monkeypatch):
    """1項目の失敗でトップページを落とさず、失敗した項目は判定不能と分かる行にする"""
    def boom(today):
        raise RuntimeError("db broken")

    monkeypatch.setattr(aq, "_DETECTORS", [
        ("残高CSV", boom, False),
        ("約定CSV", lambda today: None, False),
        ("四季報", lambda today: {"text": "四季報 26.9 が未入力: 保有 1銘柄", "url": "/x", "link_label": "入力する"}, False),
    ])

    items = aq.build_action_queue(TODAY)

    assert [i["text"] for i in items] == [
        "残高CSVの状態を判定できませんでした",
        "四季報 26.9 が未入力: 保有 1銘柄",
    ]


def _stub_kessan(monkeypatch, by_status, research=()):
    """by_status: {ステータス: {code_s: 決算予定日 (YYYY/MM/DD または "")}}"""
    stocks = {c: {"stock_name": f"銘柄{c}", "kessanbi": k} for d in by_status.values() for c, k in d.items()}
    monkeypatch.setattr(ps, "list_records", lambda: [
        {"code_s": c, "status": status} for status, d in by_status.items() for c in d
    ])
    monkeypatch.setattr(aq, "_bulk_get_stock_data", lambda codes: {c: stocks[c] for c in codes})
    monkeypatch.setattr(rs, "list_research_records", lambda: list(research))


@pytest.mark.parametrize("kessanbi, hold_fires, watch_fires", [
    ("2026/10/12", True, True),    # 当日は出す (引け後の発表が多い)
    ("2026/10/15", True, True),    # 監視は3日以内
    ("2026/10/16", True, False),
    ("2026/10/19", True, False),   # 保有・準保有は7日以内
    ("2026/10/20", False, False),
    ("2026/10/11", False, False),  # 過去日は次回予定日が未掲載の状態
    ("", False, False),
])
def test_kessan_items_fire_within_days(monkeypatch, kessanbi, hold_fires, watch_fires):
    """決算予定日が保有・準保有は7日以内、監視は3日以内のときだけ出る"""
    _stub_kessan(monkeypatch, {"1保": {"1001": kessanbi}, "2準": {"2001": kessanbi}, "3監": {"3001": kessanbi}})

    texts = [i["text"] for i in aq._kessan_items(TODAY)]

    md = "/".join(str(int(x)) for x in kessanbi.split("/")[1:])
    expected = [f"決算が近い: 保有 銘柄1001 ({md})", f"決算が近い: 準保有 1銘柄 ({md})"] if hold_fires else []
    if watch_fires:
        expected.append("監視の決算: 3日以内に 1銘柄")
    assert texts == expected


def test_kessan_items_drop_stocks_with_outlook(monkeypatch):
    """事前見通し (期待度か見通し文) を書いた銘柄は消える。空白だけ・別の決算日の記入は未記入扱い"""
    day = "2026/10/15"
    _stub_kessan(
        monkeypatch,
        {
            "1保": {c: day for c in ("1001", "1002", "1003", "1004", "1005", "1006")},
            "2準": {"2001": "2026/10/14", "2002": "2026/10/16", "2003": day},
            "3監": {"3001": day},
        },
        research=[
            {"code_s": "1001", "kessan_comments": [{"kessanbi": day, "pre_outlook": "増益なら継続"}]},
            {"code_s": "1002", "kessan_comments": [{"kessanbi": day, "pre_expectation": "○"}]},
            {"code_s": "1003", "kessan_comments": [{"kessanbi": day, "pre_outlook": "  ", "post_comment": "決算後"}]},
            {"code_s": "1004", "kessan_comments": [{"kessanbi": "2026/07/15", "pre_outlook": "前回分"}]},
            {"code_s": "2003", "kessan_comments": [{"kessanbi": day, "pre_outlook": "様子見"}]},
            {"code_s": "3001", "kessan_comments": [{"kessanbi": day, "pre_outlook": "監視は記入を問わない"}]},
        ],
    )

    items = aq._kessan_items(TODAY)
    assert [i["text"] for i in items] == [
        "決算が近い: 保有 銘柄1003 (10/15)・銘柄1004 (10/15)・銘柄1005 (10/15) 他1銘柄",
        "決算が近い: 準保有 2銘柄 (10/14〜10/16)",
        "監視の決算: 3日以内に 1銘柄",
    ]
    # 「N銘柄」「他N銘柄」にまとめた銘柄は、ポップアップに「コード 銘柄名」で出す
    assert [(i["popup_label"], i["popup"]) for i in items] == [
        ("他1銘柄", "1006 銘柄1006"), ("2銘柄", "2001 銘柄2001\n2002 銘柄2002"), ("1銘柄", "3001 銘柄3001"),
    ]


def _stub_material(monkeypatch, records, stocks=None, exits=None):
    """records: [{code_s, status, memo?}]、stocks: {code_s: stock}、exits: {code_s: {signal, first_defense}}"""
    stocks = stocks or {}
    monkeypatch.setattr(ps, "list_records", lambda: records)
    monkeypatch.setattr(aq, "_bulk_get_stock_data",
                        lambda codes: {c: {"stock_name": f"銘柄{c}", **stocks.get(c, {})} for c in codes})
    monkeypatch.setattr(aq, "_recorded_exits", lambda recs, stock_map: exits or {})
    monkeypatch.setattr(aq, "_real_today", lambda: TODAY)


@pytest.mark.parametrize("first, level, shown", [
    ("2026-10-12", "防", "row"),     # 初点灯の当日 (TODAY は月曜)
    ("2026-10-09", "防", "row"),     # 金曜点灯 → 月曜は1営業日後
    ("2026-10-08", "防", "row"),     # 2営業日後まで1銘柄1行で出す
    ("2026-10-07", "防", "stale"),   # 3営業日後からは件数の1行にまとめて出し続ける
    ("2026-10-12", "防予", None),    # 予兆は出さない
])
def test_exit_items_show_first_defense_for_three_busdays(monkeypatch, first, level, shown):
    """出口ライン「防」は、初めて点いてから3営業日は1銘柄1行、その後も防のままなら件数の1行に入る"""
    _stub_material(monkeypatch, [{"code_s": "1001", "status": "1保", "memo": {"trade_idea": "中期テーマ"}}], exits={
        "1001": {"signal": {"level": level, "reasons": ["損切りライン割れ", "日足50MA割れ確定"],
                              "stop_loss_line": 1234.6, "ma_value": 1300.2},
                 "first_defense": first},
    })

    items = aq._exit_items(TODAY)

    md = f"{int(first[5:7])}/{int(first[8:])}"
    reasons = f"損切りライン割れ 1,235円・日足50MA割れ確定 1,300円 ({md}〜)"
    if shown == "row":
        assert [i["text"] for i in items] == [f"出口ライン: 1001 銘柄1001 {reasons} 戦略 中期テーマ"]
    elif shown == "stale":
        assert [(i["text"], i["url"], i["popup"]) for i in items] == [(
            "出口ライン (4営業日以上): 1銘柄", "/portfolio/charts?status=hold&codes=1001",
            f"1001 銘柄1001 {reasons}")]
    else:
        assert items == []


@pytest.mark.parametrize("status, recovered, later_defense, expected", [
    ("2準", "2026-10-08", None, "MA回復: 準保有 銘柄1001 (日足50MA 1,300円・10/8)"),
    ("1保", "2026-10-12", None, "防のあと回復: 保有 銘柄1001 (日足50MA 1,300円・10/12)"),
    ("2準", "2026-10-07", None, None),          # 戻った日から3営業日を過ぎた
    ("2準", "2026-10-08", "2026-10-09", None),  # 戻ったあとにまた防
])
def test_recovery_items_show_for_three_busdays(monkeypatch, status, recovered, later_defense, expected):
    """防のあと MA の上に戻った日から3営業日、保有・準保有を別の行で出す (issue #526)"""
    rule = {"ma_kind": "day", "ma_window": 50, "stop_loss_pct": None, "allow_dca_lower": False}
    events = [{"level": "防", "date": "2026-10-01"}]
    if later_defense:
        events.append({"level": "防", "date": later_defense})
    _stub_material(monkeypatch, [{"code_s": "1001", "status": status, "memo": {"trade_idea": "中期テーマ"}}])
    monkeypatch.setattr(ps, "list_trade_ideas", lambda: [{"name": "中期テーマ", "exit_rule": rule}])
    monkeypatch.setattr(ps, "list_exit_alert_states", lambda: {"1001": {
        "cycle_id": f"c|中期テーマ|{ps.exit_rule_id(rule)}", "events": events,
        "recoveries": [{"date": recovered, "ma_label": "日足50MA", "ma_value": 1300.2}],
    }})

    assert [i["text"] for i in aq._recovery_items(TODAY)] == ([expected] if expected else [])


def test_recorded_exits_reads_batch_records(monkeypatch):
    """初点灯日は記録の最も古い日。最後の記録が最新の株価日なら今も「防」、古ければ戻った扱い。
    戦略を変えた銘柄の古い記録は読まない"""
    rule = {"ma_kind": None, "ma_window": None, "stop_loss_pct": 10, "allow_dca_lower": False}
    cyc = f"c|中期テーマ|{ps.exit_rule_id(rule)}"
    states = {
        "1001": {"cycle_id": cyc, "events": [{"level": "防", "date": "2026-10-12"},
                                             {"level": "防", "date": "2026-10-08"}]},
        "1002": {"cycle_id": cyc, "events": [{"level": "防", "date": "2026-10-08"}]},  # その後の株価日に記録なし
        "1003": {"cycle_id": cyc, "events": [{"level": "防", "date": "2026-10-12"}]},  # 戦略を変えた後
        "2001": {"cycle_id": cyc, "events": [{"level": "防", "date": "2026-10-12"}]},  # 1保以外は読まない
    }
    monkeypatch.setattr(ps, "list_exit_alert_states", lambda: states)
    monkeypatch.setattr(ps, "list_trade_ideas", lambda: [
        {"name": "中期テーマ", "exit_rule": rule}, {"name": "夢枠", "exit_rule": None}])
    stocks = {c: {"price_log": [(date(2026, 10, 12), 100)]} for c in ("1001", "1002", "1003")}

    memo = {"trade_idea": "中期テーマ"}
    result = aq._recorded_exits(
        [{"code_s": "1001", "status": "1保", "memo": memo}, {"code_s": "1002", "status": "1保", "memo": memo},
         {"code_s": "1003", "status": "1保", "memo": {"trade_idea": "夢枠"}},
         {"code_s": "2001", "status": "2準", "memo": memo}], stocks)

    assert {c: (v["signal"] or {}).get("date") for c, v in result.items()} == {
        "1001": "2026-10-12", "1002": None,
    }
    assert {c: v["first_defense"] for c, v in result.items()} == {
        "1001": "2026-10-08", "1002": "2026-10-08",
    }


def test_breakout_items(monkeypatch):
    """ブ・週ブは発生から3営業日。保有・準保有は銘柄名 (新しい順、4銘柄目から「他N銘柄」)、監視は件数と内訳"""
    import make_stock_db

    sigs = {
        "1001": [("ブ", date(2026, 10, 8))],                            # 2営業日後: 出る
        "1002": [("ブ", date(2026, 10, 12)), ("週ブ", date(2026, 10, 9))],  # 両方: 新しい方の日付で並ぶ
        "1003": [("週ブ", date(2026, 10, 9))],
        "1004": [("ブ", date(2026, 10, 9))],
        "1005": [("ブ", date(2026, 10, 7))],                            # 3営業日後: 出ない
        "1006": [("ポ", date(2026, 10, 12))],                           # ポは出さない
        "3001": [("ブ", date(2026, 10, 12)), ("週ブ", date(2026, 10, 12))],
        "3002": [("ブ", date(2026, 10, 9))],
    }
    _stub_material(monkeypatch, [
        {"code_s": c, "status": "3監" if c.startswith("3") else "1保"} for c in sigs
    ])
    monkeypatch.setattr(make_stock_db, "extract_signals", lambda stock: [
        {"kind": k, "sig_date": d} for k, d in sigs[stock["stock_name"][2:]]
    ])

    items = aq._breakout_items(TODAY)

    assert [(i["text"], i["url"]) for i in items] == [
        ("ブレイク点灯: 保有 銘柄1002 (ブ・週ブ)・銘柄1003 (週ブ)・銘柄1004 (ブ) 他1銘柄",
         "/portfolio/charts?status=hold&codes=1002,1003,1004,1001"),
        ("監視のブレイク: 2銘柄 (ブ2・週ブ1)", "/portfolio/charts?status=watch&codes=3001,3002"),
    ]
    assert [(i["popup_label"], i["popup"]) for i in items] == [
        ("他1銘柄", "1001 銘柄1001"), ("2銘柄", "3001 銘柄3001\n3002 銘柄3002"),
    ]


@pytest.mark.parametrize("kessan, updated, first_defense, real_today, expected", [
    ("2026/10/12", None, None, None, "ステージ未更新: 決算後 保有 1"),    # 決算当日
    ("2026/09/28", "9/27", None, None, "ステージ未更新: 決算後 保有 1"),  # 14日後まで。更新日が決算前なら未更新
    ("2026/09/27", None, None, None, None),                              # 15日後は出さない
    ("2026/10/05", "10/6", None, None, None),                            # 決算の後に更新済み
    # 17時前 (株価の基準日は前日) に今日保存した更新日を、去年と読まない
    ("2026/10/05", "10/13", None, date(2026, 10, 13), None),
    ("2026/10/05", None, "2026-10-08", None, "ステージ未更新: 防の後 保有 1"),  # 両方なら防の後だけで数える
])
def test_stage_item_after_kessan_or_defense(monkeypatch, kessan, updated, first_defense, real_today, expected):
    """決算・「防」の後14日以内で、ステージをまだ更新していない保有・準保有を数える"""
    memo = {"last_research_update": updated} if updated else {}
    _stub_material(
        monkeypatch,
        [{"code_s": "1001", "status": "1保", "memo": memo}],
        stocks={"1001": {"kessan_jisseki_date": kessan}},
        exits={"1001": {"signal": None, "first_defense": first_defense}} if first_defense else {},
    )

    if real_today:
        monkeypatch.setattr(aq, "_real_today", lambda: real_today)

    item = aq._stage_item(TODAY)

    assert (item["text"] if item else None) == expected
    if item:  # チャート一覧は該当銘柄だけに絞る
        assert item["url"] == "/portfolio/charts?status=hold&codes=1001"
