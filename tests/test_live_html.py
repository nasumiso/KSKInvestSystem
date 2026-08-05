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
import disclosure
import shintakane
import make_market_db
import market_breadth
import rironkabuka
from ks_util import http_get_html, UPD_CACHE, UPD_FORCE

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
        """kabutanからfinanceページHTMLを取得し、財務指標がパースできること"""
        html = rironkabuka.get_kabutan_html(TEST_CODE, upd=-1)
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

    def test_自己資本実額の抽出(self):
        """財務テーブルから自己資本(実額・億円)が抽出できること"""
        html = rironkabuka.get_kabutan_html(TEST_CODE, upd=-1)
        result = shihyou.get_from_kabutan(html)
        assert "jikoshihon" in result
        assert result["jikoshihon"] > 0  # トヨタなら兆円規模
        _sleep()

    def test_現金等残高の抽出(self):
        """CFテーブルから現金等残高(億円)が抽出できること"""
        html = rironkabuka.get_kabutan_html(TEST_CODE, upd=-1)
        cash = shihyou.parse_cash_kabutan(html)
        assert cash is not None
        assert cash > 0
        _sleep()

    def test_EVR統合計算(self):
        """analyze_from_kabutan が EV_Sales を計算できること"""
        result = shihyou.analyze_from_kabutan(TEST_CODE, upd=-1)
        assert "EV_Sales" in result
        # トヨタは大型製造業なので EVR は正値で 1.0 前後の想定。
        # ネットキャッシュ企業の場合は負値もあり得るため範囲は緩めに見る。
        assert isinstance(result["EV_Sales"], float)
        _sleep()

    def test_通期業績テーブルからPER_PSRが算出できる(self):
        """業績テーブル (売上高/経常益/最終益) から PER/MPER/PSR が算出できること。

        2026-08 の株探フォーマット変更 (base ページは gyouseki_block 維持・
        finance ページは fin_year_result_d へ) で MPER/PSR が全欠損したが、既存テストは
        別関数のキーだけを見ていたため素通りした。
        本番と同じ analyze_from_kabutan 経由 (= base ページ) で検証する。
        値の存在に加えて桁 (単位換算ミス) も検証する。

        取得は2段階にする。UPD_FORCE で最新HTMLをDLしてキャッシュを更新した上で、
        判定は UPD_CACHE (=キャッシュ読み) で行う。ファイルキャッシュ経由では改行が
        LF に正規化されるため、通信直後のHTML (CRLF) だけを見ると本番と条件が変わり、
        CRLF 前提の正規表現バグを取りこぼす。
        """
        shihyou.analyze_from_kabutan(TEST_CODE, upd=UPD_FORCE)  # 最新HTMLをキャッシュへ
        result = shihyou.analyze_from_kabutan(TEST_CODE, upd=UPD_CACHE)  # 本番と同じ読み方
        for key in ("MPER", "PER", "PSR"):
            assert key in result, f"{key} が欠損 (業績テーブルのフォーマット変更疑い)"
        # 単位換算ミス (100倍/1/100) を検知する。トヨタは PER 10 前後・PSR 1 前後。
        assert 1 < result["PER"] < 200, f"PER の桁が異常: {result['PER']}"
        assert 1 < result["MPER"] < 200, f"MPER の桁が異常: {result['MPER']}"
        assert 0.1 < result["PSR"] < 20, f"PSR の桁が異常: {result['PSR']}"
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


class TestLiveHtmlCreditBalance:
    """market_breadth.py — nikkei225jp.com dailyweek2.json 取得→パース (issue #211)"""

    def test_信用評価損益率の抽出(self):
        """dailyweek2.json から直近 30 週以内に float 化可能な確定値があること"""
        rows = market_breadth.fetch_credit_balance_weekly()
        assert isinstance(rows, list)
        assert len(rows) > 0, "信用評価率の確定値が 1 件も取れていない"
        # 直近 30 週分に float 化可能な値があるか
        recent = rows[-30:]
        assert all(isinstance(r["credit_eval_rate"], float) for r in recent)
        # 日付昇順 (parse_credit_balance の契約)
        dates = [r["date"] for r in recent]
        assert dates == sorted(dates)
        _sleep()


class TestLiveHtmlNikkei225jp:
    """market_breadth.py — nikkei225jp.com 日経VI / 新高値新安値 取得→パース (issue #292)

    JSON ファイル名 (SL161_1990.json / daily2year.json) や列構成が変わると失敗する。
    """

    def test_日経VIの抽出(self):
        rows = market_breadth.fetch_nikkei_vi()
        assert isinstance(rows, list) and len(rows) > 0
        recent = rows[-30:]
        assert all(isinstance(r["nikkei_vi"], float) for r in recent)
        dates = [r["date"] for r in recent]
        assert dates == sorted(dates)
        _sleep()

    def test_新高値新安値の抽出(self):
        rows = market_breadth.fetch_new_high_low()
        assert isinstance(rows, list) and len(rows) > 0
        recent = rows[-30:]
        assert all(isinstance(r["new_high"], int) and isinstance(r["new_low"], int)
                   for r in recent)
        dates = [r["date"] for r in recent]
        assert dates == sorted(dates)
        _sleep()


class TestLiveHtmlDisclosure:
    """disclosure.py — kabutanニュースHTML取得→パース"""

    def test_ニュースデータの抽出(self):
        """kabutanからニュースHTMLを取得し、全カテゴリがパースできること"""
        import requests
        url = "https://kabutan.jp/stock/news?code=7203"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text
        assert html is not None
        records = disclosure.parse_disclosure_html(html)
        assert isinstance(records, list)
        assert len(records) > 0, "ニュースが1件も取得できていない"
        # 開示のみ取得できる壊れた状態を検知: kaiji以外が必ず含まれること
        types = {r["type"] for r in records}
        non_kaiji = types - {"kaiji"}
        assert len(non_kaiji) > 0, (
            "kaiji以外のニュースカテゴリが取得できていない "
            "(HTMLフォーマット変更？ 取得type: %s)" % types
        )
        _sleep()
