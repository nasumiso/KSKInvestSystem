"""保有ポートフォリオの点検の数字のテスト (issue #589)。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import portfolio_review  # noqa: E402
import trade_review  # noqa: E402

TRADE_IDEAS = [
    {"name": "中期モメンタム", "time_horizon": "中期", "exit_rule": {"stop_loss_pct": 10}},
    {"name": "短期底値リバ", "time_horizon": "短期", "exit_rule": {"stop_loss_pct": 7}},
]


def test_買い増しを別の戦略で建てたロットはロット単位で幅超えと20日超えに出る():
    # 中期モメンタムで建て、10/15 に短期底値リバで買い増した。終値 820 円
    ep = {
        "code_s": "6264", "stock_name": "銘柄6264", "kind": "現物", "episode_key": "6264|現物|1",
        "closed": False, "trade_idea": "中期モメンタム", "current_price": 820.0, "open_date": "2026-10-01",
        "fills": [
            {"trade_date": "2026-10-01", "side": "buy", "qty": 100, "price": 800.0, "seq": 1, "broker": "楽天"},
            {"trade_date": "2026-10-15", "side": "buy", "qty": 100, "price": 900.0, "seq": 2, "broker": "楽天"},
        ],
    }
    fill_strategies = {("6264", 2): {"trade_idea": "短期底値リバ", "source": "manual",
                                     "assigned_at": "2026-10-15"}}
    lots = portfolio_review.open_lots([ep], TRADE_IDEAS, fill_strategies, "2026-11-22")
    by_seq = {x["lot_key"].split("#")[1]: x for x in lots}
    assert not by_seq["1"]["over_width"] and not by_seq["1"]["over_horizon"]
    assert by_seq["2"]["over_width"] and by_seq["2"]["over_horizon"]  # -8.9% < -7%、38日 > 20日
    assert by_seq["2"]["stop_loss_pct"] == 7.0
    # エピソード単位では、ロットの戦略が混ざると幅を当てない (だからロット単位で見る)
    assert trade_review._stop_loss_widths([ep], TRADE_IDEAS, fill_strategies)["6264|現物|1"] is None


def test_前回の約束を約定と終値と維持率で照合する():
    previous = {"date": "2026-11-15", "dataset": None, "decisions": {
        "holdings": [
            {"code_s": "A", "action": "cut", "qty": 200},                 # 200株売った → 実行
            {"code_s": "B", "action": "reduce"},                          # 株数未定、売っていない
            {"code_s": "C", "action": "hold", "exit": {"type": "price", "price": 1000}},  # 割って持ったまま
            {"code_s": "D", "action": "hold", "exit": {"type": "price", "price": 500}},   # 線の内側
            {"code_s": "E", "action": "hold", "exit": {"type": "earnings"}, "after_earnings": "半分売る"},
            {"code_s": "G", "action": "hold", "exit": {"type": "price", "price": 1000, "proposed": True}},  # 案の線
        ],
        "sell_order": {"trigger_margin_rate": 50, "codes": [{"code_s": "F", "qty": 100}]},
    }}
    result = portfolio_review.check_previous(
        previous, "2026-11-22",
        held_qty={"A": 300, "B": 100, "C": 100, "D": 100, "E": 100, "F": 100},
        sold_qty={"A": 200},
        closes={"C": {"2026-11-14": 900.0, "2026-11-18": 950.0, "2026-11-19": 1100.0},
                "D": {"2026-11-18": 600.0}, "G": {"2026-11-18": 900.0}},
        exposure_history=[{"date": "2026-11-18", "margin_rate_pct": 52.0},
                          {"date": "2026-11-20", "margin_rate_pct": 48.0}],
        kessan_dates={"E": ["2026-08-10", "2026-11-20"]})
    status = {r["code_s"]: r["status"] for r in result["holdings"]}
    assert status == {"A": "done", "B": "not_done", "C": "held_after_break", "D": "within_line",
                      "E": "earnings_passed", "G": "held_after_break"}
    assert result["sell_order"]["triggered_date"] == "2026-11-20"
    # 実行すべきだった約束: A・B・C・維持率の順番 の4件 (案の線の G は数えない)。実行は A だけ
    assert (result["due"], result["done"]) == (4, 1)


@pytest.mark.parametrize("holding, previous_holdings, error", [
    ({"code_s": "X", "action": "hold", "exit": {"type": "price", "price": 100}}, [], "持つ理由"),
    ({"code_s": "X", "action": "hold", "reason": "受注が残る"}, [], "売る条件"),
    ({"code_s": "X", "action": "hold", "reason": "受注が残る", "exit": {"type": "margin_order"}}, [],
     "維持率で売る順番"),
    ({"code_s": "X", "action": "hold", "reason": "受注が残る", "exit": {"type": "price", "price": 90}},
     [{"code_s": "X", "action": "hold", "exit": {"type": "price", "price": 100}}], "2回目以降"),
    ({"code_s": "X", "action": "reduce"}, [], None),  # 株数は未定でよい
])
def test_決定の検証は持つ理由と売る条件を求める(holding, previous_holdings, error):
    previous = [{"date": "2026-11-15", "decisions": {"holdings": previous_holdings}}]
    errors = portfolio_review.validate_decisions({"holdings": [holding]}, previous)
    assert (error in "\n".join(errors)) if error else errors == []


def test_保存は上書きせず開発機では書かない(tmp_path, monkeypatch):
    dataset, decisions = {"date": "2026-11-22"}, {"holdings": [{"code_s": "X", "action": "cut"}]}
    monkeypatch.setattr(portfolio_review.ks_util, "is_dev_data_dir", lambda: True)
    with pytest.raises(RuntimeError):
        portfolio_review.save_review(dataset, decisions, "# r", review_dir=str(tmp_path))
    monkeypatch.setattr(portfolio_review.ks_util, "is_dev_data_dir", lambda: False)
    portfolio_review.save_review(dataset, decisions, "# r", review_dir=str(tmp_path))
    assert json.loads((tmp_path / "2026-11-22.json").read_text())["decisions"] == decisions
    with pytest.raises(FileExistsError):
        portfolio_review.save_review(dataset, decisions, "# r2", review_dir=str(tmp_path))
    assert portfolio_review.latest_review(str(tmp_path))["date"] == "2026-11-22"


_LOG = [
    {"date": "2026-11-13", "state": "confirmed_uptrend", "margin_rate_pct": 70.0, "position": "within"},  # 金
    {"date": "2026-11-18", "state": "uptrend_under_pressure", "margin_rate_pct": 65.0, "position": "within"},
    {"date": "2026-11-20", "state": "market_in_correction", "margin_rate_pct": 58.0, "position": "over"},
]


@pytest.mark.parametrize("last, lit, over, log, expected", [
    # 何も起きていない
    ({"date": "2026-11-15", "dataset": {"defense": {"lit": [{"code_s": "A"}], "lot_over_keys": ["k1"]}}},
     ["A"], ["k1"], _LOG[:1], {}),
    # 新しい防と、前回に無かった幅超えのロット
    ({"date": "2026-11-15", "dataset": {"defense": {"lit": [{"code_s": "A"}], "lot_over_keys": ["k1"]}}},
     ["A", "B"], ["k1", "k2"], _LOG[:1], {"new_lit": ["B"], "new_lot_over": ["k2"]}),
    # 前回が土曜でも金曜のログを基準にし、ステートの悪化と維持率の低下 (position が over) を拾う
    ({"date": "2026-11-15", "dataset": None}, [], [], _LOG,
     {"new_lit": [], "new_lot_over": [], "state": ("confirmed_uptrend", "market_in_correction"),
      "margin": True}),
    # 基準のログが無ければ、ステートと維持率は判定しない
    ({"date": "2026-11-15", "dataset": {"defense": {}}}, [], [], _LOG[1:], {}),
])
def test_点検のきっかけは前回より後に起きたものだけ(last, lit, over, log, expected):
    t = portfolio_review.triggers_since(last, lit, over, log)
    assert t["new_lit"] == expected.get("new_lit", [])
    assert t["new_lot_over"] == expected.get("new_lot_over", [])
    assert (t["state_worse"] and (t["state_worse"]["from"], t["state_worse"]["to"])) == expected.get("state", None)
    assert bool(t["margin_down"]) == expected.get("margin", False)
    assert portfolio_review.has_trigger(t) == bool(expected)
