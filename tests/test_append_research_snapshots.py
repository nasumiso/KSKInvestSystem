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


def _make_stock(
    code_s, kessanbi="", kessan_mod_date="", kessan_jisseki_date="",
    stock_name="テスト銘柄",
):
    """最小限の stock dict を生成する"""
    return {
        "code_s": code_s,
        "stock_name": stock_name,
        "kessanbi": kessanbi,
        "kessan_mod_date": kessan_mod_date,
        "kessan_jisseki_date": kessan_jisseki_date,
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

    @pytest.mark.parametrize(
        "jisseki_delta, mod_delta, expected_deltas",
        [
            # 発表日・修正日が両方窓内 → 別行で2件 (発表行+修正行)
            (-3, 0, [-3, 0]),
            (-1, -5, [-1, -5]),
            # 発表日==修正日 (同日) → 1件に集約
            (-2, -2, [-2]),
            # 修正日が窓外 → 発表行のみ1件
            (-3, -20, [-3]),
        ],
    )
    def test_announce_and_mod_dates_become_separate_rows(
        self, db_path, monkeypatch, jisseki_delta, mod_delta, expected_deltas,
    ):
        """発表日と修正日が両方窓内なら別行で残す。同日は集約、窓外は除外。

        各 auto 行の acquired_date は取得日 (本日) になる。
        """
        stock = _make_stock(
            "3496",
            kessan_jisseki_date=_today_str(jisseki_delta),
            kessan_mod_date=_today_str(mod_delta),
        )
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        snaps = loaded["snapshots"]
        assert len(snaps) == len(expected_deltas)
        actual_dates = {s["date_yy_m"] for s in snaps}
        assert actual_dates == {_today_yy_m_d(d) for d in expected_deltas}
        # 株価依存指標の取得日は本日
        assert all(s["acquired_date"] == _today_yy_m_d() for s in snaps)

    def test_kessanbi_fallback_when_jisseki_unset(self, db_path, monkeypatch):
        """kessan_jisseki_date 未設定なら kessanbi を候補にする"""
        stock = _make_stock("3496", kessanbi=_today_str(-3))  # jisseki 未設定
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == _today_yy_m_d(-3)

    def test_kessanbi_in_window_not_dropped_by_old_jisseki(self, db_path, monkeypatch):
        """前回 jisseki が窓外でも、今回決算日 kessanbi が窓内なら拾う (PR#312 codex P2)。

        master 更新が shintakane.update_todays_kessan より先に走り、
        kessan_jisseki_date が前回分 (窓外) のまま kessanbi だけ今回決算日 (窓内)
        になるケースで、今回決算の auto スナップショットを取りこぼさないこと。
        """
        stock = _make_stock(
            "3496",
            kessan_jisseki_date=_today_str(-90),  # 前回決算 (窓外)
            kessanbi=_today_str(-2),              # 今回決算日 (窓内)
        )
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        dates = {s["date_yy_m"] for s in loaded["snapshots"]}
        assert _today_yy_m_d(-2) in dates  # kessanbi (今回決算) が拾われる

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

    def test_force_reuses_latest_kessan_event_date(self, db_path, monkeypatch):
        """force 再取得でも取得日ではなく直近決算イベント日を上書きする"""
        stock = _make_stock(
            "3496",
            kessan_jisseki_date=_today_str(-5),
            stock_name="アズーム",
        )
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["3496"], []))

        rec = rs.create_research_record("3496", "アズーム")
        old = rs.create_snapshot(
            _today_yy_m_d(-5),
            acquired_date=_today_yy_m_d(-5),
            ir_quant="old",
            data_source="auto",
        )
        rs.upsert_research_record(rec, db_path=db_path)
        rs.upsert_snapshot("3496", old, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path, code_filter=["3496"], force=True)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == _today_yy_m_d(-5)
        assert loaded["snapshots"][0]["acquired_date"] == _today_yy_m_d()
        assert loaded["snapshots"][0]["ir_quant"] != "old"

    def test_force_falls_back_to_existing_snapshot_date_when_kessan_dates_missing(self, db_path, monkeypatch):
        """実績日/修正日が無くても既存最新 snapshot 日を使って force 更新する"""
        stock = _make_stock("3496", stock_name="アズーム")
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})
        monkeypatch.setattr(portfolio, "parse_my_portforio", lambda: (["3496"], []))

        rec = rs.create_research_record("3496", "アズーム")
        old = rs.create_snapshot(
            "26.4.15",
            acquired_date="26.4.15",
            ir_quant="old",
            data_source="auto",
        )
        rs.upsert_research_record(rec, db_path=db_path)
        rs.upsert_snapshot("3496", old, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path, code_filter=["3496"], force=True)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1
        assert loaded["snapshots"][0]["date_yy_m"] == "26.4.15"
        assert loaded["snapshots"][0]["acquired_date"] == _today_yy_m_d()
        assert loaded["snapshots"][0]["ir_quant"] != "old"


class TestUpdatePtsReactions:
    """update_pts_reactions のユニットテスト (issue #154)"""

    @pytest.fixture
    def setup_db(self, db_path, monkeypatch):
        """research_shelve のパスを差し替え"""
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        from webapp import helpers as _helpers
        monkeypatch.setattr(_helpers, "RESEARCH_SHELVE", db_path, raising=False)
        return db_path

    def test_writes_pts_for_today_kessan_in_watch_set(self, setup_db, monkeypatch):
        """watch_set ∩ 当日決算 ∩ PTS データありの銘柄に PTS が書き込まれる"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        today_str = today.strftime("%Y/%m/%d")

        stock = _make_stock("3496", kessanbi=today_str, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {"3496": "+2.5"},
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=setup_db)

        make_stock_db.update_pts_reactions({"3496"}, today, stocks=stocks)

        loaded = rs.get_research_record("3496", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 1
        entry = loaded["kessan_comments"][0]
        assert entry["kessanbi"] == today_str
        assert entry["quarter"] == 4
        assert entry["post_price_changes"]["pts"] == "+2.5"

    def test_skips_when_kessanbi_is_not_today(self, setup_db, monkeypatch):
        """kessanbi != today の銘柄には PTS を書き込まない"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        yesterday = _today_str(-1)

        stock = _make_stock("3496", kessanbi=yesterday, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {"3496": "+2.5"},
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=setup_db)

        make_stock_db.update_pts_reactions({"3496"}, today, stocks=stocks)

        loaded = rs.get_research_record("3496", db_path=setup_db)
        assert loaded["kessan_comments"] == []

    def test_skips_when_not_in_watch_set(self, setup_db, monkeypatch):
        """watch_set 外の銘柄は PTS が書き込まれない (research DB 汚染防止)"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        today_str = today.strftime("%Y/%m/%d")

        stock = _make_stock("3496", kessanbi=today_str, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {"3496": "+2.5"},
        )

        # 事前レコードなし。watch_set にも含めない
        make_stock_db.update_pts_reactions({"9999"}, today, stocks=stocks)

        # 自動登録もされない (research DB 汚染なし)
        assert rs.get_research_record("3496", db_path=setup_db) is None

    def test_skips_when_pts_csv_missing(self, setup_db, monkeypatch):
        """PTS CSV 不在 (空 dict) ならスキップ、書き込みなし"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        today_str = today.strftime("%Y/%m/%d")

        stock = _make_stock("3496", kessanbi=today_str, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        # 空 dict (CSV 不在の擬似)
        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {},
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=setup_db)

        make_stock_db.update_pts_reactions({"3496"}, today, stocks=stocks)

        loaded = rs.get_research_record("3496", db_path=setup_db)
        assert loaded["kessan_comments"] == []

    def test_skips_when_pts_dict_lacks_code(self, setup_db, monkeypatch):
        """PTS dict に当該銘柄のエントリが無ければスキップ"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        today_str = today.strftime("%Y/%m/%d")

        stock = _make_stock("3496", kessanbi=today_str, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {"7203": "+0.5"},  # 別銘柄しかない
        )

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=setup_db)

        make_stock_db.update_pts_reactions({"3496"}, today, stocks=stocks)

        loaded = rs.get_research_record("3496", db_path=setup_db)
        assert loaded["kessan_comments"] == []

    def test_creates_kessan_entry_when_missing(self, setup_db, monkeypatch):
        """kessan_comments が空でも PTS 用に新規エントリが作られる"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        today_str = today.strftime("%Y/%m/%d")

        stock = _make_stock("3496", kessanbi=today_str, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {"3496": "+1.2"},
        )

        # research_shelve は登録済みだが kessan_comments は空
        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=setup_db)

        make_stock_db.update_pts_reactions({"3496"}, today, stocks=stocks)

        loaded = rs.get_research_record("3496", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 1
        entry = loaded["kessan_comments"][0]
        assert entry["post_price_changes"]["pts"] == "+1.2"

    def test_updates_pts_only_on_existing_entry(self, setup_db, monkeypatch):
        """既存 (kessanbi, quarter) エントリの PTS のみ更新、他キーは保持"""
        from datetime import datetime as _dt
        today = _dt.today().date()
        today_str = today.strftime("%Y/%m/%d")

        stock = _make_stock("3496", kessanbi=today_str, stock_name="アズーム")
        stock["kessan_quarter"] = 4
        stocks = {"3496": stock}

        monkeypatch.setattr(
            "pts_data.load_pts_changes_for_date",
            lambda d: {"3496": "+1.2"},
        )

        rec = rs.create_research_record("3496", "アズーム")
        rec["kessan_comments"] = [
            {
                "kessanbi": today_str,
                "quarter": 4,
                "pre_expectation": "◎",
                "pre_outlook": "強気",
                "post_price_changes": {"1d": "", "5d": ""},
                "post_comment": "",
                "kessan_matagi": False,
                "held_before_kessan": False,
                "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        make_stock_db.update_pts_reactions({"3496"}, today, stocks=stocks)

        loaded = rs.get_research_record("3496", db_path=setup_db)
        entry = loaded["kessan_comments"][0]
        assert entry["post_price_changes"]["pts"] == "+1.2"
        # 他フィールドは保持
        assert entry["pre_expectation"] == "◎"
        assert entry["pre_outlook"] == "強気"


class TestRefreshPtsReactions:
    """refresh_pts_reactions の薄い統合テスト

    shintakane.get_todays_pts / update_research_snapshots / update_pts_reactions
    の 3 つを順に呼び、watch_set と today_date を正しく引き継ぐことだけ確認する。
    """

    def test_呼び出し順序とwatch_setの引き継ぎ(self, monkeypatch):
        import shintakane

        calls = []

        def fake_get_todays_pts(force=False):
            calls.append(("get_todays_pts", force))

        def fake_update_research_snapshots():
            calls.append(("update_research_snapshots",))
            return {"3496", "6324"}

        def fake_update_pts_reactions(watch_set, today_date, *, stocks=None):
            calls.append(("update_pts_reactions", watch_set, today_date))

        monkeypatch.setattr(shintakane, "get_todays_pts", fake_get_todays_pts)
        monkeypatch.setattr(
            make_stock_db, "update_research_snapshots", fake_update_research_snapshots
        )
        monkeypatch.setattr(
            make_stock_db, "update_pts_reactions", fake_update_pts_reactions
        )

        make_stock_db.refresh_pts_reactions()

        # 順序: get_todays_pts → update_research_snapshots → update_pts_reactions
        assert [c[0] for c in calls] == [
            "get_todays_pts",
            "update_research_snapshots",
            "update_pts_reactions",
        ]
        # force=True で最新取得
        assert calls[0][1] is True
        # watch_set が update_pts_reactions に引き継がれる
        assert calls[2][1] == {"3496", "6324"}

    def test_watch_setが空でも例外を出さない(self, monkeypatch):
        """update_research_snapshots が空集合を返しても update_pts_reactions は呼ばれる
        (呼ばれた中で warning スキップする既存挙動を維持)"""
        import shintakane

        called_pts = []

        monkeypatch.setattr(shintakane, "get_todays_pts", lambda force=False: None)
        monkeypatch.setattr(
            make_stock_db, "update_research_snapshots", lambda: set()
        )
        monkeypatch.setattr(
            make_stock_db,
            "update_pts_reactions",
            lambda watch_set, today_date, **kw: called_pts.append(watch_set),
        )

        make_stock_db.refresh_pts_reactions()

        assert called_pts == [set()]
