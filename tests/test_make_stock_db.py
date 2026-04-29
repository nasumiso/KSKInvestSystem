"""make_stock_db.py のロジックテスト"""

from datetime import date, datetime, timedelta
import pytest

import make_stock_db


# ==================================================
# has_price_data
# ==================================================
class TestHasPriceData:
    """価格データ鮮度チェックのテスト"""

    def test_no_code_in_db(self):
        """DBに銘柄がない場合"""
        stocks = {}
        assert make_stock_db.has_price_data(stocks, "1234") is False

    def test_no_sell_pressure(self):
        """銘柄はあるが sell_pressure_ratio がない"""
        stocks = {"1234": {"stock_name": "Test"}}
        assert make_stock_db.has_price_data(stocks, "1234") is False

    def test_has_data_no_latest(self):
        """latest=False でデータあり"""
        stocks = {"1234": {"sell_pressure_ratio": [50, 60, 40, 2.5, 1.8]}}
        assert make_stock_db.has_price_data(stocks, "1234", latest=False) is True


# ==================================================
# has_gyoseki_data
# ==================================================
class TestHasGyosekiData:
    """業績データ鮮度チェックのテスト"""

    def test_no_code(self):
        """DBに銘柄がない場合"""
        stocks = {}
        has_data, reason = make_stock_db.has_gyoseki_data(stocks, "1234")
        assert has_data is False
        assert reason == make_stock_db._UPD_REASON_NO_DATA

    def test_no_access_date(self):
        """access_date_gyoseki がない場合"""
        stocks = {"1234": {"stock_name": "Test"}}
        has_data, reason = make_stock_db.has_gyoseki_data(stocks, "1234")
        assert has_data is False
        assert reason == make_stock_db._UPD_REASON_NO_DATA

    def test_has_data_no_latest(self):
        """latest=False でアクセス日あり"""
        stocks = {"1234": {"access_date_gyoseki": datetime(2025, 1, 1)}}
        has_data, reason = make_stock_db.has_gyoseki_data(stocks, "1234", latest=False)
        assert has_data is True
        assert reason == make_stock_db._UPD_REASON_NONE


# ==================================================
# get_trend_template_expr
# ==================================================
class TestGetTrendTemplateExpr:
    """トレンドテンプレート表示のテスト"""

    def test_no_key(self):
        """trend_template キーがない場合"""
        assert make_stock_db.get_trend_template_expr({}) == "-"

    def test_perfect(self):
        """全条件クリア（空リスト）"""
        assert make_stock_db.get_trend_template_expr({"trend_template": []}) == "◎"

    def test_minor_miss(self):
        """1〜2条件ミス"""
        result = make_stock_db.get_trend_template_expr({"trend_template": ["MA50"]})
        assert result.startswith("◯")
        assert "MA50" in result

    def test_moderate_miss(self):
        """3〜4条件ミス"""
        result = make_stock_db.get_trend_template_expr(
            {"trend_template": ["a", "b", "c"]}
        )
        assert result == "▲"

    def test_many_miss(self):
        """5〜6条件ミス"""
        result = make_stock_db.get_trend_template_expr(
            {"trend_template": ["a", "b", "c", "d", "e"]}
        )
        assert result == "△"

    def test_all_miss(self):
        """7条件以上ミス"""
        result = make_stock_db.get_trend_template_expr(
            {"trend_template": ["a", "b", "c", "d", "e", "f", "g"]}
        )
        assert result == ""


# ==================================================
# make_signal
# ==================================================
class TestMakeSignal:
    """シグナル生成ロジックのテスト"""

    def test_empty_stock(self):
        """空の銘柄データ"""
        signal, tags = make_stock_db.make_signal({})
        assert isinstance(signal, str)
        assert isinstance(tags, list)

    def test_no_signals(self):
        """シグナルなしの通常データ"""
        stock = {
            "sell_pressure_ratio": [50, 50, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        signal, tags = make_stock_db.make_signal(stock)
        assert "売" not in tags
        assert "警" not in tags

    def test_high_sell_pressure(self):
        """買われ過ぎシグナル"""
        stock = {
            "sell_pressure_ratio": [50, 80, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        signal, tags = make_stock_db.make_signal(stock)
        assert "[買過]" in signal

    def test_low_sell_pressure(self):
        """売られ過ぎシグナル"""
        stock = {
            "sell_pressure_ratio": [50, 20, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        signal, tags = make_stock_db.make_signal(stock)
        assert "[売過]" in signal


# ==================================================
# update_db — shihyo マージロジック
# ==================================================
class TestUpdateDbShihyoMerge:
    """update_db()のshihyoキー単位マージテスト"""

    def test_empty_shihyo_preserves_existing(self):
        """空のshihyoで既存データが消えないこと"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "shihyo": {"PER": 15.0, "PBR": 1.2, "PSR": 2.5},
                "shihyo_pt": 50,
            }
        }
        stock_data = {"code_s": "1234", "shihyo": {}, "shihyo_pt": 0}
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["shihyo"]["PER"] == 15.0
        assert stocks["1234"]["shihyo"]["PBR"] == 1.2
        assert stocks["1234"]["shihyo"]["PSR"] == 2.5

    def test_new_shihyo_merges_with_existing(self):
        """新しいshihyoデータが既存データとマージされること"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "shihyo": {"PER": 15.0, "PBR": 1.2, "ROE": 10.0},
            }
        }
        stock_data = {"code_s": "1234", "shihyo": {"PER": 20.0, "PSR": 3.0}}
        make_stock_db.update_db(stocks, stock_data)
        # PERは新しい値で更新
        assert stocks["1234"]["shihyo"]["PER"] == 20.0
        # PBR, ROEは既存値が保持
        assert stocks["1234"]["shihyo"]["PBR"] == 1.2
        assert stocks["1234"]["shihyo"]["ROE"] == 10.0
        # PSRは新規追加
        assert stocks["1234"]["shihyo"]["PSR"] == 3.0

    def test_new_stock_with_shihyo(self):
        """新規銘柄にshihyoが正常に設定されること"""
        stocks = {}
        stock_data = {"code_s": "5678", "shihyo": {"PER": 12.0}}
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["5678"]["shihyo"]["PER"] == 12.0

    def test_new_stock_with_empty_shihyo(self):
        """新規銘柄で空shihyoの場合、空dictとしてキーが初期化されること"""
        stocks = {}
        stock_data = {"code_s": "5678", "shihyo": {}}
        make_stock_db.update_db(stocks, stock_data)
        # 新規銘柄では空dictでもキーを初期化（下流でKeyErrorを防ぐ）
        assert "shihyo" in stocks["5678"]
        assert stocks["5678"]["shihyo"] == {}


class TestUpdateDbProtectedListKeys:
    """update_db()のlist型キー保護テスト"""

    def test_empty_list_preserves_existing(self):
        """空リストで既存のlist型データが消えないこと"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "stddev_volatility": [12.5, 15.0],
                "sell_pressure_ratio": [0.8, 0.6, 0.7],
                "gyoseki_current": [{"year": 2025, "sales": 1000}],
            }
        }
        stock_data = {
            "code_s": "1234",
            "stddev_volatility": [],
            "sell_pressure_ratio": [],
            "gyoseki_current": [],
        }
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["stddev_volatility"] == [12.5, 15.0]
        assert stocks["1234"]["sell_pressure_ratio"] == [0.8, 0.6, 0.7]
        assert stocks["1234"]["gyoseki_current"] == [{"year": 2025, "sales": 1000}]

    def test_new_list_overwrites_existing(self):
        """新しいlist型データが正常に上書きされること"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "stddev_volatility": [12.5, 15.0],
            }
        }
        stock_data = {
            "code_s": "1234",
            "stddev_volatility": [20.0, 25.0],
        }
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["stddev_volatility"] == [20.0, 25.0]


class TestUpdateDbProtectedZeroKeys:
    """update_db()の理論株価ゼロ値保護テスト"""

    def test_zero_rironkabuka_preserves_existing(self):
        """理論株価が0で既存値が消えないこと"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "rironkabuka": 1500,
                "rironkabuka_up": 2000,
                "rironkabuka_down": 1000,
                "rironkabuka_preceding": 1600,
            }
        }
        stock_data = {
            "code_s": "1234",
            "rironkabuka": 0,
            "rironkabuka_up": 0,
            "rironkabuka_down": 0,
            "rironkabuka_preceding": 0,
        }
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["rironkabuka"] == 1500
        assert stocks["1234"]["rironkabuka_up"] == 2000
        assert stocks["1234"]["rironkabuka_down"] == 1000
        assert stocks["1234"]["rironkabuka_preceding"] == 1600

    def test_nonzero_rironkabuka_updates(self):
        """理論株価が非0で正常に更新されること"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "rironkabuka": 1500,
            }
        }
        stock_data = {
            "code_s": "1234",
            "rironkabuka": 1800,
        }
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["rironkabuka"] == 1800

    def test_new_stock_with_zero_rironkabuka(self):
        """新規銘柄で理論株価0の場合、0が設定されること"""
        stocks = {}
        stock_data = {
            "code_s": "5678",
            "rironkabuka": 0,
        }
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["5678"]["rironkabuka"] == 0


class TestUpdateDbAccessDateDeletion:
    """update_db()のaccess_date削除テスト"""

    def test_none_access_date_deletes_existing(self):
        """access_date_*がNoneの場合、既存のaccess_dateが削除されること"""
        from datetime import datetime
        stocks = {
            "1234": {
                "code_s": "1234",
                "access_date_shihyo": datetime(2026, 1, 1),
                "shihyo": {"PER": 15.0},
            }
        }
        stock_data = {"code_s": "1234", "access_date_shihyo": None, "shihyo": {}}
        make_stock_db.update_db(stocks, stock_data)
        assert "access_date_shihyo" not in stocks["1234"]
        # shihyoの既存値は保持される
        assert stocks["1234"]["shihyo"]["PER"] == 15.0

    def test_none_access_date_no_error_when_missing(self):
        """access_date_*が元々存在しない場合にエラーにならないこと"""
        stocks = {"1234": {"code_s": "1234"}}
        stock_data = {"code_s": "1234", "access_date_gyoseki": None}
        make_stock_db.update_db(stocks, stock_data)
        assert "access_date_gyoseki" not in stocks["1234"]

    def test_valid_access_date_is_set(self):
        """access_date_*が有効値の場合は正常に設定されること"""
        from datetime import datetime
        stocks = {"1234": {"code_s": "1234"}}
        dt = datetime(2026, 3, 22)
        stock_data = {"code_s": "1234", "access_date_shihyo": dt}
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["access_date_shihyo"] == dt


class TestUpdateDbSignalKeys:
    """pocket_pivot/breakoutが保護対象外であることのテスト"""

    def test_empty_pocket_pivot_clears_existing(self):
        """pocket_pivotが空リストで既存値が消えること（正常な状態遷移）"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "pocket_pivot": [("2026-03-01", 1500)],
            }
        }
        stock_data = {"code_s": "1234", "pocket_pivot": []}
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["pocket_pivot"] == []

    def test_empty_breakout_clears_existing(self):
        """breakoutが空リストで既存値が消えること（正常な状態遷移）"""
        stocks = {
            "1234": {
                "code_s": "1234",
                "breakout": [("2026-03-01", 2000)],
            }
        }
        stock_data = {"code_s": "1234", "breakout": []}
        make_stock_db.update_db(stocks, stock_data)
        assert stocks["1234"]["breakout"] == []


# ==================================================
# main() の CLI 引数 (update / list の銘柄指定)
# ==================================================
class TestMainCLIArgs:
    """`make_stock_db.py update 6324` のように銘柄を引数指定できることのテスト"""

    def _patch_common(self, monkeypatch):
        import googledrive
        monkeypatch.setattr(googledrive, "wait_all_uploads", lambda: None)

    def test_update_with_codes(self, monkeypatch):
        """update に銘柄コードを渡すと code_list がそれになる"""
        called = {}

        def fake_update_db_rows(code_list, upd=None, tables=None):
            called["code_list"] = code_list

        monkeypatch.setattr(make_stock_db, "update_db_rows", fake_update_db_rows)
        monkeypatch.setattr(make_stock_db, "update_research_snapshots", lambda: None)
        self._patch_common(monkeypatch)

        monkeypatch.setattr("sys.argv", ["make_stock_db.py", "update", "6324"])
        make_stock_db.main()
        assert called["code_list"] == ["6324"]

    def test_update_with_multiple_codes(self, monkeypatch):
        """複数銘柄も渡せる"""
        called = {}

        def fake_update_db_rows(code_list, upd=None, tables=None):
            called["code_list"] = code_list

        monkeypatch.setattr(make_stock_db, "update_db_rows", fake_update_db_rows)
        monkeypatch.setattr(make_stock_db, "update_research_snapshots", lambda: None)
        self._patch_common(monkeypatch)

        monkeypatch.setattr(
            "sys.argv", ["make_stock_db.py", "update", "6324", "7203", "215A"]
        )
        make_stock_db.main()
        assert called["code_list"] == ["6324", "7203", "215A"]

    def test_update_without_codes_uses_default(self, monkeypatch):
        """codes 未指定時はソース内デフォルトが使われる (既存挙動維持)"""
        called = {}

        def fake_update_db_rows(code_list, upd=None, tables=None):
            called["code_list"] = code_list

        monkeypatch.setattr(make_stock_db, "update_db_rows", fake_update_db_rows)
        monkeypatch.setattr(make_stock_db, "update_research_snapshots", lambda: None)
        self._patch_common(monkeypatch)

        monkeypatch.setattr("sys.argv", ["make_stock_db.py", "update"])
        make_stock_db.main()
        assert called["code_list"] == ["471A"]

    def test_update_snapshot_flag(self, monkeypatch):
        """--snapshot フラグで update_research_snapshots が指定銘柄に絞って呼ばれる"""
        called = {"update_db_rows": False, "snapshot_kwargs": None}

        def fake_update_db_rows(code_list, upd=None, tables=None):
            called["update_db_rows"] = True

        def fake_snapshots(*, db_path=None, code_filter=None):
            called["snapshot_kwargs"] = {"db_path": db_path, "code_filter": code_filter}

        monkeypatch.setattr(make_stock_db, "update_db_rows", fake_update_db_rows)
        monkeypatch.setattr(make_stock_db, "update_research_snapshots", fake_snapshots)
        self._patch_common(monkeypatch)

        monkeypatch.setattr(
            "sys.argv", ["make_stock_db.py", "update", "6324", "7203", "--snapshot"]
        )
        make_stock_db.main()
        assert called["update_db_rows"] is True
        # update 対象銘柄だけがフィルタに渡る (ウォッチ全銘柄を走らせない)
        assert called["snapshot_kwargs"]["code_filter"] == ["6324", "7203"]

    def test_list_with_codes(self, monkeypatch):
        """list に銘柄コードを渡すと code_list がそれになる"""
        called = {}

        def fake_list_db(code_list):
            called["code_list"] = code_list

        monkeypatch.setattr(make_stock_db, "list_db", fake_list_db)
        self._patch_common(monkeypatch)

        monkeypatch.setattr("sys.argv", ["make_stock_db.py", "list", "6324"])
        make_stock_db.main()
        assert called["code_list"] == ["6324"]


# ==================================================
# compute_rs_line
# ==================================================
def _make_log(n, base=1000, step=5, d0=date(2026, 4, 28)):
    """日付降順 (date, int) タプルリスト生成。新しい日付が先頭。
    base + step*(n-i) で銘柄系列が単調増加するように作る。
    """
    return [(d0 - timedelta(days=i), base + step * (n - i)) for i in range(n)]


def _make_div_logs(stock_now, stock_past, topix_now, topix_past, n=25,
                   d0=date(2026, 4, 28)):
    """offset=20 でちょうど stock_now/past, topix_now/past となる日付降順タプル列を生成。

    index 0 = 今日 (stock_now / topix_now), index 20 = 20日前 (stock_past / topix_past)。
    """
    stock_log, topix_log = [], []
    for i in range(n):
        t = i / 20.0
        stock_log.append((d0 - timedelta(days=i),
                          int(stock_now + (stock_past - stock_now) * t)))
        topix_log.append((d0 - timedelta(days=i),
                          int(topix_now + (topix_past - topix_now) * t)))
    return stock_log, topix_log


class TestComputeRsLine:
    """rs_line (銘柄終値/TOPIX終値) 計算の単体テスト"""

    def test_returns_empty_when_stock_log_missing(self):
        """銘柄側 price_log が無いと空リスト"""
        market_db = {"topix": {"price_log": _make_log(25)}}
        assert make_stock_db.compute_rs_line({}, market_db) == []

    def test_returns_empty_when_topix_log_missing(self):
        """TOPIX 側 price_log が無いと空リスト"""
        stock = {"price_log": _make_log(25)}
        assert make_stock_db.compute_rs_line(stock, {"topix": {}}) == []
        assert make_stock_db.compute_rs_line(stock, {}) == []

    def test_basic_calculation(self):
        """全日付一致時、ratio = stock/topix で系列が返る"""
        stock = {"price_log": _make_log(25, base=2000, step=10)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=2)}}
        result = make_stock_db.compute_rs_line(stock, market_db)
        assert len(result) == 25
        # 先頭: stock = 2000 + 10*25 = 2250, topix = 1000 + 2*25 = 1050
        assert abs(result[0][1] - (2250.0 / 1050.0)) < 1e-6

    def test_skips_dates_missing_in_topix(self):
        """銘柄にあって TOPIX にない日付は除外"""
        d0 = date(2026, 4, 28)
        stock = {"price_log": [(d0, 2000), (d0 - timedelta(days=1), 1990)]}
        market_db = {"topix": {"price_log": [(d0, 1000)]}}  # 前日なし
        result = make_stock_db.compute_rs_line(stock, market_db)
        assert len(result) == 1
        assert result[0][1] == 2.0

    def test_skips_zero_topix_close(self):
        """TOPIX 終値0は計算不能なので除外"""
        d0 = date(2026, 4, 28)
        stock = {"price_log": [(d0, 2000), (d0 - timedelta(days=1), 1990)]}
        market_db = {"topix": {"price_log": [(d0, 1000), (d0 - timedelta(days=1), 0)]}}
        result = make_stock_db.compute_rs_line(stock, market_db)
        assert len(result) == 1

    def test_skips_zero_stock_close(self):
        """銘柄終値0も除外"""
        d0 = date(2026, 4, 28)
        stock = {"price_log": [(d0, 0), (d0 - timedelta(days=1), 1990)]}
        market_db = {"topix": {"price_log": [(d0, 1000), (d0 - timedelta(days=1), 1000)]}}
        result = make_stock_db.compute_rs_line(stock, market_db)
        assert len(result) == 1

    def test_handles_short_stock_log(self):
        """銘柄系列が短い場合 (上場直後) は短い分だけ"""
        stock = {"price_log": _make_log(5, base=2000)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000)}}
        result = make_stock_db.compute_rs_line(stock, market_db)
        assert len(result) == 5

    def test_descending_dates(self):
        """戻り値は日付降順 (新しい日付が先頭)"""
        stock = {"price_log": _make_log(25, base=2000)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000)}}
        result = make_stock_db.compute_rs_line(stock, market_db)
        dates = [d for d, _ in result]
        assert dates == sorted(dates, reverse=True)


# ==================================================
# compute_rs_line_changes
# ==================================================
class TestComputeRsLineChanges:
    """rs_line 騰落率 (5日前比 A / 20日前比 B) の単体テスト"""

    def test_none_when_rs_line_empty(self):
        a, b = make_stock_db.compute_rs_line_changes({}, {"topix": {}})
        assert a is None and b is None

    def test_none_when_too_short_for_short_change(self):
        """rs_line が 6本未満なら 5日前比 A も計算不能"""
        stock = {"price_log": _make_log(5, base=2000)}
        market_db = {"topix": {"price_log": _make_log(5, base=1000)}}
        a, b = make_stock_db.compute_rs_line_changes(stock, market_db)
        assert a is None and b is None

    def test_short_only_when_partial_data(self):
        """rs_line が 6本以上21本未満なら A だけ計算可、B は None"""
        stock = {"price_log": _make_log(10, base=2000)}
        market_db = {"topix": {"price_log": _make_log(10, base=1000)}}
        a, b = make_stock_db.compute_rs_line_changes(stock, market_db)
        assert a is not None and b is None

    def test_both_when_full_data(self):
        """rs_line が 21本以上で A・B 両方計算可"""
        stock = {"price_log": _make_log(25, base=2000)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000)}}
        a, b = make_stock_db.compute_rs_line_changes(stock, market_db)
        assert a is not None and b is not None

    def test_uptrend_positive_signs(self):
        """rs_line が上昇トレンド (TOPIX より速く上昇) なら A・B プラス"""
        # 銘柄: 速く上昇 (step=20), TOPIX: 緩やか (step=2) → ratio は単調増加
        stock = {"price_log": _make_log(25, base=2000, step=20)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=2)}}
        a, b = make_stock_db.compute_rs_line_changes(stock, market_db)
        assert a > 0 and b > 0

    def test_downtrend_negative_signs(self):
        """rs_line 下降トレンド (TOPIX より遅い) なら A・B マイナス"""
        # 銘柄: 緩やか上昇 (step=2), TOPIX: 速く上昇 (step=20)
        stock = {"price_log": _make_log(25, base=2000, step=2)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=20)}}
        a, b = make_stock_db.compute_rs_line_changes(stock, market_db)
        assert a < 0 and b < 0


# ==================================================
# get_rs_line_changes_expr
# ==================================================
class TestGetRsLineChangesExpr:
    """rs_line 騰落率の CSV 表示文字列テスト"""

    def test_empty_when_uncomputable(self):
        """rs_line が計算不能なら空文字"""
        s = make_stock_db.get_rs_line_changes_expr({}, {"topix": {}})
        assert s == ""

    def test_format_both_present(self):
        """A・B 両方計算可: '中期B%/短期A%' 形式 (符号付き整数)"""
        stock = {"price_log": _make_log(25, base=2000, step=20)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=2)}}
        s = make_stock_db.get_rs_line_changes_expr(stock, market_db)
        # B/A の順、符号付き
        parts = s.split("/")
        assert len(parts) == 2
        assert parts[0].startswith("+") or parts[0].startswith("-")
        assert parts[1].startswith("+") or parts[1].startswith("-")

    def test_format_partial_only_a(self):
        """A のみ計算可なら '-/+5' のように B は '-'"""
        stock = {"price_log": _make_log(10, base=2000, step=10)}
        market_db = {"topix": {"price_log": _make_log(10, base=1000, step=2)}}
        s = make_stock_db.get_rs_line_changes_expr(stock, market_db)
        # B は計算不能で "-", A は数値
        assert s.startswith("-/")

    def test_negative_format(self):
        """マイナス側の符号も正しく表示される"""
        stock = {"price_log": _make_log(25, base=2000, step=2)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=20)}}
        s = make_stock_db.get_rs_line_changes_expr(stock, market_db)
        parts = s.split("/")
        assert parts[0].startswith("-")
        assert parts[1].startswith("-")


# ==================================================
# compute_rs_line_new_high
# ==================================================
class TestComputeRsLineNewHigh:
    """rs_line 新高値判定の単体テスト"""

    def test_returns_false_when_empty(self):
        assert make_stock_db.compute_rs_line_new_high({}, {"topix": {}}) is False

    def test_returns_false_when_short(self):
        """rs_line が lookback+1 本未満なら False"""
        stock = {"price_log": _make_log(10, base=2000)}
        market_db = {"topix": {"price_log": _make_log(10, base=1000)}}
        assert make_stock_db.compute_rs_line_new_high(stock, market_db, lookback=20) is False

    def test_returns_true_when_strict_high(self):
        """rs_line[0] が直近20日の最高値より厳密に大きい → True"""
        stock = {"price_log": _make_log(25, base=2000, step=10)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=0)}}
        assert make_stock_db.compute_rs_line_new_high(stock, market_db) is True

    def test_returns_false_when_not_high(self):
        """rs_line[0] が過去より小さい → False"""
        d0 = date(2026, 4, 28)
        # 古い日付ほど高い値、今日ほど低い値
        price_log = [(d0 - timedelta(days=i), 2000 + i * 10) for i in range(25)]
        stock = {"price_log": price_log}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=0)}}
        assert make_stock_db.compute_rs_line_new_high(stock, market_db) is False

    def test_returns_false_when_equal(self):
        """同値（横ばい）は False — Q3「当日発生」のため厳密比較 >"""
        d0 = date(2026, 4, 28)
        price_log = [(d0 - timedelta(days=i), 2000) for i in range(25)]
        stock = {"price_log": price_log}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=0)}}
        assert make_stock_db.compute_rs_line_new_high(stock, market_db) is False

    def test_lookback_parameter(self):
        """lookback を変えて短期判定として使えること"""
        d0 = date(2026, 4, 28)
        # 直近6日: 2050, 2040, 2030, 2020, 2010, 2000 (新→古)
        # それ以前: 2200+ (高い値)
        price_log = []
        for i in range(6):
            price_log.append((d0 - timedelta(days=i), 2050 - i * 10))
        for i in range(6, 25):
            price_log.append((d0 - timedelta(days=i), 2200 + i))
        stock = {"price_log": price_log}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=0)}}
        # lookback=5: 直近5日の最高値 (2040) を 2050 が更新 → True
        assert make_stock_db.compute_rs_line_new_high(stock, market_db, lookback=5) is True
        # lookback=20: 過去20日に 2200+ がある → False
        assert make_stock_db.compute_rs_line_new_high(stock, market_db, lookback=20) is False


# ==================================================
# compute_rs_line_divergence
# ==================================================
class TestComputeRsLineDivergence:
    """株価×rs_line ダイバージェンス判定の単体テスト"""

    def test_returns_empty_when_no_data(self):
        assert make_stock_db.compute_rs_line_divergence({}, {"topix": {}}) == ""

    def test_returns_empty_when_short(self):
        """rs_line が offset+1 本未満なら ''"""
        stock = {"price_log": _make_log(10, base=2000)}
        market_db = {"topix": {"price_log": _make_log(10, base=1000)}}
        assert make_stock_db.compute_rs_line_divergence(stock, market_db, offset=20) == ""

    def test_bullish_divergence(self):
        """株価↓ かつ rs_line↑ → 'bullish'
        銘柄 1900/2000 (-5%), TOPIX 900/1000 (-10%) → rs +5.5%
        """
        stock_log, topix_log = _make_div_logs(1900, 2000, 900, 1000)
        stock = {"price_log": stock_log}
        market_db = {"topix": {"price_log": topix_log}}
        assert make_stock_db.compute_rs_line_divergence(stock, market_db) == "bullish"

    def test_bearish_divergence(self):
        """株価↑ かつ rs_line↓ → 'bearish'
        銘柄 2100/2000 (+5%), TOPIX 1100/1000 (+10%) → rs -4.5%
        """
        stock_log, topix_log = _make_div_logs(2100, 2000, 1100, 1000)
        stock = {"price_log": stock_log}
        market_db = {"topix": {"price_log": topix_log}}
        assert make_stock_db.compute_rs_line_divergence(stock, market_db) == "bearish"

    def test_no_divergence_same_direction(self):
        """株価・rs_line が同方向（両方プラス）→ ''"""
        stock = {"price_log": _make_log(25, base=2000, step=20)}
        market_db = {"topix": {"price_log": _make_log(25, base=1000, step=2)}}
        assert make_stock_db.compute_rs_line_divergence(stock, market_db) == ""

    def test_below_threshold(self):
        """株価変化が閾値未満なら ''
        銘柄 1980/2000 (-1%, 閾値3%未満)
        """
        stock_log, topix_log = _make_div_logs(1980, 2000, 900, 1000)
        stock = {"price_log": stock_log}
        market_db = {"topix": {"price_log": topix_log}}
        assert make_stock_db.compute_rs_line_divergence(stock, market_db) == ""

    def test_threshold_parameter(self):
        """threshold を変えれば判定が変わる
        銘柄-2%, TOPIX-4% → rs +2.08%
        threshold=3% で発火しない、threshold=1% で bullish
        """
        stock_log, topix_log = _make_div_logs(1960, 2000, 960, 1000)
        stock = {"price_log": stock_log}
        market_db = {"topix": {"price_log": topix_log}}
        assert make_stock_db.compute_rs_line_divergence(stock, market_db, threshold=3.0) == ""
        assert make_stock_db.compute_rs_line_divergence(stock, market_db, threshold=1.0) == "bullish"


# ==================================================
# make_signal — RSライン拡張
# ==================================================
class TestMakeSignalRsLine:
    """make_signal で market_db を渡したときの rs_line 系タグ付与テスト"""

    def test_market_db_none_skips_rs_line_tags(self):
        """market_db=None で呼ばれた場合、RSライン系タグは付かない（後方互換）"""
        d0 = date(2026, 4, 28)
        stock = {
            "price_log": [(d0 - timedelta(days=i), 2000 + (25 - i) * 10) for i in range(25)],
        }
        _, tags = make_stock_db.make_signal(stock)
        assert "R高" not in tags
        assert "強乖" not in tags
        assert "弱乖" not in tags

    def test_rs_line_new_high_tag(self):
        """rs_line 新高値発生時に R高 タグが付く"""
        d0 = date(2026, 4, 28)
        stock = {
            "price_log": [(d0 - timedelta(days=i), 2000 + (25 - i) * 10) for i in range(25)],
        }
        market_db = {
            "topix": {"price_log": [(d0 - timedelta(days=i), 1000) for i in range(25)]},
        }
        _, tags = make_stock_db.make_signal(stock, market_db=market_db)
        assert "R高" in tags

    def test_rs_line_bullish_divergence_tag(self):
        """強気ダイバージェンス発生時に 強乖 タグが付く"""
        stock_log, topix_log = _make_div_logs(1900, 2000, 900, 1000)
        stock = {"price_log": stock_log}
        market_db = {"topix": {"price_log": topix_log}}
        _, tags = make_stock_db.make_signal(stock, market_db=market_db)
        assert "強乖" in tags

    def test_rs_line_bearish_divergence_tag(self):
        """弱気ダイバージェンス発生時に 弱乖 タグが付く"""
        stock_log, topix_log = _make_div_logs(2100, 2000, 1100, 1000)
        stock = {"price_log": stock_log}
        market_db = {"topix": {"price_log": topix_log}}
        _, tags = make_stock_db.make_signal(stock, market_db=market_db)
        assert "弱乖" in tags

    def test_rs_line_tags_skipped_for_stale_stock(self):
        """銘柄 price_log が当日でない (古いキャッシュ) ならRS系タグは付かない。

        list_all_db は更新対象外の銘柄もCSVに出すため、price_log が数週間
        古い銘柄が混じる。連日同じシグナルが残らないように当日限定にする必要がある。
        """
        d0 = date(2026, 4, 28)
        # 銘柄 price_log は1週間前まで (新高値が立つ条件のデータ)
        stock = {
            "price_log": [(d0 - timedelta(days=7 + i), 2000 + (25 - i) * 10) for i in range(25)],
        }
        # TOPIX は当日 (d0) まで
        market_db = {
            "topix": {"price_log": [(d0 - timedelta(days=i), 1000) for i in range(32)]},
        }
        _, tags = make_stock_db.make_signal(stock, market_db=market_db)
        # rs_line[0] は1週間前の日付なので当日扱いにならず、タグは付かない
        assert "R高" not in tags
        assert "強乖" not in tags
        assert "弱乖" not in tags

    def test_rs_line_tags_emit_when_latest_date_matches(self):
        """銘柄 price_log[0] の日付が TOPIX price_log[0] と一致する場合はタグが立つ"""
        d0 = date(2026, 4, 28)
        stock = {
            "price_log": [(d0 - timedelta(days=i), 2000 + (25 - i) * 10) for i in range(25)],
        }
        market_db = {
            "topix": {"price_log": [(d0 - timedelta(days=i), 1000) for i in range(25)]},
        }
        _, tags = make_stock_db.make_signal(stock, market_db=market_db)
        assert "R高" in tags
