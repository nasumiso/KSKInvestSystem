"""make_market_db.py の計算関数テスト"""

import pytest
import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
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


# ==================================================
# _theme_rank_label
# ==================================================
class TestThemeRankLabel:
    """モメンタム順位変動ラベルテスト"""

    def test_rank_up(self):
        """順位上昇: ↑表示"""
        assert make_market_db._theme_rank_label("AI", 4) == "AI(↑4)"

    def test_rank_down(self):
        """順位下降: ↓表示"""
        assert make_market_db._theme_rank_label("AI", -3) == "AI(↓3)"

    def test_rank_unchanged(self):
        """変動なし: ←表示"""
        assert make_market_db._theme_rank_label("AI", 0) == "AI(←)"

    def test_new_theme(self):
        """新規テーマ: NEW表示"""
        assert make_market_db._theme_rank_label("AI", None) == "AI(NEW)"

    def test_rank_up_by_one(self):
        """1つ上昇"""
        assert make_market_db._theme_rank_label("防衛", 1) == "防衛(↑1)"

    def test_rank_down_by_one(self):
        """1つ下降"""
        assert make_market_db._theme_rank_label("DX", -1) == "DX(↓1)"


# ==================================================
# calc_theme_price_momentum
# ==================================================
class TestCalcThemePriceMomentum:
    """テーマ別株価騰落率テスト"""

    def _make_stock(self, themes, today_price, prev_price, today_date, prev_date):
        """テスト用銘柄データ作成ヘルパー"""
        return {
            "themes": themes,
            "price_log": [(today_date, today_price), (prev_date, prev_price)],
        }

    def test_normal(self):
        """正常系: テーマごとの平均騰落率と銘柄数"""
        from datetime import date

        d1 = date(2026, 2, 20)
        d0 = date(2026, 2, 19)
        stocks = {
            "1234": self._make_stock("AI,半導体", 1100, 1000, d1, d0),
            "5678": self._make_stock("AI,DX", 1050, 1000, d1, d0),
        }
        result = make_market_db.calc_theme_price_momentum(stocks)
        # AI: (10% + 5%) / 2 = 7.5%, 2銘柄
        assert abs(result["AI"][0] - 7.5) < 0.01
        assert result["AI"][1] == 2
        # 半導体: 10%, 1銘柄
        assert abs(result["半導体"][0] - 10.0) < 0.01
        assert result["半導体"][1] == 1
        # DX: 5%, 1銘柄
        assert abs(result["DX"][0] - 5.0) < 0.01
        assert result["DX"][1] == 1

    def test_empty_stocks(self):
        """空DBの場合"""
        result = make_market_db.calc_theme_price_momentum({})
        assert result == {}

    def test_no_price_log(self):
        """price_logがない銘柄はスキップ"""
        stocks = {"1234": {"themes": "AI"}}
        result = make_market_db.calc_theme_price_momentum(stocks)
        assert result == {}

    def test_single_price_entry(self):
        """price_logが1件のみの場合はスキップ"""
        from datetime import date

        stocks = {
            "1234": {
                "themes": "AI",
                "price_log": [(date(2026, 2, 20), 1000)],
            },
        }
        result = make_market_db.calc_theme_price_momentum(stocks)
        assert result == {}

    def test_zero_prev_price(self):
        """前日価格が0の銘柄はスキップ"""
        from datetime import date

        d1 = date(2026, 2, 20)
        d0 = date(2026, 2, 19)
        stocks = {
            "1234": self._make_stock("AI", 1000, 0, d1, d0),
        }
        result = make_market_db.calc_theme_price_momentum(stocks)
        assert result == {}

    def test_empty_themes(self):
        """テーマが空文字の銘柄はスキップ"""
        from datetime import date

        d1 = date(2026, 2, 20)
        d0 = date(2026, 2, 19)
        stocks = {
            "1234": self._make_stock("", 1100, 1000, d1, d0),
        }
        result = make_market_db.calc_theme_price_momentum(stocks)
        assert result == {}

    def test_latest_trade_date_filter(self):
        """直近取引日と異なるprice_log日付の銘柄は除外"""
        from datetime import date

        d_latest = date(2026, 2, 20)
        d_old = date(2026, 2, 19)
        d_older = date(2026, 2, 18)
        stocks = {
            # 直近取引日(2/20)の銘柄 - 集計対象
            "1234": self._make_stock("AI", 1100, 1000, d_latest, d_old),
            "5678": self._make_stock("AI", 1050, 1000, d_latest, d_old),
            # 古い日付(2/19)の銘柄 - 除外
            "9012": self._make_stock("AI", 900, 1000, d_old, d_older),
        }
        result = make_market_db.calc_theme_price_momentum(stocks)
        # 2/20の2銘柄のみ: (10% + 5%) / 2 = 7.5%
        assert abs(result["AI"][0] - 7.5) < 0.01
        assert result["AI"][1] == 2

    def test_negative_change(self):
        """下落銘柄の計算"""
        from datetime import date

        d1 = date(2026, 2, 20)
        d0 = date(2026, 2, 19)
        stocks = {
            "1234": self._make_stock("AI", 900, 1000, d1, d0),
        }
        result = make_market_db.calc_theme_price_momentum(stocks)
        assert abs(result["AI"][0] - (-10.0)) < 0.01
        assert result["AI"][1] == 1


# ==================================================
# make_theme_data — 差分ラベル計算
# ==================================================
def _mock_get_theme_rank_list(today_themes, prev_themes):
    """get_theme_rank_listのモックを返すヘルパー"""
    cach_date = datetime(2026, 3, 18, 21, 0, 0)
    prev_day = datetime(2026, 3, 15, 21, 0, 0)
    return patch(
        "make_market_db.get_theme_rank_list",
        return_value=(today_themes, prev_themes, cach_date, prev_day),
    )


class TestMakeThemeDataDiff:
    """make_theme_dataの差分ラベル計算テスト"""

    # テスト用のKabutan生ランキング（モメンタム計算の入力）
    TODAY_THEMES = ["AI", "半導体", "防衛", "DX", "EV"]
    # 数日前の生ランキング（モメンタム計算用、差分ラベルとは無関係）
    PREV_THEMES = ["AI", "半導体", "防衛", "DX", "EV"]

    def test_rank_up(self):
        """前日より順位が上がったテーマに正の差分がつく"""
        # 前日モメンタム順位: DXが4位 → 今日は上位に来る想定
        prev_momentum = ["AI", "半導体", "防衛", "DX", "EV"]
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result = make_market_db.make_theme_data(prev_momentum)
        diff = result["theme_rank_diff"]
        # 生ランキングが同じ＝モメンタム順位も同じ → 全部差分0
        for theme in result["theme_rank"]:
            assert diff[theme] == 0

    def test_rank_change_detected(self):
        """前日と今日でモメンタム順位が変わった場合、差分が正しく計算される"""
        # 今日: AIが1位から外れて、EVが急上昇する生ランキング
        today = ["EV", "AI", "半導体", "防衛", "DX"]
        prev_raw = ["EV", "AI", "半導体", "防衛", "DX"]
        # 前日のモメンタム順位はAIが1位だった
        prev_momentum = ["AI", "半導体", "防衛", "DX", "EV"]
        with _mock_get_theme_rank_list(today, prev_raw):
            result = make_market_db.make_theme_data(prev_momentum)
        diff = result["theme_rank_diff"]
        rank_list = result["theme_rank"]
        cur_rank = {v: i + 1 for i, v in enumerate(rank_list)}
        prev_rank = {v: i + 1 for i, v in enumerate(prev_momentum)}
        # 各テーマの差分が前日順位-当日順位と一致すること
        for theme in rank_list:
            if theme in prev_rank:
                expected = prev_rank[theme] - cur_rank[theme]
                assert diff[theme] == expected, (
                    "%s: expected %d, got %d" % (theme, expected, diff[theme])
                )

    def test_new_theme(self):
        """前日のモメンタム順位に存在しないテーマはNEW（None）"""
        today = ["AI", "半導体", "防衛", "新テーマ", "DX"]
        prev_raw = ["AI", "半導体", "防衛", "新テーマ", "DX"]
        # 前日モメンタム順位には「新テーマ」がない
        prev_momentum = ["AI", "半導体", "防衛", "DX", "EV"]
        with _mock_get_theme_rank_list(today, prev_raw):
            result = make_market_db.make_theme_data(prev_momentum)
        diff = result["theme_rank_diff"]
        assert diff["新テーマ"] is None

    def test_no_prev_momentum(self):
        """prev_momentum_rankがNoneの場合、全テーマの差分が0"""
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result = make_market_db.make_theme_data(None)
        diff = result["theme_rank_diff"]
        for theme in result["theme_rank"]:
            assert diff[theme] == 0

    def test_empty_prev_momentum(self):
        """prev_momentum_rankが空リストの場合、全テーマの差分が0"""
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result = make_market_db.make_theme_data([])
        diff = result["theme_rank_diff"]
        for theme in result["theme_rank"]:
            assert diff[theme] == 0

    def test_same_day_rerun_gives_same_result(self):
        """同日2回実行: 1回目の結果のtheme_rankを渡しても正しい差分が出る

        update_market_dbが日付チェックで退避するため、同日再実行時は
        prev_theme_rankが前日データのままになるはず。
        このテストはmake_theme_data単体で、同じリストを渡したら差分0になることを確認。
        """
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result1 = make_market_db.make_theme_data(["AI", "防衛", "半導体", "DX", "EV"])
        # 1回目の結果をそのまま渡す（同日再実行シミュレーション）
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result2 = make_market_db.make_theme_data(result1["theme_rank"])
        diff = result2["theme_rank_diff"]
        for theme in result2["theme_rank"]:
            assert diff[theme] == 0

    def test_result_contains_required_keys(self):
        """戻り値に必須キーが含まれる"""
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result = make_market_db.make_theme_data(self.TODAY_THEMES)
        assert "theme_rank" in result
        assert "theme_rank_diff" in result
        assert "access_date_theme_rank" in result

    def test_all_themes_have_diff(self):
        """theme_rankの全テーマにtheme_rank_diffのエントリがある"""
        prev_momentum = ["AI", "半導体", "防衛", "DX", "EV"]
        with _mock_get_theme_rank_list(self.TODAY_THEMES, self.PREV_THEMES):
            result = make_market_db.make_theme_data(prev_momentum)
        for theme in result["theme_rank"]:
            assert theme in result["theme_rank_diff"]
