"""kessan.py のテスト"""

import pytest
from unittest.mock import patch
from datetime import datetime

import kessan


class TestIsPositiveKessan:
    """決算サマリーのポジティブ判定テスト"""

    def test_positive_keywords(self):
        """ポジティブキーワード（上方修正・増益・増配・黒字浮上）"""
        assert kessan.is_positive_kessan("通期上方修正") is True
        assert kessan.is_positive_kessan("3Q経常増益") is True
        assert kessan.is_positive_kessan("増配を発表") is True
        assert kessan.is_positive_kessan("黒字浮上見込み") is True

    def test_negative_and_neutral(self):
        """ネガティブ・無関係な文字列はFalse"""
        assert kessan.is_positive_kessan("下方修正") is False
        assert kessan.is_positive_kessan("3Q経常減益") is False
        assert kessan.is_positive_kessan("") is False
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
    def test_決算日なしと2週間以上前(self, mock_dt):
        """決算日なし→空文字、2週間以上前→空文字"""
        mock_dt.today.return_value = datetime(2025, 4, 1)
        mock_dt.strptime = datetime.strptime
        assert kessan.get_kessanbi_expr({}) == ""
        assert kessan.get_kessanbi_expr({"kessanbi": "2025/03/14"}) == ""
