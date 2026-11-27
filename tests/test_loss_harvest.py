"""loss_harvest.build_plan / deadline のテスト。"""
from datetime import date

import pytest

import loss_harvest as lh

TODAY = date(2026, 11, 27)


def _pos(code, broker="楽天", kind="現物", qty=100, cost=1000.0, account="特定"):
    return {"code_s": code, "broker": broker, "kind": kind, "qty": qty, "avg_price": cost, "account": account}


@pytest.mark.parametrize("pos, status, idea, expected", [
    # Active で戦略あり → クロス候補。損切り幅 10% を超える -20% なので超過
    (_pos("6134", kind="信用"), "Active", "中期テーマ", {"group": "b", "over_stop": True, "odd_lot": False}),
    # Watch / 未登録 / 戦略なし → 売り切り候補
    (_pos("7047"), "Watch", "GARP", {"group": "a"}),
    (_pos("4390"), None, "夢枠", {"group": "a", "status": "未登録"}),
    (_pos("4970"), "Active", None, {"group": "a"}),
    # 単元未満の端数を含む現物
    (_pos("285A", qty=135), "Active", "中長期ファンダ", {"group": "b", "odd_lot": True, "over_stop": False}),
    # NISA・信用売建は対象外 (候補に出ない)
    (_pos("6890", account="NISA成長投資枠"), "Active", "中長期ファンダ", None),
    (_pos("9984", kind="信用売建"), "Active", "中長期ファンダ", None),
])
def test_candidate_grouping(pos, status, idea, expected):
    ratings = {pos["code_s"]: status} if status else {}
    plan = lh.build_plan([pos], {pos["code_s"]: 800.0}, ratings, {pos["code_s"]: idea},
                         {"中期テーマ": 10, "GARP": 20, "中長期ファンダ": 25, "夢枠": None}, {}, {}, TODAY)
    if expected is None:
        assert plan["candidates"] == [] and plan["excluded"] == 1
        return
    (c,) = plan["candidates"]
    assert c["upl"] == pytest.approx(pos["qty"] * -200)
    assert {k: c[k] for k in expected} == expected


@pytest.mark.parametrize("caps, rk_over, total_cap", [
    ({"楽天": 50_000, "SBI": 10_000}, -30_000, 60_000),     # 楽天は上限内 (残り 3万)。通算も出る
    ({"楽天": 10_000, "SBI": 10_000}, 10_000, 20_000),      # 楽天は 1万 超過
    ({"楽天": 50_000}, -30_000, None),                       # SBI 未入力 → 通算は未入力
])
def test_account_caps(caps, rk_over, total_cap):
    positions = [_pos("7047"), _pos("4970", broker="SBI", kind="信用")]  # 含み損 各 -2万
    plan = lh.build_plan(positions, {"7047": 800.0, "4970": 800.0}, {}, {}, {}, {}, caps, TODAY)
    acc = {a["broker"]: a for a in plan["accounts"]}
    assert acc["楽天"]["loss"] == pytest.approx(20_000)
    assert acc["楽天"]["over"] == pytest.approx(rk_over)
    assert plan["total"]["cap"] == total_cap
    if total_cap is None:
        assert plan["total"]["refund"] is None and plan["total"]["loss"] == pytest.approx(40_000)


@pytest.mark.parametrize("year, trade, settle", [
    (2026, date(2026, 12, 28), date(2026, 12, 30)),  # 12/30 が水曜
    (2028, date(2028, 12, 27), date(2028, 12, 29)),  # 12/30 が土曜 → 金曜受渡、約定は2営業日前
    (2029, date(2029, 12, 26), date(2029, 12, 28)),  # 12/30 が日曜、12/29 土曜 → 金曜受渡
])
def test_deadline(year, trade, settle):
    assert lh.deadline(year) == (trade, settle)


def _fill(d, kind, side, qty, broker="楽天", code="4970", seq=0):
    return {"trade_date": d, "trade_kind": kind, "side": side, "qty": qty, "broker": broker, "code_s": code, "seq": seq}


@pytest.mark.parametrize("fills, key, expected", [
    # 証券会社を分けて数える: 楽天の信用が続いていても SBI は SBI の建日から
    ([_fill("2025-11-01", "信用新規", "buy", 100), _fill("2026-10-14", "信用新規", "buy", 100, broker="SBI"),
      _fill("2026-10-20", "信用返済", "sell", 100)], ("SBI", "4970", "信用"), date(2026, 10, 14)),
    # 一度 0 に戻ったら、次に立ち上がった日が起点。現引は信用から現物へ移る
    ([_fill("2026-01-05", "現物", "buy", 100), _fill("2026-02-01", "現物", "sell", 100),
      _fill("2026-03-02", "信用新規", "buy", 100), _fill("2026-03-10", "現引", "buy", 100)],
     ("楽天", "4970", "現物"), date(2026, 3, 10)),
    # 分割の入庫が無く売りが上回っても、負にせず次の買いから数え直す
    ([_fill("2026-05-20", "現物(単元未満)", "buy", 20), _fill("2026-10-14", "現物(単元未満)", "sell", 30),
      _fill("2026-11-05", "現物", "buy", 100)], ("楽天", "4970", "現物"), date(2026, 11, 5)),
])
def test_holding_starts(fills, key, expected):
    assert lh.holding_starts(fills)[key] == expected
