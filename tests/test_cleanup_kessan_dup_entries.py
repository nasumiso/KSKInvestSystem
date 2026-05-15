"""cleanup_kessan_dup_entries.py のテスト (issue #207)."""

import os
import sys

import pytest

# scripts/ を import path に追加
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "scripts")
)

import research_shelve as rs
import cleanup_kessan_dup_entries as cleanup


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_research_shelve")


@pytest.fixture
def setup_db(db_path, monkeypatch):
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
    return db_path


def _make_memo_entry(kessanbi="2026/05/12", quarter=4, **overrides):
    e = {
        "kessanbi": kessanbi, "quarter": quarter,
        "pre_expectation": "◎", "pre_outlook": "強気",
        "post_comment": "[B] 好決算", "post_price_change": "",
        "post_price_changes": {},
        "kessan_matagi": False,
        "held_before_kessan": False, "held_after_kessan": False,
    }
    e.update(overrides)
    return e


def _make_pts_only_entry(kessanbi="2026/05/12", quarter=0, pts="+11.82"):
    return {
        "kessanbi": kessanbi, "quarter": quarter,
        "pre_expectation": "", "pre_outlook": "",
        "post_comment": "", "post_price_change": "",
        "post_price_changes": {"pts": pts},
        "kessan_matagi": False,
        "held_before_kessan": False, "held_after_kessan": False,
    }


# ===========================================
# 純関数テスト (DB 不要)
# ===========================================

class TestIsEmptyPtsOnlyEntry:
    def test_empty_pts_only_returns_true(self):
        e = _make_pts_only_entry()
        assert cleanup._is_empty_pts_only_entry(e) is True

    def test_with_memo_returns_false(self):
        e = _make_memo_entry()
        assert cleanup._is_empty_pts_only_entry(e) is False

    def test_with_held_flag_returns_false(self):
        e = _make_pts_only_entry()
        e["held_before_kessan"] = True
        assert cleanup._is_empty_pts_only_entry(e) is False

    def test_with_legacy_post_price_change_returns_false(self):
        """旧形式 post_price_change (str) があれば削除しない (codex P1 防御)"""
        e = _make_pts_only_entry()
        e["post_price_change"] = "+3.2"
        assert cleanup._is_empty_pts_only_entry(e) is False

    def test_with_1d_5d_value_returns_false(self):
        """post_price_changes に pts 以外のキーで非空値があれば削除しない (codex P1 防御)"""
        e = _make_pts_only_entry()
        e["post_price_changes"] = {"pts": "+1.0", "1d": "+0.5"}
        assert cleanup._is_empty_pts_only_entry(e) is False

    def test_with_empty_1d_5d_keys_returns_true(self):
        """1d/5d キーが空文字なら無視され削除候補になる (実データに空キーが入る経路がある)"""
        e = _make_pts_only_entry()
        e["post_price_changes"] = {"pts": "+11.82", "1d": "", "5d": ""}
        assert cleanup._is_empty_pts_only_entry(e) is True


class TestSelectWinnerAndDups:
    def test_no_duplicates(self):
        """1 件のみ → winner=None, dups=[]"""
        winner, dups = cleanup.select_winner_and_dups([_make_memo_entry()])
        assert winner is None
        assert dups == []

    def test_memo_plus_pts_only(self):
        """memo (q=4) + pts-only (q=0) → memo が winner、pts-only が dups"""
        memo = _make_memo_entry(quarter=4)
        pts_only = _make_pts_only_entry(quarter=0)
        winner, dups = cleanup.select_winner_and_dups([memo, pts_only])
        assert winner is memo
        assert len(dups) == 1
        assert dups[0] is pts_only

    def test_two_memos_no_dups(self):
        """memo 2 件 (異なる quarter) → 削除候補なし"""
        m1 = _make_memo_entry(quarter=2, pre_outlook="Q2")
        m2 = _make_memo_entry(quarter=4, pre_outlook="Q4")
        winner, dups = cleanup.select_winner_and_dups([m1, m2])
        assert winner is None
        assert dups == []

    def test_all_pts_only_keeps_largest_quarter(self):
        """pts-only が複数 → quarter 大を残し、残りを dups"""
        p0 = _make_pts_only_entry(quarter=0, pts="+1.0")
        p4 = _make_pts_only_entry(quarter=4, pts="+2.0")
        winner, dups = cleanup.select_winner_and_dups([p0, p4])
        assert winner is p4
        assert dups == [p0]


class TestMergePtsIntoWinner:
    def test_winner_has_no_pts_takes_from_dup(self):
        winner = _make_memo_entry(quarter=4)
        winner["post_price_changes"] = {}
        dup = _make_pts_only_entry(quarter=0, pts="+11.82")
        merged = cleanup.merge_pts_into_winner(winner, [dup])
        assert merged == "+11.82"
        assert winner["post_price_changes"]["pts"] == "+11.82"

    def test_winner_has_pts_keeps_winner(self):
        winner = _make_memo_entry(quarter=4)
        winner["post_price_changes"] = {"pts": "+5.0"}
        dup = _make_pts_only_entry(quarter=0, pts="+11.82")
        merged = cleanup.merge_pts_into_winner(winner, [dup])
        assert merged == "+5.0"
        assert winner["post_price_changes"]["pts"] == "+5.0"


# ===========================================
# 統合テスト (DB あり)
# ===========================================

class TestCleanupDb:
    def test_dry_run_does_not_write(self, setup_db):
        rec = rs.create_research_record("7717", "ブイ・テクノロジー")
        rec["kessan_comments"] = [
            _make_memo_entry(quarter=4),
            _make_pts_only_entry(quarter=0, pts="+11.82"),
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        summary = cleanup.cleanup_db(db_path=setup_db, dry_run=True)
        assert summary["modified"] == 1
        assert summary["deleted_entries"] == 1

        loaded = rs.get_research_record("7717", db_path=setup_db)
        # dry-run なので変更されていない
        assert len(loaded["kessan_comments"]) == 2

    def test_apply_consolidates_7717_case(self, setup_db, monkeypatch):
        """7717 想定: q=0 空 + q=4 メモ → 1 件 (q=4) に統合、PTS マージ"""
        # backup_research_db を no-op に置換 (テスト DB ではバックアップ不要)
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "ブイ・テクノロジー")
        rec["kessan_comments"] = [
            _make_memo_entry(quarter=4),
            _make_pts_only_entry(quarter=0, pts="+11.82"),
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        summary = cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        assert summary["modified"] == 1
        assert summary["deleted_entries"] == 1

        loaded = rs.get_research_record("7717", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 1
        e = loaded["kessan_comments"][0]
        assert int(e["quarter"]) == 4
        assert e["pre_outlook"] == "強気"
        assert e["post_comment"] == "[B] 好決算"
        assert e["post_price_changes"]["pts"] == "+11.82"

    def test_preserves_unrelated_dates(self, setup_db, monkeypatch):
        """同銘柄の別 kessanbi は触らない"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [
            _make_memo_entry(kessanbi="2025/02/14", quarter=2),
            _make_memo_entry(kessanbi="2026/05/12", quarter=4),
            _make_pts_only_entry(kessanbi="2026/05/12", quarter=0, pts="+11.82"),
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        loaded = rs.get_research_record("7717", db_path=setup_db)
        # 2025/02/14 の memo はそのまま残る、2026/05/12 は 1 件に統合
        assert len(loaded["kessan_comments"]) == 2
        kessanbis = sorted(e["kessanbi"] for e in loaded["kessan_comments"])
        assert kessanbis == ["2025/02/14", "2026/05/12"]

    def test_skips_single_entry(self, setup_db, monkeypatch):
        """重複なしのレコードは触らない"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [_make_memo_entry()]
        rs.upsert_research_record(rec, db_path=setup_db)

        summary = cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        assert summary["modified"] == 0
        assert summary["deleted_entries"] == 0

    def test_three_entries_with_two_memos(self, setup_db, monkeypatch):
        """memo 2 件 (q=2, q=4) + pts-only 1 件 → memo 2 件保持、pts-only のみ削除"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [
            _make_memo_entry(quarter=2, pre_outlook="Q2 メモ"),
            _make_memo_entry(quarter=4, pre_outlook="Q4 メモ"),
            _make_pts_only_entry(quarter=0, pts="+11.82"),
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        loaded = rs.get_research_record("7717", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 2
        quarters = sorted(int(e["quarter"]) for e in loaded["kessan_comments"])
        assert quarters == [2, 4]
        # winner = memo + quarter 大 = q=4 に PTS がマージされている
        for e in loaded["kessan_comments"]:
            if int(e["quarter"]) == 4:
                assert e["post_price_changes"].get("pts") == "+11.82"
            elif int(e["quarter"]) == 2:
                # q=2 は触られない
                assert e["pre_outlook"] == "Q2 メモ"

    def test_apply_keeps_winner_pts_when_both_have_pts(self, setup_db, monkeypatch):
        """winner 側に既に pts があれば優先 (上書きされない)"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "TEST")
        memo = _make_memo_entry(quarter=4)
        memo["post_price_changes"] = {"pts": "+5.0"}
        rec["kessan_comments"] = [
            memo,
            _make_pts_only_entry(quarter=0, pts="+11.82"),
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        loaded = rs.get_research_record("7717", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 1
        # winner の pts 値が保持される
        assert loaded["kessan_comments"][0]["post_price_changes"]["pts"] == "+5.0"

    def test_skips_entry_with_non_pts_price_changes(self, setup_db, monkeypatch):
        """削除候補が 1d/5d に非空値を持っていたら削除しない (codex P1 防御)"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "TEST")
        suspicious = _make_pts_only_entry(quarter=0, pts="+11.82")
        suspicious["post_price_changes"] = {"pts": "+11.82", "1d": "+0.5"}
        rec["kessan_comments"] = [
            _make_memo_entry(quarter=4),
            suspicious,
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        loaded = rs.get_research_record("7717", db_path=setup_db)
        # 1d=+0.5 を持つエントリは削除されない
        assert len(loaded["kessan_comments"]) == 2

    def test_consolidates_when_1d_5d_keys_are_empty(self, setup_db, monkeypatch):
        """7717 実データ模擬: 1d/5d キーがあっても空文字なら統合される"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "TEST")
        # 実データ模擬: ppc に 1d/5d キーが空文字で入っている
        suspicious = _make_pts_only_entry(quarter=0, pts="+11.82")
        suspicious["post_price_changes"] = {"pts": "+11.82", "1d": "", "5d": ""}
        rec["kessan_comments"] = [
            _make_memo_entry(quarter=4),
            suspicious,
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        loaded = rs.get_research_record("7717", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 1
        assert loaded["kessan_comments"][0]["post_price_changes"]["pts"] == "+11.82"

    def test_skips_entry_with_post_price_change_legacy(self, setup_db, monkeypatch):
        """旧形式 post_price_change (str) があれば削除しない (codex P1 防御)"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        rec = rs.create_research_record("7717", "TEST")
        suspicious = _make_pts_only_entry(quarter=0, pts="+11.82")
        suspicious["post_price_change"] = "+3.2"  # 旧形式
        rec["kessan_comments"] = [
            _make_memo_entry(quarter=4),
            suspicious,
        ]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        loaded = rs.get_research_record("7717", db_path=setup_db)
        assert len(loaded["kessan_comments"]) == 2

    def test_target_codes_filter(self, setup_db, monkeypatch):
        """target_codes 指定で対象銘柄のみ処理"""
        monkeypatch.setattr(rs, "backup_research_db", lambda **kw: [])

        for code in ("7717", "5032"):
            rec = rs.create_research_record(code, f"TEST_{code}")
            rec["kessan_comments"] = [
                _make_memo_entry(quarter=4),
                _make_pts_only_entry(quarter=0, pts="+1.0"),
            ]
            rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(
            db_path=setup_db, dry_run=False, target_codes=["7717"]
        )
        # 7717 は統合
        loaded_7717 = rs.get_research_record("7717", db_path=setup_db)
        assert len(loaded_7717["kessan_comments"]) == 1
        # 5032 は触らない
        loaded_5032 = rs.get_research_record("5032", db_path=setup_db)
        assert len(loaded_5032["kessan_comments"]) == 2

    def test_backup_called_on_apply(self, setup_db, monkeypatch):
        """--apply (dry_run=False) で backup_research_db が呼ばれる"""
        called = {"backup": 0}

        def fake_backup(**kw):
            called["backup"] += 1
            return ["fake.bak"]

        monkeypatch.setattr(rs, "backup_research_db", fake_backup)
        # backup を呼ぶためには cleanup_kessan_dup_entries モジュールが見る rs を差し替える
        monkeypatch.setattr(cleanup.rs, "backup_research_db", fake_backup)

        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [_make_memo_entry()]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=False)
        assert called["backup"] == 1

    def test_dry_run_does_not_call_backup(self, setup_db, monkeypatch):
        """--dry-run では backup_research_db を呼ばない"""
        called = {"backup": 0}

        def fake_backup(**kw):
            called["backup"] += 1
            return []

        monkeypatch.setattr(cleanup.rs, "backup_research_db", fake_backup)

        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [_make_memo_entry()]
        rs.upsert_research_record(rec, db_path=setup_db)

        cleanup.cleanup_db(db_path=setup_db, dry_run=True)
        assert called["backup"] == 0
