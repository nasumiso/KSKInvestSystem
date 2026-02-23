"""price.py の計算関数テスト"""

from datetime import date
import json
import os
import tempfile
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


# ==================================================
# _convert_df_to_price_list
# ==================================================
class TestConvertDfToPriceList:
    """yfinance DataFrame変換テスト"""

    def _make_df(self, rows=5):
        """テスト用のDataFrameを生成する（yfinance history互換）"""
        import pandas as pd

        dates = pd.bdate_range(end="2025-01-31", periods=rows)
        data = {
            "Open": [1000 + i * 10 for i in range(rows)],
            "High": [1020 + i * 10 for i in range(rows)],
            "Low": [990 + i * 10 for i in range(rows)],
            "Close": [1005 + i * 10 for i in range(rows)],
            "Adj Close": [1003 + i * 10 for i in range(rows)],
            "Volume": [100000 + i * 1000 for i in range(rows)],
        }
        return pd.DataFrame(data, index=dates)

    def test_output_length(self):
        """出力のレコード数がDataFrameの行数と一致"""
        df = self._make_df(5)
        result = price._convert_df_to_price_list(df)
        assert len(result) == 5

    def test_seven_elements(self):
        """各レコードが7要素（date, open, high, low, close, volume, adj_close）"""
        df = self._make_df(3)
        result = price._convert_df_to_price_list(df)
        for row in result:
            assert len(row) == 7

    def test_newest_first(self):
        """新しい日付が先頭に来ること"""
        df = self._make_df(5)
        result = price._convert_df_to_price_list(df)
        # 先頭の日付が末尾より新しい
        d0 = price.parse_date_str(result[0][0])
        d_last = price.parse_date_str(result[-1][0])
        assert d0 > d_last

    def test_date_format_japanese(self):
        """日付が"YYYY年M月D日"形式であること"""
        df = self._make_df(1)
        result = price._convert_df_to_price_list(df)
        date_str = result[0][0]
        import re
        assert re.match(r"\d{4}年\d{1,2}月\d{1,2}日", date_str)

    def test_int_types(self):
        """価格・出来高がint型であること"""
        df = self._make_df(3)
        result = price._convert_df_to_price_list(df)
        for row in result:
            for val in row[1:]:  # 日付以外
                assert isinstance(val, int)

    def test_adjclose_column_name_variant(self):
        """'Adjclose'カラム名でも動作すること"""
        import pandas as pd

        dates = pd.bdate_range(end="2025-01-31", periods=3)
        data = {
            "Open": [1000, 1010, 1020],
            "High": [1020, 1030, 1040],
            "Low": [990, 1000, 1010],
            "Close": [1005, 1015, 1025],
            "Adjclose": [1003, 1013, 1023],  # yfinance新形式
            "Volume": [100000, 101000, 102000],
        }
        df = pd.DataFrame(data, index=dates)
        result = price._convert_df_to_price_list(df)
        # adj_closeが取得されていること
        assert result[0][6] == 1023  # 最新行のAdj Close


# ==================================================
# yfinanceキャッシュのラウンドトリップ
# ==================================================
class TestYfinanceCacheRoundtrip:
    """JSONキャッシュの保存・読み込みテスト"""

    def test_save_and_load(self):
        """保存→読み込みで同一データが復元されること"""
        price_current = 1500
        price_list = [
            ["2025年1月31日", 1490, 1520, 1480, 1500, 200000, 1500],
            ["2025年1月30日", 1470, 1500, 1460, 1490, 180000, 1490],
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fname = f.name
        try:
            price._save_yfinance_cache(fname, price_current, price_list)
            loaded_pc, loaded_pl = price._load_yfinance_cache(fname)
            assert loaded_pc == price_current
            assert loaded_pl == price_list
        finally:
            os.unlink(fname)

    def test_load_nonexistent(self):
        """存在しないファイルではNoneが返ること"""
        pc, pl = price._load_yfinance_cache("/tmp/nonexistent_cache_12345.json")
        assert pc is None
        assert pl is None

    def test_load_corrupted(self):
        """壊れたJSONファイルではNoneが返ること"""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("not valid json{{{")
            fname = f.name
        try:
            pc, pl = price._load_yfinance_cache(fname)
            assert pc is None
            assert pl is None
        finally:
            os.unlink(fname)


# ==================================================
# parse_price_text_from_list
# ==================================================
class TestParsePriceTextFromList:
    """リファクタリング後の指標計算テスト"""

    def _make_price_list_7col(self, count=25):
        """テスト用の7カラム価格データを生成する
        各要素: [日付, 始値, 高値, 安値, 終値, 出来高, 調整後終値]
        日付若い順（最新が先頭）
        """
        base_price = 1000
        price_list = []
        for i in range(count):
            day_num = count - i
            date_str = "2025年1月%d日" % day_num
            open_p = base_price + i * 2
            high_p = open_p + 20
            low_p = open_p - 10
            close_p = open_p + 5
            volume = 100000 + i * 1000
            adj_close = close_p
            price_list.append([date_str, open_p, high_p, low_p, close_p, volume, adj_close])
        return price_list

    def test_returns_dict_and_list(self):
        """戻り値が(dict, list)のタプルであること"""
        price_list = self._make_price_list_7col()
        result = price.parse_price_text_from_list(1050, price_list)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], list)

    def test_contains_expected_keys(self):
        """結果dictに必要なキーが含まれること"""
        price_list = self._make_price_list_7col()
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        expected_keys = [
            "price",
            "sell_pressure_ratio",
            "stddev_volatility",
            "avg_volume_d",
            "pocket_pivot",
            "breakout",
            "price_log",
        ]
        for key in expected_keys:
            assert key in result_dict, "キー '%s' が結果dictにありません" % key

    def test_price_current_set(self):
        """現在価格がセットされること"""
        price_list = self._make_price_list_7col()
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        assert result_dict["price"] == 1050

    def test_cur_prices_format(self):
        """cur_pricesが[終値, 高値, 安値]の3要素であること"""
        price_list = self._make_price_list_7col()
        _, cur_prices = price.parse_price_text_from_list(1050, price_list)
        assert len(cur_prices) == 3
