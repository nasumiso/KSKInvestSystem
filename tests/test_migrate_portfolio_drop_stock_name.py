"""migrate_portfolio_drop_stock_name.py のテスト"""

import pytest

import portfolio_shelve as ps
import migrate_portfolio_drop_stock_name as m


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_portfolio_shelve")


@pytest.fixture
def db_with_legacy_records(db_path):
    """旧スキーマ (stock_name 付き) と新スキーマが混在した shelve を用意"""
    # 新スキーマ (stock_name なし)
    rec_new = ps.create_record("4377", status="3監")
    ps.upsert_record(rec_new, db_path=db_path)

    # 旧スキーマ風 (stock_name あり) を直接 upsert
    rec_legacy = ps.create_record("7089", status="1保")
    rec_legacy["stock_name"] = "フォースタートアップス"  # 旧データ模擬
    ps.upsert_record(rec_legacy, db_path=db_path)

    rec_legacy2 = ps.create_record("5032", status="3監")
    rec_legacy2["stock_name"] = "AnyColor"
    ps.upsert_record(rec_legacy2, db_path=db_path)

    return db_path


class TestDropStockName:

    def test_dry_run_does_not_modify(self, db_with_legacy_records):
        result = m.drop_stock_name_field(dry_run=True, db_path=db_with_legacy_records)
        assert result["total"] == 3
        assert result["cleaned"] == 2  # 7089, 5032
        assert result["skipped"] == 1  # 4377

        # shelve は変化なし
        rec = ps.get_record("7089", db_path=db_with_legacy_records)
        assert "stock_name" in rec  # 旧スキーマのまま

    def test_real_run_drops_stock_name(self, db_with_legacy_records):
        result = m.drop_stock_name_field(db_path=db_with_legacy_records)
        assert result["total"] == 3
        assert result["cleaned"] == 2
        assert result["skipped"] == 1

        for code in ("4377", "7089", "5032"):
            rec = ps.get_record(code, db_path=db_with_legacy_records)
            assert "stock_name" not in rec, f"{code} に stock_name が残っている"

    def test_other_fields_preserved(self, db_with_legacy_records):
        m.drop_stock_name_field(db_path=db_with_legacy_records)
        rec = ps.get_record("7089", db_path=db_with_legacy_records)
        assert rec["status"] == "1保"
        assert rec["code_s"] == "7089"
        assert "memo" in rec
        assert rec["registered_at"]

    def test_idempotent(self, db_with_legacy_records):
        """2 回実行しても問題なし"""
        m.drop_stock_name_field(db_path=db_with_legacy_records)
        result = m.drop_stock_name_field(db_path=db_with_legacy_records)
        assert result["cleaned"] == 0  # 2 回目はクリーン対象なし
        assert result["skipped"] == 3

    def test_empty_db(self, db_path):
        """レコードが 0 件でもエラーにならない"""
        result = m.drop_stock_name_field(db_path=db_path)
        assert result == {"total": 0, "cleaned": 0, "skipped": 0}
