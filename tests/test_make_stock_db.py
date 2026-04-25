"""make_stock_db.py のロジックテスト"""

from datetime import datetime, timedelta
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
