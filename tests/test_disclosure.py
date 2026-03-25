"""disclosure.py のテスト"""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import disclosure


class TestFilterRecentNews:
    """filter_recent_news のテスト"""

    def test_直近3日以内のレコードのみ残る(self):
        today = datetime.today()
        records = [
            {"date": (today - timedelta(days=0)).strftime("%Y%m%d"), "type": "zairyo"},
            {"date": (today - timedelta(days=2)).strftime("%Y%m%d"), "type": "kessan"},
            {"date": (today - timedelta(days=3)).strftime("%Y%m%d"), "type": "modify"},
            {"date": (today - timedelta(days=5)).strftime("%Y%m%d"), "type": "kaiji"},
            {"date": (today - timedelta(days=10)).strftime("%Y%m%d"), "type": "special"},
        ]
        result = disclosure.filter_recent_news(records, days=3)
        assert len(result) == 3  # 0日前、2日前、3日前

    def test_空リスト(self):
        result = disclosure.filter_recent_news([], days=3)
        assert result == []

    def test_すべて古いレコード(self):
        today = datetime.today()
        records = [
            {"date": (today - timedelta(days=30)).strftime("%Y%m%d"), "type": "zairyo"},
        ]
        result = disclosure.filter_recent_news(records, days=3)
        assert result == []

    def test_days引数で期間を変更できる(self):
        today = datetime.today()
        records = [
            {"date": (today - timedelta(days=0)).strftime("%Y%m%d"), "type": "zairyo"},
            {"date": (today - timedelta(days=5)).strftime("%Y%m%d"), "type": "kessan"},
            {"date": (today - timedelta(days=10)).strftime("%Y%m%d"), "type": "modify"},
        ]
        result = disclosure.filter_recent_news(records, days=7)
        assert len(result) == 2  # 0日前と5日前


def _make_todays_csv(path, rows):
    """テスト用のtodays_disclosure.csvを作成するヘルパー"""
    import csv
    with open(path, "w", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["日付", "銘柄コード", "銘柄名", "種類", "本文"])
        for row in rows:
            w.writerow(row)


class TestLoadTodaysNews:
    """load_todays_news のテスト"""

    def test_正常にCSVを読み込める(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "disclosure", "todays_disclosure.csv")
            os.makedirs(os.path.dirname(csv_path))
            _make_todays_csv(csv_path, [
                [
                    "20260314",
                    '=HYPERLINK("https://kabutan.jp/stock/chart?code=4422","4422")',
                    "VALUENEX",
                    "材料",
                    '=HYPERLINK("https://kabutan.jp/stock/news?code=4422&b=n202603140788","今週の話題株ダイジェスト")',
                ],
            ])
            with patch.object(disclosure, "DATA_DIR", tmpdir):
                result = disclosure.load_todays_news()
            assert "4422" in result
            assert len(result["4422"]) == 1
            date_e, type_e, heading, url = result["4422"][0]
            assert date_e == "26/03/14"
            assert type_e == "材料"
            assert heading == "今週の話題株ダイジェスト"
            assert "4422" in url

    def test_銘柄別に最大3件(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "disclosure", "todays_disclosure.csv")
            os.makedirs(os.path.dirname(csv_path))
            rows = []
            for i in range(5):
                rows.append([
                    "2026031%d" % i,
                    '=HYPERLINK("https://kabutan.jp/stock/chart?code=1301","1301")',
                    "極洋",
                    "材料",
                    '=HYPERLINK("https://kabutan.jp/news/%d","ニュース%d")' % (i, i),
                ])
            _make_todays_csv(csv_path, rows)
            with patch.object(disclosure, "DATA_DIR", tmpdir):
                result = disclosure.load_todays_news()
            assert len(result["1301"]) == 3

    def test_ファイルが存在しない場合は空辞書(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(disclosure, "DATA_DIR", tmpdir):
                result = disclosure.load_todays_news()
            assert result == {}

    def test_HYPERLINK式のパース(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "disclosure", "todays_disclosure.csv")
            os.makedirs(os.path.dirname(csv_path))
            _make_todays_csv(csv_path, [
                [
                    "20260313",
                    '=HYPERLINK("https://kabutan.jp/stock/chart?code=215A","215A")',
                    "テスト銘柄",
                    "開示",
                    '=HYPERLINK("https://kabutan.jp/disclosures/pdf/20260313/test","決算短信")',
                ],
            ])
            with patch.object(disclosure, "DATA_DIR", tmpdir):
                result = disclosure.load_todays_news()
            assert "215A" in result
            date_e, type_e, heading, url = result["215A"][0]
            assert date_e == "26/03/13"
            assert type_e == "開示"
            assert heading == "決算短信"
            assert url == "https://kabutan.jp/disclosures/pdf/20260313/test"


# ==================================================
# parse_disclosure_html — 新HTML構造テスト
# ==================================================

# 新HTML構造のfixture（実際のKabutanページを模した最小HTML）
_NEW_HTML_FIXTURE = """
<html>
<head><title>テスト銘柄【4334】｜ニュース｜株探（かぶたん）</title></head>
<body>
<table class="s_news_list mgbt0">
<tr>
<td class="news_time"><time datetime="2026-03-25T10:19:21+09:00">26/03/25&nbsp;10:19</time></td>
<td><div class="newslist_ctg newsctg2_b">材料</div></td>
<td><a href="/stock/news?code=4334&b=n202603250397">材料ニュース見出し</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-24T16:15:11+09:00">26/03/24&nbsp;16:15</time></td>
<td><div class="newslist_ctg newsctg3_kk_b" data-code="">決算</div></td>
<td><a href="/stock/news?code=4334&b=k202603240082">決算修正見出し</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-19T09:22:02+09:00">26/03/19&nbsp;09:22</time></td>
<td><div class="newslist_ctg newsctg12_b">５％</div></td>
<td><a href="/stock/news?code=4334&b=n202603190311">5パー見出し</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-16T11:03:00+09:00">26/03/16&nbsp;11:03</time></td>
<td><div class="newslist_ctg newsctg5_b">特集</div></td>
<td><a href="/stock/news?code=4334&b=n202603160561">特集ニュース見出し</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-15T15:38:35+09:00">26/03/15&nbsp;15:38</time></td>
<td><div class="newslist_ctg newsctg4_b">テク</div></td>
<td><img src="/images/cmn/premium_short_expired.svg" class="vat pdr4" width="16" height="16" /><a href="/stock/news?code=4334&b=n202603150805">テクニカル見出し</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-14T17:30:29+09:00">26/03/14&nbsp;17:30</time></td>
<td><div class="newslist_ctg newsctg1_b">市況</div></td>
<td><a href="/stock/news?code=4334&b=n202603141108">市況ニュース見出し</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-13T15:45:00+09:00">26/03/13&nbsp;15:45</time></td>
<td><div class="newslist_ctg newsctg_kaiji_b">開示</div></td>
<td class="td_kaiji"><a href="https://kabutan.jp/disclosures/pdf/20260313/140120260311579461/" target="pdf">開示見出し<img src="/images/cmn/pdf16.gif" alt="pdf" /></a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-03-13T15:45:00+09:00">26/03/13&nbsp;15:45</time></td>
<td><div class="newslist_ctg newsctg9_b">注目</div></td>
<td><a href="/stock/news?code=4334&b=n202603131087">決算注目見出し</a></td>
</tr>
</table>
</body>
</html>
"""


class TestParseDisclosureHtml:
    """parse_disclosure_html の新HTML構造パーステスト"""

    def test_全カテゴリが抽出される(self):
        """新HTML構造から全ニュースカテゴリが正しく抽出される"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        types = {r["type"] for r in records}
        assert "kaiji" in types, "開示が取得できていない"
        assert "zairyo" in types, "材料が取得できていない"
        assert "modify" in types, "修正が取得できていない"
        assert "5per" in types, "5パーが取得できていない"
        assert "special" in types, "特集が取得できていない"
        assert "kessan" in types, "決算が取得できていない"

    def test_合計件数(self):
        """全8行から開示1件+それ以外7件が抽出される"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        assert len(records) == 8

    def test_材料レコードの内容(self):
        """材料ニュースのフィールドが正しい"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        zairyo = [r for r in records if r["heading"] == "材料ニュース見出し"]
        assert len(zairyo) == 1
        r = zairyo[0]
        assert r["type"] == "zairyo"
        assert r["date"] == "20260325"
        assert "kabutan.jp" in r["url"]
        assert r["code_s"] == "4334"

    def test_開示レコードの内容(self):
        """開示（td_kaiji）パターンで取得"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        kaiji = [r for r in records if r["type"] == "kaiji"]
        assert len(kaiji) == 1
        assert kaiji[0]["date"] == "20260313"
        assert "disclosures/pdf" in kaiji[0]["url"]

    def test_img前置きパターン(self):
        """<img>がaタグ前にあるテクニカル記事も取得できる"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        tech = [r for r in records if r["heading"] == "テクニカル見出し"]
        assert len(tech) == 1
        assert tech[0]["type"] == "zairyo"  # テクは材料扱い

    def test_市況は材料扱い(self):
        """市況（newsctg1_b）は材料として分類される"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        shikyo = [r for r in records if r["heading"] == "市況ニュース見出し"]
        assert len(shikyo) == 1
        assert shikyo[0]["type"] == "zairyo"

    def test_日付形式(self):
        """日付がYYYYMMDD形式で抽出される"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        for r in records:
            assert len(r["date"]) == 8
            assert r["date"].isdigit()

    def test_URLが絶対パス(self):
        """全レコードのURLが絶対URLである"""
        records = disclosure.parse_disclosure_html(_NEW_HTML_FIXTURE)
        for r in records:
            assert r["url"].startswith("https://"), f"相対URL: {r['url']}"

    def test_空HTMLはエラーなし(self):
        """コードが取れないHTMLは空リストを返す"""
        result = disclosure.parse_disclosure_html("<html><head><title>test</title></head></html>")
        assert result == {}

    def test_旧HTMLフォーマットのフォールバック(self):
        """キャッシュに残った旧HTML形式でも材料等が取得できる"""
        old_html = """
        <html>
        <head><title>テスト銘柄【1234】｜ニュース｜株探（かぶたん）</title></head>
        <body>
        <td class="td_kaiji"><a href="https://kabutan.jp/disclosures/pdf/20260310/test/" target="pdf">開示見出し<img src="pdf.gif" /></a></td>
        <td class="ctg9"></td>
        <td><a href="/stock/news?code=1234&b=n202603100500">決算ニュース</a></td>
        <td class="ctg5"></td>
        <td><a href="/stock/news?code=1234&b=n202603090300">特集ニュース</a></td>
        </body>
        </html>
        """
        records = disclosure.parse_disclosure_html(old_html)
        types = {r["type"] for r in records}
        assert "kaiji" in types, "開示が取得できていない"
        assert "kessan" in types, "旧形式の決算が取得できていない"
        assert "special" in types, "旧形式の特集が取得できていない"
        assert len(records) == 3
