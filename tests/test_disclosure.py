"""disclosure.py のテスト"""

from datetime import datetime, timedelta

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
