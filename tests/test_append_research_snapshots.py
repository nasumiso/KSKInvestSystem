"""update_research_snapshots のテスト (issue #94)"""

import os
from datetime import datetime, timedelta

import pytest

import research_shelve as rs
import make_stock_db


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

    def test_unregistered_stock_skipped(self, db_path, monkeypatch):
        """research_shelve にレコードがない銘柄はスキップ"""
        today = _today_str()
        stock = _make_stock("9999", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"9999": stock})

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("9999", db_path=db_path)
        assert loaded is None

    def test_skip_existing_auto(self, db_path, monkeypatch):
        """同一 date_yy_m の auto スナップショットがある場合はスキップ"""
        today = _today_str()
        stock = _make_stock("3496", kessanbi=today)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)
        snap = rs.create_snapshot(_today_yy_m_d(), data_source="auto")
        rs.upsert_snapshot("3496", snap, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 1

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

    def test_kessanbi_and_mod_date_separate_snapshots(self, db_path, monkeypatch):
        """決算発表と決算修正が別日なら 2 件の別スナップショット"""
        ann_date = _today_str(-3)
        mod_date = _today_str()
        stock = _make_stock("3496", kessanbi=ann_date, kessan_mod_date=mod_date)
        monkeypatch.setattr(make_stock_db, "load_stock_db", lambda: {"3496": stock})

        rec = rs.create_research_record("3496", "アズーム")
        rs.upsert_research_record(rec, db_path=db_path)

        make_stock_db.update_research_snapshots(db_path=db_path)

        loaded = rs.get_research_record("3496", db_path=db_path)
        assert len(loaded["snapshots"]) == 2
        dates = {s["date_yy_m"] for s in loaded["snapshots"]}
        assert _today_yy_m_d(-3) in dates
        assert _today_yy_m_d() in dates

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
