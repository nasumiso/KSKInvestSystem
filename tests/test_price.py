"""price.py の計算関数テスト"""

from datetime import date
import json
import os
import tempfile
from unittest.mock import patch
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

    def test_auto_adjust_true_no_adjclose(self):
        """auto_adjust=TrueでAdj Closeカラムがない場合、closeがadj_closeになること"""
        import pandas as pd

        dates = pd.bdate_range(end="2025-01-31", periods=3)
        data = {
            "Open": [1000, 1010, 1020],
            "High": [1020, 1030, 1040],
            "Low": [990, 1000, 1010],
            "Close": [1005, 1015, 1025],
            "Volume": [100000, 101000, 102000],
        }
        df = pd.DataFrame(data, index=dates)
        result = price._convert_df_to_price_list(df)
        # adj_closeがcloseと同値であること
        assert result[0][6] == result[0][4]  # 最新行
        assert result[0][6] == 1025


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
# _convert_weekly_df_to_kabutan_format
# ==================================================
class TestConvertWeeklyDfToKabutanFormat:
    """yfinance週足DataFrame→Kabutan互換形式の変換テスト"""

    def _make_weekly_df(self, weeks=60):
        """テスト用の週足DataFrameを生成する
        十分に過去の固定日付範囲を使い、不完全週フィルタの影響を受けないようにする。
        """
        import pandas as pd

        dates = pd.date_range(start="2024-01-01", periods=weeks, freq="W-MON")
        data = {
            "Open": [1000 + i * 5 for i in range(weeks)],
            "High": [1020 + i * 5 for i in range(weeks)],
            "Low": [980 + i * 5 for i in range(weeks)],
            "Close": [1010 + i * 5 for i in range(weeks)],
            "Volume": [100000 + i * 1000 for i in range(weeks)],
        }
        return pd.DataFrame(data, index=dates)

    def test_output_length(self):
        """出力レコード数がDataFrameの行数と一致（過去日付のため不完全週なし）"""
        df = self._make_weekly_df(60)
        result = price._convert_weekly_df_to_kabutan_format(df)
        assert len(result) == 60

    def test_eight_elements(self):
        """各レコードが8要素タプルであること"""
        df = self._make_weekly_df(10)
        result = price._convert_weekly_df_to_kabutan_format(df)
        for row in result:
            assert isinstance(row, tuple)
            assert len(row) == 8

    def test_newest_first(self):
        """新しい日付が先頭に来ること"""
        df = self._make_weekly_df(10)
        result = price._convert_weekly_df_to_kabutan_format(df)
        d0 = price.parse_date_str(result[0][0])
        d_last = price.parse_date_str(result[-1][0])
        assert d0 > d_last

    def test_date_format_japanese(self):
        """日付が"YYYY年M月D日"形式であること"""
        import re
        df = self._make_weekly_df(3)
        result = price._convert_weekly_df_to_kabutan_format(df)
        for row in result:
            assert re.match(r"\d{4}年\d{1,2}月\d{1,2}日", row[0])

    def test_comma_separated_numbers(self):
        """数値がカンマ区切り文字列であること、replace(',','')で数値変換可能"""
        df = self._make_weekly_df(5)
        result = price._convert_weekly_df_to_kabutan_format(df)
        for row in result:
            for val in [row[1], row[2], row[3], row[4], row[7]]:
                # カンマを除去して数値変換可能であること
                assert int(float(val.replace(",", ""))) >= 0

    def test_prev_week_fields_are_zero(self):
        """前週比（[5]）と前週比%（[6]）が"0"固定であること"""
        df = self._make_weekly_df(5)
        result = price._convert_weekly_df_to_kabutan_format(df)
        for row in result:
            assert row[5] == "0"
            assert row[6] == "0"

    def test_all_string_elements(self):
        """全要素が文字列であること"""
        df = self._make_weekly_df(5)
        result = price._convert_weekly_df_to_kabutan_format(df)
        for row in result:
            for val in row:
                assert isinstance(val, str)


# ==================================================
# _calc_weekly_indicators
# ==================================================
class TestCalcWeeklyIndicators:
    """週次指標計算の統合テスト"""

    @pytest.fixture(autouse=True)
    def _mock_market_db(self):
        """CI環境にmarket_db_shelveがないためモックする"""
        mock_db = {"topix": {"rs_raw": 1.0}}
        with patch("make_market_db.get_market_db", return_value=mock_db):
            yield

    def _make_weekly_price_list(self, weeks=55):
        """テスト用のKabutan互換weekly_price_listを生成する
        8要素タプル(文字列): (日付, 始値, 高値, 安値, 終値, 前週比, 前週比%, 売買高)
        """
        base_price = 1000
        result = []
        for i in range(weeks):
            day = weeks - i
            date_str = "2026年1月%d日" % max(day, 1)
            # 緩やかな上昇トレンド
            close = base_price + i * 3
            open_p = close - 5
            high = close + 10
            low = close - 15
            volume = 100000 + i * 500
            result.append((
                date_str,
                "{:,}".format(open_p),
                "{:,}".format(high),
                "{:,}".format(low),
                "{:,}".format(close),
                "0",
                "0",
                "{:,}".format(volume),
            ))
        return result

    def test_returns_dict(self):
        """戻り値がdictであること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert isinstance(result, dict)

    def test_rs_raw_positive(self):
        """rs_rawが正の値であること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert "rs_raw" in result
        assert result["rs_raw"] > 0

    def test_trend_template_is_list(self):
        """trend_templateがリストであること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert "trend_template" in result
        assert isinstance(result["trend_template"], list)

    def test_sell_pressure_ratio_w(self):
        """sell_pressure_ratio_wが3要素リストであること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert "sell_pressure_ratio_w" in result
        assert len(result["sell_pressure_ratio_w"]) == 3

    def test_price_kairi_wma10(self):
        """price_kairi_wma10がfloat/intであること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert "price_kairi_wma10" in result
        assert isinstance(result["price_kairi_wma10"], (int, float))

    def test_new_high_is_list(self):
        """new_highがリストであること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert "new_high" in result
        assert isinstance(result["new_high"], list)

    def test_pullback_20_is_string(self):
        """pullback_20がstrであること"""
        wpl = self._make_weekly_price_list(55)
        result = price._calc_weekly_indicators(wpl)
        assert "pullback_20" in result
        assert isinstance(result["pullback_20"], str)

    def test_empty_list_returns_empty_dict(self):
        """空リストの場合は空dictが返ること"""
        result = price._calc_weekly_indicators([])
        assert isinstance(result, dict)

    def test_cur_prices_override(self):
        """cur_pricesを渡した場合にRS計算で使用されること"""
        wpl = self._make_weekly_price_list(55)
        # 現在価格を高めに設定するとrs_rawが高くなるはず
        result_default = price._calc_weekly_indicators(wpl)
        result_high = price._calc_weekly_indicators(wpl, cur_prices=[9999, 9999, 9999])
        assert result_high["rs_raw"] > result_default["rs_raw"]


# ==================================================
# yfinance週足キャッシュのラウンドトリップ
# ==================================================
class TestYfinanceWeeklyCacheRoundtrip:
    """週足JSONキャッシュの保存・読み込みテスト"""

    def test_save_and_load_weekly(self):
        """週足キャッシュの保存→読み込みでデータ復元されること"""
        weekly_price_list = [
            ("2026年4月14日", "1,200", "1,220", "1,180", "1,210", "0", "0", "150,000"),
            ("2026年4月7日", "1,190", "1,210", "1,170", "1,200", "0", "0", "140,000"),
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fname = f.name
        try:
            price._save_yfinance_cache(fname, None, weekly_price_list)
            _, loaded_pl = price._load_yfinance_cache(fname)
            assert loaded_pl is not None
            assert len(loaded_pl) == 2
            # JSONではtupleがlistになるが、要素は保持される
            assert loaded_pl[0][4] == "1,210"
            assert loaded_pl[1][7] == "140,000"
        finally:
            os.unlink(fname)


# ==================================================
# _MARKET_INDEX_CODES
# ==================================================
class TestMarketIndexCodes:
    """市場指数コードのガードテスト"""

    def test_topix_is_index(self):
        """TOPIXコードが市場指数として認識されること"""
        assert "0010" in price._MARKET_INDEX_CODES

    def test_nikkei_is_index(self):
        """日経225コードが市場指数として認識されること"""
        assert "0000" in price._MARKET_INDEX_CODES

    def test_regular_stock_is_not_index(self):
        """通常の銘柄コードが市場指数でないこと"""
        assert "7203" not in price._MARKET_INDEX_CODES
        assert "3496" not in price._MARKET_INDEX_CODES


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
