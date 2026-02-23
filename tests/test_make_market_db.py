"""make_market_db.py の計算関数テスト"""

import pytest
import os
from datetime import datetime, timedelta
from unittest.mock import patch
import make_market_db
from ks_util import DATA_DIR


# ==================================================
# parse_theme_html
# ==================================================
class TestParseThemeHtml:
    """テーマ名抽出テスト"""

    def test_normal(self):
        """正常系: 複数テーマを抽出"""
        html = (
            '<td class="acrank_url"><a href="/themes/?theme=AI">AI</a></td>'
            '<td class="acrank_url"><a href="/themes/?theme=半導体">半導体</a></td>'
            '<td class="acrank_url"><a href="/themes/?theme=DX">DX</a></td>'
        )
        result = make_market_db.parse_theme_html(html)
        assert result == ["AI", "半導体", "DX"]

    def test_empty_html(self):
        """空HTMLの場合は空リスト"""
        assert make_market_db.parse_theme_html("") == []

    def test_none(self):
        """Noneの場合は空リスト"""
        assert make_market_db.parse_theme_html(None) == []

    def test_double_quotes(self):
        """ダブルクォートのclass属性"""
        html = '<td class="acrank_url"><a href="/themes">テスト</a></td>'
        result = make_market_db.parse_theme_html(html)
        assert result == ["テスト"]

    def test_single_quotes(self):
        """シングルクォートのclass属性"""
        html = "<td class='acrank_url'><a href='/themes'>テスト</a></td>"
        result = make_market_db.parse_theme_html(html)
        assert result == ["テスト"]

    def test_no_match(self):
        """マッチしないHTML"""
        html = '<div class="other"><a href="/themes">テスト</a></div>'
        result = make_market_db.parse_theme_html(html)
        assert result == []

    def test_whitespace_stripped(self):
        """テーマ名の前後空白が除去される"""
        html = '<td class="acrank_url"><a href="/themes"> テスト </a></td>'
        result = make_market_db.parse_theme_html(html)
        assert result == ["テスト"]


# ==================================================
# get_prev_fname
# ==================================================
class TestGetPrevFname:
    """バックアップファイル名生成テスト"""

    def test_no_file_found(self):
        """ファイルが見つからない場合は空文字列を返す"""
        cur_day = datetime(2025, 2, 23)
        fname, _ = make_market_db.get_prev_fname(
            "/nonexistent/path/test.html", cur_day
        )
        assert fname == ""

    def test_date_format(self):
        """日付フォーマットがYYMMDDであること"""
        cur_day = datetime(2025, 2, 23)
        # ファイルが存在しない場合、30日分イテレーションして空文字列を返す
        # 最後のcur_dayの日付を確認
        _, last_day = make_market_db.get_prev_fname(
            "/nonexistent/path/test.html", cur_day
        )
        # 30日前の日付
        expected_day = cur_day - timedelta(30)
        assert last_day.year == expected_day.year
        assert last_day.month == expected_day.month
        assert last_day.day == expected_day.day

    def test_file_found(self):
        """ファイルが見つかった場合はそのパスを返す"""
        cur_day = datetime(2025, 2, 23)
        # DATA_DIR配下のパスを使用（get_prev_fnameがPath.relative_to(DATA_DIR)を呼ぶため）
        base_path = os.path.join(DATA_DIR, "test.html")
        expected_fname = os.path.join(DATA_DIR, "test_250222.html")
        with patch("os.path.exists") as mock_exists:
            mock_exists.side_effect = lambda f: f == expected_fname
            fname, found_day = make_market_db.get_prev_fname(
                base_path, cur_day
            )
            assert fname == expected_fname
            assert found_day.day == 22

    def test_iteration_count(self):
        """30日分イテレーションして見つからない場合"""
        cur_day = datetime(2025, 2, 23)
        with patch("os.path.exists", return_value=False):
            fname, _ = make_market_db.get_prev_fname(
                "/tmp/test.html", cur_day
            )
            assert fname == ""


# ==================================================
# update_shintakane_theme
# ==================================================
class TestUpdateShintakaneTheme:
    """テーマ集計テスト"""

    def test_normal(self):
        """正常系: テーマごとのカウント"""
        stocks = {
            "1234": {"themes": "AI,半導体"},
            "5678": {"themes": "AI,DX"},
            "9012": {"themes": "半導体,DX"},
        }
        code_list = ["1234", "5678", "9012"]
        result = make_market_db.update_shintakane_theme(stocks, code_list)
        result_dict = dict(result)
        assert result_dict["AI"] == 2
        assert result_dict["半導体"] == 2
        assert result_dict["DX"] == 2

    def test_single_stock(self):
        """銘柄1つの場合"""
        stocks = {"1234": {"themes": "AI,半導体"}}
        result = make_market_db.update_shintakane_theme(stocks, ["1234"])
        result_dict = dict(result)
        assert result_dict["AI"] == 1
        assert result_dict["半導体"] == 1

    def test_empty_code_list(self):
        """code_listが空の場合"""
        stocks = {"1234": {"themes": "AI"}}
        result = make_market_db.update_shintakane_theme(stocks, [])
        assert result == []

    def test_code_not_in_stocks(self):
        """stocksに存在しないコード"""
        stocks = {"1234": {"themes": "AI"}}
        result = make_market_db.update_shintakane_theme(stocks, ["9999"])
        assert result == []

    def test_empty_theme(self):
        """テーマが空文字の場合はスキップ"""
        stocks = {
            "1234": {"themes": ""},
            "5678": {"themes": "AI"},
        }
        result = make_market_db.update_shintakane_theme(stocks, ["1234", "5678"])
        result_dict = dict(result)
        assert "" not in result_dict
        assert result_dict["AI"] == 1

    def test_sorted_by_count_descending(self):
        """カウント降順でソートされる"""
        stocks = {
            "1234": {"themes": "AI,半導体,DX"},
            "5678": {"themes": "AI,DX"},
            "9012": {"themes": "AI"},
        }
        result = make_market_db.update_shintakane_theme(
            stocks, ["1234", "5678", "9012"]
        )
        assert result[0][0] == "AI"
        assert result[0][1] == 3
