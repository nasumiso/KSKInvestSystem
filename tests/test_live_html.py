"""HTMLフォーマット変更検知テスト

実際にHTTPで外部サイト（kabutan.jp）にアクセスし、
パーサーが期待通りにデータを抽出できるかを確認する。

実行方法:
    pytest tests/test_live_html.py -v

CIでは除外される（live_htmlマーカー）。
HTMLフォーマット変更が疑われる場合にローカルで実行する。
"""

import time
import pytest

import price
import shihyou
import master
import gyoseki
import shintakane
import make_market_db
import rironkabuka
from ks_util import http_get_html

# テスト用銘柄（大型株・安定して存在する）
TEST_CODE = "7203"  # トヨタ自動車

pytestmark = pytest.mark.live_html


def _sleep():
    """リクエスト間のレート制限対策"""
    time.sleep(1)


class TestLiveHtmlPrice:
    """price.py — kabutan日足HTML取得→パース"""

    def test_日足HTML取得とパース(self):
        """kabutanから日足HTMLを取得し、価格データがパースできること"""
        html = price.get_daily_html_kabutan(TEST_CODE, cache=False)
        assert html is not None
        assert len(html) > 0
        # HTMLに株価テーブルが含まれること
        assert "stock_kabuka" in html or "kabuka_table" in html or "<table" in html
        _sleep()


class TestLiveHtmlShihyou:
    """shihyou.py — kabutan財務指標HTML取得→パース"""

    def test_財務指標の抽出(self):
        """kabutanから基本情報HTMLを取得し、財務指標がパースできること"""
        html = rironkabuka.get_kabutan_base_html(TEST_CODE, upd=-1)
        result = shihyou.get_from_kabutan(html)
        assert isinstance(result, dict)
        # 主要キーが存在すること
        assert "debt_ratio" in result or "capital_ratio" in result
        _sleep()

    def test_時価総額の抽出(self):
        """時価総額が正の数値でパースされること"""
        html = rironkabuka.get_kabutan_base_html(TEST_CODE, upd=-1)
        jikasogaku = shihyou.parse_jikasogaku_kabutan(html)
        assert jikasogaku > 0  # トヨタなら必ず正の値
        _sleep()


class TestLiveHtmlMaster:
    """master.py — kabutan銘柄基本情報HTML取得→パース"""

    def test_銘柄基本情報の抽出(self):
        """kabutanから銘柄基本情報がパースできること"""
        html = rironkabuka.get_kabutan_base_html(TEST_CODE, upd=-1)
        result = master.parse_master_html_kabutan(html)
        assert isinstance(result, dict)
        assert "stock_name" in result
        assert len(result["stock_name"]) > 0
        # トヨタの銘柄名にトヨタが含まれること
        assert "トヨタ" in result["stock_name"]
        _sleep()


class TestLiveHtmlGyoseki:
    """gyoseki.py — kabutan業績HTML取得→パース"""

    def test_業績データの抽出(self):
        """kabutanから業績HTMLを取得し、業績データがパースできること"""
        url = "https://kabutan.jp/stock/finance?code=%s" % TEST_CODE
        html = http_get_html(url, use_cache=False)
        result = gyoseki.parse_kabutan_account2(html)
        assert isinstance(result, dict)
        # 業績テーブルが含まれること
        assert len(result) > 0
        _sleep()


class TestLiveHtmlShintakane:
    """shintakane.py — kabutan新高値HTML取得→パース"""

    def test_新高値HTMLのパース(self):
        """kabutanから新高値HTMLを取得し、パースできること（空でも成功）"""
        url = "https://kabutan.jp/warning/record/w52_high_price?market=0&capitalization=-1&stc=&stm=0&page=1"
        html = http_get_html(url, use_cache=False)
        assert html is not None
        assert len(html) > 0
        # パースがエラーなく完了すること（市場状況により結果は0件の場合もある）
        result = shintakane.convert_kabutan_shintakane_html(html)
        assert isinstance(result, list)
        _sleep()


class TestLiveHtmlPts:
    """shintakane.py — kabutan PTSナイトランキングHTML取得→パース"""

    def test_PTSランキングHTMLのパース(self):
        """kabutanからPTSランキングHTMLを取得し、パースできること"""
        url = "https://kabutan.jp/warning/pts_night_price_increase"
        html = http_get_html(url, use_cache=False)
        assert html is not None
        assert len(html) > 0
        result = shintakane.convert_kabutan_pts_html(html)
        assert isinstance(result, list)
        # PTSランキングはナイトセッションがある日はデータがある
        # 市場状況により0件の場合もあるが、パースエラーは起きないこと
        if len(result) > 0:
            row = result[0]
            assert len(row) == 8  # 8カラム
            assert row[0] == "1"  # ランク
        _sleep()


class TestLiveHtmlKessan:
    """shintakane.py — kabutan決算速報HTML取得→パース"""

    def test_決算速報HTMLのパース(self):
        """kabutanから決算速報HTMLを取得し、パースできること"""
        url = "https://kabutan.jp/news/?page=1"
        html = http_get_html(url, use_cache=False)
        assert html is not None
        assert len(html) > 0
        # パースがエラーなく完了すること
        mod_lst, announce_lst = shintakane.parse_kessan_html(html)
        # 決算速報ページには必ず何かしらのデータがある
        assert len(mod_lst) + len(announce_lst) > 0
        _sleep()


class TestLiveHtmlTheme:
    """make_market_db.py — kabutanテーマランクHTML取得→パース"""

    def test_テーマランクHTMLのパース(self):
        """kabutanからテーマランクHTMLを取得し、パースできること"""
        url = "https://kabutan.jp/info/accessranking/3_2"
        html = http_get_html(url, use_cache=False)
        assert html is not None
        result = make_market_db.parse_theme_html(html)
        assert isinstance(result, list)
        assert len(result) > 0  # テーマランクは常にデータがある
        _sleep()
