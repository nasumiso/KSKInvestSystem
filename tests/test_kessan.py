"""kessan.py のテスト"""

import pytest
from unittest.mock import patch
from datetime import datetime

import kessan


class TestIsPositiveKessan:
    """決算サマリーのポジティブ判定テスト"""

    def test_上方修正(self):
        assert kessan.is_positive_kessan("通期上方修正") is True

    def test_増益(self):
        assert kessan.is_positive_kessan("3Q経常増益") is True

    def test_増配(self):
        assert kessan.is_positive_kessan("増配を発表") is True

    def test_黒字浮上(self):
        assert kessan.is_positive_kessan("黒字浮上見込み") is True

    def test_ネガティブ(self):
        assert kessan.is_positive_kessan("下方修正") is False

    def test_減益(self):
        assert kessan.is_positive_kessan("3Q経常減益") is False

    def test_空文字(self):
        assert kessan.is_positive_kessan("") is False

    def test_無関係な文字列(self):
        assert kessan.is_positive_kessan("決算発表") is False


class TestGetKessanbiExpr:
    """決算日タグ生成テスト"""

    @patch("kessan.datetime")
    def test_決算7日前(self, mock_dt):
        """決算日の7日前 → "決MM/DD" タグ"""
        mock_dt.today.return_value = datetime(2025, 3, 7)
        mock_dt.strptime = datetime.strptime
        stock = {"kessanbi": "2025/03/14"}
        result = kessan.get_kessanbi_expr(stock)
        assert "決" in result
        assert "03/14" in result

    @patch("kessan.datetime")
    def test_決算後7日以内(self, mock_dt):
        """決算日の翌日〜7日後 → "後MM/DD" タグ"""
        mock_dt.today.return_value = datetime(2025, 3, 15)
        mock_dt.strptime = datetime.strptime
        stock = {"kessanbi": "2025/03/14"}
        result = kessan.get_kessanbi_expr(stock)
        assert "後" in result
        assert "03/14" in result

    @patch("kessan.datetime")
    def test_修正タグ(self, mock_dt):
        """決算修正日が2週間以内 → "修MM/DD" タグ"""
        mock_dt.today.return_value = datetime(2025, 3, 15)
        mock_dt.strptime = datetime.strptime
        stock = {"kessan_mod_date": "2025/03/10"}
        result = kessan.get_kessanbi_expr(stock)
        assert "修" in result
        assert "03/10" in result

    @patch("kessan.datetime")
    def test_決算日なし(self, mock_dt):
        """決算日がない場合は空文字"""
        mock_dt.today.return_value = datetime(2025, 3, 15)
        mock_dt.strptime = datetime.strptime
        stock = {}
        result = kessan.get_kessanbi_expr(stock)
        assert result == ""

    @patch("kessan.datetime")
    def test_決算発表リンク付き(self, mock_dt):
        """決算発表情報がある場合はHYPERLINK付き"""
        mock_dt.today.return_value = datetime(2025, 3, 15)
        mock_dt.strptime = datetime.strptime
        stock = {
            "kessanbi": "2025/03/14",
            "kessan_announce": "発表,/news/article/123,3Q増益",
        }
        result = kessan.get_kessanbi_expr(stock)
        assert "HYPERLINK" in result
        assert "kabutan.jp" in result

    @patch("kessan.datetime")
    def test_2週間以上前は非表示(self, mock_dt):
        """決算日が2週間以上前の場合はタグなし"""
        mock_dt.today.return_value = datetime(2025, 4, 1)
        mock_dt.strptime = datetime.strptime
        stock = {"kessanbi": "2025/03/14"}
        result = kessan.get_kessanbi_expr(stock)
        assert result == ""
