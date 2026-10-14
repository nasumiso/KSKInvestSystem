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
