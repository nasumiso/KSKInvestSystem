"""make_stock_db.py のロジックテスト"""

from datetime import datetime, timedelta
import pytest

import make_stock_db


# ==================================================
# has_price_data
# ==================================================
class TestHasPriceData:
    """価格データ鮮度チェックのテスト"""

    def test_no_code_in_db(self):
        """DBに銘柄がない場合"""
        stocks = {}
        assert make_stock_db.has_price_data(stocks, "1234") is False

    def test_no_sell_pressure(self):
        """銘柄はあるが sell_pressure_ratio がない"""
        stocks = {"1234": {"stock_name": "Test"}}
        assert make_stock_db.has_price_data(stocks, "1234") is False

    def test_has_data_no_latest(self):
        """latest=False でデータあり"""
        stocks = {"1234": {"sell_pressure_ratio": [50, 60, 40, 2.5, 1.8]}}
        assert make_stock_db.has_price_data(stocks, "1234", latest=False) is True


# ==================================================
# has_gyoseki_data
# ==================================================
class TestHasGyosekiData:
    """業績データ鮮度チェックのテスト"""

    def test_no_code(self):
        """DBに銘柄がない場合"""
        stocks = {}
        assert make_stock_db.has_gyoseki_data(stocks, "1234") is False

    def test_no_access_date(self):
        """access_date_gyoseki がない場合"""
        stocks = {"1234": {"stock_name": "Test"}}
        assert make_stock_db.has_gyoseki_data(stocks, "1234") is False

    def test_has_data_no_latest(self):
        """latest=False でアクセス日あり"""
        stocks = {"1234": {"access_date_gyoseki": datetime(2025, 1, 1)}}
        assert make_stock_db.has_gyoseki_data(stocks, "1234", latest=False) is True


# ==================================================
# get_trend_template_expr
# ==================================================
class TestGetTrendTemplateExpr:
    """トレンドテンプレート表示のテスト"""

    def test_no_key(self):
        """trend_template キーがない場合"""
        assert make_stock_db.get_trend_template_expr({}) == "-"

    def test_perfect(self):
        """全条件クリア（空リスト）"""
        assert make_stock_db.get_trend_template_expr({"trend_template": []}) == "◎"

    def test_minor_miss(self):
        """1〜2条件ミス"""
        result = make_stock_db.get_trend_template_expr({"trend_template": ["MA50"]})
        assert result.startswith("◯")
        assert "MA50" in result

    def test_moderate_miss(self):
        """3〜4条件ミス"""
        result = make_stock_db.get_trend_template_expr(
            {"trend_template": ["a", "b", "c"]}
        )
        assert result == "▲"

    def test_many_miss(self):
        """5〜6条件ミス"""
        result = make_stock_db.get_trend_template_expr(
            {"trend_template": ["a", "b", "c", "d", "e"]}
        )
        assert result == "△"

    def test_all_miss(self):
        """7条件以上ミス"""
        result = make_stock_db.get_trend_template_expr(
            {"trend_template": ["a", "b", "c", "d", "e", "f", "g"]}
        )
        assert result == ""


# ==================================================
# make_signal
# ==================================================
class TestMakeSignal:
    """シグナル生成ロジックのテスト"""

    def test_empty_stock(self):
        """空の銘柄データ"""
        signal, tags = make_stock_db.make_signal({})
        assert isinstance(signal, str)
        assert isinstance(tags, list)

    def test_no_signals(self):
        """シグナルなしの通常データ"""
        stock = {
            "sell_pressure_ratio": [50, 50, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        signal, tags = make_stock_db.make_signal(stock)
        assert "売" not in tags
        assert "警" not in tags

    def test_high_sell_pressure(self):
        """買われ過ぎシグナル"""
        stock = {
            "sell_pressure_ratio": [50, 80, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        signal, tags = make_stock_db.make_signal(stock)
        assert "[買過]" in signal

    def test_low_sell_pressure(self):
        """売られ過ぎシグナル"""
        stock = {
            "sell_pressure_ratio": [50, 20, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        signal, tags = make_stock_db.make_signal(stock)
        assert "[売過]" in signal
