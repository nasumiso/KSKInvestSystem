"""price.py の計算関数テスト"""

from datetime import date
import pytest

import price


# ==================================================
# calc_sell_pressure_ratio
# ==================================================
class TestCalcSellPressureRatio:
    """売り圧力レシオの計算テスト"""

    def _make_price_list(self, count=25):
        """テスト用の価格データを生成する
        各要素: (日付, 始値, 高値, 安値, 終値, 出来高)
        日付若い順（最新が先頭）
        """
        base_price = 1000
        price_list = []
        for i in range(count):
            open_p = base_price + i * 2
            high_p = open_p + 20
            low_p = open_p - 10
            close_p = open_p + 5
            volume = 100000 + i * 1000
            price_list.append((f"2025/01/{count - i:02d}", open_p, high_p, low_p, close_p, volume))
        return price_list

    def test_returns_five_values(self):
        """戻り値が5要素であること"""
        price_list = self._make_price_list()
        result = price.calc_sell_pressure_ratio(price_list)
        assert len(result) == 5

    def test_ratio_range(self):
        """レシオが0〜100の範囲"""
        price_list = self._make_price_list()
        result = price.calc_sell_pressure_ratio(price_list)
        sp20, sp5 = result[0], result[1]
        assert 0 <= sp20 <= 100
        assert 0 <= sp5 <= 100

    def test_volatility_nonnegative(self):
        """ボラティリティが非負"""
        price_list = self._make_price_list()
        result = price.calc_sell_pressure_ratio(price_list)
        vol20, vol5 = result[3], result[4]
        assert vol20 >= 0
        assert vol5 >= 0


# ==================================================
# parse_date_str
# ==================================================
class TestParseDateStr:
    """日付文字列パースのテスト"""

    def test_japanese_format(self):
        """YYYY年M月D日 形式"""
        result = price.parse_date_str("2025年1月15日")
        assert result == date(2025, 1, 15)

    def test_slash_format(self):
        """YYYY/MM/DD 形式"""
        result = price.parse_date_str("2025/06/10")
        assert result == date(2025, 6, 10)

    def test_hyphen_format(self):
        """YYYY-MM-DD 形式"""
        result = price.parse_date_str("2025-12-31")
        assert result == date(2025, 12, 31)

    def test_empty_string(self):
        """空文字列"""
        assert price.parse_date_str("") is None

    def test_none(self):
        """None"""
        assert price.parse_date_str(None) is None

    def test_invalid_string(self):
        """無効な文字列"""
        assert price.parse_date_str("hoge") is None

    def test_embedded_japanese(self):
        """周辺テキスト付きの日本語日付"""
        result = price.parse_date_str("決算日: 2025年 3月 1日 発表")
        assert result == date(2025, 3, 1)
