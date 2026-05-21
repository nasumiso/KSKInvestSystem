"""make_market_db.py の計算関数テスト"""

import pytest
import os
import threading
from concurrent.futures import ThreadPoolExecutor
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

    def test_empty_or_none(self):
        """空HTML・Noneの場合は空リスト"""
        assert make_market_db.parse_theme_html("") == []
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

    def test_skip_conditions(self):
        """スキップされるケース: price_logなし/1件のみ/前日価格0/テーマ空"""
        from datetime import date

        d1 = date(2026, 2, 20)
        d0 = date(2026, 2, 19)
        # price_logなし
        assert make_market_db.calc_theme_price_momentum({"1234": {"themes": "AI"}}) == {}
        # price_log 1件のみ
        assert make_market_db.calc_theme_price_momentum({
            "1234": {"themes": "AI", "price_log": [(d1, 1000)]},
        }) == {}
        # 前日価格0
        assert make_market_db.calc_theme_price_momentum({
            "1234": self._make_stock("AI", 1000, 0, d1, d0),
        }) == {}
        # テーマ空
        assert make_market_db.calc_theme_price_momentum({
            "1234": self._make_stock("", 1100, 1000, d1, d0),
        }) == {}

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
# HTML出力関連テスト
# ==================================================

class TestHtmlThemeRank:
    """テーマランクHTML生成テスト"""

    def _make_market_db(self):
        """テスト用market_db"""
        return {
            "theme_rank": ["AI", "半導体", "防衛", "DX"],
            "theme_rank_diff": {
                "AI": None,    # 新規
                "半導体": 3,   # 上昇
                "防衛": 0,     # 変動なし
                "DX": -2,      # 下降
            },
            "theme_momentum": {
                "AI": (1.5, 10),
                "半導体": (-0.3, 5),
                "防衛": (0.0, 8),
            },
            "access_date_theme_rank": datetime(2026, 3, 15),
        }

    def test_new_theme_change(self):
        """新規テーマにNEWテキストが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'NEW' in result

    def test_up_theme_change(self):
        """上昇テーマにchange-upクラスと↑テキストが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'change-up' in result
        assert '↑3' in result

    def test_down_theme_change(self):
        """下降テーマにchange-downクラスと↓テキストが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'change-down' in result
        assert '↓2' in result

    def test_flat_theme_change(self):
        """変動なしテーマに→テキストが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert '>→<' in result

    def test_rate_pos_neg(self):
        """騰落率の正負でrate-pos/rate-negクラスが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'rate-pos' in result
        assert 'rate-neg' in result

    def test_empty_theme_rank(self):
        """テーマランクが空の場合は空文字列"""
        result = make_market_db._html_theme_rank({"theme_rank": []})
        assert result == ""

    def test_rank_history(self):
        """Kabutanランキング履歴が含まれる"""
        db = self._make_market_db()
        theme_rank_data = (["AI", "半導体"], ["防衛", "DX"], None, datetime(2026, 3, 12))
        result = make_market_db._html_theme_rank(db, theme_rank_data)
        assert 'rank-history' in result
        assert '2026-03-15' in result
        assert '2026-03-12' in result

    def test_html_escape(self):
        """テーマ名にHTMLの特殊文字が含まれる場合にエスケープされる"""
        db = {
            "theme_rank": ["AI&半導体"],
            "theme_rank_diff": {"AI&半導体": 1},
            "theme_momentum": {},
        }
        result = make_market_db._html_theme_rank(db)
        assert 'AI&amp;半導体' in result
        assert 'AI&半導体' not in result or '&amp;' in result


class TestHtmlMarket:
    """市場指標HTML生成テスト"""

    def _make_market_db(self):
        """テスト用market_db (Part B: state_meta / state_history を保持)"""
        return {
            "topix": {
                "rs_raw": 1.18,
                "trend_template": [],  # 空リスト=◎
                "market_state": "uptrend_under_pressure",
                "state_meta": {
                    "distribution_days_with_close": [
                        ("26/02/13", 2700.0),
                        ("26/02/20", 2680.0),
                    ],
                    "last_ftd_date": None,
                },
                "state_history": [
                    ("26/03/13", "uptrend_under_pressure", "dd>=4"),
                    ("26/03/12", "confirmed_uptrend", "stay"),
                ],
                # 後方互換フィールド (price.py が今も埋める)
                "distribution_days": ["260213", "260220"],
                "followthrough_days": ["260305"],
                "direction_signal": "uptrend_under_pressure,26/03/13",
                "spr_buygagher": 49,
                "spr_20": 47,
                "spr_5": 45,
                "rv_20": 3.6,
                "rv_5": 5.1,
            },
            "mothers": {
                "rs_raw": 1.09,
                "trend_template": ["ma30>ma40", "RS"],
                "market_state": "confirmed_uptrend",
                "state_meta": {
                    "distribution_days_with_close": [],
                    "last_ftd_date": "26/03/10",
                },
                "state_history": [
                    ("26/03/10", "confirmed_uptrend", "ftd"),
                    ("26/03/09", "market_in_correction", "stay"),
                ],
                "distribution_days": [],
                "followthrough_days": [],
                "direction_signal": "confirmed_uptrend,26/03/10",
                "spr_buygagher": 55,
                "spr_20": 52,
                "spr_5": 50,
                "rv_20": 4.6,
                "rv_5": 5.4,
            },
            "nasdaq": {
                "rs_raw": 1.05,
                "trend_template": [],
                "market_state": "confirmed_uptrend",
                "state_meta": {
                    "distribution_days_with_close": [("26/03/01", 18000.0)],
                    "last_ftd_date": None,
                },
                "state_history": [
                    ("26/03/13", "confirmed_uptrend", "stay"),
                ],
                "distribution_days": ["260301"],
                "followthrough_days": [],
                "direction_signal": "confirmed_uptrend,26/03/13",
                "spr_buygagher": 50,
                "spr_20": 48,
                "spr_5": 47,
                "rv_20": 2.1,
                "rv_5": 3.2,
            },
            "sp500": {
                "rs_raw": 1.02,
                "trend_template": [],
                "market_state": "confirmed_uptrend",
                "state_meta": {
                    "distribution_days_with_close": [],
                    "last_ftd_date": "26/03/08",
                },
                "state_history": [
                    ("26/03/13", "confirmed_uptrend", "stay"),
                    ("26/03/08", "confirmed_uptrend", "ftd"),
                ],
                "distribution_days": [],
                "followthrough_days": ["260308"],
                "direction_signal": "confirmed_uptrend,26/03/13",
                "spr_buygagher": 51,
                "spr_20": 50,
                "spr_5": 49,
                "rv_20": 1.8,
                "rv_5": 2.5,
            },
        }

    def test_state_correction_class(self):
        """market_state が market_in_correction なら state-correction クラス"""
        db = self._make_market_db()
        db["topix"]["market_state"] = "market_in_correction"
        result = make_market_db._html_market(db)
        assert 'state-correction' in result

    def test_state_pressure_class(self):
        """market_state が uptrend_under_pressure なら state-pressure クラス"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'state-pressure' in result

    def test_state_confirmed_class(self):
        """market_state が confirmed_uptrend なら state-confirmed クラス"""
        db = self._make_market_db()
        db["topix"]["market_state"] = "confirmed_uptrend"
        result = make_market_db._html_market(db)
        assert 'state-confirmed' in result

    def test_trend_bg_class(self):
        """良好トレンド (◎/◯) に背景色クラスが付く (issue #248: portfolio_list と仕様統一)"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'trend-bg-strong' in result or 'trend-bg-good' in result

    def test_market_table_header(self):
        """テーブルヘッダーが含まれる (Part B: DD/FTD 列名改修 + 列幅 class 追加)"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'market-table' in result
        assert '<th class="col-dd">DD</th>' in result
        assert '<th class="col-ftd">FTD/ラリー</th>' in result

    # ===== Part B: 表示文言・列構成テスト =====
    def test_market_state_header_label(self):
        """列ヘッダが「市場状態」になっている"""
        result = make_market_db._html_market(self._make_market_db())
        assert '市場状態' in result
        assert '<th>シグナル</th>' not in result

    def test_state_label_japanese(self):
        """state が日本語で表示される"""
        result = make_market_db._html_market(self._make_market_db())
        # topix=pressure, mothers=confirmed が含まれるテストデータ
        assert '上昇トレンド' in result
        assert '圧力下' in result

    def test_state_with_transition_date(self):
        """遷移日が (M/D〜) で併記される"""
        result = make_market_db._html_market(self._make_market_db())
        # topix の history で uptrend_under_pressure 遷移日は 26/03/13 → 表示は (03/13〜)
        assert '(03/13〜)' in result

    def test_buygagher_eval_removed(self):
        """売り圧力レシオ列から A-E 評価が削除されている"""
        result = make_market_db._html_market(self._make_market_db())
        # 旧形式: <td>%d, %d, <strong>A</strong></td>
        for grade in ['<strong>A</strong>', '<strong>B</strong>', '<strong>C</strong>',
                      '<strong>D</strong>', '<strong>E</strong>']:
            assert grade not in result

    def test_dd_count_format(self):
        """DD列が `数 / 6` 形式で表示される (案B)"""
        result = make_market_db._html_market(self._make_market_db())
        # TOPIX は dd_with_close が2件 → "2 / 6"
        assert "2 / 6" in result
        # mothers は dd_with_close が0件 → "0 / 6"
        assert "0 / 6" in result

    def test_dd_correction_format_when_at_threshold(self):
        """DD ≥ 6 のとき "6+ / 6" 表示 + dd-correction クラス"""
        db = {
            "topix": {
                "rs_raw": 1.0, "trend_template": [],
                "market_state": "market_in_correction",
                "state_meta": {
                    "distribution_days_with_close": [
                        ("26/04/%02d" % d, 100.0) for d in range(1, 8)  # 7件
                    ],
                    "last_ftd_date": None,
                },
                "state_history": [("26/04/30", "market_in_correction", "dd>=6")],
                "spr_20": 50, "spr_5": 50, "rv_20": 1.0, "rv_5": 1.0,
            }
        }
        result = make_market_db._html_market(db)
        assert "6+ / 6" in result
        assert "dd-correction" in result

    def test_dd_pressure_class_when_4_to_5(self):
        """4 ≤ DD < 6 のとき dd-pressure クラス"""
        db = {
            "topix": {
                "rs_raw": 1.0, "trend_template": [],
                "market_state": "uptrend_under_pressure",
                "state_meta": {
                    "distribution_days_with_close": [
                        ("26/04/%02d" % d, 100.0) for d in range(1, 5)  # 4件
                    ],
                    "last_ftd_date": None,
                },
                "state_history": [("26/04/30", "uptrend_under_pressure", "dd>=4")],
                "spr_20": 50, "spr_5": 50, "rv_20": 1.0, "rv_5": 1.0,
            }
        }
        result = make_market_db._html_market(db)
        assert "4 / 6" in result
        assert "dd-pressure" in result

    def test_dd_dates_in_title_attribute(self):
        """DD詳細日付は title 属性 (ホバー) で確認できる"""
        result = make_market_db._html_market(self._make_market_db())
        # TOPIX の dd_with_close は 02/13, 02/20 → title="02/13, 02/20"
        assert 'title="02/13, 02/20"' in result

    def test_ftd_shows_last_ftd_date_when_not_correction(self):
        """confirmed/pressure 状態では FTD列に last_ftd_date を表示"""
        result = make_market_db._html_market(self._make_market_db())
        # mothers: confirmed, last_ftd_date=26/03/10 → "03/10"
        assert "03/10" in result
        # sp500: confirmed, last_ftd_date=26/03/08 → "03/08"
        assert "03/08" in result

    def test_ftd_em_dash_when_no_ftd(self):
        """FTD なしのときは "—" 表示"""
        db = {
            "topix": {
                "rs_raw": 1.0, "trend_template": [],
                "market_state": "confirmed_uptrend",
                "state_meta": {
                    "distribution_days_with_close": [],
                    "last_ftd_date": None,
                },
                "state_history": [("26/04/30", "confirmed_uptrend", "stay")],
                "spr_20": 50, "spr_5": 50, "rv_20": 1.0, "rv_5": 1.0,
            }
        }
        result = make_market_db._html_market(db)
        # FTD/ラリー 列に — が出ること
        assert "—" in result

    def test_rally_day_in_correction(self):
        """correction 状態ではラリー Day N が表示される"""
        db = {
            "topix": {
                "rs_raw": 1.0, "trend_template": [],
                "market_state": "market_in_correction",
                "state_meta": {
                    "distribution_days_with_close": [],
                    "last_ftd_date": None,
                    "rally_attempt_start_date": "26/04/28",
                    "rally_attempt_start_low": 100.0,
                },
                "state_history": [("26/04/30", "market_in_correction", "stay")],
                "daily_history": ["26/04/30", "26/04/29", "26/04/28"],  # Day 3
                "spr_20": 50, "spr_5": 50, "rv_20": 1.0, "rv_5": 1.0,
            }
        }
        result = make_market_db._html_market(db)
        assert "ラリー Day 3" in result

    def test_rally_em_dash_when_no_attempt(self):
        """correction でラリー未開始なら — 表示"""
        db = {
            "topix": {
                "rs_raw": 1.0, "trend_template": [],
                "market_state": "market_in_correction",
                "state_meta": {
                    "distribution_days_with_close": [],
                    "last_ftd_date": None,
                    "rally_attempt_start_date": None,
                },
                "state_history": [("26/04/30", "market_in_correction", "stay")],
                "daily_history": ["26/04/30"],
                "spr_20": 50, "spr_5": 50, "rv_20": 1.0, "rv_5": 1.0,
            }
        }
        result = make_market_db._html_market(db)
        # correction なので FTD 列ではなくラリー位置、ラリー未開始なら —
        assert "—" in result
        assert "ラリー Day" not in result

    def test_growth250_label(self):
        """マザーズ行が「グロース250」で表示される"""
        result = make_market_db._html_market(self._make_market_db())
        assert "グロース250" in result
        assert "マザーズ" not in result

    def test_empty_market_db(self):
        """市場データがない場合は空文字列"""
        result = make_market_db._html_market({})
        assert result == ""

    def test_nasdaq_row_rendered(self):
        """NASDAQ行が市場テーブルに表示される (issue #148)"""
        result = make_market_db._html_market(self._make_market_db())
        assert ">NASDAQ</a></strong></td>" in result

    def test_sp500_row_rendered(self):
        """S&P 500行が市場テーブルに表示される (issue #148)。
        market_nameはhtml.escapeを通るため、& → &amp; となる。"""
        result = make_market_db._html_market(self._make_market_db())
        assert ">S&amp;P 500</a></strong></td>" in result

    def test_us_indices_skipped_when_missing(self):
        """nasdaq/sp500 キー欠落時は該当行が出ず、既存の TOPIX/マザーズは出る"""
        partial_db = {
            k: v for k, v in self._make_market_db().items() if k in ("topix", "mothers")
        }
        result = make_market_db._html_market(partial_db)
        assert ">TOPIX</a></strong></td>" in result
        assert "NASDAQ" not in result
        assert "S&amp;P 500" not in result

    def test_rs_bg_yellow_when_rs_raw_high(self):
        """rs_raw >= 1.2 で濃黄背景 / 1.1〜1.2 で薄黄背景 (portfolio パレット準拠)"""
        db = self._make_market_db()
        db["topix"]["rs_raw"] = 1.25
        result = make_market_db._html_market(db)
        assert 'background:#fbbc04' in result

    def test_rs_bg_blue_when_rs_raw_low(self):
        """rs_raw <= 0.8 で青背景+白文字 (警告)"""
        db = self._make_market_db()
        db["topix"]["rs_raw"] = 0.75
        result = make_market_db._html_market(db)
        assert 'background:#4285f4;color:#fff' in result

    def test_rs_no_style_when_rs_raw_neutral(self):
        """0.9 < rs < 1.1 では背景色付かない (中立)"""
        db = {
            "topix": dict(self._make_market_db()["topix"], rs_raw=1.0)
        }
        result = make_market_db._html_market(db)
        assert 'background:#fbbc04' not in result
        assert 'background:#fce8b2' not in result
        assert 'background:#6fa8dc' not in result
        assert 'background:#4285f4' not in result

    def test_index_rs_threshold_removes_RS_from_trend(self):
        """rs_raw > 1.05 なら trend_template の "RS" 未達が除外される (issue #148 Part 1)"""
        db = {
            "mothers": dict(
                self._make_market_db()["mothers"],
                rs_raw=1.09,
                trend_template=["ma30>ma40", "RS"],
            )
        }
        result = make_market_db._html_market(db)
        # "RS" が除外されたので残りは ma30>ma40 のみ → trend_template_expr は "◯ma30>ma40"
        # "RS" だけの未達文字列が表示文字列に出てないことを確認
        # (ma30>ma40 はタグとして残るので含まれる)
        assert ',RS' not in result and 'RS,' not in result

    def test_index_rs_threshold_below_does_not_remove_RS(self):
        """rs_raw <= 1.05 なら "RS" 未達は除外されない"""
        db = {
            "mothers": dict(
                self._make_market_db()["mothers"],
                rs_raw=1.04,
                trend_template=["ma30>ma40", "RS"],
            )
        }
        result = make_market_db._html_market(db)
        # 未達リストに "RS" が含まれて表示される (◯ma30>ma40,RS のような形)
        assert 'RS' in result


class TestSprGaugeSvg:
    """build_spr_gauge_svg の欠損フォールバック・クリップ挙動テスト (issue #247)"""

    def test_both_bars_rendered_when_all_present(self):
        """4 引数すべて有効: rect 4枚 + ティック line 2本 + ヒゲ line 2本"""
        svg = make_market_db.build_spr_gauge_svg(50, 2.0, 60, 3.0)
        assert svg.startswith("<svg")
        # 各バー: 背景 rect 2枚 (左緑 + 右赤) × 2本 = 4枚
        assert svg.count('<rect') == 4
        # ラベル未指定なので緑バーは C 相当 (#d4f4d4) ×2、赤バーは固定色 (#f4c7c3) ×2
        assert svg.count('#d4f4d4') == 2
        assert svg.count('#f4c7c3') == 2
        # ティック (stroke-width=2) と ヒゲ (stroke-width=1) それぞれ 2 本ずつ
        assert svg.count('stroke-width="2"') == 2
        assert svg.count('stroke-width="1"') == 2

    def test_5day_bar_is_rendered_before_20day_bar(self):
        """需給バランスは 5日バーを上、20日バーを下に表示する"""
        svg = make_market_db.build_spr_gauge_svg(50, 2.0, 60, 3.0)
        assert svg.index("(5日)") < svg.index("(20日)")

    @pytest.mark.parametrize("spr_20,spr_5,expected_label", [
        (50, None, "20日"),   # 5日欠損 → 20日のみ
        (None, 60, "5日"),    # 20日欠損 → 5日のみ
    ])
    def test_one_side_missing_renders_only_other(self, spr_20, spr_5, expected_label):
        """spr 片側欠損: 欠損していない方だけが描画される (両側非表示の回帰防止)"""
        svg = make_market_db.build_spr_gauge_svg(spr_20, 2.0, spr_5, 3.0)
        assert svg.startswith("<svg")
        # 描画されたバー 1 本ぶん: 背景 rect 2枚 + ティック 1本 + ヒゲ 1本
        assert svg.count('<rect') == 2
        assert svg.count('stroke-width="2"') == 1
        assert svg.count('stroke-width="1"') == 1
        # 残ったバーの <title> に期待した期間ラベルが入っている
        assert "(%s)" % expected_label in svg

    def test_em_dash_when_both_spr_missing(self):
        """spr_20 / spr_5 両方欠損 → "—" を返す (rv は無視)"""
        svg = make_market_db.build_spr_gauge_svg(None, 2.0, None, 3.0)
        assert svg == "—"

    def test_no_whisker_when_rv_missing(self):
        """rv 欠損時: 該当バーはティック + 背景のみ (ヒゲ無し)"""
        svg = make_market_db.build_spr_gauge_svg(50, None, 60, 3.0)
        assert svg.startswith("<svg")
        # ヒゲは 5 日ぶんだけ (rv_5=3.0), 20日バーはヒゲ無し → stroke-width=1 が 1 本
        assert svg.count('stroke-width="1"') == 1
        # ティックは両バーぶん
        assert svg.count('stroke-width="2"') == 2

    def test_spr_clipped_to_0_100(self):
        """SPR が範囲外 (120) でも 100 にクリップしてティック描画される"""
        svg = make_market_db.build_spr_gauge_svg(120, 2.0, -10, 1.0)
        # ティックの x 座標が 100 と 0 (それぞれ x1="100" / x1="0") に固定
        assert 'x1="100" y1="0"' in svg or 'x1="100"' in svg
        assert 'x1="0"' in svg

    @pytest.mark.parametrize("sprw,expected_green_20", [
        ("A", "#5cc85c"),     # 最濃
        ("C", "#d4f4d4"),     # 現状色 (中間)
        ("E", "#f8fef8"),     # 最薄
        (None, "#d4f4d4"),    # ラベル欠損 → C 相当にフォールバック
    ])
    def test_buy_collection_label_drives_green_bar_density(self, sprw, expected_green_20):
        """週評価 → 20日バー / 日評価 → 5日バー で緑バーの濃淡をコントロールする。
        赤バーは固定色 (#f4c7c3)、ラベル欠損は C 相当にフォールバック。"""
        svg = make_market_db.build_spr_gauge_svg(50, 2.0, 60, 3.0, sprw, None)
        # 緑バー (20日) は期待濃度
        assert expected_green_20 in svg
        # 赤バーは sprw / sprbg に関わらず固定色のみ (両バーぶん 2 回)
        assert svg.count("#f4c7c3") == 2

    def test_bar_title_includes_buy_collection_label(self):
        """バー単体 <title> に '買い集めX' が併記される (ホバー時に SVG <title> が勝つため)"""
        svg = make_market_db.build_spr_gauge_svg(50, 2.0, 60, 3.0, "B", "A")
        assert "SPR 50 ±2.0 (20日) 買い集めB" in svg
        assert "SPR 60 ±3.0 (5日) 買い集めA" in svg

    def test_bar_title_omits_buy_collection_when_label_missing(self):
        """ラベル None のバーは <title> に買い集めを出さない"""
        svg = make_market_db.build_spr_gauge_svg(50, 2.0, 60, 3.0, None, None)
        assert "買い集め" not in svg


class TestSprGaugeTooltip:
    """build_spr_gauge_tooltip の表示組み立てテスト (issue #247)"""

    def test_full_tooltip(self):
        text = make_market_db.build_spr_gauge_tooltip(49, 2.3, 46, 2.5, "D", "C")
        assert "SPR 49 ±2.3 (20日)" in text
        assert "SPR 46 ±2.5 (5日)" in text
        assert text.index("(5日)") < text.index("(20日)")
        assert "買い集め 週D 日C" in text

    def test_omits_missing_spr_and_buygather(self):
        """spr 片側欠損 + 買い集め評価両欠損: 欠損部分が出ない"""
        text = make_market_db.build_spr_gauge_tooltip(49, 2.3, None, None, None, None)
        assert "(20日)" in text
        assert "(5日)" not in text
        assert "買い集め" not in text


class TestAdjustIndexTrendTemplate:
    """指数向け trend_template 補正のテスト (issue #148 Part 1)"""

    def test_removes_RS_when_above_threshold(self):
        """rs_raw > 1.05 で "RS" が除外される"""
        db = {"rs_raw": 1.10, "trend_template": ["ma30>ma40", "RS"]}
        result = make_market_db._adjust_index_trend_template(db)
        assert "RS" not in result["trend_template"]
        assert "ma30>ma40" in result["trend_template"]

    def test_keeps_RS_at_threshold(self):
        """rs_raw == 1.05 (境界値、strict >) では除外されない"""
        db = {"rs_raw": 1.05, "trend_template": ["RS"]}
        result = make_market_db._adjust_index_trend_template(db)
        assert "RS" in result["trend_template"]

    def test_keeps_RS_when_below_threshold(self):
        """rs_raw < 1.05 では "RS" は除外されない"""
        db = {"rs_raw": 1.02, "trend_template": ["RS"]}
        result = make_market_db._adjust_index_trend_template(db)
        assert "RS" in result["trend_template"]

    def test_no_op_when_RS_not_in_misses(self):
        """trend_template に "RS" がなければ何もしない"""
        db = {"rs_raw": 1.20, "trend_template": ["ma30>ma40"]}
        result = make_market_db._adjust_index_trend_template(db)
        assert result["trend_template"] == ["ma30>ma40"]

    def test_does_not_mutate_original(self):
        """元のdictを破壊しない"""
        original = {"rs_raw": 1.20, "trend_template": ["RS"]}
        original_misses = list(original["trend_template"])
        make_market_db._adjust_index_trend_template(original)
        assert original["trend_template"] == original_misses


class TestRsStyle:
    """rs_raw 値から背景色 inline style を返すテスト (portfolio パレット準拠)"""

    @pytest.mark.parametrize("rs, expected", [
        (1.25, ' style="background:#fbbc04"'),                  # >= 1.2 濃黄
        (1.20, ' style="background:#fbbc04"'),                  # 境界
        (1.15, ' style="background:#fce8b2"'),                  # >= 1.1 薄黄
        (1.10, ' style="background:#fce8b2"'),                  # 境界
        (1.00, ""),                                              # 中立
        (0.95, ""),
        (0.90, ' style="background:#6fa8dc"'),                  # <= 0.9 水色
        (0.85, ' style="background:#6fa8dc"'),
        (0.80, ' style="background:#4285f4;color:#fff"'),       # <= 0.8 青 + 白文字
        (0.70, ' style="background:#4285f4;color:#fff"'),
        (0, ""),                                                 # 未取得
        ("", ""),                                                # 空文字
    ])
    def test_rs_style(self, rs, expected):
        assert make_market_db._rs_style(rs) == expected


class TestHtmlKessan:
    """決算HTML生成テスト"""

    def test_write_to_csv_format(self):
        """write_to_csv形式（2行セット）のパース"""
        kessan_csv = [
            ["03/10", "03/15"],                        # 日付行
            ["1234銘柄A[1Q]", "5678銘柄B[2Q]"],        # 銘柄行
        ]
        result = make_market_db._html_kessan(kessan_csv)
        assert 'kessan-card' in result
        assert '1234' in result
        assert '銘柄A' in result
        assert '5678' in result

    def test_write_to_csv_single_date(self):
        """write_to_csv形式（日付1件のみ）のパース — 欠落回帰テスト"""
        kessan_csv = [
            ["04/02"],                        # 日付が1件だけの行
            ["1234銘柄A[1Q]"],                 # 銘柄行
        ]
        result = make_market_db._html_kessan(kessan_csv)
        assert 'kessan-card' in result
        assert '1234' in result
        assert '銘柄A' in result

    def test_write_to_csv_current_format(self):
        """write_to_csv_current形式（1行）のパース"""
        kessan_csv = [
            ["03/16", "1234銘柄A[1Q]", "5678銘柄B[2Q]"],
        ]
        result = make_market_db._html_kessan(kessan_csv)
        assert 'kessan-card' in result
        assert '1234' in result
        assert '5678' in result

    def test_mixed_format(self):
        """3種類の混在構造のパース"""
        kessan_csv = [
            ["03/01", "03/05"],                          # write_to_csv (before)
            ["1234銘柄A[1Q]", "5678銘柄B[2Q]"],
            ["03/16", "9012銘柄C[3Q]"],                  # write_to_csv_current
            ["04/10", "04/15"],                          # write_to_csv (future)
            ["3456銘柄D[4Q]", "7890銘柄E[0Q]"],
        ]
        result = make_market_db._html_kessan(kessan_csv)
        assert '1234' in result
        assert '9012' in result
        assert '3456' in result

    def test_empty_csv(self):
        """空リストの場合は空文字列"""
        result = make_market_db._html_kessan([])
        assert result == ""

    def test_kabutan_link(self):
        """株探リンクが生成される"""
        kessan_csv = [["03/16", "1234銘柄A[1Q]"]]
        result = make_market_db._html_kessan(kessan_csv)
        assert 'kabutan.jp/stock/chart?code=1234' in result


class TestHtmlDisclosure:
    """適宜開示HTML生成テスト"""

    def test_hyperlink_parse(self):
        """=HYPERLINK()パターンが<a>タグに変換される"""
        today_str = make_market_db.get_price_day(datetime.today()).strftime("%Y%m%d")
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [
                today_str,
                '=HYPERLINK("https://kabutan.jp/stock/chart?code=1234","1234")',
                "テスト銘柄",
                "開示",
                '=HYPERLINK("https://example.com/doc.pdf","テスト開示")',
            ],
        ]
        result = make_market_db._html_disclosure(disc_csv)
        assert '<a href="https://kabutan.jp/stock/chart?code=1234">1234</a>' in result
        assert '<a href="https://example.com/doc.pdf">テスト開示</a>' in result

    def test_html_escape(self):
        """銘柄名の特殊文字がエスケープされる"""
        today_str = make_market_db.get_price_day(datetime.today()).strftime("%Y%m%d")
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [
                today_str,
                '=HYPERLINK("https://example.com","1234")',
                "A&B<C>",
                "開示",
                '=HYPERLINK("https://example.com","テスト")',
            ],
        ]
        result = make_market_db._html_disclosure(disc_csv)
        assert 'A&amp;B&lt;C&gt;' in result

    def test_recent_details_open(self):
        """直近3日分はdetails openになる"""
        from datetime import date
        today = make_market_db.get_price_day(datetime.today())
        today_str = today.strftime("%Y%m%d")
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [today_str, "1234", "テスト", "開示", "テスト開示"],
        ]
        result = make_market_db._html_disclosure(disc_csv)
        assert '<details open>' in result

    def test_older_details_closed(self):
        """直近3日より前（30日以内）のデータはdetails（折りたたみ）になる"""
        # 10日前の日付を使用（30日以内なので表示される）
        ten_days_ago = (datetime.today() - timedelta(days=10)).strftime("%Y%m%d")
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [ten_days_ago, "1234", "テスト", "開示", "テスト開示"],
        ]
        result = make_market_db._html_disclosure(disc_csv)
        assert '<details>' in result
        assert '<details open>' not in result

    def test_empty_csv(self):
        """空リストの場合は空文字列"""
        result = make_market_db._html_disclosure([])
        assert result == ""

    def test_gyoseki_row_class(self):
        """決算・修正行にdisc-row-gyosekiクラスが付く"""
        today_str = make_market_db.get_price_day(datetime.today()).strftime("%Y%m%d")
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [today_str, "1234", "テスト", "決算", "決算発表"],
        ]
        result = make_market_db._html_disclosure(disc_csv)
        assert 'disc-row-gyoseki' in result


class TestUpdateIndexMarketStateFTD:
    """_update_index_market_state の FTD 判定統合テスト。

    issue (state遷移FTD不動作): _calc_daily_indicators が
    new_index_db["price"|"low"|"volume"] を返さなかったため、ラリー追跡が一度も
    起動せず FTD が成立しないバグの回帰防止。
    """

    def _build_meta(self, prev_state, rally_start_date=None, rally_start_low=None,
                    prev_volume=0, dd_list=None):
        return {
            "rally_attempt_start_date": rally_start_date,
            "rally_attempt_start_low": rally_start_low,
            "distribution_days_with_close": dd_list or [],
            "last_ftd_date": None,
            "prev_volume": prev_volume,
        }

    def test_rally_day1_starts_when_correction_and_close_up(self):
        """前日 correction + 当日終値 > 前日終値 で rally Day 1 が確定する。

        当日 OHLV (price/low/volume) が new_index_db に詰まっていることが前提。
        欠落するとラリー追跡が永遠に起動しない (本タスクの主バグ)。
        """
        import market_state
        prev_index_db = {
            "market_state": market_state.MARKET_IN_CORRECTION,
            "state_meta": self._build_meta(market_state.MARKET_IN_CORRECTION,
                                            prev_volume=1000),
            "state_history": [("26/05/20", market_state.MARKET_IN_CORRECTION, "dd>=6")],
        }
        # 当日: 大幅反発、出来高増、low/high あり
        new_index_db = {
            "price": 61684,
            "low": 60282,
            "high": 62043,
            "volume": 2504900,
            "daily_history": ["26/05/21", "26/05/20", "26/05/19"],
            "distribution_days_with_close": [],
            "price_log": [
                (datetime(2026, 5, 21).date(), 61684),
                (datetime(2026, 5, 20).date(), 59804),
            ],
            "price_kairi_wma10": -2.0,
        }
        make_market_db._update_index_market_state(prev_index_db, new_index_db)
        sm = new_index_db["state_meta"]
        assert sm["rally_attempt_start_date"] == "26/05/21", (
            f"ラリー Day 1 が記録されていない: {sm}"
        )
        assert sm["rally_attempt_start_low"] == 60282
        # まだ Day 1 なので FTD は成立しない (Day 4 以降が条件)
        assert sm["last_ftd_date"] is None
        assert new_index_db["market_state"] == market_state.MARKET_IN_CORRECTION

    def test_no_rally_when_close_not_up(self):
        """前日 correction でも当日終値が前日以下ならラリー Day 1 にならない。"""
        import market_state
        prev_index_db = {
            "market_state": market_state.MARKET_IN_CORRECTION,
            "state_meta": self._build_meta(market_state.MARKET_IN_CORRECTION,
                                            prev_volume=1000),
            "state_history": [],
        }
        new_index_db = {
            "price": 59500,  # 前日 59804 より下
            "low": 59000,
            "high": 60000,
            "volume": 2000000,
            "daily_history": ["26/05/21", "26/05/20"],
            "distribution_days_with_close": [],
            "price_log": [
                (datetime(2026, 5, 21).date(), 59500),
                (datetime(2026, 5, 20).date(), 59804),
            ],
            "price_kairi_wma10": -3.0,
        }
        make_market_db._update_index_market_state(prev_index_db, new_index_db)
        sm = new_index_db["state_meta"]
        assert sm["rally_attempt_start_date"] is None


class TestCreateMarketHtml:
    """create_market_html() 統合テスト"""

    def test_generates_html_file(self, tmp_path):
        """HTMLファイルが生成される"""
        market_db = {
            "theme_rank": ["AI"],
            "theme_rank_diff": {"AI": None},
            "theme_momentum": {},
            "access_date_theme_rank": datetime(2026, 3, 15),
            "topix": {
                "rs_raw": 1.0,
                "trend_template": [],
                "distribution_days": [],
                "followthrough_days": [],
                "direction_signal": "none",
                "spr_buygagher": 50,
                "spr_20": 50,
                "spr_5": 50,
                "rv_20": 3.0,
                "rv_5": 4.0,
            },
        }
        with patch.object(make_market_db, 'DATA_DIR', str(tmp_path)):
            os.makedirs(os.path.join(str(tmp_path), "code_rank_data"), exist_ok=True)
            html_path = make_market_db.create_market_html(market_db)
            assert os.path.exists(html_path)
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            assert '<!DOCTYPE html>' in content
            assert 'テーマランク' in content
            assert '市場' in content

    def test_sections_omitted_when_none(self, tmp_path):
        """引数がNoneのセクションは省略される（セクション見出しが生成されない）"""
        market_db = {
            "theme_rank": ["AI"],
            "theme_rank_diff": {"AI": None},
            "theme_momentum": {},
        }
        with patch.object(make_market_db, 'DATA_DIR', str(tmp_path)):
            os.makedirs(os.path.join(str(tmp_path), "code_rank_data"), exist_ok=True)
            html_path = make_market_db.create_market_html(market_db)
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            # セクション見出し（h2タグ）が含まれないことを検証
            # （CSS内のコメントには含まれるため、h2タグで判定）
            assert '<h2>決算日</h2>' not in content
            assert '<h2>適宜開示</h2>' not in content

    def test_disclosure_section_not_in_market_html(self, tmp_path):
        """適宜開示は market_data.html から完全に分離されている (issue #148 関連)"""
        market_db = {
            "theme_rank": ["AI"],
            "theme_rank_diff": {"AI": None},
            "theme_momentum": {},
        }
        # 決算データ・適宜開示データを与えても、適宜開示見出しは出ない
        kessan_csv = [["日付", "コード", "銘柄"]]
        with patch.object(make_market_db, 'DATA_DIR', str(tmp_path)):
            os.makedirs(os.path.join(str(tmp_path), "code_rank_data"), exist_ok=True)
            html_path = make_market_db.create_market_html(
                market_db, kessan_csv=kessan_csv
            )
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            # 適宜開示見出しが市場HTMLに含まれない
            assert '<h2>適宜開示</h2>' not in content


class TestCreateDisclosureHtml:
    """create_disclosure_html() 統合テスト (issue #148 関連で新設)"""

    def test_generates_html_file(self, tmp_path):
        """適宜開示HTMLファイルが生成される"""
        disc_csv = [
            ["日付", "コード", "銘柄名", "種類", "本文"],  # ヘッダー行
            ["20260424", '=HYPERLINK("https://example.com","6324")',
             "ハーモニック", "決算", '=HYPERLINK("https://example.com","決算短信")'],
        ]
        with patch.object(make_market_db, 'DATA_DIR', str(tmp_path)):
            os.makedirs(os.path.join(str(tmp_path), "code_rank_data"), exist_ok=True)
            html_path = make_market_db.create_disclosure_html(disc_csv)
            assert os.path.exists(html_path)
            assert html_path.endswith("disclosure_data.html")
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            assert '<!DOCTYPE html>' in content
            assert '適宜開示' in content
            assert '6324' in content

    def test_empty_disc_csv_still_produces_file(self, tmp_path):
        """disc_csv が空でもファイル自体は生成される (見出しは出ない)"""
        with patch.object(make_market_db, 'DATA_DIR', str(tmp_path)):
            os.makedirs(os.path.join(str(tmp_path), "code_rank_data"), exist_ok=True)
            html_path = make_market_db.create_disclosure_html(None)
            assert os.path.exists(html_path)


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


# ==================================================
# get_market_db — スレッドセーフティ
# ==================================================
class TestGetMarketDbThreadSafety:
    """get_market_dbのマルチスレッド同時呼び出しテスト

    修正前はキャッシュチェックとDB open/closeの間にロックがなく、
    複数スレッドが同時にキャッシュミス→シングルトンDBのopen/closeが
    競合してRuntimeError: Database not openが発生していた。
    """

    def test_concurrent_get_market_db_no_error(self, tmp_path):
        """5スレッドから同時にget_market_dbを呼んでもエラーにならない"""
        import db_shelve

        # テスト用の一時shelve DBを作成
        test_db_path = str(tmp_path / "test_market_db")
        test_db = db_shelve.ShelveDB(test_db_path)
        with test_db:
            test_db["theme_rank"] = ["AI", "半導体", "DX"]
            test_db["access_date"] = "2026-03-24"

        # キャッシュをクリアし、シングルトンをテスト用DBに差し替え
        make_market_db._market_db_cache = None
        original_get = db_shelve._market_db
        db_shelve._market_db = db_shelve.ShelveDB(test_db_path)

        errors = []
        results = []
        barrier = threading.Barrier(5)

        def worker():
            try:
                barrier.wait()  # 全スレッドが揃ってから同時に呼び出す
                result = make_market_db.get_market_db()
                results.append(result)
            except Exception as e:
                errors.append(e)

        try:
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert errors == [], f"スレッドでエラー発生: {errors}"
            assert len(results) == 5
            # 全スレッドが同じデータを取得すること
            for r in results:
                assert r["theme_rank"] == ["AI", "半導体", "DX"]
        finally:
            # クリーンアップ
            make_market_db._market_db_cache = None
            db_shelve._market_db = original_get


# ==================================================
# _html_disclosure
# ==================================================
class TestHtmlDisclosure:
    """_html_disclosure の英語版IR重複除外テスト"""

    def _row(self, heading, code="3496", name="アズーム"):
        from datetime import datetime
        today = datetime.today().strftime("%Y%m%d")
        return [
            today,
            '=HYPERLINK("https://kabutan.jp/stock/chart?code=%s","%s")' % (code, code),
            name,
            "開示",
            '=HYPERLINK("https://example.com/x","%s")' % heading,
        ]

    def test_日本語見出しはHTMLに含まれる(self):
        rows = [self._row("業績予想の修正に関するお知らせ")]
        html = make_market_db._html_disclosure(rows)
        assert "業績予想の修正に関するお知らせ" in html

    def test_ASCIIのみの見出しは除外される(self):
        rows = [self._row("Notice Concerning Status of Treasury Stock Acquisition")]
        html = make_market_db._html_disclosure(rows)
        assert "Notice Concerning" not in html

    def test_日本語と英語混在で日本語のみ残る(self):
        rows = [
            self._row("決算短信"),
            self._row("[Summary]Consolidated Financial Results"),
            self._row("自己株式の取得状況に関するお知らせ"),
        ]
        html = make_market_db._html_disclosure(rows)
        assert "決算短信" in html
        assert "自己株式の取得状況に関するお知らせ" in html
        assert "Summary" not in html
        assert "Consolidated" not in html


# ==================================================
# _is_index_fetch_valid + update_market_db 取得失敗時の保護 (issue #179)
# ==================================================
class TestIndexFetchValid:
    """_is_index_fetch_valid のユニットテスト"""

    def test_empty_dict_is_invalid(self):
        assert make_market_db._is_index_fetch_valid({}) is False

    def test_none_is_invalid(self):
        assert make_market_db._is_index_fetch_valid(None) is False

    def test_missing_price_log_is_invalid(self):
        # 週足だけ部分的に成功した dict (rs_raw 等あるが price_log 無し)
        partial = {"rs_raw": 0, "trend_template": "0/7"}
        assert make_market_db._is_index_fetch_valid(partial) is False

    def test_empty_price_log_is_invalid(self):
        assert make_market_db._is_index_fetch_valid({"price_log": []}) is False

    def test_non_empty_price_log_is_valid(self):
        valid = {"price_log": [(datetime(2026, 5, 1).date(), 3728)]}
        assert make_market_db._is_index_fetch_valid(valid) is True


class TestUpdateMarketDbSkipsOnFetchFailure:
    """update_market_db: 指数取得失敗時に前日データが上書きされないこと (issue #179)"""

    @staticmethod
    def _good_index_dict(rs=1.17):
        """price_log を持つ "成功" dict のひな形"""
        return {
            "price": 3728,
            "price_log": [(datetime(2026, 5, 1).date(), 3728)],
            "daily_history": ["26/05/01"],
            "rs_raw": rs,
            "spr_20": 50,
            "spr_5": 52,
            "rv_20": 2.22,
            "rv_5": 2.3,
            "distribution_days_with_close": [],
        }

    def _patch_common(self, prev_db):
        """get_market_db / _save_market_db / make_theme_data をパッチする contextmanager 集合"""
        from unittest.mock import patch
        captured = {}

        def fake_save(db):
            captured["saved"] = dict(db)

        return (
            captured,
            patch.object(make_market_db, "get_market_db", return_value=dict(prev_db)),
            patch.object(make_market_db, "_save_market_db", side_effect=fake_save),
            patch.object(
                make_market_db, "make_theme_data", return_value={"theme_rank": []}
            ),
        )

    def test_前日DBが取得失敗時に保持される(self):
        """前日に正常取得した topix が、当日の取得失敗で上書きされない"""
        prev_topix = self._good_index_dict(rs=1.17)
        prev_db = {"topix": prev_topix, "theme_rank": []}
        captured, p_get, p_save, p_theme = self._patch_common(prev_db)

        empty_maker = lambda: {"topix": {}}
        good_mothers = lambda: {"mothers": self._good_index_dict(rs=1.08)}
        good_nikkei = lambda: {"nikkei225": self._good_index_dict(rs=1.29)}
        good_nasdaq = lambda: {"nasdaq": self._good_index_dict(rs=1.17)}
        good_sp500 = lambda: {"sp500": self._good_index_dict(rs=1.11)}

        with p_get, p_save, p_theme, \
                patch.object(make_market_db, "make_topix_db", side_effect=empty_maker), \
                patch.object(make_market_db, "make_mothers_db", side_effect=good_mothers), \
                patch.object(make_market_db, "make_nikkei_db", side_effect=good_nikkei), \
                patch.object(make_market_db, "make_nasdaq_db", side_effect=good_nasdaq), \
                patch.object(make_market_db, "make_sp500_db", side_effect=good_sp500):
            make_market_db.update_market_db()

        saved_topix = captured["saved"]["topix"]
        assert saved_topix == prev_topix, "前日 topix dict がそのまま保持されるべき"
        # 他指数は更新されていること
        assert captured["saved"]["mothers"]["rs_raw"] == 1.08

    def test_週足のみ成功でも前日DB保持(self):
        """週足だけ部分的に成功 (rs_raw=0, price_log 無し) でも上書きされない"""
        prev_topix = self._good_index_dict(rs=1.17)
        prev_db = {"topix": prev_topix, "theme_rank": []}
        captured, p_get, p_save, p_theme = self._patch_common(prev_db)

        partial_maker = lambda: {
            "topix": {"rs_raw": 0, "trend_template": "0/7", "pullback_20": 0}
        }
        good = lambda key, rs: (lambda: {key: self._good_index_dict(rs=rs)})

        with p_get, p_save, p_theme, \
                patch.object(make_market_db, "make_topix_db", side_effect=partial_maker), \
                patch.object(make_market_db, "make_mothers_db", side_effect=good("mothers", 1.08)), \
                patch.object(make_market_db, "make_nikkei_db", side_effect=good("nikkei225", 1.29)), \
                patch.object(make_market_db, "make_nasdaq_db", side_effect=good("nasdaq", 1.17)), \
                patch.object(make_market_db, "make_sp500_db", side_effect=good("sp500", 1.11)):
            make_market_db.update_market_db()

        assert captured["saved"]["topix"] == prev_topix

    def test_初回起動で取得失敗時はキー確保(self):
        """既存DBに topix が無い状態で取得失敗 → 空 dict をセットして
        下流の market_db['topix'] 参照が KeyError にならないようにする"""
        prev_db = {"theme_rank": []}  # topix キー無し
        captured, p_get, p_save, p_theme = self._patch_common(prev_db)

        empty_maker = lambda: {"topix": {}}
        good = lambda key, rs: (lambda: {key: self._good_index_dict(rs=rs)})

        with p_get, p_save, p_theme, \
                patch.object(make_market_db, "make_topix_db", side_effect=empty_maker), \
                patch.object(make_market_db, "make_mothers_db", side_effect=good("mothers", 1.08)), \
                patch.object(make_market_db, "make_nikkei_db", side_effect=good("nikkei225", 1.29)), \
                patch.object(make_market_db, "make_nasdaq_db", side_effect=good("nasdaq", 1.17)), \
                patch.object(make_market_db, "make_sp500_db", side_effect=good("sp500", 1.11)):
            make_market_db.update_market_db()

        assert "topix" in captured["saved"]
        assert captured["saved"]["topix"] == {}
