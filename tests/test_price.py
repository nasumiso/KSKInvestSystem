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

    @pytest.mark.parametrize("text,expected", [
        ("2025年1月15日", date(2025, 1, 15)),          # YYYY年M月D日
        ("2025/06/10", date(2025, 6, 10)),             # YYYY/MM/DD (4桁年が YY 規則に食われないことの回帰防止も兼ねる)
        ("2025-12-31", date(2025, 12, 31)),            # YYYY-MM-DD
        ("26/04/28", date(2026, 4, 28)),               # YY/MM/DD (Kabutan の2桁年表記、20YY と解釈)
        ("26-04-28", date(2026, 4, 28)),               # YY-MM-DD
        ("決算日: 2025年 3月 1日 発表", date(2025, 3, 1)),  # 周辺テキスト付き
        ("", None),
        (None, None),
        ("hoge", None),
    ])
    def test_parse_date_str(self, text, expected):
        assert price.parse_date_str(text) == expected


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
            # state machine の FTD/ラリー判定が当日 OHLV をトップレベルキーから読むため必須
            "price", "low", "high", "volume",
        ):
            assert key in result

    def test_top_level_ohlv_matches_latest_row(self):
        """当日 OHLV (price/low/high/volume) が daily_price_list[0] と一致する。

        make_market_db._update_index_market_state が new_index_db["price"|"low"|
        "high"|"volume"] を参照して FTD/ラリー判定を行うため、ここで欠落すると
        state machine の上方向遷移 (correction → confirmed) が常時不発になる。
        """
        rows = self._make_price_list(25)
        result = price._calc_daily_indicators(rows)
        # daily_price_list[0] = 当日 (新しい順) なので最後に追加された n=24 のデータ
        # _make_price_list で reverse 済なので rows[0] が最新 (=close 1120)
        head = rows[0]
        assert result["price"] == int(head[4].replace(",", ""))
        assert result["low"] == int(head[3].replace(",", ""))
        assert result["high"] == int(head[2].replace(",", ""))
        assert result["volume"] == int(head[7].replace(",", ""))

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

    def test_price_log_capped_at_30(self):
        """price_log は最大 30 件"""
        rows = self._make_price_list_with_real_dates(35)
        result = price._calc_daily_indicators(rows)
        assert len(result["price_log"]) == 30

    @pytest.mark.parametrize("n,break_days,exp_streak,exp_ever", [
        # 全日 終値>10ma → 現在も維持中
        (45, [], True, False),
        # 直近 [0,1,2] を割込み → 古い側に達成窓あり、現在は割れ
        (50, [0, 1, 2], False, True),
        # 単発飛び石割れは porosity 救済 → 現在も維持中 (range(0,45,5) の最新日=0 は割れ→False)
        (45, list(range(0, 45, 5)), False, True),
        # データ不足 (39本未満) → 未達成
        (30, [], False, False),
        # 達成後に5日以上連続割れ → 赤太点線リセット(False)、黒太点線は残る(True)
        (50, [0, 1, 2, 3, 4], False, True),
    ])
    def test_ma10_above_streak_30(self, n, break_days, exp_streak, exp_ever):
        """ma10_above_streak_30 (現在も維持中) と ma10_streak_ever (実績あり・現在は割れ) の判定。

        break日は終値・安値とも極端に下げる。porosity 仕様では「終値割れ + 翌営業日の終値も
        10ma以下」で初めて連続切断 (violation)。単発の飛び石割れは翌日終値回復で救済される。
        """
        rows = self._make_price_list(n)  # 先頭=最新、単調増加 (新しいほど高い)
        for bd in break_days:
            d, o, h, _l, _c, r5, r6, v = rows[bd]
            rows[bd] = (d, o, h, "0", "1", r5, r6, v)  # 終値1・安値0で10maを確実に割り込む
        result = price._calc_daily_indicators(rows)
        assert result["ma10_above_streak_30"] is exp_streak
        assert result["ma10_streak_ever"] is exp_ever
        assert isinstance(result["price_kairi_ma10"], float)

    @pytest.mark.parametrize("bd,next_close,expected", [
        # 30連続窓の途中 (bd=5) で終値だけ10maを割る単発割れ。
        # 翌営業日 (index bd-1) の終値が翌日10ma上に戻るか否かで維持/切断が変わる。
        (5, None, True),   # 翌日終値 > 翌日10ma → porosity 救済 → 30連続維持
        (5, 1, False),     # 翌日終値 <= 翌日10ma → violation 成立 → 切断
        # 最新日 (bd=0) の割れは翌営業日が未到来 = violation 未確定。先取り誤判定を
        # 避けるため不成立 (False) に倒す。next_close は無関係 (翌日が存在しない)。
        (0, None, False),
    ])
    def test_ma10_streak_porosity(self, bd, next_close, expected):
        """終値割れ時の porosity 救済/violation 切断、および最新日の未確定扱いを検証する。"""
        # 39本ちょうどにして30連続窓を1つ (s=0, i=0..29) に絞り、bd を必ず含める。
        rows = self._make_price_list(39)  # 先頭=最新、単調増加
        # 割れ日 bd: 終値だけ10maを割り込ませ、安値は基準値 (500) を置く
        d, o, h, _l, _c, r5, r6, v = rows[bd]
        rows[bd] = (d, o, h, "500", "1", r5, r6, v)
        # bd>0 のときのみ翌営業日 bd-1 の終値を可変 (翌日10ma回復の有無で violation 判定)
        if bd > 0 and next_close is not None:
            d2, o2, h2, l2, _c2, r52, r62, v2 = rows[bd - 1]
            rows[bd - 1] = (d2, o2, h2, l2, str(next_close), r52, r62, v2)
        result = price._calc_daily_indicators(rows)
        assert result["ma10_above_streak_30"] is expected

    def test_ma10_kairi_survives_missing_low(self):
        """安値1件が非数値 (株探 "－" 等) でも、終値ベースの乖離率は計算される。

        従来は終値さえ読めれば乖離率を出せていた。安値欠損が乖離率や終値回復ベースの
        streak 判定まで巻き込んで全滅する回帰が起きないことを確認する。
        """
        rows = self._make_price_list(39)  # 全日 終値>10ma
        d, o, h, _l, c, r5, r6, v = rows[7]
        rows[7] = (d, o, h, "－", c, r5, r6, v)  # 安値だけ非数値に欠損
        result = price._calc_daily_indicators(rows)
        # 乖離率は終値だけで計算できる → None にならない
        assert isinstance(result["price_kairi_ma10"], float)
        assert result["ma10_above_streak_30"] is True

    def test_ma10_streak_missing_low_break_day_is_rescued_by_close(self):
        """割れ日の安値欠損があっても、翌日終値が10ma上なら streak は維持される。"""
        rows = self._make_price_list(39)
        d, o, h, _l, _c, r5, r6, v = rows[5]
        rows[5] = (d, o, h, "－", "1", r5, r6, v)  # 終値割れ + 安値欠損
        result = price._calc_daily_indicators(rows)
        assert isinstance(result["price_kairi_ma10"], float)
        assert result["ma10_above_streak_30"] is True

    def test_ma10_break_confirmed_triggers_after_a_day_low_broken(self):
        """A日(10ma割れ初日)の安値を翌日以降の安値が下回ったとき ma10_break_confirmed=True。

        50本で streak_ever=True を確立するパターン: break_days=[0,1,2] で直近3日を割れさせる。
        rows[2]=A日(最初に割れた日)、rows[1]/rows[0]が翌日以降。
        A日安値=100、rows[0]の安値=50 にして A日安値を下回らせる。
        """
        rows = self._make_price_list(50)
        # rows[2]: A日。close=1(割れ), low=100
        d, o, h, _l, _c, r5, r6, v = rows[2]
        rows[2] = (d, o, h, "100", "1", r5, r6, v)
        # rows[1]: 翌日。安値100(A日と同値 → 未達)
        d, o, h, _l, _c, r5, r6, v = rows[1]
        rows[1] = (d, o, h, "100", "1", r5, r6, v)
        # rows[0]: 最新日。安値50でA日安値(100)を下回る
        d, o, h, _l, _c, r5, r6, v = rows[0]
        rows[0] = (d, o, h, "50", "1", r5, r6, v)
        result = price._calc_daily_indicators(rows)
        assert result["ma10_streak_ever"] is True
        assert result["ma10_break_confirmed"] is True

    def test_generic_ma_violation_matches_ma10_break_confirmed(self):
        """汎用 MA 違反判定は既存 10MA 確定判定と同じ結果になる。"""
        from exit_line import calc_ma_violation

        rows = self._make_price_list(50)
        for i, low in ((2, "100"), (1, "100"), (0, "50")):
            d, o, h, _l, _c, r5, r6, v = rows[i]
            rows[i] = (d, o, h, low, "1", r5, r6, v)
        closes = [int(float(r[4].replace(",", ""))) for r in rows]
        lows = [int(float(r[3].replace(",", ""))) for r in rows]
        generic = calc_ma_violation(
            closes, lows,
            lambda i: sum(closes[i:i + 10]) / 10 if len(closes) >= i + 10 else None,
        )
        assert generic["confirmed"] is price._calc_daily_indicators(rows)["ma10_break_confirmed"]

    def test_weekly_ma_violation_uses_prior_week_value_at_week_boundary(self):
        """前週のA日は前週までのWMA、今週の確定日は今週のWMAで判定する。"""
        from datetime import timedelta

        def row(dt, low, close):
            return (dt.strftime("%Y/%m/%d"), "0", "0", str(low), str(close), "0", "0", "0")

        daily = [row(date(2026, 2, 9), 80, 90), row(date(2026, 2, 6), 90, 99)]
        # 2/2週の終値40を含む今週の30WMAは98、前週時点の30WMAは100。
        weekly = [row(date(2026, 2, 2), 0, 40)]
        weekly.extend(row(date(2026, 1, 26) - timedelta(days=7 * i), 0, 100) for i in range(30))
        result = price.calc_weekly_ma_violation(daily, weekly, 30)
        assert result["confirmed"] is True
        assert result["ma_value"] == pytest.approx(98.0)

    def test_ma10_break_confirmed_false_when_a_day_low_not_broken(self):
        """A日安値を下回る日がなければ ma10_break_confirmed=False。"""
        rows = self._make_price_list(50)
        # rows[2]: A日。close=1, low=100
        d, o, h, _l, _c, r5, r6, v = rows[2]
        rows[2] = (d, o, h, "100", "1", r5, r6, v)
        # rows[0]/rows[1]: 安値100(A日と同値) → A日安値を下回らない
        for i in range(2):
            d, o, h, _l, _c, r5, r6, v = rows[i]
            rows[i] = (d, o, h, "100", "1", r5, r6, v)
        result = price._calc_daily_indicators(rows)
        assert result["ma10_streak_ever"] is True
        assert result["ma10_break_confirmed"] is False

    def test_ma10_break_confirmed_false_when_recovered(self):
        """10maを回復している場合 (ma10_streak_ever=False) は ma10_break_confirmed=False。"""
        rows = self._make_price_list(45)  # 全日終値>10ma → currently_holding=True, streak_ever=False
        result = price._calc_daily_indicators(rows)
        assert result["ma10_streak_ever"] is False
        assert result["ma10_break_confirmed"] is False

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
        """入力が30件未満なら全件保存"""
        rows = self._make_price_list_with_real_dates(10)
        result = price._calc_daily_indicators(rows)
        assert len(result["price_log"]) == 10

    def test_price_log_skips_invalid_date(self):
        """日付パース不能行は skip し、他の行は保存される"""
        rows = self._make_price_list_with_real_dates(30)
        # 中央付近の日付を不正にする
        d, o, h, l, c, r5, r6, v = rows[10]
        rows[10] = ("INVALID_DATE", o, h, l, c, r5, r6, v)
        result = price._calc_daily_indicators(rows)
        # 不正行を除いた29件が保存される
        assert len(result["price_log"]) == 29


# ==================================================
# _is_daily_cache_fresh
# ==================================================
class TestIsDailyCacheFresh:
    """日足キャッシュの鮮度判定テスト"""

    def test_weekend_rolls_back_to_friday(self):
        """土曜17時以降でも直近金曜の日次キャッシュを新鮮扱いする。"""
        with patch("price.recent_weekday", return_value=date(2026, 6, 12)):
            pl = [["2026年6月12日", 100, 110, 90, 105, 1000, 105]]
            assert price._is_daily_cache_fresh(pl) is True

    def test_stale_when_before_recent_friday(self):
        """直近金曜より古い日次キャッシュは週末でも古い扱いする。"""
        with patch("price.recent_weekday", return_value=date(2026, 6, 12)):
            pl = [["2026年6月11日", 100, 110, 90, 105, 1000, 105]]
            assert price._is_daily_cache_fresh(pl) is False


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

    @pytest.mark.parametrize("input_weeks,expected_len", [
        (20, 20),  # ≤25 で全件保持
        (30, 25),  # 25 で切り詰め (issue #239 仕様)
    ])
    def test_price_week_log_built(self, input_weeks, expected_len):
        """price_week_log が直近 25 週分を (date, float) タプル日付降順で保存する"""
        from datetime import date as _date
        wpl = self._make_weekly_price_list(input_weeks)
        result = price._calc_weekly_indicators(wpl)
        assert "price_week_log" in result
        log = result["price_week_log"]
        assert len(log) == expected_len
        # 各要素は (date, float)
        for dt, close in log:
            assert isinstance(dt, _date)
            assert isinstance(close, float)
        # 日付降順 (新しいものが先頭)
        dates = [dt for dt, _ in log]
        assert dates == sorted(dates, reverse=True)


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
        from datetime import date as _date, timedelta
        base_price = 1000
        price_list = []
        d0 = _date(2025, 12, 31)
        for i in range(count):
            dt = d0 - timedelta(days=i)
            date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
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

    def test_daily_ma_violation_accepts_yfinance_rows(self):
        """yfinanceの7列日足から50MA違反状態を計算できること。"""
        price_list = self._make_price_list_7col(count=55)
        result = price.calc_daily_ma_violation(price_list, 50)
        assert result["ma_value"] is not None
        assert result["confirmed"] is False

    def test_yahoo_price_data_saves_daily_ma_violation(self, monkeypatch):
        """通常のyfinance更新結果にも50MA違反状態を保存すること。"""
        price_list = self._make_price_list_7col(count=55)
        monkeypatch.setattr(
            price, "get_daily_data_yfinance",
            lambda _code_s, _stock, _upd: (1050, price_list),
        )
        monkeypatch.setattr(price, "set_db_code", lambda _record, _code_s: None)
        monkeypatch.setattr(price.os.path, "exists", lambda _path: False)

        result_dict, _ = price.get_price_data_yahoo("1234", {})

        assert result_dict["ma50_violation"] == price.calc_daily_ma_violation(
            price_list, 50
        )

    def test_cur_prices_format(self):
        """cur_pricesが[終値, 高値, 安値]の3要素であること"""
        price_list = self._make_price_list_7col()
        _, cur_prices = price.parse_price_text_from_list(1050, price_list)
        assert len(cur_prices) == 3

    def test_price_log_capped_at_30(self):
        """price_log は LOG_DAY=30 に揃う"""
        # 35 件入力 → 上限 30 件のみ保持
        price_list = self._make_price_list_7col(count=35)
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        assert len(result_dict["price_log"]) == 30

    def test_price_log_handles_short_input(self):
        """LOG_DAY より少ない入力でも安全に処理される (range の length ガード)"""
        price_list = self._make_price_list_7col(count=5)
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        # 入力が5件しかなければ price_log も最大5件
        assert len(result_dict["price_log"]) <= 5

    def test_pocket_pivot_ma25_detection(self):
        """issue #110: MA10乖離は4超だがMA25乖離は4以内なら検出される

        直近10日平均(MA10)より過去25日平均(MA25)が高い「下落基調からの反発」を、
        MA25 併用で拾えることを確認する。
        """
        from datetime import date as _date, timedelta
        d0 = _date(2025, 12, 31)
        price_list = []
        for i in range(30):
            dt = d0 - timedelta(days=i)
            date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
            if i == 0:
                # 当日(最新): 上昇日・出来高最大・安値1045
                close = 1046
                low = 1045
                vol = 999999
            elif i < 10:
                close = 1000  # 直近10日 → MA10≈1000
                low = close - 5
                vol = 100000
            else:
                close = 1100  # 過去10〜24日 → MA25 を引き上げる
                low = close - 5
                vol = 100000
            price_list.append([date_str, close, close + 10, low, close, vol, close])
        result_dict, _ = price.parse_price_text_from_list(1046, price_list)
        # ref_price=1045: kairi10=(1045-1000.6)/1000.6≈4.4%(>4) だが
        # kairi25=(1045-1060)/1060<0(<=4) なので検出される
        assert len(result_dict["pocket_pivot"]) >= 1

    @pytest.mark.parametrize(
        "today_close, today_vol, expect_break, expect_per_nonneg",
        [
            # 通常出来高ブレイク (vol>=1.5*avg) → 検出 (回帰)
            (1010, 300000, True, True),
            # ストップ高張り付き: 低出来高だが前日比+20% → 新規検出・per>=0
            (1200, 50000, True, True),
            # 微増・低出来高 (前日比+1%, vol<avg) → 非検出
            (1010, 50000, False, None),
        ],
    )
    def test_breakout_stop_high(self, today_close, today_vol,
                                expect_break, expect_per_nonneg):
        """issue #253: ストップ高張り付き (前日比+20%) で出来高条件をスキップ検知"""
        from datetime import date as _date, timedelta
        d0 = _date(2025, 12, 31)
        price_list = []
        for i in range(25):
            dt = d0 - timedelta(days=i)
            date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
            if i == 0:
                close = today_close
                low = 1000  # 当日安値=前日終値で ref_price を抑え kairi<=5
                vol = today_vol
            else:
                close = 1000  # 過去は横ばい → ma10≈1000, avg_vol≈100000
                low = 995
                vol = 100000
            price_list.append([date_str, close, close + 10, low, close, vol, close])
        result_dict, _ = price.parse_price_text_from_list(today_close, price_list)
        breaks = result_dict["breakout"]
        if expect_break:
            assert len(breaks) >= 1
            # per (出来高超過率%) は 0 床で非負
            per = int(breaks[0].split(",")[1])
            assert per >= 0
        else:
            assert len(breaks) == 0

    @pytest.mark.parametrize(
        "today_close, today_low, prev_close, expect_regular, expect_ext",
        [
            # 乖離+9%: 正規ブレイク(+5%以内)から弾かれ extended に入る
            (1150, 1120, 1100, False, True),
            # 乖離+22%: 当日は extended、前日もブレイク条件を満たし regular に入る
            (1300, 1280, 1250, True, True),
            # 乖離が小さい(押し目位置): 正規ブレイクで extended には入らない
            (1080, 1070, 1050, True, False),
        ],
    )
    def test_breakout_extended(self, today_close, today_low, prev_close,
                               expect_regular, expect_ext):
        """高値追い圏 (+5% < kairi <= 25%) で弾かれたブレイク候補が
        breakout_extended に入ること (詳細チャートの半透明マーカー用)。"""
        from datetime import date as _date, timedelta
        d0 = _date(2025, 12, 31)
        price_list = []
        for i in range(25):
            dt = d0 - timedelta(days=i)
            date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
            if i == 0:
                close, low, vol = today_close, today_low, 300000
            elif i == 1:
                close, low, vol = prev_close, prev_close - 5, 100000
            else:
                close, low, vol = 1000, 995, 100000  # 横ばい → ma10≈1000
            price_list.append([date_str, close, close + 10, low, close, vol, close])
        result_dict, _ = price.parse_price_text_from_list(today_close, price_list)
        assert (len(result_dict["breakout"]) >= 1) == expect_regular
        ext = result_dict["breakout_extended"]
        assert (len(ext) >= 1) == expect_ext
        # extended は "MM/DD,kairi,per" の3要素形式 (per=出来高超過率、マーカー強度用)
        if ext:
            assert len(str(ext[0]).split(",")) == 3

    @pytest.mark.parametrize("bd,next_close,expected", [
        # 7カラム経路 (close=adj_close=[6]) の porosity 回帰。割れ日 (bd) で
        # adj_close だけ10maを割り、翌営業日 (bd-1) の終値回復で維持/切断が分岐することを検証。
        (5, None, True),   # 翌日終値 > 翌日10ma → porosity 救済 → 30連続維持
        (5, 1, False),     # 翌日終値 <= 翌日10ma → violation 成立 → 切断
        # 最新日 (bd=0) の割れは翌営業日が未到来 = violation 未確定 → 不成立 (False)。
        (0, None, False),
    ])
    def test_ma10_streak_porosity_7col(self, bd, next_close, expected):
        """parse_price_text_from_list (7カラム経路) でも porosity 判定が効くことの回帰。

        全日 adj_close>10ma を確実に満たすよう刻みを大きく取り、39本で30連続窓を
        1つ (i=0..29) に絞る。bd をその窓に含め violation 成立時のみ False になる構成。
        """
        from datetime import date as _date, timedelta
        d0 = _date(2025, 12, 31)
        price_list = []
        # i 小 (新しい) ほど高値になる急上昇データ → 全日 adj_close>10ma
        for i in range(39):
            dt = d0 - timedelta(days=i)
            date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
            close = 1000 + (38 - i) * 50  # 最新ほど高い
            price_list.append([date_str, close, close + 20, close - 10,
                               close, 100000 + i * 1000, close])
        d, o, h, _l, c, v, _adj = price_list[bd]
        # 割れ日 bd: adj_close ([6]) だけ10maを割り込ませ、安値 ([3]) は基準値 500
        price_list[bd] = [d, o, h, 500, c, v, 1]
        # bd>0 のときのみ翌営業日 bd-1 の adj_close ([6]) を可変 (翌日10ma回復の有無で判定)
        if bd > 0 and next_close is not None:
            d2, o2, h2, l2, c2, v2, _adj2 = price_list[bd - 1]
            price_list[bd - 1] = [d2, o2, h2, l2, c2, v2, next_close]
        result_dict, _ = price.parse_price_text_from_list(1050, price_list)
        assert result_dict["ma10_above_streak_30"] is expected


# ==================================================
# get_momentum_calib (issue #104)
# ==================================================
from datetime import datetime, timedelta


class TestGetMomentumCalib:
    """モメンタム calib 取得とフォールバック判定のテスト

    正常系1本 + 「壊れた calib は fallback」を parametrize で集約。
    """

    def test_returns_calib_when_valid(self):
        """有効な calib は値を返す (source=calib)"""
        market_db = {
            "momentum_calib": {
                "loc": -0.10,
                "scale": 0.25,
                "sample_count": 1000,
                "updated_at": datetime.now() - timedelta(days=1),
            }
        }
        loc, scale, source = price.get_momentum_calib(market_db=market_db)
        assert (loc, scale, source) == (-0.10, 0.25, "calib")

    @pytest.mark.parametrize(
        "calib, reason",
        [
            (None, "no_calib"),  # calib 自体無し → market_db = {}
            (
                {"loc": -0.10, "scale": 0.25, "sample_count": 100,
                 "updated_at": datetime.now()},
                "sample_count_too_low",
            ),
            (
                {"loc": -0.10, "scale": 0.25, "sample_count": 1000},
                "updated_at_missing",
            ),
            (
                {"loc": -0.10, "scale": 0.25, "sample_count": 1000,
                 "updated_at": datetime.now() - timedelta(
                     days=price.MOMENTUM_CALIB_MAX_AGE_DAYS + 1)},
                "too_old",
            ),
            (
                {"scale": 0.25, "sample_count": 1000,
                 "updated_at": datetime.now() - timedelta(days=1)},
                "loc_missing",
            ),
            (
                {"loc": -0.10, "scale": 0, "sample_count": 1000,
                 "updated_at": datetime.now() - timedelta(days=1)},
                "scale_invalid",
            ),
        ],
    )
    def test_falls_back_when_calib_broken(self, calib, reason):
        """壊れた calib (各種パターン) はデフォルト値にフォールバック"""
        market_db = {"momentum_calib": calib} if calib is not None else {}
        loc, scale, source = price.get_momentum_calib(market_db=market_db)
        assert source == "fallback", f"reason={reason}"
        assert loc == price.MOMENTUM_CALIB_DEFAULT_LOC
        assert scale == price.MOMENTUM_CALIB_DEFAULT_SCALE


# ==================================================
# calc_momentum_pt_value (issue #104 Phase 2)
# ==================================================
class TestCalcMomentumPtLognormal:
    """対数正規分布モデルで momentum_pt を算出する純粋関数のテスト"""

    def _market_db_with_calib(self, loc, scale):
        """有効な calib 値を持つ market_db を組み立てる"""
        return {
            "topix": {"rs_raw": 1.0},
            "momentum_calib": {
                "loc": loc,
                "scale": scale,
                "sample_count": 1000,
                "updated_at": datetime.now() - timedelta(days=1),
            },
        }

    def test_returns_zero_when_topix_rs_raw_zero(self):
        """TOPIX rs_raw = 0 のとき 0"""
        market_db = {"topix": {"rs_raw": 0}}
        assert price.calc_momentum_pt_value(rs_raw=1.2, market_db=market_db) == 0

    def test_returns_zero_when_topix_missing(self):
        """TOPIX キーがないとき 0"""
        market_db = {}
        assert price.calc_momentum_pt_value(rs_raw=1.2, market_db=market_db) == 0

    def test_returns_zero_when_rs_raw_non_positive(self):
        """rs_raw <= 0 (異常値) のとき 0"""
        market_db = self._market_db_with_calib(loc=0.0, scale=0.3)
        assert price.calc_momentum_pt_value(rs_raw=0, market_db=market_db) == 0
        assert price.calc_momentum_pt_value(rs_raw=-1.0, market_db=market_db) == 0

    def test_rs_rel_at_mode_gives_50(self):
        """rs_rel = exp(loc) (対数正規分布の中央値) で momentum_pt ~ 50"""
        import math
        loc, scale = -0.05, 0.3
        market_db = self._market_db_with_calib(loc=loc, scale=scale)
        # TOPIX rs_raw=1.0 なので rs_rel == rs_raw
        rs_raw = math.exp(loc)
        result = price.calc_momentum_pt_value(rs_raw=rs_raw, market_db=market_db)
        assert 49 <= result <= 51

    def test_rs_rel_at_plus_one_sigma_gives_84(self):
        """rs_rel = exp(loc + scale) で momentum_pt ~ 84 (+1σ)"""
        import math
        loc, scale = -0.05, 0.3
        market_db = self._market_db_with_calib(loc=loc, scale=scale)
        rs_raw = math.exp(loc + scale)
        result = price.calc_momentum_pt_value(rs_raw=rs_raw, market_db=market_db)
        assert 83 <= result <= 85

    def test_rs_rel_at_minus_one_sigma_gives_16(self):
        """rs_rel = exp(loc - scale) で momentum_pt ~ 16 (-1σ)"""
        import math
        loc, scale = -0.05, 0.3
        market_db = self._market_db_with_calib(loc=loc, scale=scale)
        rs_raw = math.exp(loc - scale)
        result = price.calc_momentum_pt_value(rs_raw=rs_raw, market_db=market_db)
        assert 15 <= result <= 17

    def test_calib_vs_fallback_diverges(self):
        """キャリブ値とフォールバック値で結果が変わる"""
        # キャリブ: loc=0.0, scale=0.1 (scaleが狭い) → +0.1の rs_rel は +1σ → ~84
        # フォールバック: loc=-0.058, scale=0.275 → log(1.105)=0.0998, z=(0.0998+0.058)/0.275=0.575 → ~72
        import math
        market_db_calib = self._market_db_with_calib(loc=0.0, scale=0.1)
        rs_raw_at_plus_sigma = math.exp(0.1)
        result_calib = price.calc_momentum_pt_value(
            rs_raw=rs_raw_at_plus_sigma, market_db=market_db_calib
        )
        # フォールバック (calib なし)
        market_db_fb = {"topix": {"rs_raw": 1.0}}
        result_fb = price.calc_momentum_pt_value(
            rs_raw=rs_raw_at_plus_sigma, market_db=market_db_fb
        )
        assert result_calib != result_fb
        # キャリブ側は +1σ なので 80超
        assert result_calib >= 80


# ==================================================
# Stalling Day 判定 (issue #117 Part B)
# ==================================================
class TestStallingDay:
    """Stalling Day (停滞日) 判定"""

    def _row(self, date, open_p, high, low, close, ratio_pct, volume):
        """8要素タプルを作る"""
        return (
            date,
            "{:,}".format(open_p),
            "{:,}".format(high),
            "{:,}".format(low),
            "{:,}".format(close),
            "0",
            "{:.2f}".format(ratio_pct),
            "{:,}".format(volume),
        )

    def test_is_stalling_day_basic(self):
        """52週高値圏 + 微増 + 出来高増 + 下半分引け → True"""
        # 52週高値=1000、当日 close=975 (97.5% > 97%)、ratio=+0.2%、出来高増、下半分
        d = self._row("260501", 970, 985, 970, 975, 0.2, 12000)
        # 下半分: dl=970, dh=985, half=977.5、close=975 ≤ 977.5
        pd = self._row("260430", 968, 980, 965, 970, 0.0, 10000)
        assert price._is_stalling_day(d, pd, high52_weekly=1000) is True

    def test_is_stalling_day_skipped_below_high(self):
        """52週高値の97%未満なら False"""
        # close=900、52週高値=1000 → 90%
        d = self._row("260501", 895, 905, 890, 900, 0.2, 12000)
        pd = self._row("260430", 895, 905, 890, 898, 0.0, 10000)
        assert price._is_stalling_day(d, pd, high52_weekly=1000) is False

    def test_is_stalling_day_skipped_too_high_ratio(self):
        """前日比 +0.4% 以上なら False (FTD候補に近い)"""
        d = self._row("260501", 970, 985, 970, 985, 0.5, 12000)
        pd = self._row("260430", 968, 980, 965, 980, 0.0, 10000)
        assert price._is_stalling_day(d, pd, high52_weekly=1000) is False

    def test_is_stalling_day_skipped_negative_ratio(self):
        """前日比マイナスなら False (通常DDの領域)"""
        d = self._row("260501", 970, 985, 970, 975, -0.1, 12000)
        pd = self._row("260430", 968, 980, 965, 980, 0.0, 10000)
        assert price._is_stalling_day(d, pd, high52_weekly=1000) is False

    def test_is_stalling_day_skipped_no_volume_increase(self):
        """出来高増していなければ False"""
        d = self._row("260501", 970, 985, 970, 975, 0.2, 10000)
        pd = self._row("260430", 968, 980, 965, 970, 0.0, 12000)
        assert price._is_stalling_day(d, pd, high52_weekly=1000) is False

    def test_is_stalling_day_skipped_upper_close(self):
        """終値が日足の上半分なら False (下半分引けが条件)"""
        # close=982、low=970, high=985、half=977.5 → 上半分
        d = self._row("260501", 970, 985, 970, 982, 0.2, 12000)
        pd = self._row("260430", 968, 980, 965, 980, 0.0, 10000)
        assert price._is_stalling_day(d, pd, high52_weekly=1000) is False

    def test_is_stalling_day_skipped_no_high52(self):
        """high52_weekly が無ければ False"""
        d = self._row("260501", 970, 985, 970, 975, 0.2, 12000)
        pd = self._row("260430", 968, 980, 965, 970, 0.0, 10000)
        assert price._is_stalling_day(d, pd, high52_weekly=None) is False
        assert price._is_stalling_day(d, pd, high52_weekly=0) is False

    def test_add_stalling_days_appends(self):
        """add_stalling_days で既存DD に Stalling Day が追加される"""
        # 既に通常DDが1つあり、別の日が Stalling Day 候補
        daily_price_list = [
            self._row("260501", 970, 985, 970, 975, 0.2, 12000),  # Stalling 候補
            self._row("260430", 968, 980, 965, 970, 0.0, 10000),  # 前日
            self._row("260429", 980, 990, 970, 970, -0.5, 11000),  # 通常DD
            self._row("260428", 980, 990, 970, 975, 0.0, 9000),
        ]
        dic = {
            "distribution_days": ["260429"],
            "distribution_days_with_close": [("260429", 970.0)],
        }
        price.add_stalling_days(dic, daily_price_list, high52_weekly=1000)
        assert "260501" in dic["distribution_days"]
        assert ("260501", 975.0) in dic["distribution_days_with_close"]
        # 既存の通常DDは保持
        assert "260429" in dic["distribution_days"]

    def test_add_stalling_days_no_duplicate(self):
        """既に通常DDとして計上されている日は重複追加しない"""
        daily_price_list = [
            self._row("260501", 970, 985, 970, 975, 0.2, 12000),
            self._row("260430", 968, 980, 965, 970, 0.0, 10000),
        ]
        dic = {
            "distribution_days": ["260501"],
            "distribution_days_with_close": [("260501", 975.0)],
        }
        price.add_stalling_days(dic, daily_price_list, high52_weekly=1000)
        # 同じ日付が2回追加されていないこと
        assert dic["distribution_days"].count("260501") == 1
        assert len(dic["distribution_days_with_close"]) == 1

    def test_add_stalling_days_skipped_no_high52(self):
        """high52_weekly が無い場合は何もしない"""
        daily_price_list = [
            self._row("260501", 970, 985, 970, 975, 0.2, 12000),
        ]
        dic = {"distribution_days": [], "distribution_days_with_close": []}
        price.add_stalling_days(dic, daily_price_list, high52_weekly=None)
        assert dic["distribution_days"] == []


# ==================================================
# 月足位置評価 (issue #53)
# ==================================================
class TestCalcMonthlyPosition:
    """月足特徴量計算のテスト"""

    def _make_monthly_list(self, bars):
        """(high, low, close) のリスト (新しい月が先頭) から月足リストを生成する。
        日付は2026年6月から過去へ1ヶ月ずつ遡る月初ラベル。
        """
        rows = []
        year, month = 2026, 6
        for high, low, close in bars:
            date_str = "%d年%d月1日" % (year, month)
            rows.append((
                date_str, "{:,}".format(low), "{:,}".format(high),
                "{:,}".format(low), "{:,}".format(close), "0", "0", "1,000",
            ))
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        return rows

    def _make_bars(self, break_high=1500):
        """10年高安=3000/800、3年基準線=1200 の40ヶ月分バーを生成する。
        先頭 (最新月) の高値 break_high で月破の有無を制御できる。
        """
        bars = [(break_high, 1000, 1400)]                # 最新月
        bars += [(1200, 950, 1000) for _ in range(38)]   # 直近3年含む滞留期間
        bars += [(3000, 800, 900)]                       # 10年高安 (基準線窓の外)
        return bars

    def test_basic_features(self):
        """低位滞留からのブレイクシナリオで全特徴量を確認"""
        ml = self._make_monthly_list(self._make_bars(break_high=1500))
        mp = price._calc_monthly_position(ml, price_current=1400)
        assert mp["months"] == 40
        assert mp["high_10y"] == 3000
        assert mp["low_10y"] == 800
        assert mp["pos_10y_pct"] == 27.3  # (1400-800)/2200*100
        assert mp["high_3y_prior"] == 1200  # 直近3ヶ月を除く3年窓の高値
        assert mp["break_month"] == "2026-06"  # 最新月高値1500 > 1200
        assert mp["pos_3y_median_pct"] == 9.1  # 終値1000のレンジ位置中央値

    def test_no_break_month(self):
        """基準線を超えなければ break_month は None"""
        ml = self._make_monthly_list(self._make_bars(break_high=1100))
        mp = price._calc_monthly_position(ml, price_current=1000)
        assert mp["break_month"] is None

    @pytest.mark.parametrize(
        "ml, price_current",
        [
            ([], 1000),  # データなし
            ([("2026年6月1日", "1,000", "1,000", "1,000", "1,000", "0", "0", "100")], 1000),  # レンジゼロ
        ],
    )
    def test_returns_none(self, ml, price_current):
        """計算不能 (データなし・レンジゼロ) は None"""
        assert price._calc_monthly_position(ml, price_current) is None


class TestMonthlyConvertAndCacheFresh:
    """月足DataFrame変換 (当月バー除外) とキャッシュ鮮度判定のテスト"""

    def test_current_month_bar_excluded(self):
        """未確定の当月バーが除外されること"""
        import pandas as pd
        from datetime import datetime as _dt
        dates = pd.date_range(start="2026-01-01", periods=7, freq="MS")  # 1月〜7月
        df = pd.DataFrame({
            "Open": [1000] * 7, "High": [1100] * 7, "Low": [900] * 7,
            "Close": [1050] * 7, "Volume": [10000] * 7,
        }, index=dates)
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 7, 12, 20, 0)
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            result = price._convert_monthly_df_to_kabutan_format(df)
        assert len(result) == 6  # 当月 (2026-07) が除外される
        assert result[0][0] == "2026年6月1日"  # 新しい月が先頭

    @pytest.mark.parametrize(
        "head_label, expected",
        [
            ("2026年6月1日", True),   # 先月バーあり = 新鮮
            ("2026年5月1日", False),  # 先月バー欠落 = 古い
        ],
    )
    def test_freshness(self, head_label, expected):
        """先頭バーが先月の月初以降なら新鮮判定"""
        from datetime import datetime as _dt
        row = (head_label, "0", "0", "0", "0", "0", "0", "0")
        with patch("price.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 7, 12, 20, 0)
            mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
            assert price._is_monthly_cache_fresh([row]) is expected


# ==================================================
# calc_volume_dryup_breakout (週ブ, issue #384)
# ==================================================
def _make_vdb_price_list(volumes, closes):
    """出来高・終値リスト (新しい日が先頭) から price_list を組み立てる。

    calc_volume_dryup_breakout が参照するのは [5]=出来高 と [6]=終値のみ。
    日付は最新を先頭に 1日刻みで降順に振る。
    """
    price_list = []
    for i, (vol, close) in enumerate(zip(volumes, closes)):
        day = 30 - i  # 先頭ほど新しい日
        date_str = "2025年6月%d日" % day
        # [date, open, high, low, close, volume, adj_close]。判定は [5],[6] のみ使用。
        price_list.append([date_str, close, close, close, close, vol, close])
    return price_list


class TestCalcVolumeDryupBreakout:
    """週ブ (出来高dry up後の5日間出来高拡大ブレイクアウト) 検知テスト。"""

    # 基準となる setup 出来高・終値 (中央値=1000, 終値フラット=1000)。
    # 各ケースは先頭 (expand=直近5日 / dry up窓=直近10日) だけを差し替える。
    BASE_VOL = [1000] * 30
    BASE_CLOSE = [1000] * 30

    def _case(self, vol_head=None, close_head=None, length=26):
        """先頭を差し替えた (volumes, closes) を返す。length で末尾を切る。"""
        vols = list(self.BASE_VOL)
        closes = list(self.BASE_CLOSE)
        if vol_head:
            vols[: len(vol_head)] = vol_head
        if close_head:
            closes[: len(close_head)] = close_head
        return vols[:length], closes[:length]

    # 検出成立の基本形: expand=出来高拡大(3日以上≥1200,平均≥1500)、
    # index5 を強dry up(400≤450)、終値は expand が setup 20日高値(1000)を上抜け。
    DETECT_VOL = [1800, 1600, 1300, 1000, 1900] + [400] + [1000] * 20
    DETECT_CLOSE = [1100, 1080, 1070, 1060, 1050] + [1000] * 21

    def test_detects_dryup_then_expansion_breakout(self):
        """ケース1: dry up後の5日拡大+価格上抜けで検出され per/dryup 値も正しい。"""
        vols, closes = self._case(
            vol_head=self.DETECT_VOL, close_head=self.DETECT_CLOSE, length=25
        )
        price_list = _make_vdb_price_list(vols, closes)
        result = price.calc_volume_dryup_breakout(price_list)
        assert len(result) == 1
        mmdd, per, dryup = result[0].split(",")
        assert mmdd == "06/30"  # 最新日
        # per = 5日平均(1520)/中央値(1000)*100 = 152
        assert int(per) == 152
        # dryup = 直近10日最小(400)/中央値(1000)*100 = 40
        assert int(dryup) == 40

    def test_single_big_volume_not_detected(self):
        """ケース2: 直近5日で1日だけ極端な大商いでは検出されない (3日以上条件)。"""
        # index0 だけ突出、他 expand は基準以下。平均は 1.5 倍を超えるが ≥1200 は1日のみ。
        vol_head = [6000, 1000, 1000, 1000, 1000] + [400]
        vols, closes = self._case(
            vol_head=vol_head, close_head=self.DETECT_CLOSE, length=25
        )
        price_list = _make_vdb_price_list(vols, closes)
        assert price.calc_volume_dryup_breakout(price_list) == []

    def test_no_dryup_not_detected(self):
        """ケース3: 出来高拡大+上抜けがあっても dry up がなければ検出されない。"""
        # dry up 窓 (index5..14) を基準比 0.6 超に保つ (全て 1000)。
        vols, closes = self._case(
            vol_head=[1800, 1600, 1300, 1000, 1900],  # dry up 差し替えなし
            close_head=self.DETECT_CLOSE,
            length=25,
        )
        price_list = _make_vdb_price_list(vols, closes)
        assert price.calc_volume_dryup_breakout(price_list) == []

    def test_no_price_breakout_not_detected(self):
        """ケース4: dry up+出来高拡大があっても価格上抜けがなければ検出されない。"""
        # 終値をフラット (1000) に保つ → 最新終値 == setup高値で上抜けなし。
        vols, closes = self._case(
            vol_head=self.DETECT_VOL, close_head=None, length=25
        )
        price_list = _make_vdb_price_list(vols, closes)
        assert price.calc_volume_dryup_breakout(price_list) == []

    def test_intraweek_breakout_but_latest_back_in_range_not_detected(self):
        """ケース6: 5日内に瞬間ブレイクがあっても最新終値がレンジ内なら検出しない。

        直近5日のどこか1日が20日高値を超えても、発生日 (最新終値) が
        setup 高値以下ならレンジ内へ戻ったとみなす (Codex P2 指摘)。
        """
        # setup に古い高値 (index24=2000) を置き setup_close_high=2000。
        # expand は index1 で瞬間ブレイク (2100>2000) するが、最新 index0=1500 は
        # setup 高値未満。ただし MA10 (直近10日平均≒1179) は上回るため、上抜け条件
        # だけで弾かれることを確認する (MA10 条件では落ちない構成)。
        close_head = [1500, 2100, 1080, 1060, 1050]
        vols, closes = self._case(
            vol_head=self.DETECT_VOL, close_head=close_head, length=25
        )
        closes[24] = 2000  # setup 期間の古い高値
        price_list = _make_vdb_price_list(vols, closes)
        assert price.calc_volume_dryup_breakout(price_list) == []

    def test_no_duplicate_on_consecutive_days(self):
        """ケース5: 前日から条件継続でも重複発火せず、初成立日1件のみ記録される。"""
        # 26本用意し index0,1 の両方が成立する構成にする。expand を1日ずらしても
        # 拡大条件を満たすよう、先頭6日を出来高拡大帯にする。
        vol_head = [1800, 1600, 1300, 1500, 1900, 1400] + [400] + [1000] * 19
        close_head = [1120, 1100, 1080, 1070, 1060, 1050] + [1000] * 20
        price_list = _make_vdb_price_list(vol_head, close_head[:26])
        # index1 起点も成立することを確認 (前提の妥当性チェック)。
        assert price._vdb_check(price_list, 1) is not None
        assert price._vdb_check(price_list, 0) is not None
        result = price.calc_volume_dryup_breakout(price_list)
        # index0 は前日 (index1) も成立 → スキップ。index1 が初成立日として1件のみ。
        assert len(result) == 1
        assert result[0].split(",")[0] == "06/29"  # index1 の日付 (30-1)
