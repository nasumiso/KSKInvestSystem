"""約定と終値からの日次の値洗いのテスト (issue #594 段階1)。"""

import pytest

import equity_history as eh

D1, D2 = "2025-01-06", "2025-01-07"


def _fill(day, side, kind, qty, price, code_s="1111", broker="楽天"):
    return {"broker": broker, "code_s": code_s, "trade_date": day, "side": side,
            "trade_kind": kind, "qty": qty, "price": price}


@pytest.mark.parametrize("fills, closes, splits, pnl, p, r", [
    # 同じ日の買いと、翌日の売り。分母は初日に建てた額、翌日は前日の P + 差し引きで増やした分 (0)
    ([_fill(D1, "buy", "現物", 100, 1000), _fill(D2, "sell", "現物", 100, 1015)],
     {D1: 1010, D2: 1020}, {}, [1000, 500], [101000, 0], [1000 / 100000, 500 / 101000]),
    # 現引は株数が変わらない (信用と現物の二重計上をしない)
    ([_fill(D1, "buy", "信用新規", 100, 1000), _fill(D2, "buy", "現引", 100, 1000)],
     {D1: 1000, D2: 1100}, {}, [0, 10000], [100000, 110000], [0, 0.1]),
    # 分割前の約定は、さかのぼって調整された終値の基準 (2倍の株数・半分の単価) に直す
    ([_fill(D1, "buy", "現物", 100, 1000)],
     {D1: 500, D2: 510}, {"1111": [(D2, 2.0)]}, [0, 2000], [100000, 102000], [0, 0.02]),
    # 空売りは値下がりで益、P は絶対額
    ([_fill(D1, "sell", "信用新規", 100, 1000)],
     {D1: 990, D2: 980}, {}, [1000, 1000], [99000, 98000], [0.01, 1000 / 99000]),
    # 取込より前からの持ち越し: 買いの無い売りは、初日から持っていたとみなし初日の終値で値洗いを始める
    ([_fill(D2, "sell", "現物", 100, 1100)],
     {D1: 1000, D2: 1050}, {}, [0, 10000], [100000, 0], [0, 0.1]),
    # 同じ日に返済が新規より先に並んでも、引け時点で足りていれば持ち越しにしない
    ([_fill(D1, "buy", "信用新規", 100, 1000), _fill(D2, "sell", "信用返済", 100, 1010),
      _fill(D2, "sell", "信用返済", 100, 1010), _fill(D2, "buy", "信用新規", 100, 1010)],
     {D1: 1000, D2: 1010}, {}, [0, 1000], [100000, 0], [0, 0.01]),
    # 証券会社間で移した株 (マネックスで買い SBI で売る) を二重に数えない
    ([_fill(D1, "buy", "現物", 100, 1000, broker="マネックス"),
      _fill(D2, "sell", "現物", 100, 1010, broker="SBI")],
     {D1: 1000, D2: 1010}, {}, [0, 1000], [100000, 0], [0, 0.01]),
])
def test_build_series(fills, closes, splits, pnl, p, r):
    result = eh.build_series(eh.to_moves(fills, splits), {"1111": closes}, [D1, D2])
    assert [row["pnl"] for row in result["days"]] == pytest.approx(pnl)
    assert [row["P"] for row in result["days"]] == pytest.approx(p)
    assert [row["r"] for row in result["days"]] == pytest.approx(r)


def test_long_and_short_on_same_code_do_not_offset():
    """同じ銘柄の買い玉と売り玉は相殺せず、P はそれぞれの絶対額の合計になる。"""
    fills = [_fill(D1, "buy", "現物", 100, 1000), _fill(D1, "sell", "信用新規", 100, 1000)]
    result = eh.build_series(eh.to_moves(fills, {}), {"1111": {D1: 1000, D2: 1010}}, [D1, D2])
    assert [row["P"] for row in result["days"]] == pytest.approx([200000, 202000])
    assert result["days"][1]["pnl"] == pytest.approx(0)
