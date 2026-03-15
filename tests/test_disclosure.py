"""disclosure.py のテスト"""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

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
