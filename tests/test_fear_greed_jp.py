"""日本市場版 Fear & Greed Index のテスト (issue #212)。"""

import random

import pytest

import fear_greed_jp as fg


# --- normalize_percentile: 境界・外れ値頑健性 (現行の主正規化) -------------
@pytest.mark.parametrize("value,history,expected", [
    (50, [], 50.0),                       # 履歴空 → 中立
    (5, [10, 20, 30, 40], 0.0),           # 全要素より小 → 0
    (50, [10, 20, 30, 40], 100.0),        # 全要素より大 → 100
    (25, [10, 20, 30, 40], 50.0),         # 中央 (2件が下) → 50
    (20, [10, 20, 30, 40], 37.5),         # 中央順位: below1 + equal0.5 = 1.5/4
    # 外れ値頑健性: 極端な1点があっても percentile は圧縮されない
    # (min-max なら value=11 は (11-10)/(1000-10)≈0.1 だが percentile は 75)
    (11, [10, 12, 13, 1000], 25.0),       # 11 より下は 10 のみ → 1/4 = 25
])
def test_normalize_percentile(value, history, expected):
    assert fg.normalize_percentile(value, history) == expected


# --- normalize_min_max: 境界 (旧方式、参考用に残置) -----------------------
@pytest.mark.parametrize("value,history,expected", [
    (50, [], 50.0),            # 履歴空 → 中立
    (5, [5, 5, 5], 50.0),      # 最大最小一致 → 中立
    (100, [0, 100], 100.0),    # 上限
    (0, [0, 100], 0.0),        # 下限
    (50, [0, 100], 50.0),      # 中央
    (200, [0, 100], 100.0),    # 上方クリップ
    (-50, [0, 100], 0.0),      # 下方クリップ
])
def test_normalize_min_max(value, history, expected):
    assert fg.normalize_min_max(value, history) == expected


# --- rating 境界 ----------------------------------------------------------
@pytest.mark.parametrize("score,rating", [
    (24, "Extreme Fear"), (25, "Fear"), (44, "Fear"), (45, "Neutral"),
    (54, "Neutral"), (55, "Greed"), (74, "Greed"), (75, "Extreme Greed"),
    (100, "Extreme Greed"),
])
def test_rating_boundaries(score, rating):
    assert fg._rating(score) == rating


# --- volatility 方向反転 (VI 高い=Fear=低スコア) + raw 値 ------------------
def test_volatility_inverted():
    # VI 最大値(最新)→ 反転で score 0、最小値→ score 100。raw は VI 実値。
    hist_high = [{"nikkei_vi": v} for v in [10, 20, 40]]   # 最新=最大
    hist_low = [{"nikkei_vi": v} for v in [40, 20, 10]]    # 最新=最小
    score_h, raw_h = fg.compute_component_volatility(hist_high)
    score_l, raw_l = fg.compute_component_volatility(hist_low)
    assert (score_h, raw_h) == (0.0, 40)
    assert (score_l, raw_l) == (100.0, 10)


# --- 合成: None 成分は除外して残りで平均 ---------------------------------
@pytest.mark.parametrize("breadth,vi,expect_components,expect_score", [
    # 全成分そろう (momentum はデータ不足で None になりうるので strength/breadth/vol で検証)
    (
        # strength: 新高値-新安値, breadth: 値上がり-値下がり
        [{"nikkei_close": 100, "new_high": 0, "new_low": 10, "advances": 0, "declines": 10},
         {"nikkei_close": 100, "new_high": 10, "new_low": 0, "advances": 10, "declines": 0}],
        [{"nikkei_vi": 30}, {"nikkei_vi": 10}],  # VI 最新=最小 → vol=100
        # momentum は close 一定で None になりうる。breadth (McClellan 累積) は
        # EMA の立ち上がり分 (_MCO_WARMUP 本) が要るので2行では None
        {"strength", "volatility"},
        None,  # score は計算可能であればよい (個別値は別アサート)
    ),
])
def test_compose_excludes_none(breadth, vi, expect_components, expect_score):
    result = fg.compute_fear_greed_jp(breadth, vi)
    assert result is not None
    # None でない成分が平均に使われている。各成分は {"score","raw"} dict。
    valid = {k for k, v in result["components"].items() if v is not None}
    assert expect_components <= valid
    for k in valid:
        assert "score" in result["components"][k]
        assert "raw" in result["components"][k]
    assert 0.0 <= result["score"] <= 100.0
    assert result["rating"] in (
        "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")


def test_compose_all_none_returns_none():
    # データ1点ずつ (len<2) で全成分 None → None
    assert fg.compute_fear_greed_jp(
        [{"nikkei_close": 100, "new_high": 1, "new_low": 1, "advances": 1, "declines": 1}],
        [{"nikkei_vi": 20}],
    ) is None


# --- breadth: McClellan 累積型 (issue #524) -------------------------------
def _flat_rows(n, adv=900, dec=700):
    return [{"advances": adv, "declines": dec} for _ in range(n)]


@pytest.mark.parametrize("n,expect_none", [
    (fg._MCO_WARMUP + 1, True),    # 立ち上がり分 + 1本では正規化の履歴が無い
    (fg._MCO_WARMUP + 2, False),
])
def test_breadth_needs_warmup(n, expect_none):
    assert (fg.compute_component_breadth(_flat_rows(n)) is None) is expect_none


def test_mcclellan_constant_breadth_stays_flat():
    # 差が一定なら EMA19 と EMA39 は一致し続け、Oscillator=0 で累積値も 0 のまま
    assert fg.mcclellan_summation([900] * 50, [700] * 50) == [0.0] * 50


def test_breadth_one_day_crash_moves_less_than_daily_diff():
    # 日々ばらつく300日のあと1日だけ大きく値下がり。旧方式 (1日差分) は最下位近くまで
    # 落ちるが、累積型は Oscillator 1日分しか動かないので下げ幅が小さい
    rng = random.Random(0)
    calm = []
    for _ in range(300):
        adv = min(max(int(800 + rng.gauss(0, 250)), 100), 1500)
        calm.append({"advances": adv, "declines": 1600 - adv})
    crash = calm + [{"advances": 100, "declines": 1500}]

    def drop(fn):
        return fn(calm)[0] - fn(crash)[0]

    assert drop(fg.compute_component_breadth_daily) > 50
    assert drop(fg.compute_component_breadth) < 10
