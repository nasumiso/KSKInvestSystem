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
