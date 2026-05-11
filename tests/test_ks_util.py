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
    """営業日ベース日付判定のテスト（17:00 境界）"""

    def test_before_cutoff(self):
        """17:00 前は前日"""
        dt = datetime(2025, 6, 10, 16, 59)
        assert ks_util.get_price_day(dt) == date(2025, 6, 9)

    def test_at_cutoff(self):
        """17:00 ちょうどは当日"""
        dt = datetime(2025, 6, 10, 17, 0)
        assert ks_util.get_price_day(dt) == date(2025, 6, 10)

    def test_after_cutoff(self):
        """17:00 より後は当日"""
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


# ==================================================
# http_get_html (issue #43)
# ==================================================
class TestHttpGetHtmlSession:
    """http_get_html の Session フォールバック / 例外パスのテスト

    issue #43:
      - _global_session を廃止、ContextVar (use_requests_session) と直接 requests.get の
        2 段階フォールバックに簡略化
      - ConnectionError / ReadTimeout 発生時は常に ("", 500) を返す
        (旧コード: `res.status_code if "res" in locals() else 500` は常に 500 を返す
         死にコードだったので簡略化)
    """

    def test_uses_context_session_when_available(self, monkeypatch, tmp_path):
        """ContextVarセッションが有効ならそのSession.get経由で通信する"""
        from unittest.mock import MagicMock

        # キャッシュ書込時の Path.relative_to(DATA_DIR) のため、DATA_DIR を tmp_path に差し替え
        monkeypatch.setattr(ks_util, "DATA_DIR", str(tmp_path))

        mock_response = MagicMock()
        mock_response.text = "<html>ctx</html>"
        mock_response.status_code = 200
        mock_response.encoding = "utf-8"

        # 直接 requests.get が呼ばれていないことを検出するため
        direct_get = MagicMock(
            side_effect=AssertionError("requests.get should not be called")
        )
        monkeypatch.setattr(ks_util.requests, "get", direct_get)

        with ks_util.use_requests_session() as session:
            session.get = MagicMock(return_value=mock_response)
            html = ks_util.http_get_html(
                "http://example.com/test1",
                use_cache=False,
                cache_dir=str(tmp_path),
            )
            assert html == "<html>ctx</html>"
            session.get.assert_called_once()
            direct_get.assert_not_called()

    def test_falls_back_to_direct_requests_get(self, monkeypatch, tmp_path):
        """ContextVarセッションが無い場合は requests.get を直接使う"""
        from unittest.mock import MagicMock

        monkeypatch.setattr(ks_util, "DATA_DIR", str(tmp_path))

        mock_response = MagicMock()
        mock_response.text = "<html>direct</html>"
        mock_response.status_code = 200
        mock_response.encoding = "utf-8"
        direct_get = MagicMock(return_value=mock_response)
        monkeypatch.setattr(ks_util.requests, "get", direct_get)

        # ContextVar セッションを使わない
        html = ks_util.http_get_html(
            "http://example.com/test2",
            use_cache=False,
            cache_dir=str(tmp_path),
        )
        assert html == "<html>direct</html>"
        direct_get.assert_called_once()

    def test_returns_500_on_connection_error_with_status(self, monkeypatch, tmp_path):
        """ConnectionError 発生時、with_status=True なら ("", 500) を返す"""
        from unittest.mock import MagicMock
        import requests

        direct_get = MagicMock(
            side_effect=requests.exceptions.ConnectionError("boom")
        )
        monkeypatch.setattr(ks_util.requests, "get", direct_get)

        result = ks_util.http_get_html(
            "http://example.com/error",
            use_cache=False,
            cache_dir=str(tmp_path),
            with_status=True,
        )
        assert result == ("", 500)

    def test_returns_500_on_read_timeout_with_status(self, monkeypatch, tmp_path):
        """ReadTimeout 発生時、with_status=True なら ("", 500) を返す"""
        from unittest.mock import MagicMock
        import requests

        direct_get = MagicMock(
            side_effect=requests.exceptions.ReadTimeout("timed out")
        )
        monkeypatch.setattr(ks_util.requests, "get", direct_get)

        result = ks_util.http_get_html(
            "http://example.com/timeout",
            use_cache=False,
            cache_dir=str(tmp_path),
            with_status=True,
        )
        assert result == ("", 500)

    def test_returns_empty_on_connection_error_without_status(
        self, monkeypatch, tmp_path
    ):
        """ConnectionError 発生時、with_status=False なら "" のみ返す"""
        from unittest.mock import MagicMock
        import requests

        direct_get = MagicMock(
            side_effect=requests.exceptions.ConnectionError("boom")
        )
        monkeypatch.setattr(ks_util.requests, "get", direct_get)

        html = ks_util.http_get_html(
            "http://example.com/error_nostatus",
            use_cache=False,
            cache_dir=str(tmp_path),
        )
        assert html == ""

    def test_global_session_removed(self):
        """issue #43: _global_session / use_requests_global_session は削除済み"""
        assert not hasattr(ks_util, "_global_session")
        assert not hasattr(ks_util, "use_requests_global_session")


class TestUseRequestsSessionThreadIsolation:
    """ThreadPoolExecutor の各ワーカーで Session が独立して生成されることを確認

    issue #43: 旧 _global_session は複数スレッドで requests.Session を共有して
    レースコンディションを起こしていた。ContextVar 版 use_requests_session を
    各ワーカーで呼ぶ形にすると、ワーカーごとに独立した Session が生成される。
    """

    def test_each_worker_gets_distinct_session(self):
        from concurrent.futures import ThreadPoolExecutor

        def worker(_):
            with ks_util.use_requests_session() as s:
                return id(s)

        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(worker, range(20)))

        # ThreadPoolExecutor 5 ワーカー × 20 タスクで、単一 Session 共有
        # (= 旧 _global_session の挙動) ではないことを検証。
        # 同一スレッドが再利用された場合は use_requests_session の終了時に
        # session.close され、次回呼び出しで新規 Session が作られる。
        # 並列度 5 で実行しているので、少なくとも 5 種類の Session id が
        # 生成されていれば「単一 Session 共有」ではないことが言える。
        assert len(set(results)) >= 5
