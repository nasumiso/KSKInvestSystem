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
        """base_day 当日の決算は today_entries に振り分けられる (recent_past でも future でもない)"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 19, 0)  # 17時以降 → base_day=4/27
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
        """base_day より前の決算は recent_past_entries"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 19, 0)  # 17時以降 → base_day=4/27
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
        """base_day より後の決算は future_entries"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 19, 0)  # 17時以降 → base_day=4/27
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

    def test_kessan_before_17_uses_previous_day_as_today(self, kessan_env):
        """17時前は base_day=前日。前日決算カードを当日扱い (today_entries) にする"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 9, 0)  # 9時 = base_day は 4/26
        pf_dict = {
            "6501": {
                "code_s": "6501",
                "stock_name": "日立製作所",
                "kessanbi": "2026/04/26",  # base_day 当日に相当
                "kessan_quarter": 4,
            },
            "9984": {
                "code_s": "9984",
                "stock_name": "ソフトバンクG",
                "kessanbi": "2026/04/27",  # カレンダー上の今日だが base_day より未来
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        today_keys = [k for k, _ in result["today_entries"]]
        future_keys = [k for k, _ in result["future_entries"]]
        # 17時前は base_day=4/26 が「今日」扱い
        assert "2026/04/26" in today_keys
        # カレンダー上の今日 (4/27) は base_day より未来なので future
        assert "2026/04/27" in future_keys

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


# ==================================================
# 条件付き書式 (issue #177)
# ==================================================
from datetime import date  # noqa: E402

C = helpers.PORTFOLIO_COLORS  # 短縮エイリアス
TODAY = date(2026, 5, 10)    # テスト用固定基準日


class TestComputeCellStyles:
    """compute_cell_styles のユニットテスト (issue #177)"""

    # --- 順位 (ルール 14, 31) ---
    def test_rank_lt_300_strong_yellow(self):
        styles = helpers.compute_cell_styles({"rank": 299}, today=TODAY)
        assert styles["rank"] == f"background:{C['濃黄']}"

    def test_rank_300_no_color(self):
        styles = helpers.compute_cell_styles({"rank": 300}, today=TODAY)
        assert "rank" not in styles

    def test_rank_none_no_color(self):
        styles = helpers.compute_cell_styles({"rank": None}, today=TODAY)
        assert "rank" not in styles

    # --- 売上成長 (ルール 17) ---
    def test_sales_growth_30_or_more_light_yellow(self):
        styles = helpers.compute_cell_styles({"sales_growth_raw": 30}, today=TODAY)
        assert styles["sales_growth"] == f"background:{C['薄黄']}"

    def test_sales_growth_29_no_color(self):
        styles = helpers.compute_cell_styles({"sales_growth_raw": 29}, today=TODAY)
        assert "sales_growth" not in styles

    def test_sales_growth_none_no_color(self):
        styles = helpers.compute_cell_styles({"sales_growth_raw": None}, today=TODAY)
        assert "sales_growth" not in styles

    # --- 利益成長 (ルール 17) ---
    def test_profit_growth_30_or_more_light_yellow(self):
        styles = helpers.compute_cell_styles({"profit_growth_raw": 30}, today=TODAY)
        assert styles["profit_growth"] == f"background:{C['薄黄']}"

    def test_profit_growth_29_no_color(self):
        styles = helpers.compute_cell_styles({"profit_growth_raw": 29}, today=TODAY)
        assert "profit_growth" not in styles

    # --- PER (ルール 16): PEG 的指標 (利益成長% + 配当%) / PER > 1 ---
    def test_per_peg_above_1_light_yellow(self):
        # (30 + 1) / 20 = 1.55 > 1
        styles = helpers.compute_cell_styles(
            {"per_raw": 20, "profit_growth_raw": 30, "dividend_raw": 1.0},
            today=TODAY,
        )
        assert styles["per"] == f"background:{C['薄黄']}"

    def test_per_peg_exact_1_no_color(self):
        # (20 + 0) / 20 = 1.0 (>= ではなく > なので no color)
        styles = helpers.compute_cell_styles(
            {"per_raw": 20, "profit_growth_raw": 20, "dividend_raw": 0.0},
            today=TODAY,
        )
        assert "per" not in styles

    def test_per_zero_no_color(self):
        styles = helpers.compute_cell_styles(
            {"per_raw": 0, "profit_growth_raw": 30, "dividend_raw": 1.0},
            today=TODAY,
        )
        assert "per" not in styles

    def test_per_with_no_growth_no_color(self):
        styles = helpers.compute_cell_styles(
            {"per_raw": 20, "profit_growth_raw": None, "dividend_raw": 1.0},
            today=TODAY,
        )
        assert "per" not in styles

    def test_per_with_no_dividend_treated_as_zero(self):
        # スプシ式 (I+L)/J>1 はセル空欄を 0 扱いする → 配当 None でも PER 色付け対象
        # 例: 6366 千代田化工建設 (per=3.3, profit_growth=232, dividend=None) → (232+0)/3.3=70 > 1
        styles = helpers.compute_cell_styles(
            {"per_raw": 3.3, "profit_growth_raw": 232, "dividend_raw": None},
            today=TODAY,
        )
        assert styles["per"] == f"background:{C['薄黄']}"

    def test_per_no_dividend_low_growth_no_color(self):
        # 配当 None (= 0) で利益成長が PER 未満 → 色なし
        styles = helpers.compute_cell_styles(
            {"per_raw": 30, "profit_growth_raw": 25, "dividend_raw": None},
            today=TODAY,
        )
        assert "per" not in styles

    # --- 理論株価乖離 (ルール 15): > 50 → 薄黄 ---
    def test_theoretical_diff_51_light_yellow(self):
        styles = helpers.compute_cell_styles({"theoretical_diff_raw": 51}, today=TODAY)
        assert styles["theoretical_diff"] == f"background:{C['薄黄']}"

    def test_theoretical_diff_50_no_color(self):
        styles = helpers.compute_cell_styles({"theoretical_diff_raw": 50}, today=TODAY)
        assert "theoretical_diff" not in styles

    # --- 配当 (ルール 32, 33): >=5 濃黄 / >3 薄黄 ---
    def test_dividend_5_strong_yellow(self):
        styles = helpers.compute_cell_styles({"dividend_raw": 5.0}, today=TODAY)
        assert styles["dividend"] == f"background:{C['濃黄']}"

    def test_dividend_4_light_yellow(self):
        styles = helpers.compute_cell_styles({"dividend_raw": 4.0}, today=TODAY)
        assert styles["dividend"] == f"background:{C['薄黄']}"

    def test_dividend_3_no_color(self):
        styles = helpers.compute_cell_styles({"dividend_raw": 3.0}, today=TODAY)
        assert "dividend" not in styles

    def test_dividend_above_3_strict(self):
        # 3.01 → 薄黄 (3 ちょうどではない)
        styles = helpers.compute_cell_styles({"dividend_raw": 3.01}, today=TODAY)
        assert styles["dividend"] == f"background:{C['薄黄']}"

    # --- 進捗率乖離 (ルール 9, 10): <C3>タグ 薄赤 / 営利乖離≧20 濃黄 ---
    def test_progress_diff_c3_tag_light_red(self):
        styles = helpers.compute_cell_styles(
            {"gyoseki_quarity_expr": "[A]20%,30%[Q]15%,25%<C3>"},
            today=TODAY,
        )
        assert styles["progress_diff"] == f"background:{C['薄赤']}"

    def test_progress_diff_eiri_20_strong_yellow(self):
        styles = helpers.compute_cell_styles(
            {"progress_diff_eiri_raw": 20, "gyoseki_quarity_expr": ""},
            today=TODAY,
        )
        assert styles["progress_diff"] == f"background:{C['濃黄']}"

    def test_progress_diff_c3_takes_priority_over_eiri(self):
        # 両方マッチしても <C3> 薄赤が優先
        styles = helpers.compute_cell_styles(
            {"progress_diff_eiri_raw": 30, "gyoseki_quarity_expr": "...<C3>"},
            today=TODAY,
        )
        assert styles["progress_diff"] == f"background:{C['薄赤']}"

    def test_progress_diff_eiri_19_no_color(self):
        styles = helpers.compute_cell_styles(
            {"progress_diff_eiri_raw": 19, "gyoseki_quarity_expr": ""},
            today=TODAY,
        )
        assert "progress_diff" not in styles

    # --- 決算日 (ルール 22, 23): 更新日±1ヶ月+3Q 濃黄 / ±1ヶ月のみ 薄黄 ---
    def test_kessanbi_within_month_3q_strong_yellow(self):
        styles = helpers.compute_cell_styles(
            {
                "kessanbi_raw": date(2026, 5, 14),
                "memo": {"last_research_update": "4/26"},
                "quarter": "3Q",
            },
            today=TODAY,
        )
        assert styles["kessanbi_md"] == f"background:{C['濃黄']}"

    def test_kessanbi_within_month_not_3q_light_yellow(self):
        styles = helpers.compute_cell_styles(
            {
                "kessanbi_raw": date(2026, 5, 14),
                "memo": {"last_research_update": "4/26"},
                "quarter": "1Q",
            },
            today=TODAY,
        )
        assert styles["kessanbi_md"] == f"background:{C['薄黄']}"

    def test_kessanbi_before_update_within_month_light_yellow(self):
        # 決算日が更新日より前でも ±1ヶ月以内なら色付け (codex P2 対応: 絶対日数差判定)
        # 例: today=6/15、更新日 5/20、決算日 5/10 (更新日の 10 日前) → 薄黄
        styles = helpers.compute_cell_styles(
            {
                "kessanbi_raw": date(2026, 5, 10),
                "memo": {"last_research_update": "5/20"},
                "quarter": "1Q",
            },
            today=date(2026, 6, 15),
        )
        assert styles["kessanbi_md"] == f"background:{C['薄黄']}"

    def test_kessanbi_outside_month_no_color(self):
        styles = helpers.compute_cell_styles(
            {
                "kessanbi_raw": date(2026, 6, 30),  # 4/26 + 1ヶ月超
                "memo": {"last_research_update": "4/26"},
                "quarter": "3Q",
            },
            today=TODAY,
        )
        assert "kessanbi_md" not in styles

    def test_kessanbi_before_update_outside_month_no_color(self):
        # 決算日が更新日より前で 1 ヶ月超なら色なし
        # 例: today=6/15、更新日 5/20、決算日 4/10 (更新日の 40 日前) → 色なし
        styles = helpers.compute_cell_styles(
            {
                "kessanbi_raw": date(2026, 4, 10),
                "memo": {"last_research_update": "5/20"},
                "quarter": "1Q",
            },
            today=date(2026, 6, 15),
        )
        assert "kessanbi_md" not in styles

    def test_kessanbi_no_data_no_color(self):
        styles = helpers.compute_cell_styles(
            {"kessanbi_raw": None, "memo": {"last_research_update": "4/26"}},
            today=TODAY,
        )
        assert "kessanbi_md" not in styles

    # --- 更新日 (ルール 1, 8): 14日以上前 薄灰 / 30日以上前 濃灰 ---
    def test_last_research_update_14_days_ago_light_gray(self):
        # TODAY = 2026/5/10、14日前 = 4/26
        styles = helpers.compute_cell_styles(
            {"memo": {"last_research_update": "4/26"}},
            today=TODAY,
        )
        assert styles["last_research_update"] == f"background:{C['薄灰']}"

    def test_last_research_update_30_days_ago_dark_gray(self):
        # TODAY = 2026/5/10、30日前 = 4/10
        styles = helpers.compute_cell_styles(
            {"memo": {"last_research_update": "4/10"}},
            today=TODAY,
        )
        assert styles["last_research_update"] == f"background:{C['濃灰']}"

    def test_last_research_update_recent_no_color(self):
        # TODAY = 2026/5/10、5日前 = 5/5
        styles = helpers.compute_cell_styles(
            {"memo": {"last_research_update": "5/5"}},
            today=TODAY,
        )
        assert "last_research_update" not in styles

    def test_last_research_update_future_md_treated_as_last_year(self):
        # TODAY = 2026/5/10、"6/1" は未来 → 2025/6/1 扱い (約11ヶ月前) → 濃灰
        styles = helpers.compute_cell_styles(
            {"memo": {"last_research_update": "6/1"}},
            today=TODAY,
        )
        assert styles["last_research_update"] == f"background:{C['濃灰']}"

    def test_last_research_update_dash_no_color(self):
        styles = helpers.compute_cell_styles(
            {"memo": {"last_research_update": "—"}},
            today=TODAY,
        )
        assert "last_research_update" not in styles

    # --- ステージ (ルール 13): "2S" 含む → 薄赤 ---
    def test_stage_2s_light_red(self):
        styles = helpers.compute_cell_styles(
            {"memo": {"stage": "2S(3T)"}},
            today=TODAY,
        )
        assert styles["stage"] == f"background:{C['薄赤']}"

    def test_stage_3s_no_color(self):
        styles = helpers.compute_cell_styles(
            {"memo": {"stage": "3S"}},
            today=TODAY,
        )
        assert "stage" not in styles

    # --- RS (ルール 27, 28): >80 濃黄 / >=70 薄黄 ---
    def test_rs_above_80_strong_yellow(self):
        styles = helpers.compute_cell_styles({"rs_raw": 81}, today=TODAY)
        assert styles["rs"] == f"background:{C['濃黄']}"

    def test_rs_80_light_yellow(self):
        # 80 ちょうどは ">80" にマッチしない、">=70" にマッチ
        styles = helpers.compute_cell_styles({"rs_raw": 80}, today=TODAY)
        assert styles["rs"] == f"background:{C['薄黄']}"

    def test_rs_70_light_yellow(self):
        styles = helpers.compute_cell_styles({"rs_raw": 70}, today=TODAY)
        assert styles["rs"] == f"background:{C['薄黄']}"

    def test_rs_69_no_color(self):
        styles = helpers.compute_cell_styles({"rs_raw": 69}, today=TODAY)
        assert "rs" not in styles

    # --- トレンド (ルール 24, 25, 26): "◎" 濃黄 / "◯" 薄黄 / 空欄 水色 ---
    def test_trend_circle_double_strong_yellow(self):
        styles = helpers.compute_cell_styles({"trend_template": "◎pr>ma10"}, today=TODAY)
        assert styles["trend_template"] == f"background:{C['濃黄']}"

    def test_trend_circle_light_yellow(self):
        styles = helpers.compute_cell_styles({"trend_template": "◯RS"}, today=TODAY)
        assert styles["trend_template"] == f"background:{C['薄黄']}"

    def test_trend_dash_water_blue(self):
        styles = helpers.compute_cell_styles({"trend_template": "—"}, today=TODAY)
        assert styles["trend_template"] == f"background:{C['水色']}"

    def test_trend_empty_water_blue(self):
        styles = helpers.compute_cell_styles({"trend_template": ""}, today=TODAY)
        assert styles["trend_template"] == f"background:{C['水色']}"

    def test_trend_double_takes_priority_over_single(self):
        # "◎◯" → ◎ 優先 (実運用ではこうはならないが、評価順を担保)
        styles = helpers.compute_cell_styles({"trend_template": "◎◯"}, today=TODAY)
        assert styles["trend_template"] == f"background:{C['濃黄']}"

    def test_trend_other_text_no_color(self):
        # ▲ や ▽ など色付け対象外の表記は色なし
        styles = helpers.compute_cell_styles({"trend_template": "▲"}, today=TODAY)
        assert "trend_template" not in styles

    # --- シグナル (ルール 2-7): 赤 > 青背景 > 青文字色 ---
    def test_signal_red_for_po(self):
        styles = helpers.compute_cell_styles({"tags": "ポ"}, today=TODAY)
        assert styles["tags"] == f"background:{C['赤']};color:#fff"

    def test_signal_red_for_bu(self):
        styles = helpers.compute_cell_styles({"tags": "ブ"}, today=TODAY)
        assert styles["tags"] == f"background:{C['赤']};color:#fff"

    def test_signal_red_for_sai(self):
        styles = helpers.compute_cell_styles({"tags": "最"}, today=TODAY)
        assert styles["tags"] == f"background:{C['赤']};color:#fff"

    def test_signal_blue_bg_for_kei(self):
        styles = helpers.compute_cell_styles({"tags": "警"}, today=TODAY)
        assert styles["tags"] == f"background:{C['青']};color:#fff"

    def test_signal_blue_bg_for_uri(self):
        styles = helpers.compute_cell_styles({"tags": "売"}, today=TODAY)
        assert styles["tags"] == f"background:{C['青']};color:#fff"

    def test_signal_blue_text_for_oshi(self):
        styles = helpers.compute_cell_styles({"tags": "押"}, today=TODAY)
        assert styles["tags"] == f"color:{C['青']}"

    def test_signal_red_priority_over_blue(self):
        styles = helpers.compute_cell_styles({"tags": "警/ポ"}, today=TODAY)
        assert styles["tags"] == f"background:{C['赤']};color:#fff"

    def test_signal_blue_bg_priority_over_oshi(self):
        styles = helpers.compute_cell_styles({"tags": "警/押"}, today=TODAY)
        assert styles["tags"] == f"background:{C['青']};color:#fff"

    def test_signal_no_match_no_color(self):
        styles = helpers.compute_cell_styles({"tags": ""}, today=TODAY)
        assert "tags" not in styles

    # --- 買い集め (ルール 20, 21): スコア合計 ≧ 8 濃黄 / ≦ 4 水色 ---
    def test_buy_collection_aa_strong_yellow(self):
        # A=5, A=5, sum=10 >= 8
        styles = helpers.compute_cell_styles({"buy_collection": "A,A"}, today=TODAY)
        assert styles["buy_collection"] == f"background:{C['濃黄']}"

    def test_buy_collection_ab_strong_yellow(self):
        # A=5, B=4, sum=9 >= 8
        styles = helpers.compute_cell_styles({"buy_collection": "A,B"}, today=TODAY)
        assert styles["buy_collection"] == f"background:{C['濃黄']}"

    def test_buy_collection_bc_no_color(self):
        # B=4, C=3, sum=7
        styles = helpers.compute_cell_styles({"buy_collection": "B,C"}, today=TODAY)
        assert "buy_collection" not in styles

    def test_buy_collection_dd_water_blue(self):
        # D=2, D=2, sum=4 <= 4
        styles = helpers.compute_cell_styles({"buy_collection": "D,D"}, today=TODAY)
        assert styles["buy_collection"] == f"background:{C['水色']}"

    def test_buy_collection_ee_water_blue(self):
        styles = helpers.compute_cell_styles({"buy_collection": "E,E"}, today=TODAY)
        assert styles["buy_collection"] == f"background:{C['水色']}"

    def test_buy_collection_invalid_no_color(self):
        styles = helpers.compute_cell_styles({"buy_collection": "—"}, today=TODAY)
        assert "buy_collection" not in styles

    # --- 時価総額 (ルール 29, 30): カテゴリ "中" / "大" → 薄黄 ---
    def test_market_cap_chu_light_yellow(self):
        styles = helpers.compute_cell_styles({"market_cap_category": "中"}, today=TODAY)
        assert styles["market_cap"] == f"background:{C['薄黄']}"

    def test_market_cap_dai_light_yellow(self):
        styles = helpers.compute_cell_styles({"market_cap_category": "大"}, today=TODAY)
        assert styles["market_cap"] == f"background:{C['薄黄']}"

    def test_market_cap_kyokusho_no_color(self):
        styles = helpers.compute_cell_styles({"market_cap_category": "極小"}, today=TODAY)
        assert "market_cap" not in styles

    def test_market_cap_tokudai_no_color(self):
        # "大" 完全一致なので "特大" は対象外
        styles = helpers.compute_cell_styles({"market_cap_category": "特大"}, today=TODAY)
        assert "market_cap" not in styles

    def test_market_cap_none_no_color(self):
        styles = helpers.compute_cell_styles({"market_cap_category": None}, today=TODAY)
        assert "market_cap" not in styles

    # --- 統合: 空 row でも例外なし ---
    def test_empty_row_only_trend_water_blue(self):
        # 空 row では trend_template が空欄扱いで水色のみ付く (他はすべて無)
        styles = helpers.compute_cell_styles({}, today=TODAY)
        assert styles == {"trend_template": f"background:{C['水色']}"}

    def test_default_today_uses_date_today(self):
        # today 省略時は date.today() を使う (落ちないことの確認)
        styles = helpers.compute_cell_styles({"rank": 100})
        assert styles["rank"] == f"background:{C['濃黄']}"


class TestMarketCapCategory:
    """_market_cap_category のユニットテスト"""

    def test_kyokusho(self):
        assert helpers._market_cap_category(99) == "極小"

    def test_sho(self):
        assert helpers._market_cap_category(100) == "小"
        assert helpers._market_cap_category(399) == "小"

    def test_chu(self):
        assert helpers._market_cap_category(400) == "中"
        assert helpers._market_cap_category(999) == "中"

    def test_dai(self):
        assert helpers._market_cap_category(1000) == "大"
        assert helpers._market_cap_category(2999) == "大"

    def test_tokudai(self):
        assert helpers._market_cap_category(3000) == "特大"
        assert helpers._market_cap_category(99999) == "特大"

    def test_none(self):
        assert helpers._market_cap_category(None) is None

    def test_non_numeric(self):
        assert helpers._market_cap_category("abc") is None


class TestBuyCollectionScore:
    """_buy_collection_score_sum のユニットテスト"""

    def test_aa(self):
        assert helpers._buy_collection_score_sum("A,A") == 10

    def test_ab(self):
        assert helpers._buy_collection_score_sum("A,B") == 9

    def test_cc(self):
        assert helpers._buy_collection_score_sum("C,C") == 6

    def test_ee(self):
        assert helpers._buy_collection_score_sum("E,E") == 2

    def test_invalid_format(self):
        assert helpers._buy_collection_score_sum("—") is None
        assert helpers._buy_collection_score_sum("") is None
        assert helpers._buy_collection_score_sum(None) is None

    def test_unknown_letter(self):
        assert helpers._buy_collection_score_sum("A,Z") is None

    def test_with_spaces(self):
        # "A, B" のようなスペース付きでも動く
        assert helpers._buy_collection_score_sum("A, B") == 9


class TestFormatPer:
    """_format_per のユニットテスト (二桁以上は整数、一桁は小数1桁)"""

    def test_two_digit_int(self):
        assert helpers._format_per(25) == "25"

    def test_two_digit_float(self):
        assert helpers._format_per(30.0) == "30"

    def test_two_digit_float_rounds(self):
        # 二桁以上は整数化 (四捨五入)
        assert helpers._format_per(25.4) == "25"
        assert helpers._format_per(25.6) == "26"

    def test_single_digit_float(self):
        assert helpers._format_per(5.3) == "5.3"

    def test_single_digit_int(self):
        assert helpers._format_per(3) == "3.0"

    def test_zero(self):
        assert helpers._format_per(0) == "0.0"

    def test_boundary_ten(self):
        # ちょうど 10 は整数表記
        assert helpers._format_per(10) == "10"
        assert helpers._format_per(10.0) == "10"

    def test_boundary_just_below_ten(self):
        assert helpers._format_per(9.9) == "9.9"

    def test_negative_under_ten(self):
        # 負の PER (赤字) は一桁扱い
        assert helpers._format_per(-3.5) == "-3.5"

    def test_none(self):
        assert helpers._format_per(None) == "—"

    def test_string(self):
        assert helpers._format_per("25") == "—"


class TestProgressQuarterAndDiff:
    """_progress_quarter_and_diff の quarter_label 表示テスト"""

    def test_quarter_zero_returns_0q(self, monkeypatch):
        """1Q 未発表 (quarter=0) は '0Q' を表示する (旧仕様の '—' から変更)"""
        # calc_progress_rate が {"quarter": 0} だけ返すケース (1Q 未発表 / 新年度初期)
        import gyoseki
        monkeypatch.setattr(gyoseki, "calc_progress_rate", lambda stock: {"quarter": 0})
        label, diff = helpers._progress_quarter_and_diff({"code_s": "0001"})
        assert label == "0Q"
        assert diff == "—"  # sales/profit が無いので diff は "—"

    def test_quarter_3_with_full_data(self, monkeypatch):
        """3Q + 全数値あり -> '3Q' + diff 文字列"""
        import gyoseki
        monkeypatch.setattr(
            gyoseki, "calc_progress_rate",
            lambda stock: {
                "quarter": 3,
                "sales": 70.0, "sales_pre": 72.0,
                "profit": 62.0, "profit_pre": 44.0,
            },
        )
        label, diff = helpers._progress_quarter_and_diff({"code_s": "0001"})
        assert label == "3Q"
        assert diff == "-2/+18"

    def test_no_progress_dict_returns_dash(self, monkeypatch):
        """calc_progress_rate がデータ不足で {} を返すときは '—'"""
        import gyoseki
        monkeypatch.setattr(gyoseki, "calc_progress_rate", lambda stock: {})
        label, diff = helpers._progress_quarter_and_diff({"code_s": "0001"})
        assert label == "—"
        assert diff == "—"

    def test_empty_stock_returns_dash(self):
        label, diff = helpers._progress_quarter_and_diff({})
        assert label == "—"
        assert diff == "—"

    def test_calc_raises_returns_dash(self, monkeypatch):
        """calc_progress_rate が例外を投げても '—' で安全にフォールバック"""
        import gyoseki
        def boom(stock):
            raise RuntimeError("boom")
        monkeypatch.setattr(gyoseki, "calc_progress_rate", boom)
        label, diff = helpers._progress_quarter_and_diff({"code_s": "0001"})
        assert label == "—"
        assert diff == "—"


class TestCollectGyoutaiThemeChoices:
    """issue #187: portfolio_shelve 全レコードから datalist 候補を集計する。"""

    def test_flatten_and_unique_and_sort(self):
        records = [
            {"memo": {"gyoutai_themes": ["半導体", "AI"]}},
            {"memo": {"gyoutai_themes": ["AI", "ロボット"]}},
            {"memo": {"gyoutai_themes": ["半導体"]}},
        ]
        assert helpers.collect_gyoutai_theme_choices(records) == [
            "AI",
            "ロボット",
            "半導体",
        ]

    def test_strips_whitespace_and_removes_empty(self):
        records = [
            {"memo": {"gyoutai_themes": [" 半導体 ", "", "  ", "AI"]}},
        ]
        assert helpers.collect_gyoutai_theme_choices(records) == ["AI", "半導体"]

    def test_missing_gyoutai_themes_returns_empty(self):
        records = [{"memo": {}}, {"memo": {"gyoutai_themes": None}}]
        assert helpers.collect_gyoutai_theme_choices(records) == []

    def test_missing_memo_returns_empty(self):
        assert helpers.collect_gyoutai_theme_choices([{}]) == []

    def test_empty_records_returns_empty(self):
        assert helpers.collect_gyoutai_theme_choices([]) == []

    def test_ignores_non_str_elements(self):
        """defensive: 想定外の型 (None, int) が混入してもクラッシュしない"""
        records = [
            {"memo": {"gyoutai_themes": ["AI", None, 123, "半導体"]}},
        ]
        assert helpers.collect_gyoutai_theme_choices(records) == ["AI", "半導体"]


class TestListPortfolioWithIndicators:
    """list_portfolio_with_indicators の sort_key / status_query / status_label 検証 (issue #178)。

    外部参照 (_bulk_get_stock_data, _bulk_resolve_stock_names, compute_cell_styles) は
    monkeypatch でスタブし、並び順とフィールド埋めだけを検証する。
    """

    @pytest.fixture
    def stub_externals(self, monkeypatch):
        """rank を rec から直接読めるよう _extract_indicators_for_portfolio もスタブ。"""
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: {c: {} for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names", lambda codes: {c: f"name_{c}" for c in codes})
        monkeypatch.setattr(helpers, "compute_cell_styles", lambda row, today: {})
        # _extract_indicators_for_portfolio は rec の rank を上書きしないよう空 dict を返す
        monkeypatch.setattr(helpers, "_extract_indicators_for_portfolio", lambda stock: {})

    def _make(self, code_s, status, rank=None, themes=None):
        return {
            "code_s": code_s,
            "status": status,
            "rank": rank,
            "memo": {"gyoutai_themes": themes or []},
        }

    def test_sort_by_gyoutai_then_rank(self, stub_externals):
        records = [
            self._make("0001", "1保", rank=30, themes=["人材"]),
            self._make("0002", "1保", rank=10, themes=["半導体"]),
            self._make("0003", "1保", rank=5, themes=["人材"]),
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key="gyoutai")
        # 業態順 (人材→半導体) かつ業態内は rank 昇順
        assert [r["code_s"] for r in rows] == ["0003", "0001", "0002"]

    def test_sort_by_rank_only(self, stub_externals):
        records = [
            self._make("0001", "1保", rank=30, themes=["人材"]),
            self._make("0002", "1保", rank=10, themes=["半導体"]),
            self._make("0003", "1保", rank=5, themes=["人材"]),
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key="rank")
        assert [r["code_s"] for r in rows] == ["0003", "0002", "0001"]

    def test_empty_gyoutai_goes_to_end_under_gyoutai_sort(self, stub_externals):
        records = [
            self._make("0001", "1保", rank=10, themes=[]),
            self._make("0002", "1保", rank=20, themes=["半導体"]),
            self._make("0003", "1保", rank=5, themes=None),
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key="gyoutai")
        # 半導体が先頭、空 themes は末尾 (末尾内は rank 昇順)
        assert [r["code_s"] for r in rows] == ["0002", "0003", "0001"]

    def test_gyoutai_uses_first_line_only(self, stub_externals):
        """themes[0] のみで判定 (themes[1] 以降は無視)"""
        records = [
            self._make("0001", "1保", rank=10, themes=["AI", "人材"]),
            self._make("0002", "1保", rank=20, themes=["人材", "AI"]),
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key="gyoutai")
        # 0001 の themes[0]=AI が先、0002 の themes[0]=人材 が後
        assert [r["code_s"] for r in rows] == ["0001", "0002"]

    def test_status_query_label_filled(self, stub_externals):
        records = [
            self._make("0001", "1保"),
            self._make("0002", "2準"),
            self._make("0003", "3監"),
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key="rank")
        by_code = {r["code_s"]: r for r in rows}
        assert by_code["0001"]["status_query"] == "hold"
        assert by_code["0001"]["status_label"] == "保有"
        assert by_code["0002"]["status_query"] == "semi"
        assert by_code["0002"]["status_label"] == "準保有"
        assert by_code["0003"]["status_query"] == "watch"
        assert by_code["0003"]["status_label"] == "監視"


class TestMarkdownToHtml:
    """_markdown_to_html: *太字* / **赤字** 変換"""

    def test_single_asterisk_becomes_bold(self):
        assert helpers._markdown_to_html("*強調*") == "<b>強調</b>"

    def test_double_asterisk_becomes_red(self):
        assert (
            helpers._markdown_to_html("**重要**")
            == '<span style="color:#ff0000">重要</span>'
        )

    def test_mixed_in_one_text(self):
        result = helpers._markdown_to_html("**赤** と *太* を混在")
        assert '<span style="color:#ff0000">赤</span>' in result
        assert "<b>太</b>" in result

    def test_empty_returns_empty(self):
        assert helpers._markdown_to_html("") == ""

    def test_url_still_linkified(self):
        result = helpers._markdown_to_html("see https://example.com")
        assert '<a href="https://example.com" target="_blank">' in result
