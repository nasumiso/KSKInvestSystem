"""エクスポージャーガイドのテスト (issue #362 / #480)。"""

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


def test_exposure_settings_merges_nested_dicts(tmp_path):
    """1項目だけ更新しても他のカスタム値が消えない (PR #423 レビュー指摘)。

    浅い update だと ranges / modifiers 全体が差し替わり、未指定項目が
    デフォルトへ巻き戻る (docstring の「未指定は現在値を引き継ぐ」に反する)。
    """
    db_path = str(tmp_path / "portfolio_shelve")

    ps.set_exposure_settings({"ranges": {PRESSURE: [85, 95]}}, db_path=db_path)
    ps.set_exposure_settings({"ranges": {CONFIRMED: [110, 130]}}, db_path=db_path)
    ranges = ps.get_exposure_settings(db_path=db_path)["ranges"]
    assert ranges[PRESSURE] == [85, 95]      # 先に入れたカスタム値が残る
    assert ranges[CONFIRMED] == [110, 130]

    # modifiers は threshold だけ更新しても penalty が残る
    ps.set_exposure_settings(
        {"modifiers": {"fng_jp": {"threshold": 70.0}}}, db_path=db_path
    )
    modifiers = ps.get_exposure_settings(db_path=db_path)["modifiers"]
    assert modifiers["fng_jp"] == {"threshold": 70.0, "penalty": 10}
    assert modifiers["credit_eval_rate"] == {"threshold": -3.0, "penalty": 10}


@pytest.mark.parametrize(
    "payload",
    [
        {"base_amount": float("nan")},
        {"base_amount": float("inf")},
        {"ranges": {CONFIRMED: [float("nan"), 120]}},
        {"ranges": {CONFIRMED: [100, float("inf")]}},
        {"modifiers": {"fng_jp": {"threshold": float("nan"), "penalty": 10}}},
        {"modifiers": {"fng_jp": {"threshold": 75.0, "penalty": float("inf")}}},
    ],
)
def test_exposure_settings_rejects_non_finite_values(tmp_path, payload):
    """nan/inf を拒否する (PR #423 レビュー指摘)。

    <= 0 比較は nan/inf に対して False を返すため、既存の非負チェックだけでは
    素通りする。保存後は int() 変換 (CLI 表示・万円換算) で例外になり、
    以後この設定を読む WebApp も含めて壊れる。実データで再現し、誤って
    本番 DB に nan を書き込んでしまったため math.isfinite() で保存前に拒否する。
    """
    db_path = str(tmp_path / "portfolio_shelve")
    with pytest.raises(ValueError):
        ps.set_exposure_settings(payload, db_path=db_path)
    # 拒否後も設定は変化しない (デフォルトのまま)
    assert ps.get_exposure_settings(db_path=db_path)["base_amount"] == \
        ps.EXPOSURE_DEFAULTS["base_amount"]


@pytest.mark.parametrize(
    "topix_date, mothers_date, expected_indexes",
    [
        # 両方新しい → 両方採用
        ("2026-08-28", "2026-08-28", {"topix", "mothers"}),
        # topix だけ取得失敗で据え置き → topix のみ除外し mothers は生かす
        ("2026-08-10", "2026-08-28", {"mothers"}),
        # 両方古い → 全除外 (ガイドを出さない)
        ("2026-08-10", "2026-08-10", set()),
    ],
)
def test_read_index_states_uses_per_index_date(
    topix_date, mothers_date, expected_indexes
):
    """指数ごとの最新日足で鮮度を判定する (PR #423 レビュー指摘)。

    market_db のファイル mtime は指数取得が失敗した日も更新される
    (失敗時は前日データを保持したまま DB 全体を保存するため) ので、
    mtime では古いステートを鮮度内と誤認する。
    """
    market_db = {
        "topix": {"market_state": CORRECTION,
                  "price_log": [(date.fromisoformat(topix_date), 4000)]},
        "mothers": {"market_state": CONFIRMED,
                    "price_log": [(date.fromisoformat(mothers_date), 700)]},
    }
    states, _ = exposure_guide.read_index_states(market_db, today=date(2026, 8, 29))
    assert set(states) == expected_indexes


def test_read_index_states_reads_newest_end_of_price_log():
    """price_log は日付降順なので [0] が最新 (末尾は1ヶ月以上前になりうる)。"""
    market_db = {
        "topix": {
            "market_state": CORRECTION,
            "price_log": [
                (date(2026, 8, 28), 4000),  # 最新
                (date(2026, 7, 16), 3900),  # 古い端
            ],
        }
    }
    states, dates = exposure_guide.read_index_states(
        market_db, today=date(2026, 8, 29)
    )
    assert states == {"topix": CORRECTION}
    assert dates["topix"] == "2026-08-28"


def test_read_index_states_dates_are_per_index():
    """指数ごとの日付を個別に返す (単一の代表日付に潰さない)。

    採用した指数の最大日付だけを1つ返す設計だと、一部の指数だけ取得失敗で
    古いまま残っている日に、その指数がどの日付に基づくか失われる
    (PR #423 レビュー指摘)。
    """
    market_db = {
        "topix": {"market_state": CORRECTION,
                  "price_log": [(date(2026, 8, 28), 4000)]},
        "mothers": {"market_state": CONFIRMED,
                    "price_log": [(date(2026, 8, 27), 700)]},  # topixより1日古いが鮮度内
    }
    states, dates = exposure_guide.read_index_states(
        market_db, today=date(2026, 8, 29)
    )
    assert set(states) == {"topix", "mothers"}  # 両方鮮度内 (3日以内)
    assert dates == {"topix": "2026-08-28", "mothers": "2026-08-27"}


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
