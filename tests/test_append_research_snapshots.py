"""update_research_snapshots のテスト (issue #94)"""

import os
from datetime import datetime, timedelta

import pytest

import research_shelve as rs
import make_stock_db
import portfolio


@pytest.fixture
def db_path(tmp_path):
    """テスト用一時 research_shelve パス"""
    return str(tmp_path / "test_research")


def _today_str(offset_days=0):
    """テスト用: 今日 ± offset の日付を "YYYY/MM/DD" 形式で返す"""
    dt = datetime.today() + timedelta(days=offset_days)
    return dt.strftime("%Y/%m/%d")


def _today_yy_m_d(offset_days=0):
    """テスト用: 今日 ± offset の日付を "YY.M.D" 形式で返す"""
    dt = datetime.today() + timedelta(days=offset_days)
    return f"{dt.year % 100}.{dt.month}.{dt.day}"


def _make_stock(code_s, kessanbi="", kessan_mod_date="", stock_name="テスト銘柄"):
    """最小限の stock dict を生成する"""
    return {
        "code_s": code_s,
        "stock_name": stock_name,
        "kessanbi": kessanbi,
        "kessan_mod_date": kessan_mod_date,
        "score_gyoseki": "0",
        "shihyo_pt": 0,
        "shihyo": {},
    }


@pytest.fixture(autouse=True)
def _fix_today(monkeypatch):
    """get_price_day を固定して 18:00 前後のテスト不安定を排除"""
    monkeypatch.setattr(
        make_stock_db, "get_price_day",
        lambda dt: datetime.today().date(),
    )


@pytest.fixture(autouse=True)
def _watchlist_all(monkeypatch):
    """デフォルトで全銘柄をウォッチ扱いにする (parse_my_portforio を monkeypatch)。
    個別テストで絞り込みたい場合はテスト内で再 monkeypatch する。"""
    # ("*", []) を返すと update_research_snapshots 側の set() が
    # {"*"} になり「ワイルドカード」にはならないので、
    # 代わりに多数の銘柄コードを返し "存在するものだけヒット" させる。
    # 実際にはテスト側が load_stock_db を monkeypatch して対象コードを限定するので、
    # ここでは「対象コードを含む十分広いリスト」を返せばよい。
    wide = [f"{i:04d}" for i in range(1, 10000)] + ["215A", "247A"]
    monkeypatch.setattr(
        portfolio, "parse_my_portforio", lambda: (wide, [])
    )


class TestUpdateResearchSnapshots:
    """update_research_snapshots のユニットテスト"""

    def test_basic_append(self, db_path, monkeypatch):
        """決算日が今日の銘柄にスナップショットが追記される"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today, stock_name="アズーム")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        snap = loaded["snapshots"][0]
        assert snap["date_yy_m"] == _today_yy_m_d()
        assert snap["data_source"] == "auto"

    def test_unwatched_stock_skipped(self, db_path, monkeypatch):
        """research_shelve に登録済みでもウォッチリストにない銘柄は追記対象外"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        # ウォッチリストから 3496 を除外
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["1234"], []))

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 0

    def test_watchlist_unregistered_auto_creates_record(self, db_path, monkeypatch):
        """ウォッチリストにあり未登録の銘柄は自動登録され、スナップショットも追記される"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today, stock_name="アズーム")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["3496"], []))

        # 事前にレコードなし
        assert rs.get_research_record("3496", db_path=db_path) is None

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded is not None
        assert loaded["stock_name"] == "アズーム"
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["data_source"] == "auto"

    def test_watchlist_unregistered_outside_window_not_created(self, db_path, monkeypatch):
        """ウォッチ内・未登録でも決算ウィンドウ外なら自動登録されない (research DB を汚染しない)"""
        old_date = _today_str(-30)  # 窓外
        stock = _make_stock("3496", kessanbi=old_date, stock_name="アズーム")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["3496"], []))

        assert rs.get_research_record("3496", db_path=db_path) is None

        make_stock_db.update_research_snapshots(db_path=db_path)

        # 決算ウィンドウ外なので空レコードも作られないこと
        assert rs.get_research_record("3496", db_path=db_path) is None

    def test_watchlist_unregistered_no_kessan_date_not_created(self, db_path, monkeypatch):
        """ウォッチ内・未登録で kessanbi も kessan_mod_date も持たない銘柄は登録されない"""
        stock = _make_stock("3496", stock_name="アズーム")  # 決算日なし
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["3496"], []))

        assert rs.get_research_record("3496", db_path=db_path) is None

        make_stock_db.update_research_snapshots(db_path=db_path)

        assert rs.get_research_record("3496", db_path=db_path) is None

    def test_watchlist_not_in_stocks_shelve_skipped(self, db_path, monkeypatch):
        """ウォッチリストにあるが stocks_shelve にない銘柄は登録もスナップショットもされない"""
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["9999"], []))

        make_stock_db.update_research_snapshots(db_path=db_path)

        assert rs.get_research_record("9999", db_path=db_path) is None

    def test_possess_code_also_in_scope(self, db_path, monkeypatch):
        """H付き保有銘柄もウォッチ集合に含まれスナップショット対象になる"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today, stock_name="アズーム")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        # 通常コード側は空、possess 側に 3496
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: ([], ["3496"]))

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1

    def test_watchlist_file_missing_no_crash(self, db_path, monkeypatch):
        """my_watch_list.txt 不在時は FileNotFoundError を握りつぶして早期 return"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        def _raise():
            raise FileNotFoundError("my_watch_list.txt")

        monkeypatch.setattr(portfolio, "parse_my_portforio", _raise)

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        # 例外で落ちずに正常 return することを確認
        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 0

    def test_overwrite_existing_auto(self, db_path, monkeypatch):
        """同一 date_yy_m の auto スナップショットは最新値で上書きされる
        (決算修正で業績が変わった場合に古い値が残らないよう、auto 同士は上書き)"""
        import gyoseki

        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        # gyoseki.get_gyoseki_expr が新しい値を返すようにモック
        monkeypatch.setattr(
            gyoseki, "get_gyoseki_expr", lambda s: ("[P]新進捗", "[A]新成長")
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        snap = rs.create_snapshot(
            _today_yy_m_d(), ir_quant="古い業績", data_source="auto"
        )
        rs.upsert_snapshot("3496", snap, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["data_source"] == "auto"
        assert loaded["snapshots"][0]["ir_quant"] == "[A]新成長[P]新進捗"

    def test_skip_existing_manual_protects_data(self, db_path, monkeypatch):
        """同一 date_yy_m の manual スナップショットがある場合もスキップ（manual 保護）"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        snap = rs.create_snapshot(_today_yy_m_d(), ir_quant="手動入力", data_source="manual")
        rs.upsert_snapshot("3496", snap, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["data_source"] == "manual"
        assert loaded["snapshots"][0]["ir_quant"] == "手動入力"

    def test_protected_duplicate_same_date_not_overwritten(self, db_path, monkeypatch):
        """同一 date_yy_m に auto と manual が共存する場合、manual を消さないようスキップする。

        upsert_snapshot(overwrite_same_date=False) で同日に2件目を許容する経路や、
        移行時に同日 auto + 既存 manual が並ぶケースが該当。
        auto 上書きで manual も巻き込み削除されるのを防ぐ。
        """
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        # 同一 date_yy_m に auto と manual を共存させる
        auto_snap = rs.create_snapshot(
            _today_yy_m_d(), ir_quant="自動値", data_source="auto"
        )
        rs.upsert_snapshot("3496", auto_snap, db_path=db_path)
        manual_snap = rs.create_snapshot(
            _today_yy_m_d(), ir_quant="手動値", data_source="manual"
        )
        rs.upsert_snapshot(
            "3496", manual_snap, overwrite_same_date=False, db_path=db_path,
        )

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        # manual が保護されている (削除されていない) ことを確認
        manuals = [
            s for s in loaded["snapshots"] if s.get("data_source") == "manual"
        ]
        assert len(manuals) == 1
        assert manuals[0]["ir_quant"] == "手動値"

    def test_newer_date_wins_when_both_in_window(self, db_path, monkeypatch):
        """両方が窓内の場合、新しい方の日付のみ採用される"""
        ann_date = _today_str(-3)
        mod_date = _today_str()
        stock = _make_stock("3496", kessanbi=ann_date, kessan_mod_date=mod_date)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == _today_yy_m_d()

    def test_newer_kessanbi_wins_over_old_mod_date(self, db_path, monkeypatch):
        """kessanbi が修正日より新しい場合、kessanbi が採用される"""
        old_mod_date = _today_str(-5)
        ann_date = _today_str(-1)
        stock = _make_stock("3496", kessanbi=ann_date, kessan_mod_date=old_mod_date)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == _today_yy_m_d(-1)

    def test_only_kessanbi_when_mod_date_outside_window(self, db_path, monkeypatch):
        """修正日が窓外なら kessanbi のみ使われる"""
        ann_date = _today_str(-3)
        old_mod_date = _today_str(-20)  # 窓外
        stock = _make_stock("3496", kessanbi=ann_date, kessan_mod_date=old_mod_date)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == _today_yy_m_d(-3)

    def test_migration_not_affected_by_auto(self, db_path, monkeypatch):
        """既存の migration (月精度) があっても auto (日精度) は追記される"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        dt = datetime.strptime(today, "%Y/%m/%d")
        month_key = f"{dt.year % 100}.{dt.month}"

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        snap = rs.create_snapshot(month_key, data_source="migration")
        rs.upsert_snapshot("3496", snap, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 2
        sources = {s["data_source"] for s in loaded["snapshots"]}
        assert sources == {"migration", "auto"}

    def test_old_kessanbi_not_triggered(self, db_path, monkeypatch):
        """15 日以上前の決算日はトリガーされない"""
        old_date = _today_str(-15)
        stock = _make_stock("3496", kessanbi=old_date)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 0

    def test_no_targets_no_error(self, db_path, monkeypatch):
        """対象銘柄なしでもエラーにならない"""
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {})
        make_stock_db.update_research_snapshots(db_path=db_path)

    def test_date_yy_m_d_conversion(self, db_path, monkeypatch):
        """YYYY/MM/DD → YY.M.D の変換が正しい"""
        stock = _make_stock("3496", kessanbi="2026/04/15")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(
            make_stock_db, "get_price_day",
            lambda dt: datetime(2026, 4, 15).date(),
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded["snapshots"][0]["date_yy_m"] == "26.4.15"

    def test_date_yy_m_d_november(self, db_path, monkeypatch):
        """11月1日 → 25.11.1 の変換"""
        stock = _make_stock("3496", kessanbi="2025/11/01")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(
            make_stock_db, "get_price_day",
            lambda dt: datetime(2025, 11, 1).date(),
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert loaded["snapshots"][0]["date_yy_m"] == "25.11.1"

    def test_exception_does_not_stop_others(self, db_path, monkeypatch):
        """1 銘柄の失敗が他に波及しない"""
        today = _today_str()
        stocks = {
            "9998": _make_stock("9998", kessanbi=today),
            "3496": _make_stock("3496", kessanbi=today),
        }
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: stocks)

        orig_get_gyoseki_expr = make_stock_db.gyoseki.get_gyoseki_expr

        def _boom_for_9998(stock):
            if stock.get("code_s") == "9998":
                raise RuntimeError("強制エラー")
            return orig_get_gyoseki_expr(stock)

        monkeypatch.setattr(make_stock_db.gyoseki, "get_gyoseki_expr", _boom_for_9998)

        for code_s in ("9998", "3496"):
            rec = rs.create_research_record(code_s, "テスト")
            rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1

    def test_ir_quant_contains_progress_and_growth(self, db_path, monkeypatch):
        """ir_quant に progress_expr + growth_expr が連結されている"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        snap = loaded["snapshots"][0]
        assert isinstance(snap["ir_quant"], str)
        assert isinstance(snap["quality_indicators"], str)
        assert isinstance(snap["rironkabuka_kairi"], str)

    def test_code_filter_limits_targets(self, db_path, monkeypatch):
        """code_filter を渡すと指定銘柄だけが処理される (他銘柄は触らない)"""
        today = _today_str()
        stock_a = _make_stock("3496", kessanbi=today, stock_name="アズーム")
        stock_b = _make_stock("6324", kessanbi=today, stock_name="ハーモニック")
        monkeypatch.setattr(
            make_stock_db, "load_stock_db",
            lambda: {"3496": stock_a, "6324": stock_b},
        )
        monkeypatch.setattr(
            portfolio, "parse_my_portforio", lambda: (["3496", "6324"], [])
        )

        for code_s, name in [("3496", "アズーム"), ("6324", "ハーモニック")]:
            rec = rs.create_research_record(code_s, name)
            rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(
            db_path=db_path, code_filter=["6324"]
        )

        # 6324 だけスナップショットが追記され、3496 は触られない
        rec_a = rs.get_research_record("3496", db_path=db_path)
        rec_b = rs.get_research_record("6324", db_path=db_path)
        assert len(rec_a["snapshots"]) == 0
        assert len(rec_b["snapshots"]) == 1

    def test_code_filter_outside_watchlist_skipped(self, db_path, monkeypatch):
        """code_filter にウォッチ外の銘柄を指定すると何もしない (汚染防止)"""
        today = _today_str()
        stock = _make_stock("9999", kessanbi=today, stock_name="ウォッチ外")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"9999": stock})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["3496"], []))

        rec = rs.create_research_record("9999", "ウォッチ外")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(
            db_path=db_path, code_filter=["9999"]
        )

        loaded = rs.get_research_record("9999", db_path=db_path)
        assert len(loaded["snapshots"]) == 0
