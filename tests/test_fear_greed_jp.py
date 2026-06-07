"""日本市場版 Fear & Greed Index のテスト (issue #212)。"""

import pytest

import fear_greed_jp as fg


# --- normalize_min_max: 境界 ---------------------------------------------
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


# --- volatility 方向反転 (VI 高い=Fear=低スコア) --------------------------
def test_volatility_inverted():
    # VI 最大値(最新)→ 反転で 0 付近、最小値→ 100 付近
    hist_high = [{"nikkei_vi": v} for v in [10, 20, 40]]   # 最新=最大
    hist_low = [{"nikkei_vi": v} for v in [40, 20, 10]]    # 最新=最小
    assert fg.compute_component_volatility(hist_high) == 0.0
    assert fg.compute_component_volatility(hist_low) == 100.0


# --- 合成: None 成分は除外して残りで平均 ---------------------------------
@pytest.mark.parametrize("breadth,vi,expect_components,expect_score", [
    # 全成分そろう (momentum はデータ不足で None になりうるので strength/breadth/vol で検証)
    (
        # strength: 新高値-新安値, breadth: 値上がり-値下がり
        [{"nikkei_close": 100, "new_high": 0, "new_low": 10, "advances": 0, "declines": 10},
         {"nikkei_close": 100, "new_high": 10, "new_low": 0, "advances": 10, "declines": 0}],
        [{"nikkei_vi": 30}, {"nikkei_vi": 10}],  # VI 最新=最小 → vol=100
        {"strength", "breadth", "volatility"},   # momentum は close 一定で None になりうる
        None,  # score は計算可能であればよい (個別値は別アサート)
    ),
])
def test_compose_excludes_none(breadth, vi, expect_components, expect_score):
    result = fg.compute_fear_greed_jp(breadth, vi)
    assert result is not None
    # None でない成分が平均に使われている
    valid = {k for k, v in result["components"].items() if v is not None}
    assert expect_components <= valid
    assert 0.0 <= result["score"] <= 100.0
    assert result["rating"] in (
        "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")


def test_compose_all_none_returns_none():
    # データ1点ずつ (len<2) で全成分 None → None
    assert fg.compute_fear_greed_jp(
        [{"nikkei_close": 100, "new_high": 1, "new_low": 1, "advances": 1, "declines": 1}],
        [{"nikkei_vi": 20}],
    ) is None
