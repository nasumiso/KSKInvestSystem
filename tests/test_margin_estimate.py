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


def _settle(price, tate_price=None, settle_pl=None, day="2026-10-26", broker="楽天", code="1111"):
    return {"broker": broker, "code_s": code, "trade_kind": "信用返済", "side": "sell", "qty": 100,
            "price": price, "tate_price": tate_price, "settle_pl": settle_pl, "trade_date": day}


@pytest.mark.parametrize("fills, expected_rate", [
    # 200株 @1000 (基準日の終値 900、評価損 -20,000、保証金 80,000) のうち 100株を 850 で返済 (損益 -15,000)。
    # 残り 100株の評価損 -15,000 → 保証金 = 80,000 + 20,000 - 15,000 - 15,000 = 70,000 / 建玉 100,000
    ([_settle(850, tate_price=1000)], 70.0),
    # 決済損益が分からない (未確定 CSV) → 減った分の損は基準日の値 (-10,000) で残す: 80,000 - 5,000 = 75%
    ([_settle(850)], 75.0),
    # 基準日の返済・他社の返済は数えない
    ([_settle(850, tate_price=1000, day="2026-10-23"), _settle(850, settle_pl=-15000, broker="SBI")], 75.0),
])
def test_estimate_with_realized(fills, expected_rate):
    """基準日より後の信用返済は、基準日の評価損を外して決済損益に置き換える"""
    base_snap, _ = me.build_snapshot("楽天", _positions(margin_qty=200), {"1111": 900.0})
    baseline = me.make_baseline("楽天", 40.0, base_snap, "2026-10-23")
    snap, _ = me.build_snapshot("楽天", _positions(margin_qty=100), {"1111": 850.0})
    result, _ = me.estimate(baseline, snap, me.realized_since(fills, "楽天", "2026-10-23"))
    assert result["rate_pct"] == pytest.approx(expected_rate)
    # SBI は CSV の決済損益をそのまま使う
    assert me.realized_since([_settle(850, settle_pl=-14000, broker="SBI")], "SBI", "2026-10-23") == {"1111": -14000.0}


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
        "楽天": {"guarantee": 4_000_000, "tate_total": 10_000_000, "spot_value": 5_000_000,
                 "margin_value": 9_000_000, "rate_pct": 40.0},
        "SBI": {"guarantee": 500_000, "tate_total": 2_000_000, "spot_value": 0,
                "margin_value": 2_000_000, "rate_pct": 25.0},
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


@pytest.mark.parametrize("emotion, fng, state, expected", [
    (None, 50, "confirmed_uptrend", 0),          # 未入力は中立
    (-2, 15, "market_in_correction", -3),        # Extreme Fear は逆張りで 1 段。市場ステートの上限はかけない
    (-3, 15, None, -3),                          # ±3 で止める
    (2, 80, "confirmed_uptrend", 3),             # Extreme Greed は過熱側へ 1 段
    (-2, 40, "market_in_correction", 0),         # F&G が普通の日は、調整局面で 0 段より攻めない
    (-2, 40, "uptrend_under_pressure", -1),      # 上昇圧力下は −1 段まで
    (-2, 40, "confirmed_uptrend", -2),           # 上昇確定は上限なし
    (-2, None, None, -1),                        # F&G・市場ステートが取れない日は上昇圧力下と同じ上限
])
def test_target_step(emotion, fng, state, expected):
    """感情スコアが主。F&G が Extreme なら逆張りで 1 段、それ以外は市場ステートで攻める側に上限"""
    step, parts = me.target_step(emotion, fng, state)
    assert step == expected and parts


@pytest.mark.parametrize("guarantee, step, expected", [
    # 保証金 700万・建玉 1000万 (時価 900万)・現物 500万 → 運用総額 1400万、維持率 70%
    # 段 0 (80%): 建玉 875万 → 125万減らす。時価に直して 125 × 0.9 = 112.5万 → 1287.5万。±5pt で 1235〜1347
    (7_000_000, 0, {"target_value": 12_875_000, "position": "over", "floor_capped": False}),
    # 段 −1 (70%): 今の建玉のまま → エリア内
    (7_000_000, -1, {"target_value": 14_000_000, "position": "within", "floor_capped": False}),
    # 段 −3 (50%): 建玉 1400万 (+400万) → 1800万。多め側の端 45% は建玉 1555.6万だが、
    # 追証の下限 (700 − 0.8×0.2×500) / 0.4 = 建玉 1550万 で頭打ち → 1950万
    (7_000_000, -3, {"target_value": 18_000_000, "position": "under", "floor_capped": True}),
])
def test_target_area(guarantee, step, expected):
    """目標の建玉 = 保証金 ÷ 目標維持率。増やす側はそのまま、減らす側は時価に直し、追証の下限で頭打ち"""
    area = me.target_area(guarantee, 10_000_000, 5_000_000, 9_000_000, step)
    assert area["target_value"] == pytest.approx(expected["target_value"])
    assert (area["position"], area["floor_capped"]) == (expected["position"], expected["floor_capped"])
    assert area["area_lower"] <= area["target_value"] <= area["area_upper"]
    if area["floor_capped"]:  # 頭打ちの端は、一律 20% 下落で維持率がちょうど 20% になる量
        assert area["area_upper"] == pytest.approx(14_000_000 + 5_500_000)
