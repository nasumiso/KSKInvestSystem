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

    def test_new_theme_class(self):
        """新規テーマにtheme-newクラスが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'theme-new' in result
        assert 'NEW' in result

    def test_up_theme_class(self):
        """上昇テーマにtheme-upクラスが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'theme-up' in result
        assert '↑3' in result

    def test_down_theme_class(self):
        """下降テーマにtheme-downクラスが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'theme-down' in result
        assert '↓2' in result

    def test_flat_theme_class(self):
        """変動なしテーマにtheme-flatクラスが付く"""
        result = make_market_db._html_theme_rank(self._make_market_db())
        assert 'theme-flat' in result

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
        """テスト用market_db"""
        return {
            "topix": {
                "rs_raw": 1.18,
                "trend_template": [],  # 空リスト=◎
                "distribution_days": ["260213", "260220"],
                "followthrough_days": ["260305"],
                "direction_signal": "sell 26/03/13",
                "spr_buygagher": 49,
                "spr_20": 47,
                "spr_5": 45,
                "rv_20": 3.6,
                "rv_5": 5.1,
            },
            "mothers": {
                "rs_raw": 1.09,
                "trend_template": ["ma30>ma40", "RS"],
                "distribution_days": [],
                "followthrough_days": [],
                "direction_signal": "buy 26/03/10",
                "spr_buygagher": 55,
                "spr_20": 52,
                "spr_5": 50,
                "rv_20": 4.6,
                "rv_5": 5.4,
            },
        }

    def test_signal_sell_class(self):
        """sellシグナルにsignal-sellクラスが付く"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'signal-sell' in result

    def test_signal_buy_class(self):
        """buyシグナルにsignal-buyクラスが付く"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'signal-buy' in result

    def test_trend_good_class(self):
        """良好トレンド（◎/◯）にtrend-goodクラスが付く"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'trend-good' in result

    def test_market_table_header(self):
        """テーブルヘッダーが含まれる"""
        result = make_market_db._html_market(self._make_market_db())
        assert 'market-table' in result
        assert 'ディストリビューション' in result

    def test_empty_market_db(self):
        """市場データがない場合は空文字列"""
        result = make_market_db._html_market({})
        assert result == ""


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
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [
                "20260315",
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
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            [
                "20260315",
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
        """古いデータはdetails（折りたたみ）になる"""
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            ["20250101", "1234", "テスト", "開示", "テスト開示"],
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
        disc_csv = [
            ["日付", "銘柄コード", "銘柄名", "種類", "本文"],
            ["20260315", "1234", "テスト", "決算", "決算発表"],
        ]
        result = make_market_db._html_disclosure(disc_csv)
        assert 'disc-row-gyoseki' in result


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
