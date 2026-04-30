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

    def test_backfills_missing_5d_from_price_log(
        self, populated_db, monkeypatch
    ):
        """過去エントリで 5d が欠損していれば price_log から補完される (issue #133)"""
        from datetime import date as _date, timedelta as _td
        kessan = _date(2026, 4, 1)
        # 決算日以下の終値 + 1d, 2d, ..., 6d 後の終値
        log = [(kessan - _td(days=1), 1000)]
        for i, pr in enumerate([1032, 1040, 1045, 1048, 1051], start=1):
            log.append((kessan + _td(days=i), pr))
        monkeypatch.setattr(
            helpers,
            "_bulk_price_logs",
            lambda codes: {"3496": log} if "3496" in codes else {},
        )
        # today を決算後に固定 (補完対象は dt < base_day のもののみ)
        monkeypatch.setattr(
            helpers, "get_price_day", lambda _: _date(2026, 4, 25)
        )
        # 1d だけ持つエントリを直接挿入 (5d は欠損)
        rec = rs.get_research_record("3496")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/04/01",
            "quarter": 4,
            "pre_expectation": "○",
            "pre_outlook": "テスト",
            "post_price_changes": {"1d": "+3.2", "5d": ""},
            "post_comment": "",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }]
        rs.upsert_research_record(rec)

        detail = helpers.get_research_detail("3496")
        entry = detail["kessan_comments"][0]
        assert entry["post_price_changes"]["1d"] == "+3.2"
        # 5d が補完されていること (1000 → 1051 = +5.1%)
        assert entry["post_price_changes"]["5d"] == "+5.1"

    def test_does_not_backfill_future_entries(
        self, populated_db, monkeypatch
    ):
        """未来の決算エントリ (dt >= base_day) は補完対象外"""
        from datetime import date as _date, timedelta as _td
        kessan = _date(2026, 5, 1)  # 未来
        log = [(kessan - _td(days=1), 1000), (kessan + _td(days=1), 1050)]
        monkeypatch.setattr(
            helpers,
            "_bulk_price_logs",
            lambda codes: {"3496": log},
        )
        monkeypatch.setattr(
            helpers, "get_price_day", lambda _: _date(2026, 4, 25)
        )
        rec = rs.get_research_record("3496")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/05/01",
            "quarter": 1,
            "pre_expectation": "",
            "pre_outlook": "",
            "post_price_changes": {"1d": "", "5d": ""},
            "post_comment": "",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }]
        rs.upsert_research_record(rec)

        detail = helpers.get_research_detail("3496")
        entry = detail["kessan_comments"][0]
        # 未来エントリなので補完されず空のまま
        assert entry["post_price_changes"] == {"1d": "", "5d": ""}


class TestGetMarketKessanData:
    """get_market_kessan_data の振り分けロジックテスト"""

    @pytest.fixture
    def kessan_env(self, populated_db, monkeypatch):
        """pf_kessan_shelve / portfolio / 価格ログ等を共通モック"""
        # 当日決算/過去/未来をテストごとに pf_dict で渡せるよう、
        # load_pf_kessan_db は外側から指定可能なヘルパとして組み立てる
        from datetime import datetime as _dt

        def setup(pf_dict, today_dt):
            # kessan.load_pf_kessan_db
            import kessan as _k
            monkeypatch.setattr(_k, "load_pf_kessan_db", lambda: pf_dict)
            # portfolio.parse_my_portforio (ウォッチ・保有なしで全銘柄通す)
            import portfolio as _p
            monkeypatch.setattr(
                _p, "parse_my_portforio", lambda: ([], [])
            )
            # 価格ログ (反応率計算で使われるが、当日扱いの検証では不要)
            monkeypatch.setattr(helpers, "_bulk_price_logs", lambda codes: {})
            # datetime.today() を固定
            class FrozenDateTime(_dt):
                @classmethod
                def today(cls):
                    return today_dt
                @classmethod
                def now(cls, tz=None):
                    return today_dt
            monkeypatch.setattr(helpers, "datetime", FrozenDateTime)
            # get_price_day も同じ datetime を参照するため、helpers 経由で暗黙にカバーされる
        return setup

    def test_today_kessan_goes_to_today_entries(self, kessan_env):
        """当日決算は today_entries に振り分けられる (recent_past でも future でもない)"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 10, 0)  # 4/27 朝
        pf_dict = {
            "6501": {
                "code_s": "6501",
                "stock_name": "日立製作所",
                "kessanbi": "2026/04/27",
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        today_keys = [k for k, _ in result["today_entries"]]
        past_keys = [k for k, _ in result["recent_past_entries"]]
        future_keys = [k for k, _ in result["future_entries"]]
        assert "2026/04/27" in today_keys
        assert "2026/04/27" not in past_keys
        assert "2026/04/27" not in future_keys

    def test_yesterday_kessan_in_recent_past(self, kessan_env):
        """前日決算は従来通り recent_past_entries"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 10, 0)
        pf_dict = {
            "6501": {
                "code_s": "6501",
                "stock_name": "日立製作所",
                "kessanbi": "2026/04/26",
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        past_keys = [k for k, _ in result["recent_past_entries"]]
        today_keys = [k for k, _ in result["today_entries"]]
        assert "2026/04/26" in past_keys
        assert "2026/04/26" not in today_keys

    def test_tomorrow_kessan_in_future(self, kessan_env):
        """翌日以降の決算は future_entries"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 10, 0)
        pf_dict = {
            "6501": {
                "code_s": "6501",
                "stock_name": "日立製作所",
                "kessanbi": "2026/04/28",
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        future_keys = [k for k, _ in result["future_entries"]]
        past_keys = [k for k, _ in result["recent_past_entries"]]
        today_keys = [k for k, _ in result["today_entries"]]
        assert "2026/04/28" in future_keys
        assert "2026/04/28" not in past_keys
        assert "2026/04/28" not in today_keys

    def test_today_kessan_morning_before_18(self, kessan_env):
        """18 時前 (base_day=前日) でも当日決算は today_entries に入る"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 9, 0)  # 9 時 = base_day は 4/26
        pf_dict = {
            "6501": {
                "code_s": "6501",
                "stock_name": "日立製作所",
                "kessanbi": "2026/04/27",
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        today_keys = [k for k, _ in result["today_entries"]]
        # 18 時前 (base_day=4/26) でも today_cal=4/27 が判定基準なので
        # 当日決算は today_entries に分類される
        assert "2026/04/27" in today_keys

    def test_today_kessan_includes_pts_in_post_price_changes(
        self, kessan_env, db_path
    ):
        """当日決算エントリの post_price_changes に PTS キーが含まれる (issue #154)"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 19, 0)
        pf_dict = {
            "6501": {
                "code_s": "6501",
                "stock_name": "日立製作所",
                "kessanbi": "2026/04/27",
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        # research_shelve に PTS 入りの kessan_comments を予め登録
        rec = rs.create_research_record("6501", "日立製作所")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/04/27",
            "quarter": 4,
            "pre_expectation": "",
            "pre_outlook": "",
            "post_price_changes": {"pts": "+2.5", "1d": "", "5d": ""},
            "post_comment": "",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        today_entries = result["today_entries"]
        # 4/27 のエントリがあり、その中の 6501 が PTS を持つ
        found = False
        for kessanbi, stocks in today_entries:
            if kessanbi != "2026/04/27":
                continue
            for s in stocks:
                if s["code_s"] == "6501":
                    assert s["post_price_changes"].get("pts") == "+2.5"
                    found = True
        assert found, "6501 のエントリが today_entries に見つからない"


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
        # price_log は未登録でOK
        # calc_price_reaction (旧API) と calc_price_reactions (新API) の両方を差し替え。
        # 実コードは calc_price_reactions を呼ぶが、互換ラッパ経由を含めて空に固定する。
        monkeypatch.setattr(helpers, "calc_price_reaction", lambda c, k: "")
        monkeypatch.setattr(
            helpers,
            "calc_price_reactions",
            lambda c, k: {key: "" for key, _ in helpers.KESSAN_REACTION_PERIODS},
        )
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


class TestPriceReactionFromLog:
    """_price_reaction_from_log の N営業日後計算 (issue #133)"""

    def _make_log(self, kessan_dt, before_pr, after_prs):
        """決算日以下の終値 + 決算日より後の営業日終値リストから price_log を作る"""
        from datetime import timedelta as _td
        log = [(kessan_dt - _td(days=1), before_pr)]
        for i, pr in enumerate(after_prs, start=1):
            log.append((kessan_dt + _td(days=i), pr))
        return log

    def test_returns_1d_change_when_n_is_1(self):
        from datetime import date
        kessan = date(2026, 4, 1)
        log = self._make_log(kessan, 1000, [1032])
        assert helpers._price_reaction_from_log(log, kessan, n_business_days=1) == "+3.2"

    def test_returns_5d_change_when_n_is_5(self):
        from datetime import date
        kessan = date(2026, 4, 1)
        # 1000 → 1d後 1032, ..., 5d後 1051 (=+5.1%)
        log = self._make_log(kessan, 1000, [1032, 1040, 1045, 1048, 1051])
        assert helpers._price_reaction_from_log(log, kessan, n_business_days=5) == "+5.1"

    def test_returns_empty_when_n5_not_enough_log(self):
        from datetime import date
        kessan = date(2026, 4, 1)
        # 後ろが4本しか無い → n=5 で取得不可
        log = self._make_log(kessan, 1000, [1010, 1020, 1030, 1040])
        assert helpers._price_reaction_from_log(log, kessan, n_business_days=5) == ""

    def test_returns_empty_when_no_before_price(self):
        from datetime import date, timedelta
        kessan = date(2026, 4, 1)
        # 決算日以下の log が無い (全部 future)
        log = [(kessan + timedelta(days=i), 1000 + i) for i in range(1, 6)]
        assert helpers._price_reaction_from_log(log, kessan, n_business_days=1) == ""

    def test_negative_change_format(self):
        from datetime import date
        kessan = date(2026, 4, 1)
        log = self._make_log(kessan, 1000, [985])
        assert helpers._price_reaction_from_log(log, kessan, n_business_days=1) == "-1.5"

    def test_invalid_n_returns_empty(self):
        from datetime import date
        kessan = date(2026, 4, 1)
        log = self._make_log(kessan, 1000, [1032])
        assert helpers._price_reaction_from_log(log, kessan, n_business_days=0) == ""


class TestCalcPriceReactions:
    """calc_price_reactions の dict 返却 (issue #133)"""

    def test_returns_dict_with_both_periods(self, monkeypatch):
        from datetime import date, timedelta
        kessan = date(2026, 4, 1)
        log = [(kessan - timedelta(days=1), 1000)]
        for i, pr in enumerate([1032, 1040, 1045, 1048, 1051], start=1):
            log.append((kessan + timedelta(days=i), pr))
        monkeypatch.setattr(helpers, "get_stock_data", lambda c: {"price_log": log})
        result = helpers.calc_price_reactions("5032", "2026/04/01")
        assert result == {"1d": "+3.2", "5d": "+5.1"}

    def test_invalid_kessanbi_returns_empty_dict(self):
        result = helpers.calc_price_reactions("5032", "invalid-date")
        assert result == {"1d": "", "5d": ""}

    def test_partial_log_returns_partial_dict(self, monkeypatch):
        from datetime import date, timedelta
        kessan = date(2026, 4, 1)
        # 後ろ1本しか無い → 1d は取れて 5d は ""
        log = [(kessan - timedelta(days=1), 1000), (kessan + timedelta(days=1), 1050)]
        monkeypatch.setattr(helpers, "get_stock_data", lambda c: {"price_log": log})
        result = helpers.calc_price_reactions("5032", "2026/04/01")
        assert result["1d"] == "+5.0"
        assert result["5d"] == ""


class TestNormalizePostPriceChanges:
    """normalize_kessan_post_price_changes の後方互換正規化 (issue #133)"""

    def test_new_format_passthrough(self):
        entry = {"post_price_changes": {"1d": "+3", "5d": "+5"}}
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "+3", "5d": "+5"}

    def test_old_format_lifts_to_1d(self):
        entry = {"post_price_change": "-15"}
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "-15", "5d": ""}

    def test_both_present_prefers_new(self):
        entry = {
            "post_price_change": "-15",
            "post_price_changes": {"1d": "+2", "5d": "+3"},
        }
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "+2", "5d": "+3"}

    def test_neither_present_returns_empty(self):
        result = rs.normalize_kessan_post_price_changes({})
        assert result == {"1d": "", "5d": ""}

    def test_partial_new_format_filled_with_empty(self):
        entry = {"post_price_changes": {"1d": "+3"}}  # 5d 欠落
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "+3", "5d": ""}


class TestSaveKessanCommentMultiPeriod:
    """save_kessan_comment の dict 化 + 期間別ガード (issue #133)"""

    @pytest.fixture
    def setup_db(self, db_path, monkeypatch):
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr(helpers, "RESEARCH_SHELVE", db_path, raising=False)
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rs.upsert_research_record(rec, db_path=db_path)
        from datetime import date as _date
        monkeypatch.setattr(helpers, "get_price_day", lambda _: _date(2026, 4, 25))
        monkeypatch.setattr(helpers, "_is_possess_now", lambda c: False)
        return db_path

    def _form(self, **overrides):
        base = {
            "kessanbi": "2026/03/11",
            "quarter": "3",
            "pre_expectation": "○",
            "pre_outlook": "事前",
            "post_comment": "",
        }
        base.update(overrides)
        return base

    def test_new_entry_saves_post_price_changes_dict(self, setup_db, monkeypatch):
        """新規保存時、post_price_changes が dict で保存され post_price_change は含まれない"""
        monkeypatch.setattr(
            helpers, "calc_price_reactions",
            lambda c, k: {"1d": "+3.2", "5d": "+5.1"},
        )
        entry = helpers.save_kessan_comment("5032", self._form())
        assert entry["post_price_changes"] == {"1d": "+3.2", "5d": "+5.1"}
        # 旧キーは新規エントリには含めない
        assert "post_price_change" not in entry

    def test_overwrite_keeps_existing_5d_when_new_calc_fails(self, setup_db, monkeypatch):
        """既存に 5d=+5.1 がある状態で再計算が 5d="" を返したら、5d は既存値を保持"""
        # フェーズ1: 両期間取れる
        monkeypatch.setattr(
            helpers, "calc_price_reactions",
            lambda c, k: {"1d": "+3.2", "5d": "+5.1"},
        )
        helpers.save_kessan_comment("5032", self._form())
        # フェーズ2: 5d だけ取れず "" になる
        monkeypatch.setattr(
            helpers, "calc_price_reactions",
            lambda c, k: {"1d": "+4.0", "5d": ""},
        )
        entry = helpers.save_kessan_comment("5032", self._form())
        assert entry["post_price_changes"]["1d"] == "+4.0"
        # 既存の +5.1 が保持されている
        assert entry["post_price_changes"]["5d"] == "+5.1"

    def test_overwrite_legacy_record_lifts_1d_value(self, setup_db, monkeypatch):
        """既存が旧 post_price_change のみ持つレコードを上書きする時、1d 既存値が保持される"""
        # 旧形式のみのレコードを直接挿入
        rec = rs.get_research_record("5032")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/03/11",
                "quarter": 3,
                "pre_expectation": "○",
                "pre_outlook": "init",
                "post_price_change": "-7.5",  # 旧形式のみ
                "post_comment": "",
                "kessan_matagi": False,
                "held_before_kessan": False,
                "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec)
        # 新計算が両方 "" を返す → 既存 1d="-7.5" が保持されるはず
        monkeypatch.setattr(
            helpers, "calc_price_reactions",
            lambda c, k: {"1d": "", "5d": ""},
        )
        entry = helpers.save_kessan_comment("5032", self._form(pre_outlook="updated"))
        assert entry["post_price_changes"]["1d"] == "-7.5"
        assert entry["post_price_changes"]["5d"] == ""

    def test_overwrite_preserves_pts_key(self, setup_db, monkeypatch):
        """既存エントリに PTS キーがある状態で save_kessan_comment しても消えない (issue #154)"""
        # 既存に PTS 入りで 1 件
        rec = rs.get_research_record("5032")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/03/11",
                "quarter": 3,
                "pre_expectation": "○",
                "pre_outlook": "init",
                "post_price_changes": {"pts": "+2.5", "1d": "+3.2", "5d": ""},
                "post_comment": "",
                "kessan_matagi": False,
                "held_before_kessan": False,
                "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec)
        # webapp で再保存 (calc_price_reactions は 1d/5d だけ返す)
        monkeypatch.setattr(
            helpers, "calc_price_reactions",
            lambda c, k: {"1d": "+4.0", "5d": "+5.1"},
        )
        entry = helpers.save_kessan_comment("5032", self._form(pre_outlook="updated"))
        # 1d/5d は新値で更新、PTS キーは既存値を維持
        assert entry["post_price_changes"]["1d"] == "+4.0"
        assert entry["post_price_changes"]["5d"] == "+5.1"
        assert entry["post_price_changes"]["pts"] == "+2.5"


class TestUpsertKessanPtsChange:
    """upsert_kessan_pts_change のテスト (issue #154)"""

    @pytest.fixture
    def setup_db(self, db_path, monkeypatch):
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr(helpers, "RESEARCH_SHELVE", db_path, raising=False)
        return db_path

    def test_creates_new_entry_when_no_kessan_comment(self, setup_db, monkeypatch):
        """既存 kessan_comments が空の銘柄に PTS を upsert → 新規エントリ作成"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rs.upsert_research_record(rec)
        entry = helpers.upsert_kessan_pts_change("5032", "2026/04/30", 4, "+2.5")
        assert entry["kessanbi"] == "2026/04/30"
        assert entry["quarter"] == 4
        assert entry["post_price_changes"] == {"pts": "+2.5"}
        # 永続化されていること
        loaded = rs.get_research_record("5032")
        assert loaded["kessan_comments"][0]["post_price_changes"]["pts"] == "+2.5"

    def test_updates_existing_entry_pts_only(self, setup_db, monkeypatch):
        """既存 (kessanbi, quarter) エントリの PTS のみ更新、他キーは保持"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/04/30",
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
        rs.upsert_research_record(rec)
        entry = helpers.upsert_kessan_pts_change("5032", "2026/04/30", 4, "-1.8")
        assert entry["post_price_changes"]["pts"] == "-1.8"
        # 他フィールドは保持
        assert entry["pre_expectation"] == "◎"
        assert entry["pre_outlook"] == "強気"

    def test_creates_record_when_research_record_missing(
        self, setup_db, monkeypatch
    ):
        """research_shelve に未登録 → add_stock 経由で先行登録される"""
        # add_stock の中身は stocks_shelve に依存するためモック
        called = {"add_stock": 0}

        def fake_add_stock(code_s):
            # add_stock が呼ばれたら、空レコードを直接作る
            r = rs.create_research_record(code_s, "TEST")
            rs.upsert_research_record(r)
            called["add_stock"] += 1
            return code_s

        monkeypatch.setattr(helpers, "add_stock", fake_add_stock)
        entry = helpers.upsert_kessan_pts_change("5032", "2026/04/30", 4, "+2.5")
        assert called["add_stock"] == 1
        assert entry["post_price_changes"]["pts"] == "+2.5"

    def test_rejects_invalid_kessanbi(self, setup_db):
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rs.upsert_research_record(rec)
        with pytest.raises(ValueError):
            helpers.upsert_kessan_pts_change("5032", "2026-04-30", 4, "+2.5")

    def test_separate_quarter_creates_new_entry(self, setup_db):
        """同じ kessanbi でも quarter が違えば別エントリ"""
        rec = rs.create_research_record("5032", "ANYCOLOR")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/04/30",
                "quarter": 4,
                "pre_expectation": "",
                "pre_outlook": "",
                "post_price_changes": {"pts": "+2.5"},
                "post_comment": "",
                "kessan_matagi": False,
                "held_before_kessan": False,
                "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec)
        helpers.upsert_kessan_pts_change("5032", "2026/04/30", 1, "+5.0")
        loaded = rs.get_research_record("5032")
        # 2 エントリある (q=4 と q=1)
        assert len(loaded["kessan_comments"]) == 2
        quarters = sorted(int(e["quarter"]) for e in loaded["kessan_comments"])
        assert quarters == [1, 4]
