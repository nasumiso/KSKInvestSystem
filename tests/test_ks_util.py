"""ks_util.py の純粋関数テスト"""

from datetime import datetime, date, timedelta
import pytest

import ks_util


# ==================================================
# step_func
# ==================================================
class TestStepFunc:
    """区間関数 step_func のテスト"""

    def test_basic(self):
        """基本的な区間マッチ"""
        # val=35 は xs[1]=30 を超えるので ys[1] にマッチ
        result = ks_util.step_func(35, [0, 30, 60], [100, 50, 0])
        assert result == 50

    def test_below_first(self):
        """最小区間より小さい場合は ys[0]（min_val未指定）"""
        result = ks_util.step_func(-5, [0, 30, 60], [100, 50, 0])
        assert result == 100

    def test_above_last(self):
        """最大区間を超える場合は最後の ys"""
        result = ks_util.step_func(100, [0, 30, 60], [100, 50, 0])
        assert result == 0

    def test_exact_boundary(self):
        """境界値ちょうどは「超える」に含まれない"""
        # val=30 は xs[1]=30 を「超えない」ので xs[0]=0 にマッチ
        result = ks_util.step_func(30, [0, 30, 60], [100, 50, 0])
        assert result == 100

    def test_just_above_boundary(self):
        """境界値をわずかに超える場合"""
        result = ks_util.step_func(30.1, [0, 30, 60], [100, 50, 0])
        assert result == 50

    def test_min_val(self):
        """min_val 指定時、最小区間以下で min_val を返す"""
        result = ks_util.step_func(-5, [0, 30, 60], [100, 50, 0], min_val=-1)
        assert result == -1

    def test_equity_ratio_mapping(self):
        """理論株価で使われる自己資本比率のマッピング"""
        # equity_ratio=45 → xs[2]=33 を超えるので ys[2]=65
        result = ks_util.step_func(
            45, [0, 10, 33, 50, 67, 80], [50, 60, 65, 70, 75, 80]
        )
        assert result == 65


# ==================================================
# average
# ==================================================
class TestAverage:
    """平均関数のテスト"""

    def test_normal(self):
        assert ks_util.average([1, 2, 3, 4, 5]) == 3.0

    def test_single(self):
        assert ks_util.average([42]) == 42.0

    def test_float(self):
        assert ks_util.average([1.5, 2.5]) == 2.0

    def test_empty_raises(self):
        """空リストは ZeroDivisionError"""
        with pytest.raises((ZeroDivisionError, TypeError)):
            ks_util.average([])


# ==================================================
# cramp
# ==================================================
class TestCramp:
    """値のクランプ関数のテスト"""

    def test_within_range(self):
        assert ks_util.cramp(5, 0, 10) == 5

    def test_below_low(self):
        assert ks_util.cramp(-5, 0, 10) == 0

    def test_above_high(self):
        assert ks_util.cramp(15, 0, 10) == 10

    def test_at_boundary(self):
        assert ks_util.cramp(0, 0, 10) == 0
        assert ks_util.cramp(10, 0, 10) == 10


# ==================================================
# sumproduct
# ==================================================
class TestSumproduct:
    """内積計算のテスト"""

    def test_basic(self):
        # 1*4 + 2*5 + 3*6 = 4 + 10 + 18 = 32
        assert ks_util.sumproduct([1, 2, 3], [4, 5, 6]) == 32

    def test_single_element(self):
        assert ks_util.sumproduct([3], [7]) == 21

    def test_three_lists(self):
        # 1*2*3 + 4*5*6 = 6 + 120 = 126
        assert ks_util.sumproduct([1, 4], [2, 5], [3, 6]) == 126


# ==================================================
# get_price_day
# ==================================================
class TestGetPriceDay:
    """営業日ベース日付判定のテスト（18:00 境界）"""

    def test_before_cutoff(self):
        """18:00 前は前日"""
        dt = datetime(2025, 6, 10, 17, 59)
        assert ks_util.get_price_day(dt) == date(2025, 6, 9)

    def test_at_cutoff(self):
        """18:00 ちょうどは当日"""
        dt = datetime(2025, 6, 10, 18, 0)
        assert ks_util.get_price_day(dt) == date(2025, 6, 10)

    def test_after_cutoff(self):
        """18:00 より後は当日"""
        dt = datetime(2025, 6, 10, 20, 0)
        assert ks_util.get_price_day(dt) == date(2025, 6, 10)

    def test_midnight(self):
        """深夜0時は前日"""
        dt = datetime(2025, 6, 10, 0, 0)
        assert ks_util.get_price_day(dt) == date(2025, 6, 9)


# ==================================================
# get_db_code / set_db_code
# ==================================================
class TestDbCode:
    """銘柄コード取得・設定のテスト"""

    def test_set_and_get(self):
        rec = {}
        ks_util.set_db_code(rec, "1234")
        assert ks_util.get_db_code(rec) == "1234"

    def test_set_from_int(self):
        """整数を渡しても文字列として格納"""
        rec = {}
        ks_util.set_db_code(rec, 5678)
        assert rec["code_s"] == "5678"

    def test_get_fallback_to_code_int(self):
        """code_s がなければ code(int) からフォールバック"""
        rec = {"code": 42}
        assert ks_util.get_db_code(rec) == "0042"

    def test_get_no_code(self):
        """どちらもなければ空文字列"""
        rec = {}
        assert ks_util.get_db_code(rec) == ""

    def test_alphabetic_code(self):
        """アルファベット入り銘柄コード"""
        rec = {}
        ks_util.set_db_code(rec, "215A")
        assert ks_util.get_db_code(rec) == "215A"
