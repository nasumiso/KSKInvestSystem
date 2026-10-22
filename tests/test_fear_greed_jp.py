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


# --- volatility: 50日平均との乖離 + 方向反転 (issue #524) ----------------
@pytest.mark.parametrize("latest,expected", [
    (40.0, 0.0),     # ふだん (50日平均) よりずっと高い → 最も Fear
    (12.0, 100.0),   # ふだんより低い → 最も Greed
])
def test_volatility_vs_ma50_inverted(latest, expected):
    rng = random.Random(0)
    hist = [{"nikkei_vi": 20 + rng.uniform(-2, 2)} for _ in range(80)]
    score, raw = fg.compute_component_volatility(hist + [{"nikkei_vi": latest}])
    assert (score, raw) == (expected, latest)


def test_volatility_level_alone_does_not_mean_fear():
    # VI が何ヶ月も 30 で高止まりしているだけなら、水準 (旧方式) は Fear 側だが
    # 50日平均との乖離 (新方式) は中立付近にとどまる
    rng = random.Random(1)
    low = [{"nikkei_vi": 15 + rng.uniform(-1, 1)} for _ in range(200)]
    high = [{"nikkei_vi": 30 + rng.uniform(-1, 1)} for _ in range(100)]
    hist = low + high
    assert fg.compute_component_volatility_level(hist)[0] < 40
    assert 20 < fg.compute_component_volatility(hist)[0] < 80


# --- safe haven: 株と債券の20日騰落率の差 (issue #524) --------------------
@pytest.mark.parametrize("last_close,last_yield,expect_high", [
    (110.0, 1.0, True),    # 株が上がり金利は横ばい → 安全資産を求めていない = Greed
    (90.0, 0.7, False),    # 株が下がり金利も下がる (債券高) → Fear
])
def test_safe_haven_direction(last_close, last_yield, expect_high):
    rng = random.Random(2)
    breadth, jgb = [], []
    for i in range(60):
        d = "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)
        breadth.append({"date": d, "nikkei_close": 100 + rng.uniform(-1, 1)})
        jgb.append({"date": d, "jgb10": 1.0 + rng.uniform(-0.01, 0.01)})
    breadth.append({"date": "2026-04-01", "nikkei_close": last_close})
    jgb.append({"date": "2026-04-01", "jgb10": last_yield})
    score, _ = fg.compute_component_safe_haven(breadth, jgb)
    assert (score > 90) is expect_high and (score < 10) is (not expect_high)


@pytest.mark.parametrize("last_jgb_date,expect_used", [
    ("2026-04-01", True),    # 基準日と同じ日まで金利がある
    ("2026-03-20", False),   # 当月分が更新されず古い → 安全資産は外す
])
def test_safe_haven_dropped_when_jgb_is_stale(last_jgb_date, expect_used):
    breadth = [{"date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28), "nikkei_close": 100 + i}
               for i in range(60)] + [{"date": "2026-04-01", "nikkei_close": 200}]
    jgb = [{"date": b["date"], "jgb10": 1.0} for b in breadth if b["date"] <= last_jgb_date]
    result = fg.compute_fear_greed_jp(breadth, [], jgb)
    assert (result["components"]["safe_haven"] is not None) is expect_used


# --- 合成: None 成分は除外して残りで平均 ---------------------------------
@pytest.mark.parametrize("breadth,vi,expect_components,expect_score", [
    # 全成分そろう (momentum はデータ不足で None になりうるので strength/breadth/vol で検証)
    (
        # strength: 新高値-新安値, breadth: 値上がり-値下がり
        [{"nikkei_close": 100, "new_high": 0, "new_low": 10, "advances": 0, "declines": 10},
         {"nikkei_close": 100, "new_high": 10, "new_low": 0, "advances": 10, "declines": 0}],
        [{"nikkei_vi": 30}] * 60 + [{"nikkei_vi": 10}],  # VI 最新が50日平均より低い → Greed
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
