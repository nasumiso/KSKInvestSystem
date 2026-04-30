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

    def test_yy_slash_format(self):
        """YY/MM/DD 形式 (Kabutan の2桁年表記、20YY と解釈)"""
        result = price.parse_date_str("26/04/28")
        assert result == date(2026, 4, 28)

    def test_yy_hyphen_format(self):
        """YY-MM-DD 形式"""
        result = price.parse_date_str("26-04-28")
        assert result == date(2026, 4, 28)

    def test_yy_format_does_not_match_4digit(self):
        """4桁年は YY 形式の正規表現に引っかからない (回帰防止)"""
        # 4桁年は規則 (2) でパースされるため、YY 規則がうっかり動作しないこと
        result = price.parse_date_str("2025/06/10")
        assert result == date(2025, 6, 10)


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
# _convert_daily_df_to_kabutan_format (issue #148)
# ==================================================
class TestConvertDailyDfToKabutanFormat:
    """yfinance日足DataFrame→Kabutan互換8要素タプル形式の変換テスト"""

    def _make_daily_df(self, days=30, base_close=1000):
        import pandas as pd

        dates = pd.date_range(start="2024-01-01", periods=days, freq="B")
        data = {
            "Open": [base_close + i * 5 for i in range(days)],
            "High": [base_close + 20 + i * 5 for i in range(days)],
            "Low": [base_close - 20 + i * 5 for i in range(days)],
            "Close": [base_close + 10 + i * 5 for i in range(days)],
            "Volume": [100000 + i * 1000 for i in range(days)],
        }
        return pd.DataFrame(data, index=dates)

    def test_output_length(self):
        df = self._make_daily_df(30)
        result = price._convert_daily_df_to_kabutan_format(df)
        assert len(result) == 30

    def test_eight_elements(self):
        df = self._make_daily_df(10)
        result = price._convert_daily_df_to_kabutan_format(df)
        for row in result:
            assert isinstance(row, tuple)
            assert len(row) == 8

    def test_newest_first(self):
        """新しい日付が先頭に来る (Kabutan互換のYY/MM/DD文字列順序で確認)"""
        df = self._make_daily_df(10)
        result = price._convert_daily_df_to_kabutan_format(df)
        # YY/MM/DD は文字列ソートで時系列順になる
        assert result[0][0] > result[-1][0]

    def test_date_format_kabutan_compatible(self):
        """日付がKabutan日次互換のYY/MM/DD形式 (_html_marketのs[3:]切り出しと整合)"""
        import re
        df = self._make_daily_df(5)
        result = price._convert_daily_df_to_kabutan_format(df)
        for row in result:
            assert re.match(r"^\d{2}/\d{2}/\d{2}$", row[0])

    def test_prev_diff_field_zero(self):
        """前日比 (index 5) は'0'固定 (指標計算で未使用)"""
        df = self._make_daily_df(5)
        result = price._convert_daily_df_to_kabutan_format(df)
        for row in result:
            assert row[5] == "0"

    def test_prev_diff_pct_calculated(self):
        """前日比% (index 6) が前日終値からの変動率として計算されている"""
        df = self._make_daily_df(5)
        result = price._convert_daily_df_to_kabutan_format(df)
        # result[0] が最新、result[1] が前日。result[0][6] = (close0 - close1) / close1 * 100
        close0 = int(result[0][4].replace(",", ""))
        close1 = int(result[1][4].replace(",", ""))
        expected = (close0 - close1) * 100.0 / close1
        actual = float(result[0][6])
        # 文字列フォーマット時に小数2桁に丸めているため、誤差は0.01未満で十分
        assert abs(actual - expected) < 0.01

    def test_oldest_row_prev_diff_pct_zero(self):
        """最古日 (前日データなし) の前日比%は'0'固定"""
        df = self._make_daily_df(5)
        result = price._convert_daily_df_to_kabutan_format(df)
        assert result[-1][6] == "0"

    def test_all_string_elements(self):
        df = self._make_daily_df(5)
        result = price._convert_daily_df_to_kabutan_format(df)
        for row in result:
            for val in row:
                assert isinstance(val, str)


# ==================================================
# _calc_daily_indicators (parse_price_d_html_kabutan からの切り出し: issue #148)
# ==================================================
class TestCalcDailyIndicators:
    """日次指標計算のテスト"""

    def _make_price_list(self, n=25, with_distribution=False):
        """8要素タプル文字列リストを生成 (新しい日付が先頭)"""
        rows = []
        for i in range(n):
            day = "26%02d%02d" % ((i // 28) + 1, (i % 28) + 1)
            close = 1000 + i * 5
            open_p = close - 3
            high = close + 10
            low = close - 10
            volume = 100000 + i * 100
            ratio = 0.5
            rows.append((
                day,
                "{:,}".format(open_p),
                "{:,}".format(high),
                "{:,}".format(low),
                "{:,}".format(close),
                "0",
                "{:.2f}".format(ratio),
                "{:,}".format(volume),
            ))
        # 新しい日付が先頭なので reverse
        rows.reverse()
        return rows

    def test_returns_required_keys(self):
        rows = self._make_price_list(25)
        result = price._calc_daily_indicators(rows)
        for key in (
            "distribution_days", "distribution_days_with_close", "followthrough_days",
            "daily_history", "direction_signal",
            "spr_20", "spr_5", "spr_buygagher", "rv_20", "rv_5",
        ):
            assert key in result

    def test_empty_input_returns_empty(self):
        result = price._calc_daily_indicators([])
        assert result == {}

    def test_direction_signal_default_empty(self):
        """direction_signal は _calc_daily_indicators の段階では空文字。
        最終値は make_market_db.py の State Machine 計算で上書きされる
        (issue #117 Part A)"""
        rows = self._make_price_list(25)
        result = price._calc_daily_indicators(rows)
        assert result["direction_signal"] == ""

    def test_distribution_days_with_close_format(self):
        """distribution_days_with_close は (date, close) タプルのリスト"""
        rows = self._make_price_list(25)
        result = price._calc_daily_indicators(rows)
        for entry in result["distribution_days_with_close"]:
            assert len(entry) == 2
            date, close = entry
            assert isinstance(date, str)
            assert isinstance(close, float)

    def test_daily_history_newest_first(self):
        """daily_history は新しい日付が先頭"""
        rows = self._make_price_list(25)
        result = price._calc_daily_indicators(rows)
        history = result["daily_history"]
        assert len(history) > 0
        # rows[0] が最新日付なので history[0] と一致
        assert history[0] == rows[0][0]

    def test_zero_range_day_does_not_crash(self):
        """高値=安値の日があってもZeroDivisionErrorにならない"""
        rows = self._make_price_list(25)
        # rows[0] は最新。高値=安値で値幅ゼロにする
        d, o, _h, _l, c, r5, r6, v = rows[0]
        rows[0] = (d, o, c, c, c, r5, r6, v)
        result = price._calc_daily_indicators(rows)
        # 例外を起こさず、必須キーが返る
        assert "distribution_days" in result

    def _make_price_list_with_real_dates(self, n=30, base_close=1000):
        """parse_date_strで解釈可能な日付ラベル付きの8要素タプル列を生成
        日付は 'YYYY/MM/DD' 形式、新しい日付が先頭。
        """
        from datetime import date as _date, timedelta
        rows = []
        d0 = _date(2026, 4, 28)
        for i in range(n):
            dt = d0 - timedelta(days=i)
            close = base_close + (n - i) * 5  # 単調増加（新しいほど高い）
            open_p = close - 3
            high = close + 10
            low = close - 10
            volume = 100000 + i * 100
            rows.append((
                "{:04d}/{:02d}/{:02d}".format(dt.year, dt.month, dt.day),
                "{:,}".format(open_p),
                "{:,}".format(high),
                "{:,}".format(low),
                "{:,}".format(close),
                "0",
                "0.50",
                "{:,}".format(volume),
            ))
        return rows

    def test_price_log_returned(self):
        """戻り値dictに price_log キーが含まれる"""
        rows = self._make_price_list_with_real_dates(30)
        result = price._calc_daily_indicators(rows)
        assert "price_log" in result
        assert isinstance(result["price_log"], list)

    def test_price_log_capped_at_25(self):
        """price_log は最大 25 件"""
        rows = self._make_price_list_with_real_dates(30)
        result = price._calc_daily_indicators(rows)
        assert len(result["price_log"]) == 25

    def test_price_log_tuple_format(self):
        """price_log の各要素は (date, int) タプル"""
        rows = self._make_price_list_with_real_dates(30)
        result = price._calc_daily_indicators(rows)
        for dt, close in result["price_log"]:
            assert isinstance(dt, date)
            assert isinstance(close, int)

    def test_price_log_descending_dates(self):
        """price_log は日付降順 (新しい日付が先頭)"""
        rows = self._make_price_list_with_real_dates(30)
        result = price._calc_daily_indicators(rows)
        dates = [d for d, _ in result["price_log"]]
        assert dates == sorted(dates, reverse=True)

    def test_price_log_close_values_match_input(self):
        """price_log の終値は入力 [4]終値 と一致 (前日比% [6] と取り違えていない)"""
        rows = self._make_price_list_with_real_dates(30, base_close=2000)
        result = price._calc_daily_indicators(rows)
        # 先頭は新しい日付。入力先頭の終値と一致
        expected_close = int(rows[0][4].replace(",", ""))
        assert result["price_log"][0][1] == expected_close

    def test_price_log_short_input(self):
        """入力が25件未満なら全件保存"""
        rows = self._make_price_list_with_real_dates(10)
        result = price._calc_daily_indicators(rows)
        assert len(result["price_log"]) == 10

    def test_price_log_skips_invalid_date(self):
        """日付パース不能行は skip し、他の行は保存される"""
        rows = self._make_price_list_with_real_dates(25)
        # 中央付近の日付を不正にする
        d, o, h, l, c, r5, r6, v = rows[10]
        rows[10] = ("INVALID_DATE", o, h, l, c, r5, r6, v)
        result = price._calc_daily_indicators(rows)
        # 不正行を除いた24件が保存される
        assert len(result["price_log"]) == 24


# ==================================================
# _is_weekly_cache_fresh
# ==================================================
class TestIsWeeklyCacheFresh:
    """週足キャッシュの鮮度判定テスト(最新確定週を含むか)"""

    def _row(self, date_str):
        """指定日付ラベルの8要素ダミー行"""
        return (date_str, "0", "0", "0", "0", "0", "0", "0")

    def test_empty_list_is_stale(self):
        """空リストは古い判定"""
        assert price._is_weekly_cache_fresh([]) is False

    def test_invalid_date_is_stale(self):
        """日付パース失敗は古い判定"""
        assert price._is_weekly_cache_fresh([self._row("invalid")]) is False

    def test_contains_latest_confirmed_week(self):
        """先頭バーの金曜 <= price_day なら新鮮"""
        # price_day=2026-04-17(金), 先頭バー=2026年4月13日(月) → 金曜=2026-04-17
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 17, 20, 0)
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月13日"), self._row("2026年4月6日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_missing_latest_confirmed_week(self):
        """先頭バーの金曜 > price_day なら古い(＝最新確定週が未取得)"""
        # price_day=2026-04-17(金), 先頭バー=2026年4月6日(月) → 金曜=2026-04-10
        # 2026-04-17 > 2026-04-10 なので先頭=04/06週 = 古い(04/13週が欠落)
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 17, 20, 0)
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月6日"), self._row("2026年3月30日")]
            assert price._is_weekly_cache_fresh(pl) is False

    def test_weekend_rolls_back_to_friday(self):
        """週末(日曜)実行時でも直近金曜基準で新鮮判定できる

        regression: 2026-04-19(日曜)に実行すると get_price_day=2026-04-19 を返すが、
        直近確定週の金曜は 2026-04-17。先頭=2026/04/13(月)のバー金曜=04/17 は
        最新確定週の金曜に一致するので新鮮判定されるべき。
        """
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 19, 20, 0)  # 日曜
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月13日"), self._row("2026年4月6日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_saturday_rolls_back_to_friday(self):
        """土曜実行時も同様(直近金曜に丸める)"""
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 18, 20, 0)  # 土曜
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月13日"), self._row("2026年4月6日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_monday_uses_last_fridays_confirmation(self):
        """月曜夜: 先週金曜(04/17)が直近確定金曜。先頭=04/13バーの金曜=04/17 で新鮮判定

        regression: 以前は月曜の当日(04/20)を確定金曜として扱っていたため、
        週足に先週分が揃っていても毎回 stale 判定され全銘柄を再ダウンロードしていた。
        """
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 20, 20, 0)  # 月曜
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月13日"), self._row("2026年4月6日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_thursday_uses_last_fridays_confirmation(self):
        """木曜夜: 直近確定金曜は先週金曜(04/17)。先頭=04/13バー(金=04/17)で新鮮"""
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 23, 20, 0)  # 木曜
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月13日"), self._row("2026年4月6日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_friday_uses_same_day_as_confirmation(self):
        """金曜夜(18時以降): 今日(04/24)が直近確定金曜。先頭=04/20バーの金曜=04/24で新鮮"""
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 24, 20, 0)  # 金曜20:00
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            pl = [self._row("2026年4月20日"), self._row("2026年4月13日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_friday_before_close_uses_previous_week(self):
        """金曜18時前: get_price_dayが前日(木)を返す→直近確定金曜は先週金曜"""
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 24, 10, 0)  # 金曜朝
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            # 先頭=04/13バー(金=04/17)、18時前なので確定金曜=04/17で新鮮
            pl = [self._row("2026年4月13日"), self._row("2026年4月6日")]
            assert price._is_weekly_cache_fresh(pl) is True

    def test_monday_stale_when_missing_last_week(self):
        """月曜夜: 先週分がキャッシュに無ければ古い判定"""
        from datetime import datetime as _dt
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 4, 20, 20, 0)  # 月曜
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            # 先頭=04/06バー(金=04/10) < 確定金曜=04/17 → 古い
            pl = [self._row("2026年4月6日"), self._row("2026年3月30日")]
            assert price._is_weekly_cache_fresh(pl) is False


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

    def test_price_log_capped_at_25(self):
        """price_log は LOG_DAY=25 に揃う"""
        # 30 件入力 → 上限 25 件のみ保持
        price_list = self._make_price_list_7col(count=30)
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        assert len(result_dict["price_log"]) == 25

    def test_price_log_handles_short_input(self):
        """LOG_DAY より少ない入力でも安全に処理される (range の length ガード)"""
        price_list = self._make_price_list_7col(count=5)
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        # 入力が5件しかなければ price_log も最大5件
        assert len(result_dict["price_log"]) <= 5
