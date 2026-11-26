"""推定維持率の概算ロジックのテスト (issue #527)。"""

from datetime import date, datetime

import pytest

import margin_estimate as me


def _positions(margin_qty=100, margin_price=1000.0, spot_qty=0, spot_price=500.0, broker="楽天",
               spot_account="特定"):
    rows = [{"broker": broker, "kind": "信用", "code_s": "1111", "qty": margin_qty,
             "avg_price": margin_price}]
    if spot_qty:
        rows.append({"broker": broker, "account": spot_account, "kind": "現物", "code_s": "2222",
                     "qty": spot_qty, "avg_price": spot_price})
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
    ({"1111": 1000.0, "2222": 450.0}, {"spot_qty": 100, "spot_account": "NISA成長投資枠"}, 40.0),  # NISA の現物は代用に数えない
])
def test_estimate_scenarios(closes, kw, expected_rate):
    """基準日からの差分で推定する。評価損と代用の値動きだけが保証金を動かす"""
    spot = {"spot_qty": 100} if "spot_qty" in kw else {}
    baseline = _baseline(**spot)
    snap, _ = me.build_snapshot("楽天", _positions(**{**spot, **kw}), closes)
    result, reason = me.estimate(baseline, snap)
    assert reason is None
    assert result["rate_pct"] == pytest.approx(expected_rate)
    if kw.get("spot_account") == "NISA成長投資枠":  # 代用には数えないが、運用総額には入る
        assert result["spot_value"] == 0 and result["nisa_value"] == pytest.approx(45_000)


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
    """追証までの下落率は建玉ごとに時価で出す。総合は合算で出し、証券会社別の警戒を注記する"""
    def est(guarantee, close, spot=0):
        return {"guarantee": guarantee, "tate_total": 10_000_000, "spot_value": spot,
                "margin_rows": [(10_000, 1000.0, close)]}
    # 含み損の建玉 (時価 900 万) と現物 500 万: 400 万 − (900 万 + 400 万)·x = 200 万 → 15.4%
    assert me.fall_to_threshold([est(4_000_000, 900.0, 5_000_000)], 20) == pytest.approx(15.385, abs=1e-2)
    # 含み益 25% の建玉は 20% 下げるまで保証金を減らさない: 1250 万·x − 250 万 = 200 万 → 36%
    assert me.fall_to_threshold([est(4_000_000, 1250.0)], 20) == pytest.approx(36.0, abs=1e-2)
    assert me.fall_to_threshold([est(1_000_000, 900.0)], 20) == 0.0  # すでに 20% 未満
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


CREDIT = [  # 2026-11 の実例: 11/13 の値が底値圏で前週より改善 → 11/20 から買い向かい。11/20 は再び悪化
    {"date": "2026-11-06", "credit_eval_rate": -21.08},
    {"date": "2026-11-13", "credit_eval_rate": -15.49},
    {"date": "2026-11-20", "credit_eval_rate": -22.88},
]


@pytest.mark.parametrize("credit, day, expected", [
    (CREDIT, "2026-11-19", None),          # 公表は約1週遅れ: データ日 + 7日から使う
    (CREDIT, "2026-11-20", "2027-02-20"),  # 合図から3か月
    (CREDIT, "2027-01-10", "2027-02-20"),  # 合図のあとに悪化しても取り消さない
    (CREDIT, "2027-02-21", None),          # 3か月で終わる
    (CREDIT + [{"date": "2026-12-04", "credit_eval_rate": -18.0}], "2027-03-01", "2027-03-11"),  # 合図が続けば延びる
    ([{"date": "2026-11-06", "credit_eval_rate": -14.0},
      {"date": "2026-11-13", "credit_eval_rate": -16.0}], "2026-12-01", None),  # 底値圏でも悪化なら合図でない
    ([{"date": "2026-11-06", "credit_eval_rate": -20.0},
      {"date": "2026-11-13", "credit_eval_rate": -14.0}], "2026-12-01", None),  # 改善でも底値圏の外
])
def test_boost_until(credit, day, expected):
    """張りたい量: 信用評価損益率 (公表値) が −15% 以下で前週より改善したら、最後の合図から3か月 1.6倍"""
    assert me.boost_until(credit, day) == expected
    if expected:  # 始まりは期間の最初の合図 (延びても動かない)
        assert me.boost_period(credit, day)[0] == "2026-11-20"
    assert me.want_multiple(credit, day) == (1.6 if expected else 1.2)


@pytest.mark.parametrize("rows, expected_dd", [
    ([{"date": "2026-11-27", "equity": 100.0}], [0.0]),  # 反映初日 (暫定行だけ) は下落率 0
    ([{"date": "2026-11-27", "equity": 100.0},
      {"date": "2026-11-30", "equity": 90.0},
      # 保証金率の入れ直しで 30 入った (入金など) → 高値も 30 ずらし、跳ねない
      {"date": "2026-12-01", "equity": 120.0, "equity_gap": 30.0},
      # 四半期が変わったら、高値を直前の行 (120) に置き換える
      {"date": "2027-01-04", "equity": 110.0}],
     [0.0, 0.1, 1 - 120 / 130, 1 - 110 / 120]),
])
def test_drawdown_series(rows, expected_dd):
    """張れる量の物差し: 口座の高値からの下落率。入出金と四半期末の置き換えを入れる"""
    assert [r["drawdown"] for r in me.drawdown_series(rows)] == pytest.approx(expected_dd)


def test_week_targets_step_one_per_week():
    """今週の目標は、前の週の最後の行で min(張りたい量, 張れる量) へ先週の目標から1段。週は土曜始まり"""
    rows = [
        {"date": "2026-11-27", "equity": 100.0, "multiple": 1.35},  # 金曜。初回は 1.35 以下の段 = 1.2
        {"date": "2026-11-28", "equity": 100.0, "multiple": 1.35},  # 土曜は来週の分
        {"date": "2026-12-04", "equity": 85.0, "multiple": 1.3},    # −15% で張れる量は下限 0.8
    ]
    weeks = me.week_targets(rows, CREDIT)
    assert [(w["week"], w["target"]) for w in weeks] == [
        ("2026-11-21", 1.2), ("2026-11-28", 1.4), ("2026-12-05", 1.2)]
    assert me.target_for(weeks, "2026-11-27")["target"] == 1.2
    assert me.target_for(weeks, "2026-11-28")["target"] == 1.4  # 土日に来週の目標が出る
    assert me.room_cap(0.15) == 0.8 and me.room_cap(0.05) == pytest.approx(1.5)


@pytest.mark.parametrize("target_total, expected", [
    # 保証金 700万・建玉 1000万 (時価 900万)・現物 500万 → 運用総額 1400万。エリアは目標 ±5%
    (12_000_000, {"target_value": 12_000_000, "position": "over", "floor_capped": False}),
    (14_000_000, {"target_value": 14_000_000, "position": "within", "floor_capped": False}),
    # 多め側の端 1995万は、追証の下限 (700 − 0.8×0.2×500) / 0.4 = 建玉 1550万 → 1950万で頭打ち
    (19_000_000, {"target_value": 19_000_000, "position": "under", "floor_capped": True}),
])
def test_equity_and_target_area(target_total, expected):
    """自己資本 = 保証金 + 0.2×代用の現物 + NISA + 信用の評価益 + 保証金の外の現金。
    適正エリアは目標の倍率 × 自己資本 ±5%"""
    est = {"guarantee": 7_000_000, "spot_value": 5_000_000, "nisa_value": 1_000_000,
           "margin_rows": [(100, 1000.0, 1200.0), (100, 1000.0, 900.0)],  # 評価益 2万だけ足す
           "outside_cash": 500_000}
    assert me.equity(est) == pytest.approx(9_520_000)
    # 基準値に入れた保証金の外の現金は、推定と合算を通って自己資本に届く
    base = me.make_baseline("楽天", 40.0, me.build_snapshot("楽天", _positions(), {"1111": 1000.0})[0],
                            "2026-11-26", outside_cash=300_000)
    est1, _ = me.estimate(base, me.build_snapshot("楽天", _positions(), {"1111": 1000.0})[0])
    assert me.equity(me.combine({"楽天": est1})) == pytest.approx(40_000 + 300_000)
    area = me.target_area(7_000_000, 10_000_000, 5_000_000, 9_000_000, target_total)
    assert area["target_value"] == pytest.approx(expected["target_value"])
    assert (area["position"], area["floor_capped"]) == (expected["position"], expected["floor_capped"])
    if area["floor_capped"]:  # 頭打ちの端は、一律 20% 下落で維持率がちょうど 20% になる量
        assert area["area_upper"] == pytest.approx(14_000_000 + 5_500_000)


@pytest.mark.parametrize("price_log, expected", [
    # 日経の日足 (日付降順)。公表日 (金曜) の終値からの騰落 × CREDIT_NIKKEI_BETA を公表値に足す
    ([(date(2026, 11, 10), 60459), (date(2026, 11, 9), 58775), (date(2026, 11, 6), 57435)],
     (-21.08 + me.CREDIT_NIKKEI_BETA * (60459 / 57435 - 1) * 100, "2026-11-10")),
    ([(date(2026, 11, 10), 60459), (date(2026, 11, 4), 62189)],
     (-21.08 + me.CREDIT_NIKKEI_BETA * (60459 / 62189 - 1) * 100, "2026-11-10")),  # 公表日が休場なら手前の終値
    ([(date(2026, 11, 10), 60459)], None),                  # 公表日以前の終値が無い
    ([(date(2026, 11, 6), 57435), (date(2026, 11, 5), 61078)], None),  # 最新の日足が公表日のまま
])
def test_credit_nowcast(price_log, expected):
    """公表値は週次で約1週遅れるので、日経平均の騰落から今日の値を推す。推せなければ None"""
    got = me.credit_nowcast(-21.08, "2026-11-06", price_log)
    assert (got is None) if expected is None else (got[0] == pytest.approx(expected[0]) and got[1] == expected[1])
