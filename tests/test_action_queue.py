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
        ("残高CSV", boom),
        ("約定CSV", lambda today: None),
        ("四季報", lambda today: {"text": "四季報 26.9 が未入力: 保有 1銘柄", "url": "/x", "link_label": "入力する"}),
    ])

    items = aq.build_action_queue(TODAY)

    assert [i["text"] for i in items] == [
        "残高CSVの状態を判定できませんでした",
        "四季報 26.9 が未入力: 保有 1銘柄",
    ]
