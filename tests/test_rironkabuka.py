"""rironkabuka.py の計算関数テスト"""

import pytest
import rironkabuka


# ==================================================
# calc_theory_price
# ==================================================
class TestCalcTheoryPrice:
    """はっしゃん式理論株価の計算テスト"""

    def test_normal_case(self):
        """正常値での理論株価計算"""
        # BPS=1000, EPS=100, 自己資本比率=50%, price=1500
        result = rironkabuka.calc_theory_price(1000, 100, 50, 1500)
        theory, up, down, preceding = result
        # 理論株価は正の整数
        assert theory > 0
        assert up > theory  # 上限 > 理論株価
        assert down < theory  # 下限 < 理論株価
        assert preceding is None  # preceding_eps未指定

    def test_bps_zero(self):
        """BPS=0 の場合"""
        result = rironkabuka.calc_theory_price(0, 100, 50, 1000)
        theory, up, down, preceding = result
        assert theory >= 0
        assert up >= 0
        assert down >= 0

    def test_high_equity_ratio(self):
        """高自己資本比率（80%超）"""
        result = rironkabuka.calc_theory_price(2000, 200, 85, 3000)
        theory, up, down, preceding = result
        assert theory > 0
        assert down > 0  # 解散価値は正

    def test_low_equity_ratio(self):
        """低自己資本比率（10%未満）"""
        result = rironkabuka.calc_theory_price(500, 50, 5, 800)
        theory, up, down, preceding = result
        assert theory >= 0

    def test_with_preceding_eps(self):
        """先行EPS指定ありの場合"""
        result = rironkabuka.calc_theory_price(1000, 100, 50, 1500, preceding_eps=120)
        theory, up, down, preceding = result
        assert preceding is not None
        assert preceding >= 0

    def test_negative_eps(self):
        """赤字（EPS < 0）の場合"""
        result = rironkabuka.calc_theory_price(1000, -50, 50, 500)
        theory, up, down, preceding = result
        assert theory >= 0  # max(0) で0以上が保証される

    def test_no_price(self):
        """price=0 の場合（リスク補正なし）"""
        result = rironkabuka.calc_theory_price(1000, 100, 50, 0)
        theory, up, down, preceding = result
        assert theory > 0


# ==================================================
# get_rironkabuka_kairi_fromprice
# ==================================================
class TestGetRironkabukaKairiFromprice:
    """理論株価乖離率の計算テスト"""

    def test_normal(self):
        """正常な乖離率計算"""
        kairi, kairi_up, kairi_down, kairi_preceding = (
            rironkabuka.get_rironkabuka_kairi_fromprice(1500, 2000, 1000, 1600, 1200)
        )
        # 理論株価1500 vs 現在株価1200 → 乖離率 = (1500-1200)/1200*100 = 25%
        assert pytest.approx(kairi, rel=0.01) == 25.0
        assert kairi_up > kairi  # 上限乖離 > 理論乖離
        assert kairi_down < kairi  # 下限乖離 < 理論乖離

    def test_no_price(self):
        """price=0 の場合はすべて0"""
        result = rironkabuka.get_rironkabuka_kairi_fromprice(1500, 2000, 1000, 1600, 0)
        assert result == (0, 0, 0, 0)

    def test_no_preceding(self):
        """先行理論株価が None"""
        kairi, kairi_up, kairi_down, kairi_preceding = (
            rironkabuka.get_rironkabuka_kairi_fromprice(1000, 1500, 800, None, 1000)
        )
        assert kairi_preceding is None

    def test_preceding_zero(self):
        """先行理論株価が 0"""
        kairi, kairi_up, kairi_down, kairi_preceding = (
            rironkabuka.get_rironkabuka_kairi_fromprice(1000, 1500, 800, 0, 1000)
        )
        assert kairi_preceding is None


# ==================================================
# get_preceding_eps
# ==================================================
class TestGetPrecedingEps:
    """予想EPS計算のテスト"""

    def test_normal(self):
        """正常な進捗率ベース予想EPS"""
        # eps=100, quarter=2, profit進捗率50%, profit_pre前期80%
        # progress_predict = 50 + (100 - 80) = 70
        # preceding_eps = 100 * 70 / 100 = 70
        result = rironkabuka.get_preceding_eps(100, 2, 50, 80)
        assert result == 70.0

    def test_profit_zero(self):
        """利益進捗率が 0 → None"""
        result = rironkabuka.get_preceding_eps(100, 1, 0, 80)
        assert result is None

    def test_profit_pre_zero(self):
        """前期利益進捗率が 0 → None"""
        result = rironkabuka.get_preceding_eps(100, 1, 50, 0)
        assert result is None

    def test_both_none(self):
        """両方 None"""
        result = rironkabuka.get_preceding_eps(100, 1, None, None)
        assert result is None

    def test_high_progress(self):
        """高進捗率の場合"""
        # eps=200, profit=90%, profit_pre=75%
        # progress_predict = 90 + (100 - 75) = 115
        # preceding_eps = 200 * 115 / 100 = 230
        result = rironkabuka.get_preceding_eps(200, 3, 90, 75)
        assert result == 230.0
