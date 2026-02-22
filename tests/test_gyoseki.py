"""gyoseki.py の計算関数テスト"""

import pytest
import gyoseki


# ==================================================
# calc_growth_rate
# ==================================================
class TestCalcGrowthRate:
    """成長率計算のテスト（-100~+100%）"""

    def test_positive_growth(self):
        """正の成長"""
        # 100 → 120 = +20%
        assert gyoseki.calc_growth_rate(100, 120) == 20

    def test_negative_growth(self):
        """マイナス成長"""
        # 100 → 80 = -20%
        assert gyoseki.calc_growth_rate(100, 80) == -20

    def test_no_change(self):
        """変化なし"""
        assert gyoseki.calc_growth_rate(100, 100) == 0

    def test_zero_both(self):
        """ゼロ同士"""
        assert gyoseki.calc_growth_rate(0, 0) == 0

    def test_from_negative(self):
        """赤字からの成長（林式計算）"""
        result = gyoseki.calc_growth_rate(-100, 50)
        # (50 - (-100)) / ((|50| + |-100|) * 0.5) = 150 / 75 = 2.0
        # return int(100 * 2.0 - 100) = 100
        assert result == 100

    def test_large_growth(self):
        """大幅成長"""
        # 100 → 300 = +200%
        assert gyoseki.calc_growth_rate(100, 300) == 200


# ==================================================
# calc_growth_rate2
# ==================================================
class TestCalcGrowthRate2:
    """赤字考慮成長率計算のテスト"""

    def test_positive_to_positive(self):
        """正→正: 単純比率"""
        # 100 → 120 = 1.2
        assert gyoseki.calc_growth_rate2(100, 120) == pytest.approx(1.2)

    def test_both_zero(self):
        """ゼロ同士"""
        assert gyoseki.calc_growth_rate2(0, 0) == 1.0

    def test_negative_to_positive(self):
        """赤字→黒字"""
        result = gyoseki.calc_growth_rate2(-100, 50)
        # (50 - (-100)) / ((|50| + |-100|) * 0.5) = 150 / 75 = 2.0
        assert result == pytest.approx(2.0)

    def test_positive_to_negative(self):
        """黒字→赤字"""
        result = gyoseki.calc_growth_rate2(100, -50)
        # (-50 - 100) / ((|-50| + |100|) * 0.5) = -150 / 75 = -2.0
        assert result == pytest.approx(-2.0)


# ==================================================
# calc_cagr
# ==================================================
class TestCalcCagr:
    """年平均成長率（CAGR）のテスト"""

    def test_normal(self):
        """正常な成長"""
        # [100, 110, 121] → 2年で21%成長 → CAGR ≈ 10%
        result = gyoseki.calc_cagr([100, 110, 121])
        assert result == pytest.approx(0.1, abs=0.001)

    def test_single_value(self):
        """1要素 → 0"""
        assert gyoseki.calc_cagr([100]) == 0

    def test_empty(self):
        """空リスト → 0"""
        assert gyoseki.calc_cagr([]) == 0

    def test_first_zero(self):
        """初期値ゼロ → 0"""
        assert gyoseki.calc_cagr([0, 100, 200]) == 0

    def test_first_negative(self):
        """初期値マイナス → 0"""
        assert gyoseki.calc_cagr([-50, 100, 200]) == 0

    def test_constant(self):
        """変化なし"""
        result = gyoseki.calc_cagr([100, 100, 100])
        assert result == pytest.approx(0.0)

    def test_decline(self):
        """減少トレンド"""
        # [100, 90, 81] → 2年で-19% → CAGR ≈ -10%
        result = gyoseki.calc_cagr([100, 90, 81])
        assert result == pytest.approx(-0.1, abs=0.001)


# ==================================================
# calc_gyoseki_score
# ==================================================
class TestCalcGyosekiScore:
    """業績スコアリングのテスト"""

    def test_empty_tables(self):
        """空テーブルのとき"""
        result = gyoseki.calc_gyoseki_score({})
        assert result == 20  # check_table 失敗時のデフォルト

    def test_insufficient_data(self):
        """データ不足のとき"""
        tables = {"gyoseki_current": [], "gyoseki_quarter": []}
        result = gyoseki.calc_gyoseki_score(tables)
        assert result == 20

    def test_with_sample_data(self):
        """サンプルデータでスコア計算が正常終了すること"""
        # 通期テーブル: [決算期, 売上高, 営業益, 経常益, 最終益, 一株益, 一株配]
        table_current = [
            ["2020.03", 10000, 1000, 1100, 800, 80, 20],
            ["2021.03", 11000, 1200, 1300, 900, 90, 22],
            ["2022.03", 12000, 1400, 1500, 1000, 100, 25],
            ["2023.03", 13000, 1600, 1700, 1100, 110, 28],
            ["2024.03", 14000, 1800, 1900, 1200, 120, 30],
            ["予2025.03", 15000, 2000, 2100, 1400, 140, 35],
            # 前期比行
            ["前期比", "+7.1", "+11.1", "+10.5", "+16.7", "+16.7", "+16.7"],
        ]
        # 四半期テーブル: 9行（今年4Q＋前年4Q＋前年同期比）
        table_quarter = [
            ["23.04-06", 3000, 400, 420, 280, 28, 5.0],  # 前年1Q
            ["23.07-09", 3100, 420, 440, 290, 29, 5.1],  # 前年2Q
            ["23.10-12", 3200, 440, 460, 300, 30, 5.2],  # 前年3Q
            ["24.01-03", 3300, 460, 480, 310, 31, 5.3],  # 前年4Q
            ["24.04-06", 3500, 500, 520, 350, 35, 5.5],  # 今年1Q
            ["24.07-09", 3600, 520, 540, 360, 36, 5.6],  # 今年2Q
            ["24.10-12", 3700, 540, 560, 370, 37, 5.7],  # 今年3Q
            ["25.01-03", 3800, 560, 580, 380, 38, 5.8],  # 今年4Q
            # 前年同期比行
            ["前年同期比", "+15.2", "+21.7", "+20.8", "+22.6", "+22.6", "+9.4"],
        ]
        tables = {
            "gyoseki_current": table_current,
            "gyoseki_quarter": table_quarter,
        }
        result = gyoseki.calc_gyoseki_score(tables)
        # スコアは0〜100の範囲になる想定
        assert isinstance(result, (int, float))
        assert result >= 0
