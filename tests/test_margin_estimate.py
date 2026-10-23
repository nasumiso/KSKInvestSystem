"""推定維持率の概算ロジックのテスト (issue #527)。"""

from datetime import datetime

import pytest

import margin_estimate as me


def _positions(margin_qty=100, margin_price=1000.0, spot_qty=0, spot_price=500.0, broker="楽天"):
    rows = [{"broker": broker, "kind": "信用", "code_s": "1111", "qty": margin_qty,
             "avg_price": margin_price}]
    if spot_qty:
        rows.append({"broker": broker, "kind": "現物", "code_s": "2222", "qty": spot_qty,
                     "avg_price": spot_price})
    return rows


def _baseline(rate=40.0, **kw):
    snap, _ = me.build_snapshot("楽天", _positions(**kw), {"1111": 1000.0, "2222": 500.0})
    return me.make_baseline("楽天", rate, snap, "2026-10-23")


@pytest.mark.parametrize("closes, kw, expected_rate", [
    ({"1111": 1000.0, "2222": 500.0}, {}, 40.0),                        # 基準日と同じ → 入力値のまま
    ({"1111": 800.0, "2222": 500.0}, {}, 20.0),                         # 評価損 -20,000 → 保証金 20,000 / 建玉 100,000
    ({"1111": 1200.0, "2222": 500.0}, {}, 40.0),                        # 評価益は保証金に数えない
    ({"1111": 1000.0, "2222": 450.0}, {"spot_qty": 100}, 36.0),         # 代用 0.8 × 100 × -50 = -4,000 → 36,000 / 100,000
    ({"1111": 1000.0, "2222": 500.0}, {"margin_qty": 200}, 20.0),       # 建玉が倍 → 建玉合計 200,000 (保証金は同じ)
])
def test_estimate_scenarios(closes, kw, expected_rate):
    """基準日からの差分で推定する。評価損と代用の値動きだけが保証金を動かす"""
    spot = {"spot_qty": 100} if "spot_qty" in kw else {}
    baseline = _baseline(**spot)
    snap, _ = me.build_snapshot("楽天", _positions(**{**spot, **kw}), closes)
    result, reason = me.estimate(baseline, snap)
    assert reason is None
    assert result["rate_pct"] == pytest.approx(expected_rate)


@pytest.mark.parametrize("positions, closes, reason_part", [
    (_positions() + [{"broker": "楽天", "kind": "信用売建", "code_s": "3333", "qty": 100, "avg_price": 100.0}],
     {"1111": 1000.0}, "信用売建"),
    ([{"broker": "楽天", "kind": "現物", "code_s": "2222", "qty": 100, "avg_price": 500.0}],
     {"2222": 500.0}, "信用建玉がありません"),
    (_positions(margin_price=None), {"1111": 1000.0}, "建単価"),
    (_positions(), {}, "終値が取れない"),   # 分母だけ大きく、評価損が出せない状態で低い維持率を出さない
])
def test_not_estimated(positions, closes, reason_part):
    """売建がある・信用なし・建単価が欠けた・信用建玉の終値が無い証券会社は推定しない"""
    snap, reason = me.build_snapshot("楽天", positions, closes)
    assert snap is None and reason_part in reason


def test_fall_and_combine_and_input_check():
    """追証までの下落率は (保証金 - T×建玉) ÷ (建玉 + 0.8×現物)。総合は合算で出し、証券会社別の警戒を注記する"""
    # 保証金 400 万・建玉 1000 万・現物 500 万: 20% まで (400-200) / (1000+400) = 14.3%
    assert me.fall_to_threshold(4_000_000, 10_000_000, 5_000_000, 20) == pytest.approx(14.2857, abs=1e-3)
    assert me.fall_to_threshold(1_000_000, 10_000_000, 0, 20) == 0.0  # すでに 20% 未満
    combined = me.combine({
        "楽天": {"guarantee": 4_000_000, "tate_total": 10_000_000, "spot_value": 5_000_000, "rate_pct": 40.0},
        "SBI": {"guarantee": 500_000, "tate_total": 2_000_000, "spot_value": 0, "rate_pct": 25.0},
    })
    assert combined["rate_pct"] == pytest.approx(37.5)
    assert combined["low_brokers"] == ["SBI"]
    # 古い残高CSV・古い株価では基準値を保存させない
    now = datetime(2026, 10, 23, 20, 0)
    src = [{"broker": "楽天", "kind": k, "as_of": "2026-10-23"} for k in ("現物", "信用")]
    assert me.check_baseline_inputs("楽天", src, ["2026-10-23"], now) is None
    assert "信用 CSV が 2026-10-15" in me.check_baseline_inputs(
        "楽天", [src[0], {**src[1], "as_of": "2026-10-15"}], ["2026-10-23"], now)
    assert "現物 CSV が取り込まれていません" in me.check_baseline_inputs("楽天", src[1:], ["2026-10-23"], now)
    assert "日次バッチ" in me.check_baseline_inputs("楽天", src, ["2026-10-22"], now)
