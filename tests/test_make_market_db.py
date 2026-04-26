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
            "nasdaq": {
                "rs_raw": 1.05,
                "trend_template": [],
                "distribution_days": ["260301"],
                "followthrough_days": [],
                "direction_signal": "neutral 26/03/13",
                "spr_buygagher": 50,
                "spr_20": 48,
                "spr_5": 47,
                "rv_20": 2.1,
                "rv_5": 3.2,
            },
            "sp500": {
                "rs_raw": 1.02,
                "trend_template": [],
                "distribution_days": [],
                "followthrough_days": ["260308"],
                "direction_signal": "neutral 26/03/13",
                "spr_buygagher": 51,
                "spr_20": 50,
                "spr_5": 49,
                "rv_20": 1.8,
                "rv_5": 2.5,
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

    def test_nasdaq_row_rendered(self):
        """NASDAQ行が市場テーブルに表示される (issue #148)"""
        result = make_market_db._html_market(self._make_market_db())
        assert "<td><strong>NASDAQ</strong></td>" in result

    def test_sp500_row_rendered(self):
        """S&P 500行が市場テーブルに表示される (issue #148)。
        market_nameはhtml.escapeを通るため、& → &amp; となる。"""
        result = make_market_db._html_market(self._make_market_db())
        assert "<td><strong>S&amp;P 500</strong></td>" in result

    def test_us_indices_skipped_when_missing(self):
        """nasdaq/sp500 キー欠落時は該当行が出ず、既存の TOPIX/マザーズは出る"""
        partial_db = {
            k: v for k, v in self._make_market_db().items() if k in ("topix", "mothers")
        }
        result = make_market_db._html_market(partial_db)
        assert "<td><strong>TOPIX</strong></td>" in result
        assert "NASDAQ" not in result
        assert "S&amp;P 500" not in result


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
