"""エクスポージャーガイドのテスト (issue #362)。"""

import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import exposure_guide  # noqa: E402
import market_state  # noqa: E402
import portfolio_shelve as ps  # noqa: E402

CONFIRMED = market_state.CONFIRMED_UPTREND
PRESSURE = market_state.UPTREND_UNDER_PRESSURE
CORRECTION = market_state.MARKET_IN_CORRECTION

ALL_UP = {"topix": CONFIRMED, "mothers": CONFIRMED, "nikkei225": CONFIRMED}


@pytest.mark.parametrize(
    "category_values, index_states, expected_state",
    [
        # 全額グロースで グロースが調整 → 調整相場
        ({"グロース": 1000.0}, {"mothers": CORRECTION, "topix": CONFIRMED}, CORRECTION),
        # 大型とグロース半々で 大型=confirmed(1.0) / グロース=correction(0.0) → 0.5 で圧力下
        (
            {"TOPIX": 500.0, "グロース": 500.0},
            {"topix": CONFIRMED, "mothers": CORRECTION},
            PRESSURE,
        ),
        # 「その他」は TOPIX 寄せ
        ({"その他": 100.0}, {"topix": CONFIRMED}, CONFIRMED),
        # ステート不明の指数は加重から除外し、残りだけで判定する
        (
            {"TOPIX": 900.0, "グロース": 100.0},
            {"topix": CONFIRMED},  # mothers 欠損
            CONFIRMED,
        ),
        # ノーポジ → None (呼び出し側が fallback へ)
        ({"TOPIX": 0.0}, ALL_UP, None),
        # 全カテゴリのステート不明 → None
        ({"TOPIX": 1000.0}, {}, None),
    ],
)
def test_weighted_state(category_values, index_states, expected_state):
    state, _ = exposure_guide.weighted_state(category_values, index_states)
    assert state == expected_state


@pytest.mark.parametrize(
    "index_states, expected",
    [
        # TOPIX とグロースの悪い方
        ({"topix": CONFIRMED, "mothers": CORRECTION}, CORRECTION),
        ({"topix": PRESSURE, "mothers": CONFIRMED}, PRESSURE),
        # 片方欠損ならもう片方を使う
        ({"topix": CONFIRMED}, CONFIRMED),
        # 両方欠損 (nikkei225 は fallback 対象外)
        ({"nikkei225": CONFIRMED}, None),
    ],
)
def test_fallback_state(index_states, expected):
    state, _ = exposure_guide.fallback_state(index_states)
    assert state == expected


@pytest.mark.parametrize(
    "credit, fng, expected",
    [
        # 未発動 (どちらも閾値未満)
        (-8.0, 50.0, (100, 120, [])),
        # 信用評価損益率が 0% に近い → 上限のみ -10
        (-2.0, 50.0, (100, 110, ["credit_eval_rate"])),
        # 日本版F&G が過熱 → 上限のみ -10
        (-8.0, 80.0, (100, 110, ["fng_jp"])),
        # 両方発動 → 重複適用
        (-2.0, 80.0, (100, 100, ["credit_eval_rate", "fng_jp"])),
        # 取得失敗 (None) は発動させない
        (None, None, (100, 120, [])),
    ],
)
def test_apply_modifiers(credit, fng, expected):
    modifiers = ps.EXPOSURE_DEFAULTS["modifiers"]
    assert exposure_guide.apply_modifiers([100, 120], credit, fng, modifiers) == expected


def test_apply_modifiers_does_not_invert_range():
    """削りすぎてもレンジは反転せず下限に丸める。"""
    modifiers = {
        "credit_eval_rate": {"threshold": -3.0, "penalty": 50},
        "fng_jp": {"threshold": 75.0, "penalty": 50},
    }
    lower, upper, applied = exposure_guide.apply_modifiers(
        [100, 120], -2.0, 80.0, modifiers
    )
    assert (lower, upper) == (100, 100)
    assert len(applied) == 2


@pytest.mark.parametrize(
    "total_value, expected_position, expected_deviation",
    [
        (26_500_000, "within", 0.0),   # 100% ちょうど (レンジ 100-120)
        (31_800_000, "within", 0.0),   # 120% ちょうど (上限は含む)
        (34_450_000, "over", 10.0),    # 130% → 上限 +10pt
        (21_200_000, "under", -20.0),  # 80% → 下限 -20pt
    ],
)
def test_evaluate_exposure_position(total_value, expected_position, expected_deviation):
    settings = dict(ps.EXPOSURE_DEFAULTS)
    result = exposure_guide.evaluate_exposure(
        total_value, {"TOPIX": total_value}, ALL_UP, -8.0, 50.0, settings
    )
    assert result["state"] == CONFIRMED
    assert result["position"] == expected_position
    assert result["deviation_pct"] == pytest.approx(expected_deviation)


@pytest.mark.parametrize(
    "index_states, base_amount, expect_state, expect_ratio",
    [
        # 市場ステート全欠損 → ガイドを出さない (例外は投げない)
        ({}, 26_500_000, False, True),
        # 基準運用額が未設定 → 比率は出さないがレンジは出す
        (ALL_UP, 0, True, False),
    ],
)
def test_evaluate_exposure_missing_inputs(
    index_states, base_amount, expect_state, expect_ratio
):
    settings = dict(ps.EXPOSURE_DEFAULTS)
    settings["base_amount"] = base_amount
    result = exposure_guide.evaluate_exposure(
        10_000_000, {"TOPIX": 10_000_000}, index_states, None, None, settings
    )
    assert (result["state"] is not None) == expect_state
    assert (result["ratio_pct"] is not None) == expect_ratio
    if not expect_state:
        # ステート不明ならレンジ・乖離も出さない
        assert result["range_lower"] is None
        assert result["deviation_pct"] is None
    if not expect_ratio:
        # 基準運用額が無いと乖離は判定できない
        assert result["deviation_pct"] is None
        assert result["position"] is None


@pytest.mark.parametrize(
    "kind, date_str, expected",
    [
        # 信用評価損益率は週次なので 10 日まで許容 (当日一致を求めると永久に記録できない)
        ("credit_balance", "2026-08-21", True),
        ("credit_balance", "2026-08-15", False),
        # 日次指標は 3 日 (連休を考慮)
        ("fng_jp", "2026-08-28", True),
        ("fng_jp", "2026-08-20", False),
        (None, None, False),
    ],
)
def test_is_fresh(kind, date_str, expected):
    if kind is None:
        assert exposure_guide._is_fresh(None, "fng_jp", today=date(2026, 8, 29)) is False
        return
    assert (
        exposure_guide._is_fresh(date_str, kind, today=date(2026, 8, 29)) is expected
    )


def test_exposure_settings_roundtrip_and_validation(tmp_path):
    db_path = str(tmp_path / "portfolio_shelve")

    # 未保存でもデフォルトが返る
    settings = ps.get_exposure_settings(db_path=db_path)
    assert settings["base_amount"] == ps.EXPOSURE_DEFAULTS["base_amount"]

    # base_amount だけ更新してもレンジ等は引き継がれる
    ps.set_exposure_settings({"base_amount": 30_000_000}, db_path=db_path)
    saved = ps.get_exposure_settings(db_path=db_path)
    assert saved["base_amount"] == 30_000_000
    assert saved["ranges"] == ps.EXPOSURE_DEFAULTS["ranges"]

    with pytest.raises(ValueError):
        ps.set_exposure_settings({"base_amount": 0}, db_path=db_path)
    with pytest.raises(ValueError):
        ps.set_exposure_settings(
            {"ranges": {CONFIRMED: [120, 100]}}, db_path=db_path
        )


def _stub_entry(entry):
    """build_daily_entry の差し替え用スタブを返す。"""
    def _build(**kwargs):
        return entry
    return _build


def test_daily_log_overwrites_same_date(tmp_path, monkeypatch):
    """同一日付の再実行は上書きし、履歴を重複させない。"""
    log_path = str(tmp_path / "exposure_log.json")
    monkeypatch.setattr(exposure_guide, "EXPOSURE_LOG_PATH", log_path)

    entries = [
        {"date": "2026-08-28", "ratio_pct": 80.0, "total_value": 1, "state": CONFIRMED},
        {"date": "2026-08-29", "ratio_pct": 85.0, "total_value": 2, "state": CONFIRMED},
        {"date": "2026-08-29", "ratio_pct": 90.0, "total_value": 3, "state": CONFIRMED},
    ]
    for entry in entries:
        monkeypatch.setattr(
            exposure_guide, "build_daily_entry", _stub_entry(entry)
        )
        exposure_guide.record_daily_log()

    with open(log_path, encoding="utf-8") as f:
        history = json.load(f)["history"]
    assert [h["date"] for h in history] == ["2026-08-28", "2026-08-29"]
    assert history[-1]["ratio_pct"] == 90.0  # 後勝ち
