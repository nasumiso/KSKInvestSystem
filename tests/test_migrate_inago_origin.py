"""migrate_inago_origin_to_research.py のテスト。

イナゴ元を portfolio から research へ移す移行の、冪等性と未登録銘柄の扱いを検証する。
"""

import pytest

import migrate_inago_origin_to_research as mig
import research_shelve as rs


@pytest.fixture
def research_db(tmp_path, monkeypatch):
    path = str(tmp_path / "test_research_shelve")
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", path)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", path)
    return path


def _record(code_s, origin):
    return {"code_s": code_s, "memo": {"inago_origin": origin}}


def test_migration_creates_copies_and_preserves_existing(research_db):
    """未登録は作成、空欄は上書き、既存値は温存し、空文字は対象外。"""
    rs.upsert_research_record(
        rs.create_research_record("4377", "ワンキャリア"), db_path=research_db
    )
    rs.upsert_research_record(
        rs.create_research_record("5032", "AnyColor", inago_origin="先に入れた値"),
        db_path=research_db,
    )
    records = [
        _record("4377", "ゆーさく"),   # research あり・空欄 -> copy
        _record("5032", "がっしー"),   # research あり・既存値 -> skip
        _record("7089", "ケイ"),       # research 無し -> create+copy
        _record("6232", ""),           # 空欄 -> 対象外
    ]

    plans = mig.collect_migrations(records)
    assert {p["code_s"]: p["action"] for p in plans} == {
        "4377": "copy", "5032": "skip(既存値)", "7089": "create+copy",
    }

    assert mig.apply_migrations(plans) == 2
    assert rs.get_research_record("4377", db_path=research_db)["inago_origin"] == "ゆーさく"
    assert rs.get_research_record("5032", db_path=research_db)["inago_origin"] == "先に入れた値"
    assert rs.get_research_record("7089", db_path=research_db)["inago_origin"] == "ケイ"

    # 再実行しても既に入った値は上書きしない (冪等)
    assert mig.apply_migrations(mig.collect_migrations(records)) == 0
