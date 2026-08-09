"""トレンドテンプレート修正のテスト (issue #340 Phase 1)"""

from datetime import datetime
import pytest
import make_stock_db

_TODAY = datetime.now()


def _make_stock(trend_template, pocket_pivot=None, breakout=None):
    """テスト用銘柄データを生成する"""
    stock = {
        "trend_template": trend_template,
        "access_date_price": _TODAY,
    }
    if pocket_pivot is not None:
        stock["pocket_pivot"] = pocket_pivot
    if breakout is not None:
        stock["breakout"] = breakout
    return stock


# ==================================================
# get_trend_template_expr — 欠損(None)の表示
# ==================================================
class TestGetTrendTemplateExpr:
    @pytest.mark.parametrize("trend_template,expected", [
        ([], "◎"),                          # 全条件通過
        (["RS"], "◯RS"),                    # 1件 miss
        (["RS", "ma40Up"], "◯RS,ma40Up"),   # 2件 miss
        (["RS", "ma40Up", "pr>ma10"], "▲"), # 3件 miss
        (None, "—"),                         # 欠損
    ])
    def test_expr(self, trend_template, expected):
        stock = {"trend_template": trend_template}
        assert make_stock_db.get_trend_template_expr(stock) == expected


# ==================================================
# extract_signals — 欠損(None)でシグナル無効化
# ==================================================
class TestExtractSignalsWithNone:
    def test_none_trend_template_suppresses_signals(self):
        """週足データ欠損(None)のときポ/ブシグナルを返さない"""
        stock = _make_stock(
            trend_template=None,
            pocket_pivot=["06/01,120", "06/03,110"],
            breakout=["06/10,150"],
        )
        result = make_stock_db.extract_signals(stock, max_delta_days=None)
        assert result == []

    def test_empty_trend_template_passes_signals(self):
        """全条件通過([])のときシグナルは通す"""
        stock = _make_stock(
            trend_template=[],
            pocket_pivot=["06/20,120"],
        )
        result = make_stock_db.extract_signals(stock, max_delta_days=None)
        assert any(s["kind"] == "ポ" for s in result)
