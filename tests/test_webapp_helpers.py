"""webapp/helpers.py のテスト (tmp_path で一時DBを作成)"""

import os

import pytest

import research_shelve as rs
from webapp import helpers


@pytest.fixture
def db_path(tmp_path):
    """テスト用一時DBパスを返す"""
    return str(tmp_path / "test_research_shelve")


@pytest.fixture
def populated_db(db_path, monkeypatch):
    """テストデータ入りDBを準備し、helpers のDB参照先を差し替える"""
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)

    # テストデータ投入
    rec = rs.create_research_record(
        "3496", "アズーム",
        overall_rating="A",
        memo="テストメモ",
        overview="駐車場サブリース",
        institutional_comment="成長性高い",
        openwork="3.72",
        cramer="Buy推奨",
        shikiho_comments=["最高益", "新規事業"],
        analysis_date_raw="11/13",
        kessan_date_raw="01/30",
    )
    rs.upsert_research_record(rec, db_path=db_path)

    snap1 = rs.create_snapshot("26.4", ir_quant="[A]28%", ir_comment="好調", data_source="auto")
    snap2 = rs.create_snapshot("26.1", ir_quant="[A]26%", ir_comment="順調", data_source="auto")
    rs.upsert_snapshot("3496", snap1, db_path=db_path)
    rs.upsert_snapshot("3496", snap2, db_path=db_path)

    return db_path


class TestGetResearchDetail:
    """get_research_detail のテスト"""

    def test_existing_record(self, populated_db):
        rec = helpers.get_research_detail("3496")
        assert rec is not None
        assert rec["code_s"] == "3496"
        assert rec["stock_name"] == "アズーム"

    def test_nonexistent_record(self, populated_db):
        rec = helpers.get_research_detail("9999")
        assert rec is None


class TestSearchRecords:
    """search_records のテスト"""

    def test_no_filter(self, populated_db):
        results = helpers.search_records()
        assert len(results) == 1
        assert results[0]["code_s"] == "3496"

    def test_rating_filter(self, populated_db):
        results = helpers.search_records(rating="A")
        assert len(results) == 1

        results = helpers.search_records(rating="S")
        assert len(results) == 0

    def test_keyword_filter(self, populated_db):
        results = helpers.search_records(keyword="駐車場")
        assert len(results) == 1

        results = helpers.search_records(keyword="存在しない")
        assert len(results) == 0


class TestSaveMemo:
    """save_memo のテスト"""

    def test_save_memo_updates_fields(self, populated_db):
        form = {
            "overall_rating": "S",
            "institutional_comment": "更新コメント",
            "memo": "更新メモ",
            "openwork": "4.0",
            "cramer": "Strong Buy",
        }
        helpers.save_memo("3496", form)

        rec = helpers.get_research_detail("3496")
        assert rec["overall_rating"] == "S"
        assert rec["memo"] == "更新メモ"
        assert rec["openwork"] == "4.0"
        assert rec["cramer"] == "Strong Buy"
        assert rec["institutional_comment"] == "更新コメント"

    def test_save_memo_preserves_other_fields(self, populated_db):
        form = {
            "overall_rating": "B",
            "institutional_comment": "",
            "memo": "",
            "openwork": "",
            "cramer": "",
        }
        helpers.save_memo("3496", form)

        rec = helpers.get_research_detail("3496")
        # memo以外のフィールドが保持されている
        assert rec["stock_name"] == "アズーム"
        assert rec["overview"] == "駐車場サブリース"
        assert len(rec["snapshots"]) == 2


class TestSaveShikiho:
    """save_shikiho のテスト"""

    def test_save_shikiho_updates_fields(self, populated_db):
        form = {
            "overview": "更新概要",
            "shikiho_comments_0": "コメント1",
            "shikiho_periods_0": "26.3",
            "shikiho_comments_1": "コメント2",
            "shikiho_periods_1": "25.12",
            "shikiho_comments_2": "コメント3",
            "shikiho_periods_2": "25.9",
        }
        helpers.save_shikiho("3496", form)

        rec = helpers.get_research_detail("3496")
        assert rec["overview"] == "更新概要"
        assert rec["shikiho_comments"] == [
            {"period": "26.3", "comment": "コメント1"},
            {"period": "25.12", "comment": "コメント2"},
            {"period": "25.9", "comment": "コメント3"},
        ]

    def test_save_shikiho_empty_comments_skipped(self, populated_db):
        form = {
            "overview": "概要",
            "shikiho_comments_0": "有効",
            "shikiho_periods_0": "26.3",
            "shikiho_comments_1": "  ",  # 空白のみ → スキップ
            "shikiho_periods_1": "25.12",
            "shikiho_comments_2": "有効2",
            "shikiho_periods_2": "25.9",
        }
        helpers.save_shikiho("3496", form)

        rec = helpers.get_research_detail("3496")
        assert rec["shikiho_comments"] == [
            {"period": "26.3", "comment": "有効"},
            {"period": "25.9", "comment": "有効2"},
        ]


class TestSaveIrComments:
    """save_ir_comments のテスト"""

    def test_save_ir_comments_updates(self, populated_db):
        form = {
            "ir_comment_26.4": "更新コメント26.4",
            "ir_comment_26.1": "更新コメント26.1",
        }
        helpers.save_ir_comments("3496", form)

        rec = helpers.get_research_detail("3496")
        snaps = rec["snapshots"]
        # 降順ソートなので 26.4 が先頭
        assert snaps[0]["ir_comment"] == "更新コメント26.4"
        assert snaps[1]["ir_comment"] == "更新コメント26.1"

    def test_save_ir_comments_partial(self, populated_db):
        """一部のスナップショットのみ更新"""
        form = {"ir_comment_26.4": "26.4のみ更新"}
        helpers.save_ir_comments("3496", form)

        rec = helpers.get_research_detail("3496")
        snaps = rec["snapshots"]
        assert snaps[0]["ir_comment"] == "26.4のみ更新"
        assert snaps[1]["ir_comment"] == "順調"  # 未変更


class TestHasRecentDisclosure:
    """has_recent_disclosure のテスト"""

    @staticmethod
    def _today():
        from datetime import datetime
        from ks_util import get_price_day
        return get_price_day(datetime.today())

    def test_empty_list(self):
        assert helpers.has_recent_disclosure([]) is False

    def test_within_7days(self):
        today = self._today()
        date_str = today.strftime("%m/%d")
        disclosures = [(date_str, "決算", "タイトル", "http://example.com")]
        assert helpers.has_recent_disclosure(disclosures, days=7) is True

    def test_exactly_7days_ago(self):
        from datetime import timedelta
        d = self._today() - timedelta(days=7)
        disclosures = [(d.strftime("%m/%d"), "決算", "タイトル", "http://example.com")]
        assert helpers.has_recent_disclosure(disclosures, days=7) is True

    def test_older_than_7days(self):
        from datetime import timedelta
        d = self._today() - timedelta(days=10)
        disclosures = [(d.strftime("%m/%d"), "決算", "タイトル", "http://example.com")]
        assert helpers.has_recent_disclosure(disclosures, days=7) is False

    def test_mixed_recent_and_old(self):
        from datetime import timedelta
        today = self._today()
        old = today - timedelta(days=20)
        recent = today - timedelta(days=3)
        disclosures = [
            (old.strftime("%m/%d"), "決算", "古い", "http://example.com"),
            (recent.strftime("%m/%d"), "決算", "新しい", "http://example.com"),
        ]
        assert helpers.has_recent_disclosure(disclosures, days=7) is True

    def test_invalid_date_skipped(self):
        disclosures = [("invalid", "決算", "タイトル", "http://example.com")]
        assert helpers.has_recent_disclosure(disclosures, days=7) is False

    def test_year_boundary(self, monkeypatch):
        """年跨ぎ: 今日より未来の MM/DD は前年扱い"""
        import webapp.helpers as h
        from datetime import date
        # 今日を 2026/1/3 に固定
        class _FakeDatetime:
            @staticmethod
            def today():
                class _D:
                    def __init__(self, y, m, d):
                        self._d = date(y, m, d)
                    def date(self):
                        return self._d
                return _D(2026, 1, 3)
        monkeypatch.setattr(h, "datetime", _FakeDatetime)
        monkeypatch.setattr(h, "get_price_day", lambda _: date(2026, 1, 3))
        # 12/28 は昨年扱いで6日前 → True
        disclosures = [("12/28", "決算", "昨年末", "http://example.com")]
        assert helpers.has_recent_disclosure(disclosures, days=7) is True


class TestSaveKessanCommentMatagi:
    """save_kessan_comment の held_before/after + kessan_matagi AND 判定 (issue #138)"""

    @pytest.fixture
    def setup_db(self, db_path, monkeypatch):
        """RESEARCH_SHELVE を tmp_path に差し替えて初期レコードを 1 件入れる"""
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr(helpers, "RESEARCH_SHELVE", db_path, raising=False)
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rs.upsert_research_record(rec, db_path=db_path)
        # price_log は未登録でOK (calc_price_reaction は空文字を返す)
        monkeypatch.setattr(helpers, "calc_price_reaction", lambda c, k: "")
        return db_path

    @pytest.fixture
    def today_2026_03_15(self, monkeypatch):
        """テスト用に get_price_day を 2026/3/15 に固定"""
        from datetime import date as _date
        monkeypatch.setattr(helpers, "get_price_day", lambda _: _date(2026, 3, 15))

    def _form(self, **overrides):
        base = {
            "kessanbi": "2026/03/11",  # today より前 → 決算後
            "quarter": "3",
            "pre_expectation": "○",
            "pre_outlook": "事前",
            "post_comment": "",
        }
        base.update(overrides)
        return base

    def test_past_entry_with_possess_sets_held_after_only(
        self, setup_db, today_2026_03_15, monkeypatch
    ):
        """過去の決算 + 現在保有 → held_after_kessan のみ True (before は False のまま)"""
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: True)
        entry = helpers.save_kessan_comment("5032", self._form())
        assert entry["held_before_kessan"] is False
        assert entry["held_after_kessan"] is True
        # AND 判定なので kessan_matagi は False
        assert entry["kessan_matagi"] is False

    def test_future_entry_with_possess_sets_held_before_only(
        self, setup_db, today_2026_03_15, monkeypatch
    ):
        """未来の決算 + 現在保有 → held_before_kessan のみ True"""
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: True)
        form = self._form(kessanbi="2026/04/20")
        entry = helpers.save_kessan_comment("5032", form)
        assert entry["held_before_kessan"] is True
        assert entry["held_after_kessan"] is False
        assert entry["kessan_matagi"] is False

    def test_new_entry_without_possess_sets_both_false(
        self, setup_db, today_2026_03_15, monkeypatch
    ):
        """新規作成時、保有してなければ held フラグは両方 False"""
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: False)
        entry = helpers.save_kessan_comment("5032", self._form())
        assert entry["held_before_kessan"] is False
        assert entry["held_after_kessan"] is False
        assert entry["kessan_matagi"] is False

    def test_two_phase_save_flips_matagi_by_and(
        self, setup_db, today_2026_03_15, monkeypatch
    ):
        """
        決算前に保有で保存 → held_before=True、
        時間を進めて決算後に保有で再保存 → held_after=True で AND で kessan_matagi=True
        """
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: True)
        # フェーズ1: today=2026/3/15, kessanbi=2026/4/20 (未来)
        from datetime import date as _date
        monkeypatch.setattr(helpers, "get_price_day", lambda _: _date(2026, 3, 15))
        form = self._form(kessanbi="2026/04/20")
        helpers.save_kessan_comment("5032", form)

        # フェーズ2: 時計進行、today=2026/4/25 で同じ kessanbi は過去に変わる
        monkeypatch.setattr(helpers, "get_price_day", lambda _: _date(2026, 4, 25))
        entry = helpers.save_kessan_comment("5032", form)
        assert entry["held_before_kessan"] is True
        assert entry["held_after_kessan"] is True
        assert entry["kessan_matagi"] is True

    def test_existing_true_is_not_downgraded(
        self, setup_db, today_2026_03_15, monkeypatch
    ):
        """既存 held_after=True / kessan_matagi=True は現在非保有でも下げない"""
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: True)
        # 1回目: 保有中で held_after を立てる
        helpers.save_kessan_comment("5032", self._form(pre_outlook="init"))
        # matagi を手動で True にしておく (二相統合の代用)
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: False)
        entry2 = helpers.save_kessan_comment(
            "5032", self._form(pre_outlook="update"),
        )
        # held_after が False に下がっていないこと
        assert entry2["held_after_kessan"] is True

    def test_form_override_wins(self, setup_db, today_2026_03_15, monkeypatch):
        """form 明示の kessan_matagi が最優先"""
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: True)
        form = self._form(kessan_matagi="1")
        entry = helpers.save_kessan_comment("5032", form)
        # held_after しか True にならないが、form override で kessan_matagi=True
        assert entry["kessan_matagi"] is True


class TestPersistKessanHeldFlags:
    """_persist_kessan_held_flags の挙動 (並行書き込み安全対応)"""

    @pytest.fixture
    def setup_db(self, db_path, monkeypatch):
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/03/11",
                "quarter": 3,
                "pre_expectation": "○",
                "pre_outlook": "既存見通し",
                "post_price_change": "-15",
                "post_comment": "[E] -15% x",
                "kessan_matagi": False,
                "held_before_kessan": False,
                "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec, db_path=db_path)
        return db_path

    def test_promotes_held_after_only(self, setup_db):
        """held_after=True のみ指定すれば後側だけが True 化、matagi は False"""
        helpers._persist_kessan_held_flags([
            ("5032", "2026/03/11", 3, {"held_after_kessan": True}),
        ])
        loaded = rs.get_research_record("5032")
        entry = loaded["kessan_comments"][0]
        assert entry["held_after_kessan"] is True
        assert entry["held_before_kessan"] is False
        assert entry["kessan_matagi"] is False
        # 他フィールドは温存
        assert entry["pre_outlook"] == "既存見通し"

    def test_and_promotion_activates_matagi(self, setup_db):
        """held_before=True と held_after=True が両方立てば kessan_matagi も True"""
        # 先に held_before を立て、
        helpers._persist_kessan_held_flags([
            ("5032", "2026/03/11", 3, {"held_before_kessan": True}),
        ])
        # 次に held_after を立てる → AND で kessan_matagi=True
        helpers._persist_kessan_held_flags([
            ("5032", "2026/03/11", 3, {"held_after_kessan": True}),
        ])
        loaded = rs.get_research_record("5032")
        entry = loaded["kessan_comments"][0]
        assert entry["held_before_kessan"] is True
        assert entry["held_after_kessan"] is True
        assert entry["kessan_matagi"] is True

    def test_noop_when_no_match(self, setup_db):
        """未マッチのターゲットは黙ってスキップ"""
        helpers._persist_kessan_held_flags([
            ("5032", "2099/01/01", 1, {"held_after_kessan": True}),
        ])
        loaded = rs.get_research_record("5032")
        assert loaded["kessan_comments"][0]["held_after_kessan"] is False
        assert len(loaded["kessan_comments"]) == 1
