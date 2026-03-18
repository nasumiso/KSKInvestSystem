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
        result = rironkabuka.calc_theory_price(1000, 100, 50, 1500)
        theory, up, down, preceding = result
        assert theory > 0
        assert up > theory
        assert down < theory
        assert preceding is None

    def test_edge_cases(self):
        """エッジケース: BPS=0, 赤字EPS, price=0"""
        # BPS=0
        theory, up, down, _ = rironkabuka.calc_theory_price(0, 100, 50, 1000)
        assert theory >= 0
        # 赤字EPS
        theory, _, _, _ = rironkabuka.calc_theory_price(1000, -50, 50, 500)
        assert theory >= 0
        # price=0
        theory, _, _, _ = rironkabuka.calc_theory_price(1000, 100, 50, 0)
        assert theory > 0

    def test_with_preceding_eps(self):
        """先行EPS指定ありの場合"""
        result = rironkabuka.calc_theory_price(1000, 100, 50, 1500, preceding_eps=120)
        _, _, _, preceding = result
        assert preceding is not None
        assert preceding >= 0


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
        assert pytest.approx(kairi, rel=0.01) == 25.0
        assert kairi_up > kairi
        assert kairi_down < kairi

    def test_missing_values(self):
        """price=0, preceding=None/0 の各欠損パターン"""
        assert rironkabuka.get_rironkabuka_kairi_fromprice(1500, 2000, 1000, 1600, 0) == (0, 0, 0, 0)
        _, _, _, kp = rironkabuka.get_rironkabuka_kairi_fromprice(1000, 1500, 800, None, 1000)
        assert kp is None
        _, _, _, kp = rironkabuka.get_rironkabuka_kairi_fromprice(1000, 1500, 800, 0, 1000)
        assert kp is None


# ==================================================
# get_preceding_eps
# ==================================================
class TestGetPrecedingEps:
    """予想EPS計算のテスト"""

    def test_normal(self):
        """正常な進捗率ベース予想EPS"""
        result = rironkabuka.get_preceding_eps(100, 2, 50, 80)
        assert result == 70.0

    def test_invalid_inputs_return_none(self):
        """利益進捗率が0/None → None"""
        assert rironkabuka.get_preceding_eps(100, 1, 0, 80) is None
        assert rironkabuka.get_preceding_eps(100, 1, 50, 0) is None
        assert rironkabuka.get_preceding_eps(100, 1, None, None) is None
