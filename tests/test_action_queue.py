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

    assert [i["text"] for i in aq._kessan_items(TODAY)] == [
        "決算が近い: 保有 銘柄1003 (10/15)・銘柄1004 (10/15)・銘柄1005 (10/15) 他1銘柄",
        "決算が近い: 準保有 2銘柄 (10/14〜10/16)",
        "監視の決算: 3日以内に 1銘柄",
    ]


def _stub_material(monkeypatch, records, stocks=None, exits=None):
    """records: [{code_s, status, memo?}]、stocks: {code_s: stock}、exits: {code_s: {signal, first_defense}}"""
    stocks = stocks or {}
    monkeypatch.setattr(ps, "list_records", lambda: records)
    monkeypatch.setattr(aq, "_bulk_get_stock_data",
                        lambda codes: {c: {"stock_name": f"銘柄{c}", **stocks.get(c, {})} for c in codes})
    monkeypatch.setattr(aq, "_recorded_exits", lambda recs, stock_map: exits or {})


@pytest.mark.parametrize("first, level, shown", [
    ("2026-10-12", "防", True),    # 初点灯の当日 (TODAY は月曜)
    ("2026-10-09", "防", True),    # 金曜点灯 → 月曜は1営業日後
    ("2026-10-08", "防", True),    # 2営業日後まで出す
    ("2026-10-07", "防", False),   # 3営業日後は出さない (点きっぱなしの銘柄で埋まらないため)
    ("2026-10-12", "防予", False),  # 予兆は出さない
])
def test_exit_items_show_first_defense_for_three_busdays(monkeypatch, first, level, shown):
    """出口ライン「防」は、今の保有サイクルで初めて点いてから3営業日だけ出る"""
    _stub_material(monkeypatch, [{"code_s": "1001", "status": "1保"}], exits={
        "1001": {"signal": {"level": level, "reasons": ["損切りライン割れ"]}, "first_defense": first},
    })

    items = aq._exit_items(TODAY)

    if shown:
        md = f"{int(first[5:7])}/{int(first[8:])}"
        assert [i["text"] for i in items] == [f"出口ライン: 1001 銘柄1001 損切りライン割れ ({md}〜)"]
    else:
        assert items == []


def test_recorded_exits_reads_batch_records(monkeypatch):
    """初点灯日は記録の最も古い日。最後の記録が最新の株価日なら今も「防」、古ければ戻った扱い"""
    states = {
        "1001": {"events": [{"level": "防", "date": "2026-10-12"}, {"level": "防", "date": "2026-10-08"}]},
        "1002": {"events": [{"level": "防", "date": "2026-10-08"}]},  # その後の株価日に記録なし
        "2001": {"events": [{"level": "防", "date": "2026-10-12"}]},  # 1保以外は読まない
    }
    monkeypatch.setattr(ps, "list_exit_alert_states", lambda: states)
    stocks = {c: {"price_log": [(date(2026, 10, 12), 100)]} for c in ("1001", "1002")}

    result = aq._recorded_exits(
        [{"code_s": "1001", "status": "1保"}, {"code_s": "1002", "status": "1保"},
         {"code_s": "2001", "status": "2準"}], stocks)

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
         "/portfolio/charts?status=hold"),
        ("監視のブレイク: 2銘柄 (ブ2・週ブ1)", "/portfolio/charts?status=watch"),
    ]


@pytest.mark.parametrize("kessan, updated, first_defense, expected", [
    ("2026/10/12", None, None, "ステージ未更新: 決算後 保有 1"),    # 決算当日
    ("2026/09/28", "9/27", None, "ステージ未更新: 決算後 保有 1"),  # 14日後まで。更新日が決算前なら未更新
    ("2026/09/27", None, None, None),                              # 15日後は出さない
    ("2026/10/05", "10/6", None, None),                            # 決算の後に更新済み
    ("2026/10/05", None, "2026-10-08", "ステージ未更新: 防の後 保有 1"),  # 両方なら防の後だけで数える
])
def test_stage_item_after_kessan_or_defense(monkeypatch, kessan, updated, first_defense, expected):
    """決算・「防」の後14日以内で、ステージをまだ更新していない保有・準保有を数える"""
    memo = {"last_research_update": updated} if updated else {}
    _stub_material(
        monkeypatch,
        [{"code_s": "1001", "status": "1保", "memo": memo}],
        stocks={"1001": {"kessan_jisseki_date": kessan}},
        exits={"1001": {"signal": None, "first_defense": first_defense}} if first_defense else {},
    )

    item = aq._stage_item(TODAY)

    assert (item["text"] if item else None) == expected
