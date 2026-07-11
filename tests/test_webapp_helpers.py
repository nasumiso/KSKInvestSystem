"""webapp/helpers.py のテスト (tmp_path で一時DBを作成)"""

import html
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
            "post_price_changes": {"1d": "", "5d": "", "20d": ""},
            "post_comment": "",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }]
        rs.upsert_research_record(rec)

        detail = helpers.get_research_detail("3496")
        entry = detail["kessan_comments"][0]
        # 未来エントリなので補完されず空のまま
        assert entry["post_price_changes"] == {"1d": "", "5d": "", "20d": ""}


class TestBackfillPersistence:
    """backfill で補完した反応率が shelve に確定保存される (price_log の
    30日ウィンドウから決算日が外れても消えないように)"""

    def _setup_entry(self, monkeypatch, post_price_changes, log):
        """過去決算エントリ1件を挿入し、price_log と today を固定する"""
        from datetime import date as _date
        monkeypatch.setattr(
            helpers,
            "_bulk_price_logs",
            lambda codes: {"3496": log} if "3496" in codes else {},
        )
        monkeypatch.setattr(
            helpers, "get_price_day", lambda _: _date(2026, 4, 25)
        )
        rec = rs.get_research_record("3496")
        rec["kessan_comments"] = [{
            "kessanbi": "2026/04/01",
            "quarter": 4,
            "pre_expectation": "○",
            "pre_outlook": "テスト",
            "post_price_changes": dict(post_price_changes),
            "post_comment": "",
            "kessan_matagi": False,
            "held_before_kessan": False,
            "held_after_kessan": False,
        }]
        rs.upsert_research_record(rec)

    @staticmethod
    def _log_with_after_prices():
        """決算日前営業日終値 + 5営業日後までの終値 (5d まで計算可能)"""
        from datetime import date as _date, timedelta as _td
        kessan = _date(2026, 4, 1)
        log = [(kessan - _td(days=1), 1000)]
        for i, pr in enumerate([1032, 1040, 1045, 1048, 1051], start=1):
            log.append((kessan + _td(days=i), pr))
        return log

    def test_backfilled_5d_is_persisted(self, populated_db, monkeypatch):
        """補完した 5d が再読込 (= 別呼び出し) でも残る = 永続化されている"""
        self._setup_entry(
            monkeypatch,
            {"1d": "+3.2", "5d": ""},
            self._log_with_after_prices(),
        )
        # 1回目: get_research_detail 内 backfill が走り永続化される
        helpers.get_research_detail("3496")
        # 別ルートで生の shelve を再読込し、永続化されたか確認
        rec = rs.get_research_record("3496")
        assert rec["kessan_comments"][0]["post_price_changes"]["5d"] == "+5.1"

    def test_existing_nonempty_and_pts_not_overwritten(
        self, populated_db, monkeypatch
    ):
        """既存の非空値・pts キーは backfill 永続化で上書きされない"""
        self._setup_entry(
            monkeypatch,
            # 5d は手動入力済み (+99)、pts も入っている → どちらも温存される
            {"pts": "+2.5", "1d": "+3.2", "5d": "+99"},
            self._log_with_after_prices(),
        )
        helpers.get_research_detail("3496")
        ppc = rs.get_research_record("3496")["kessan_comments"][0]["post_price_changes"]
        assert ppc["5d"] == "+99"   # 既存値温存
        assert ppc["pts"] == "+2.5"  # 未知キー温存

    def test_uncomputable_period_not_persisted(
        self, populated_db, monkeypatch
    ):
        """price_log に決算日付近が無く計算不能なら何も永続化されない (冪等)"""
        from datetime import date as _date, timedelta as _td
        # 決算日 (2026/04/01) より後だけの log → before_price が取れず計算不能
        log = [(_date(2026, 4, 20) + _td(days=i), 1000 + i) for i in range(5)]
        self._setup_entry(monkeypatch, {"1d": "+3.2", "5d": ""}, log)
        helpers.get_research_detail("3496")
        ppc = rs.get_research_record("3496")["kessan_comments"][0]["post_price_changes"]
        assert ppc["5d"] == ""  # 計算不能なので空のまま


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

    def test_kessan_older_than_90_days_is_dropped(self, kessan_env):
        """90日 (約1四半期) より前の過去決算は recent_past/older_past いずれにも含まれない

        履歴蓄積による DOM/メモリ肥大化を防ぐためのカットオフ (issue #203 議論)。
        """
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 19, 0)  # base_day=4/27
        pf_dict = {
            # 8日前 (4/19): older_past に入る (recent_cutoff=4/20 より古い)
            "6501": {
                "code_s": "6501", "stock_name": "8日前",
                "kessanbi": "2026/04/19", "kessan_quarter": 4,
            },
            # 91日前 (1/26): older_cutoff=1/27 より古いので drop される
            "9984": {
                "code_s": "9984", "stock_name": "91日前",
                "kessanbi": "2026/01/26", "kessan_quarter": 3,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        recent_keys = [k for k, _ in result["recent_past_entries"]]
        older_keys = [k for k, _ in result["older_past_entries"]]
        # 8日前は older_past
        assert "2026/04/19" in older_keys
        assert "2026/04/19" not in recent_keys
        # 91日前は両方に入らない (drop)
        assert "2026/01/26" not in recent_keys
        assert "2026/01/26" not in older_keys

    def test_past_entries_excludes_kessan_older_than_90_days(self, kessan_env):
        """past_entries (空状態判定で使う後方互換キー) も90日カットオフ後の値を返す

        market.html の空状態判定 `not future_entries and not past_entries` が
        90日超の決算しかないケースで誤って False になり、カードも空状態メッセージも
        出ない画面破綻を防ぐ。
        """
        from datetime import datetime as _dt
        today_dt = _dt(2026, 4, 27, 19, 0)  # base_day=4/27
        pf_dict = {
            # 91日前のみ → 90日カットオフで past_entries は空になるべき
            "9984": {
                "code_s": "9984", "stock_name": "91日前のみ",
                "kessanbi": "2026/01/26", "kessan_quarter": 3,
            },
        }
        kessan_env(pf_dict, today_dt)
        result = helpers.get_market_kessan_data()
        # past_entries も past_entries_all ではなく90日カットオフ後 = 空
        assert result["past_entries"] == []
        assert result["future_entries"] == []
        # 空状態メッセージが出る条件 (not future and not past) を満たす
        assert not result["future_entries"] and not result["past_entries"]

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


class TestGetMarketKessanDataDuplicateEntries:
    """issue #207: 同 (code_s, kessanbi) で複数 quarter エントリ併存ケース。

    upsert_kessan_pts_change が quarter=0 で append したフォールバック空エントリと、
    手動メモ入りの quarter=4 エントリが両方残っているとき、メモ側を winner に選ぶ。
    """

    @pytest.fixture
    def kessan_env(self, populated_db, monkeypatch):
        """TestGetMarketKessanData と同じ kessan_env fixture (重複定義避けるため再利用)"""
        from datetime import datetime as _dt

        def setup(pf_dict, today_dt):
            import kessan as _k
            monkeypatch.setattr(_k, "load_pf_kessan_db", lambda: pf_dict)
            import portfolio as _p
            monkeypatch.setattr(_p, "parse_my_portforio", lambda: ([], []))
            monkeypatch.setattr(helpers, "_bulk_price_logs", lambda codes: {})

            class FrozenDateTime(_dt):
                @classmethod
                def today(cls):
                    return today_dt
                @classmethod
                def now(cls, tz=None):
                    return today_dt
            monkeypatch.setattr(helpers, "datetime", FrozenDateTime)
        return setup

    def test_memo_entry_not_overwritten_by_empty(self, kessan_env):
        """7717 想定: q=4 メモあり + q=0 PTS-only の 2 件 → memo が表示される"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 5, 12, 19, 0)
        pf_dict = {
            "7717": {
                "code_s": "7717",
                "stock_name": "ブイ・テクノロジー",
                "kessanbi": "2026/05/12",
                "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        rec = rs.create_research_record("7717", "ブイ・テクノロジー")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_expectation": "◎", "pre_outlook": "通期上振れ期待",
                "post_comment": "[B] 好決算で反応良好",
                "post_price_changes": {"pts": "", "1d": "", "5d": ""},
                "kessan_matagi": False,
                "held_before_kessan": False, "held_after_kessan": False,
            },
            {
                "kessanbi": "2026/05/12", "quarter": 0,
                "pre_expectation": "", "pre_outlook": "", "post_comment": "",
                "post_price_changes": {"pts": "+11.82", "1d": "", "5d": ""},
                "kessan_matagi": False,
                "held_before_kessan": False, "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        for kessanbi, stocks in result["today_entries"]:
            if kessanbi != "2026/05/12":
                continue
            for s in stocks:
                if s["code_s"] == "7717":
                    # メモあり側 (q=4) の値が出ている
                    assert s["pre_outlook"] == "通期上振れ期待"
                    assert s["post_comment"] == "[B] 好決算で反応良好"
                    assert s["pre_expectation"] == "◎"
                    return
        assert False, "7717 が today_entries に見つからない"

    def test_pts_merged_from_separate_entry(self, kessan_env):
        """winner (q=4 メモ) に PTS が無くても、別エントリ (q=0) の PTS が引き継がれる"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 5, 12, 19, 0)
        pf_dict = {
            "7717": {
                "code_s": "7717", "stock_name": "テスト",
                "kessanbi": "2026/05/12", "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        rec = rs.create_research_record("7717", "テスト")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "メモあり", "post_comment": "コメあり",
                "post_price_changes": {"pts": "", "1d": "", "5d": ""},
            },
            {
                "kessanbi": "2026/05/12", "quarter": 0,
                "pre_outlook": "", "post_comment": "",
                "post_price_changes": {"pts": "+11.82", "1d": "", "5d": ""},
            },
        ]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        for kessanbi, stocks in result["today_entries"]:
            if kessanbi != "2026/05/12":
                continue
            for s in stocks:
                if s["code_s"] == "7717":
                    assert s["post_price_changes"].get("pts") == "+11.82"
                    return
        assert False, "7717 が today_entries に見つからない"

    def test_higher_quarter_wins_when_both_have_memo(self, kessan_env):
        """両方メモあり → quarter 大優先 (q=2 と q=4 → q=4 が選ばれる)"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 5, 12, 19, 0)
        pf_dict = {
            "7717": {
                "code_s": "7717", "stock_name": "テスト",
                "kessanbi": "2026/05/12", "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        rec = rs.create_research_record("7717", "テスト")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 2,
                "pre_outlook": "Q2 メモ", "post_comment": "Q2 コメ",
            },
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "Q4 メモ", "post_comment": "Q4 コメ",
            },
        ]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        for kessanbi, stocks in result["today_entries"]:
            if kessanbi != "2026/05/12":
                continue
            for s in stocks:
                if s["code_s"] == "7717":
                    assert s["pre_outlook"] == "Q4 メモ"
                    assert s["post_comment"] == "Q4 コメ"
                    return
        assert False, "7717 が today_entries に見つからない"

    def test_single_entry_unchanged(self, kessan_env):
        """1 件のみのエントリは従来通り表示される (regression 防御)"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 5, 12, 19, 0)
        pf_dict = {
            "7717": {
                "code_s": "7717", "stock_name": "テスト",
                "kessanbi": "2026/05/12", "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        rec = rs.create_research_record("7717", "テスト")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "単独メモ",
                "post_price_changes": {"pts": "+1.0", "1d": "", "5d": ""},
            },
        ]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        for kessanbi, stocks in result["today_entries"]:
            if kessanbi != "2026/05/12":
                continue
            for s in stocks:
                if s["code_s"] == "7717":
                    assert s["pre_outlook"] == "単独メモ"
                    assert s["post_price_changes"].get("pts") == "+1.0"
                    return
        assert False, "7717 が today_entries に見つからない"

    def test_pf_only_replaced_by_memo_entry(self, kessan_env):
        """pf_dict 由来 base (has_comment=False) は kessan_comments の memo entry で置き換わる"""
        from datetime import datetime as _dt
        today_dt = _dt(2026, 5, 12, 19, 0)
        pf_dict = {
            "7717": {
                "code_s": "7717", "stock_name": "テスト",
                "kessanbi": "2026/05/12", "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        rec = rs.create_research_record("7717", "テスト")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "kessan_comments 由来",
            },
        ]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        for kessanbi, stocks in result["today_entries"]:
            if kessanbi != "2026/05/12":
                continue
            for s in stocks:
                if s["code_s"] == "7717":
                    assert s["pre_outlook"] == "kessan_comments 由来"
                    assert s["has_comment"] is True
                    return
        assert False, "7717 が today_entries に見つからない"

    def test_pf_base_does_not_overwrite_research_held_or_pts(self, kessan_env):
        """pf-only ベース行が research 側の「メモなしだが held / 反応率あり」エントリを
        上書きしない (codex P1 review 反映: kessan_matagi / held_* / post_price_changes 保護)。
        """
        from datetime import datetime as _dt
        today_dt = _dt(2026, 5, 12, 19, 0)
        pf_dict = {
            "7717": {
                "code_s": "7717", "stock_name": "テスト",
                "kessanbi": "2026/05/12", "kessan_quarter": 4,
            },
        }
        kessan_env(pf_dict, today_dt)
        rec = rs.create_research_record("7717", "テスト")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                # メモは無いが、kessan_matagi / held_* / 反応率あり (= pf-only プレースホルダではない)
                "pre_expectation": "", "pre_outlook": "", "post_comment": "",
                "post_price_changes": {"pts": "", "1d": "+5.5", "5d": ""},
                "kessan_matagi": True,
                "held_before_kessan": True,
                "held_after_kessan": True,
            },
        ]
        rs.upsert_research_record(rec)

        result = helpers.get_market_kessan_data()
        for kessanbi, stocks in result["today_entries"]:
            if kessanbi != "2026/05/12":
                continue
            for s in stocks:
                if s["code_s"] == "7717":
                    # research 側のフラグ・反応率が表示される
                    assert s["kessan_matagi"] is True
                    assert s["held_before_kessan"] is True
                    assert s["held_after_kessan"] is True
                    assert s["post_price_changes"].get("1d") == "+5.5"
                    return
        assert False, "7717 が today_entries に見つからない"


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

    @pytest.mark.parametrize("field,value", [
        ("memo", "新しい総括"),
        ("overall_rating", "S"),
        ("institutional_comment", "更新コメント"),
        ("openwork", "4.0"),
        ("cramer", "Strong Buy"),
    ])
    def test_save_memo_changed_updates_analysis_date(self, populated_db, field, value):
        """手動メモ各項目の変更で分析日が当日 (暦日) に自動更新される"""
        form = {"analysis_date_raw": "11/13", field: value}
        helpers.save_memo("3496", form)
        rec = helpers.get_research_detail("3496")
        assert rec["analysis_date_raw"] == helpers._today_analysis_date()

    def test_save_memo_unchanged_keeps_analysis_date(self, populated_db):
        """メモ・総括が無変更 (他フィールドのみ変更) なら分析日は触らない"""
        form = {
            "overall_rating": "A",
            "institutional_comment": "成長性高い",
            "memo": "テストメモ",  # fixture と同値 = 無変更
            "openwork": "3.72",
            "cramer": "Buy推奨",
            "analysis_date_raw": "11/13",
        }
        helpers.save_memo("3496", form)
        rec = helpers.get_research_detail("3496")
        assert rec["analysis_date_raw"] == "11/13"

    def test_save_memo_manual_date_wins_over_auto(self, populated_db):
        """分析日を手動編集した保存ではメモ変更があっても手動値を採用"""
        form = {
            "memo": "新しい総括",
            "analysis_date_raw": "25/12/1",
            "analysis_date_raw__dirty": "1",
        }
        helpers.save_memo("3496", form)
        rec = helpers.get_research_detail("3496")
        assert rec["analysis_date_raw"] == "25/12/1"

    def test_save_memo_stale_submitted_date_is_ignored_when_not_dirty(self, populated_db):
        """未編集 input の古い分析日が再送されても手動編集扱いせず当日更新する"""
        form = {
            "memo": "新しい総括",
            "analysis_date_raw": "11/13",
            "analysis_date_raw__dirty": "",
        }
        helpers.save_memo("3496", form)
        rec = helpers.get_research_detail("3496")
        assert rec["analysis_date_raw"] == helpers._today_analysis_date()


class TestSaveStockNamePrev:
    """issue #236: save_stock_name_prev のテスト"""

    @pytest.mark.parametrize(
        "input_value, expected",
        [
            ("南海電鉄", "南海電鉄"),       # 通常文字列はそのまま保存
            ("", None),                     # 空文字 → None リセット
            ("   ", None),                  # 前後空白だけ → None リセット
            ("  TEPCO  ", "TEPCO"),         # 前後空白を strip して保存
        ],
    )
    def test_save_stock_name_prev_variants(self, populated_db, input_value, expected):
        helpers.save_stock_name_prev("3496", input_value)
        rec = helpers.get_research_detail("3496")
        assert rec["stock_name_prev"] == expected

    def test_save_stock_name_prev_unknown_code_raises(self, populated_db):
        import pytest as _pytest
        with _pytest.raises(KeyError):
            helpers.save_stock_name_prev("9999", "any")


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

    @pytest.mark.parametrize("form,expect_today", [
        ({
            "overview": "更新概要",
            "shikiho_comments_0": "コメント1",
            "shikiho_periods_0": "26.3",
        }, True),
        ({
            "overview": "駐車場サブリース",
            "shikiho_comments_0": "最高益",
            "shikiho_periods_0": "",
            "shikiho_comments_1": "新規事業",
            "shikiho_periods_1": "",
        }, False),
    ])
    def test_save_shikiho_analysis_date(self, populated_db, form, expect_today):
        """四季報の実変更時のみ分析日を当日へ自動更新する"""
        helpers.save_shikiho("3496", form)
        rec = helpers.get_research_detail("3496")
        expected = helpers._today_analysis_date() if expect_today else "11/13"
        assert rec["analysis_date_raw"] == expected


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

    @pytest.mark.parametrize("form,expect_today", [
        ({"ir_comment_26.4": "新コメント"}, True),   # 変更あり → 分析日を当日に
        ({"ir_comment_26.4": "好調"}, False),        # 無変更再保存 → 分析日は不変
    ])
    def test_save_ir_comments_analysis_date(self, populated_db, form, expect_today):
        """IR分析コメントの実変更時のみ分析日を当日へ自動更新する"""
        helpers.save_ir_comments("3496", form)
        rec = helpers.get_research_detail("3496")
        expected = helpers._today_analysis_date() if expect_today else "11/13"
        assert rec["analysis_date_raw"] == expected


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


class TestFormatReactionDigits:
    """_format_reaction の桁数ルール: |x|>=10 は整数 / <10 は小数1桁"""

    @pytest.mark.parametrize("before,after,expected", [
        # |x| < 10 → 小数1桁
        (1000, 1032, "+3.2"),
        (1000, 985, "-1.5"),
        (1000, 1099, "+9.9"),
        (1000, 999, "-0.1"),
        # |x| >= 10 → 整数
        (1000, 1100, "+10"),
        (1000, 899, "-10"),
        (1000, 1162, "+16"),
        (1000, 1513, "+51"),
        (1000, 700, "-30"),
    ])
    def test_format_rule(self, before, after, expected):
        assert helpers._format_reaction(before, after) == expected


class TestCalcPriceReactions:
    """calc_price_reactions の dict 返却 (issue #133)"""

    def test_returns_dict_with_all_periods(self, monkeypatch):
        from datetime import date, timedelta
        kessan = date(2026, 4, 1)
        log = [(kessan - timedelta(days=1), 1000)]
        # 1d=+3.2, 5d=+5.1, 20d=+12 (>=10 で整数表記)
        prs = [1032, 1040, 1045, 1048, 1051,
               1055, 1060, 1062, 1065, 1070,
               1075, 1078, 1082, 1085, 1090,
               1095, 1100, 1105, 1110, 1120]
        for i, pr in enumerate(prs, start=1):
            log.append((kessan + timedelta(days=i), pr))
        monkeypatch.setattr(helpers, "get_stock_data", lambda c: {"price_log": log})
        result = helpers.calc_price_reactions("5032", "2026/04/01")
        assert result == {"1d": "+3.2", "5d": "+5.1", "20d": "+12"}

    def test_invalid_kessanbi_returns_empty_dict(self):
        result = helpers.calc_price_reactions("5032", "invalid-date")
        assert result == {"1d": "", "5d": "", "20d": ""}

    def test_partial_log_returns_partial_dict(self, monkeypatch):
        from datetime import date, timedelta
        kessan = date(2026, 4, 1)
        # 後ろ1本しか無い → 1d は取れて 5d / 20d は ""
        log = [(kessan - timedelta(days=1), 1000), (kessan + timedelta(days=1), 1050)]
        monkeypatch.setattr(helpers, "get_stock_data", lambda c: {"price_log": log})
        result = helpers.calc_price_reactions("5032", "2026/04/01")
        assert result["1d"] == "+5.0"
        assert result["5d"] == ""
        assert result["20d"] == ""


class TestNormalizePostPriceChanges:
    """normalize_kessan_post_price_changes の後方互換正規化 (issue #133)"""

    def test_new_format_passthrough(self):
        entry = {"post_price_changes": {"1d": "+3", "5d": "+5", "20d": "+12"}}
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "+3", "5d": "+5", "20d": "+12"}

    def test_old_format_lifts_to_1d(self):
        entry = {"post_price_change": "-15"}
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "-15", "5d": "", "20d": ""}

    def test_both_present_prefers_new(self):
        entry = {
            "post_price_change": "-15",
            "post_price_changes": {"1d": "+2", "5d": "+3", "20d": "+5"},
        }
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "+2", "5d": "+3", "20d": "+5"}

    def test_neither_present_returns_empty(self):
        result = rs.normalize_kessan_post_price_changes({})
        assert result == {"1d": "", "5d": "", "20d": ""}

    def test_partial_new_format_filled_with_empty(self):
        entry = {"post_price_changes": {"1d": "+3"}}  # 5d / 20d 欠落
        result = rs.normalize_kessan_post_price_changes(entry)
        assert result == {"1d": "+3", "5d": "", "20d": ""}


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


class TestUpsertKessanPtsChangeFallback:
    """issue #207: quarter=0 引数のフォールバックマッチング (本番経路 make_stock_db.py:1708)。

    cron で kessan_quarter 取得失敗 → q=0 で呼ばれたとき、同 kessanbi の
    最大 quarter エントリにマージし重複 append を防ぐ。
    """

    @pytest.fixture
    def setup_db(self, db_path, monkeypatch):
        monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", db_path)
        monkeypatch.setattr(helpers, "RESEARCH_SHELVE", db_path, raising=False)
        return db_path

    def test_quarter_zero_merges_into_existing_quarter4(self, setup_db):
        """q=4 メモ既存 → q=0 PTS upsert で同エントリに pts 追記、append されない"""
        rec = rs.create_research_record("7717", "ブイ・テクノロジー")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_expectation": "◎", "pre_outlook": "強気",
                "post_price_changes": {"1d": "", "5d": ""},
                "post_comment": "[B] 好決算", "kessan_matagi": False,
                "held_before_kessan": False, "held_after_kessan": False,
            },
        ]
        rs.upsert_research_record(rec)
        helpers.upsert_kessan_pts_change("7717", "2026/05/12", 0, "+11.82")
        loaded = rs.get_research_record("7717")
        assert len(loaded["kessan_comments"]) == 1
        e = loaded["kessan_comments"][0]
        assert e["quarter"] == 4  # q=4 のまま
        assert e["pre_outlook"] == "強気"
        assert e["post_comment"] == "[B] 好決算"
        assert e["post_price_changes"]["pts"] == "+11.82"

    def test_pts_only_overwrites_pts_field(self, setup_db):
        """q=0 フォールバックマージで post_comment 等は変更されない"""
        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "メモ", "post_comment": "コメ",
                "post_price_changes": {"pts": "+1.0", "1d": "+0.5", "5d": ""},
            },
        ]
        rs.upsert_research_record(rec)
        helpers.upsert_kessan_pts_change("7717", "2026/05/12", 0, "+11.82")
        loaded = rs.get_research_record("7717")
        e = loaded["kessan_comments"][0]
        assert e["pre_outlook"] == "メモ"
        assert e["post_comment"] == "コメ"
        # 1d は保持、pts のみ上書き
        assert e["post_price_changes"]["1d"] == "+0.5"
        assert e["post_price_changes"]["pts"] == "+11.82"

    def test_quarter_zero_picks_max_quarter_when_multiple(self, setup_db):
        """同 kessanbi で q=2 と q=4 がある時 q=4 を選ぶ"""
        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 2,
                "pre_outlook": "Q2", "post_price_changes": {},
            },
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "Q4", "post_price_changes": {},
            },
        ]
        rs.upsert_research_record(rec)
        helpers.upsert_kessan_pts_change("7717", "2026/05/12", 0, "+9.99")
        loaded = rs.get_research_record("7717")
        # q=2 と q=4 は維持 (新規 append しない)
        assert len(loaded["kessan_comments"]) == 2
        # q=4 に pts が入っている
        for e in loaded["kessan_comments"]:
            if int(e["quarter"]) == 4:
                assert e["post_price_changes"]["pts"] == "+9.99"
                assert e["pre_outlook"] == "Q4"
            elif int(e["quarter"]) == 2:
                assert e["post_price_changes"].get("pts", "") == ""

    def test_quarter_zero_appends_when_no_kessanbi_match(self, setup_db):
        """該当 kessanbi のエントリなし → 新規 append (従来挙動互換)"""
        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2025/02/14", "quarter": 4,
                "pre_outlook": "別決算日メモ",
            },
        ]
        rs.upsert_research_record(rec)
        helpers.upsert_kessan_pts_change("7717", "2026/05/12", 0, "+1.5")
        loaded = rs.get_research_record("7717")
        assert len(loaded["kessan_comments"]) == 2
        # 新規 append された方
        new_entry = next(
            e for e in loaded["kessan_comments"] if e["kessanbi"] == "2026/05/12"
        )
        assert int(new_entry["quarter"]) == 0
        assert new_entry["post_price_changes"]["pts"] == "+1.5"

    def test_explicit_quarter_appends_when_no_match(self, setup_db):
        """quarter=2 引数で完全一致なし → 従来通り新規 append (フォールバックしない)"""
        rec = rs.create_research_record("7717", "TEST")
        rec["kessan_comments"] = [
            {
                "kessanbi": "2026/05/12", "quarter": 4,
                "pre_outlook": "Q4 メモ",
            },
        ]
        rs.upsert_research_record(rec)
        helpers.upsert_kessan_pts_change("7717", "2026/05/12", 2, "+3.3")
        loaded = rs.get_research_record("7717")
        # フォールバックせず q=4 と q=2 の 2 件
        assert len(loaded["kessan_comments"]) == 2
        quarters = sorted(int(e["quarter"]) for e in loaded["kessan_comments"])
        assert quarters == [2, 4]


# ==================================================
# 条件付き書式 (issue #177)
# ==================================================
from datetime import date  # noqa: E402

C = helpers.PORTFOLIO_COLORS  # 短縮エイリアス
TODAY = date(2026, 5, 10)    # テスト用固定基準日


class TestComputeCellStyles:
    """compute_cell_styles のユニットテスト (issue #177)

    各ルールの「色が付く代表値」「色が付かない境界値」を parametrize テーブルで集約。
    複雑な特殊ケース (優先順位、空欄処理、未来日処理など) は別関数で個別検証。
    """

    # ----- 数値しきい値系: 1ルール = (色付く代表, 色つかない境界) -----
    @pytest.mark.parametrize(
        "row, field, expected",
        [
            # 順位 (rank<300 で 濃黄、=300 で色なし)
            ({"rank": 299}, "rank", f"background:{C['濃黄']}"),
            ({"rank": 300}, "rank", None),
            ({"rank": None}, "rank", None),
            # 売上成長 (>=30 で 薄黄)
            ({"sales_growth_raw": 30}, "sales_growth", f"background:{C['薄黄']}"),
            ({"sales_growth_raw": 29}, "sales_growth", None),
            # 利益成長 (>=30 で 薄黄)
            ({"profit_growth_raw": 30}, "profit_growth", f"background:{C['薄黄']}"),
            ({"profit_growth_raw": 29}, "profit_growth", None),
            # 理論株価乖離 (>50)
            ({"theoretical_diff_raw": 51}, "theoretical_diff", f"background:{C['薄黄']}"),
            ({"theoretical_diff_raw": 50}, "theoretical_diff", None),
            # 配当 (>=5 濃黄 / >3 薄黄 / =3 色なし)
            ({"dividend_raw": 5.0}, "dividend", f"background:{C['濃黄']}"),
            ({"dividend_raw": 4.0}, "dividend", f"background:{C['薄黄']}"),
            ({"dividend_raw": 3.0}, "dividend", None),
            # RS (>80 濃黄 / >=70 薄黄 / 80 ちょうどは薄黄)
            ({"rs_raw": 81}, "rs", f"background:{C['濃黄']}"),
            ({"rs_raw": 80}, "rs", f"background:{C['薄黄']}"),
            ({"rs_raw": 70}, "rs", f"background:{C['薄黄']}"),
            ({"rs_raw": 69}, "rs", None),
            # 進捗率乖離 (営利>=20 で 濃黄、<20 で色なし)
            ({"progress_diff_eiri_raw": 20, "gyoseki_quarity_expr": ""}, "progress_diff",
             f"background:{C['濃黄']}"),
            ({"progress_diff_eiri_raw": 19, "gyoseki_quarity_expr": ""}, "progress_diff", None),
            # ステージ (2S=濃黄 単色 / 3S=水色 単色 / 4S=青 単色 + 白文字)
            ({"memo": {"stage": "2S(3T)"}}, "stage", f"background:{C['濃黄']}"),
            ({"memo": {"stage": "3S"}}, "stage", f"background:{C['水色']}"),
            ({"memo": {"stage": "4S"}}, "stage", f"background:{C['青']};color:#fff"),
            ({"memo": {"stage": ""}}, "stage", None),
            # 時価総額 (カテゴリ "中"/"大" → 薄黄、それ以外なし)
            ({"market_cap_category": "中"}, "market_cap", f"background:{C['薄黄']}"),
            ({"market_cap_category": "大"}, "market_cap", f"background:{C['薄黄']}"),
            ({"market_cap_category": "極小"}, "market_cap", None),
            ({"market_cap_category": "特大"}, "market_cap", None),  # "大" 完全一致のみ
            ({"market_cap_category": None}, "market_cap", None),
        ],
    )
    def test_simple_threshold_rules(self, row, field, expected):
        """単純な数値しきい値・カテゴリマッチング系のルール。"""
        styles = helpers.compute_cell_styles(row, today=TODAY)
        if expected is None:
            assert field not in styles
        else:
            assert styles[field] == expected

    # ----- PER の PEG 的指標 (利益成長 + 配当)/PER > 1 → 薄黄 -----
    @pytest.mark.parametrize(
        "per, growth, dividend, expected_color",
        [
            (20, 30, 1.0, f"background:{C['薄黄']}"),    # (30+1)/20 = 1.55 > 1
            (20, 20, 0.0, None),                          # (20+0)/20 = 1.0 (> なので no color)
            (0, 30, 1.0, None),                           # PER 0 → no color
            (20, None, 1.0, None),                        # 成長 None → no color
            (3.3, 232, None, f"background:{C['薄黄']}"),  # 配当 None は 0 扱い: (232+0)/3.3 > 1
            (30, 25, None, None),                         # 配当 None × 低成長 → 色なし
        ],
    )
    def test_per_peg_rule(self, per, growth, dividend, expected_color):
        styles = helpers.compute_cell_styles(
            {"per_raw": per, "profit_growth_raw": growth, "dividend_raw": dividend},
            today=TODAY,
        )
        if expected_color is None:
            assert "per" not in styles
        else:
            assert styles["per"] == expected_color

    # ----- トレンド (記号表示と背景色は独立。40週MA危険条件は青で上書き) -----
    @pytest.mark.parametrize(
        "row, expected",
        [
            ({"trend_template": "◎pr>ma10"}, f"background:{C['濃黄']}"),
            ({"trend_template": "◯RS"}, f"background:{C['薄黄']}"),
            (
                {"trend_template": "◯", "trend_template_misses": ["pr>ma30,40", "ma40Up"]},
                f"background:{C['青']}",
            ),
            (
                {"trend_template": "", "trend_template_misses": ["pr>ma30,40", "ma40Up", "RS"]},
                f"background:{C['青']}",
            ),
            ({"trend_template": "×"}, None),
            ({"trend_template": "—"}, f"background:{C['赤']}"),       # 未評価/データ欠損
            ({"trend_template": ""}, f"background:{C['赤']}"),
            ({"trend_template": "◎◯", "trend_template_misses": ["pr>ma30,40", "ma40Up"]}, f"background:{C['青']}"),
            ({"trend_template": "▲"}, None),                          # 対象外記号
        ],
    )
    def test_trend_template_rule(self, row, expected):
        styles = helpers.compute_cell_styles(row, today=TODAY)
        if expected is None:
            assert "trend_template" not in styles
        else:
            assert styles["trend_template"] == expected

    # ----- シグナル (赤 > 青背景 > 青文字色 の優先順位) -----
    @pytest.mark.parametrize(
        "tags, expected",
        [
            ("最", f"background:{C['赤']};color:#fff"),
            ("警", f"background:{C['青']};color:#fff"),
            ("売", f"background:{C['青']};color:#fff"),
            ("早売", f"background:{C['青']};color:#fff"),
            ("押", f"color:{C['青']}"),
            ("警/押", f"background:{C['青']};color:#fff"),     # 青背景 > 青文字
            ("早売/押", f"background:{C['青']};color:#fff"),   # 売り系背景 > 押し目文字
            ("", None),
        ],
    )
    def test_signal_priority(self, tags, expected):
        styles = helpers.compute_cell_styles({"tags": tags}, today=TODAY)
        if expected is None:
            assert "tags" not in styles
        else:
            assert styles["tags"] == expected

    def test_signal_cell_uses_signal_display_style(self):
        """ポ/ブの赤背景は新シグナル列に移す"""
        style = "background:rgba(234,67,53,0.85);color:#fff"
        styles = helpers.compute_cell_styles(
            {"tags": "高", "signal_mark": "ポ/ブ", "signal_display": {"style": style}},
            today=TODAY,
        )
        assert "tags" not in styles
        assert styles["signal"] == style


class TestFormatTagsTooltip:
    """タグ列 tooltip の補助文言"""

    def test_early_sell_tooltip_adds_short_description(self):
        tooltip = helpers._format_tags_tooltip("早売")
        assert "早売" in tooltip
        assert "急騰後の10ma利確ライン割れ" in tooltip

    def test_non_early_sell_tooltip_keeps_plain_tags(self):
        assert helpers._format_tags_tooltip("売/押") == "売/押"

    # ----- 進捗率乖離: <C3>=赤(注目) 単独 / eiri≧20 = 濃黄 単独 / 両該当=左右分割 -----
    def test_progress_diff_c3_only(self):
        """<C3> タグのみで eiri 不該当: 赤 (注目) 単色 + 白文字"""
        styles = helpers.compute_cell_styles(
            {"progress_diff_eiri_raw": 10, "gyoseki_quarity_expr": "[A]20%<C3>"},
            today=TODAY,
        )
        assert styles["progress_diff"] == f"background:{C['赤']};color:#fff"

    def test_progress_diff_c3_and_eiri_split(self):
        """<C3> + eiri≧20 両該当: 左半分=赤 / 右半分=濃黄 の linear-gradient"""
        styles = helpers.compute_cell_styles(
            {"progress_diff_eiri_raw": 30, "gyoseki_quarity_expr": "[A]20%<C3>"},
            today=TODAY,
        )
        assert styles["progress_diff"] == (
            f"background:linear-gradient(to right,"
            f"{C['赤']} 50%,{C['濃黄']} 50%)"
        )

    # ----- ステージ: 2 つ併存は強い順に左 -----
    @pytest.mark.parametrize(
        "stage, expected",
        [
            # 2S + 3S → 左 2S(濃黄) / 右 3S(水色) (注: 強い=数字大の S なので 3S が左)
            ("2S/3S", (
                f"background:linear-gradient(to right,"
                f"{C['水色']} 50%,{C['濃黄']} 50%)"
            )),
            # 3S + 4S → 左 4S(青) / 右 3S(水色)
            ("3S/4S", (
                f"background:linear-gradient(to right,"
                f"{C['青']} 50%,{C['水色']} 50%)"
            )),
            # 2S + 4S → 左 4S(青) / 右 2S(濃黄)
            ("2S/4S", (
                f"background:linear-gradient(to right,"
                f"{C['青']} 50%,{C['濃黄']} 50%)"
            )),
        ],
    )
    def test_stage_split_two_marks(self, stage, expected):
        styles = helpers.compute_cell_styles({"memo": {"stage": stage}}, today=TODAY)
        assert styles["stage"] == expected

    # ----- 決算日 ±1ヶ月 + 3Q → 濃黄、それ以外 → 薄黄/色なし -----
    @pytest.mark.parametrize(
        "kessanbi, today, quarter, expected",
        [
            # 内: 更新日 4/26 ± 1ヶ月以内 (5/14)
            (date(2026, 5, 14), TODAY, "3Q", f"background:{C['濃黄']}"),  # 3Q → 濃黄
            (date(2026, 5, 14), TODAY, "1Q", f"background:{C['薄黄']}"),  # 1Q → 薄黄
            # 内: 決算日が更新日より前 (5/20 - 5/10 = 10日)
            (date(2026, 5, 10), date(2026, 6, 15), "1Q", f"background:{C['薄黄']}"),
            # 外: 1ヶ月超
            (date(2026, 6, 30), TODAY, "3Q", None),
            (date(2026, 4, 10), date(2026, 6, 15), "1Q", None),  # 40日前
            # データなし
            (None, TODAY, "3Q", None),
        ],
    )
    def test_kessanbi_rule(self, kessanbi, today, quarter, expected):
        update_md = "4/26" if today == TODAY else "5/20"
        row = {
            "kessanbi_raw": kessanbi,
            "memo": {"last_research_update": update_md},
            "quarter": quarter,
        }
        styles = helpers.compute_cell_styles(row, today=today)
        if expected is None:
            assert "kessanbi_md" not in styles
        else:
            assert styles["kessanbi_md"] == expected

    # ----- 更新日 (14日前 薄灰 / 30日前 濃灰 / 未来日は前年扱い) -----
    @pytest.mark.parametrize(
        "md, expected",
        [
            ("4/26", f"background:{C['薄灰']}"),   # 14日前
            ("4/10", f"background:{C['濃灰']}"),   # 30日前
            ("5/5", None),                          # 5日前 → 色なし
            ("6/1", f"background:{C['濃灰']}"),    # 未来日 → 前年扱い (約11ヶ月前)
            ("—", None),
        ],
    )
    def test_last_research_update_rule(self, md, expected):
        styles = helpers.compute_cell_styles(
            {"memo": {"last_research_update": md}}, today=TODAY,
        )
        if expected is None:
            assert "last_research_update" not in styles
        else:
            assert styles["last_research_update"] == expected

    # ----- 統合: 空 row / today デフォルト -----
    def test_empty_row_only_trend_data_missing_red(self):
        """空 row では trend_template が欠損扱いで赤のみ付く"""
        styles = helpers.compute_cell_styles({}, today=TODAY)
        assert styles == {"trend_template": f"background:{C['赤']}"}

    def test_default_today_uses_date_today(self):
        """today 省略時は date.today() を使う (落ちないことの確認)"""
        styles = helpers.compute_cell_styles({"rank": 100})
        assert styles["rank"] == f"background:{C['濃黄']}"


class TestMarketCapCategory:
    """_market_cap_category のユニットテスト

    境界値 (99/100/399/400/999/1000/2999/3000) と異常値を parametrize で集約。
    """

    @pytest.mark.parametrize(
        "billion_yen, expected",
        [
            (99, "極小"),       # 100 未満
            (100, "小"),         # 境界
            (399, "小"),
            (400, "中"),         # 境界
            (999, "中"),
            (1000, "大"),        # 境界
            (2999, "大"),
            (3000, "特大"),      # 境界
            (None, None),
            ("abc", None),       # 非数値
        ],
    )
    def test_category(self, billion_yen, expected):
        assert helpers._market_cap_category(billion_yen) == expected


class TestFormatPer:
    """_format_per のユニットテスト (二桁以上は整数、一桁は小数1桁)

    境界 10 / 一桁 / 二桁 / 不正値 を parametrize で集約。
    """

    @pytest.mark.parametrize(
        "value, expected",
        [
            (10, "10"),       # 境界 (二桁扱い)
            (10.0, "10"),
            (25.6, "26"),     # 四捨五入
            (9.9, "9.9"),     # 一桁
            (3, "3.0"),       # 整数でも一桁は小数表記
            (0, "0.0"),
            (-3.5, "-3.5"),   # 負値
            (None, "—"),
            ("25", "—"),      # 不正値
        ],
    )
    def test_format(self, value, expected):
        assert helpers._format_per(value) == expected


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
        assert diff == "-2/18"

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


class TestListPortfolioWithIndicators:
    """list_portfolio_with_indicators のソート / status_query / status_label 検証。

    外部参照 (_bulk_get_stock_data, _bulk_resolve_stock_names, compute_cell_styles) は
    monkeypatch でスタブし、並び順とフィールド埋めだけを検証する。
    """

    @pytest.fixture
    def stub_externals(self, monkeypatch):
        """rank を rec から直接読めるよう _extract_indicators_for_portfolio もスタブ。"""
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: {c: {} for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names", lambda codes: {c: f"name_{c}" for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_name_prevs", lambda codes: {c: None for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_overall_ratings", lambda codes: {c: "" for c in codes})
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

    def test_empty_gyoutai_goes_to_end(self, stub_externals):
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
            self._make("0001", "1保", rank=1, themes=["A"]),
            self._make("0002", "2準", rank=2, themes=["B"]),
            self._make("0003", "3監", rank=3, themes=["C"]),
        ]
        rows = helpers.list_portfolio_with_indicators(records)
        by_code = {r["code_s"]: r for r in rows}
        assert by_code["0001"]["status_query"] == "hold"
        assert by_code["0001"]["status_label"] == "保有"
        assert by_code["0002"]["status_query"] == "semi"
        assert by_code["0002"]["status_label"] == "準保有"
        assert by_code["0003"]["status_query"] == "watch"
        assert by_code["0003"]["status_label"] == "監視"

    def test_overall_rating_filled_from_research_shelve(self, populated_db, monkeypatch):
        rec = rs.get_research_record("3496")
        rec["overall_rating"] = "S"
        rs.upsert_research_record(rec)
        rs.upsert_research_record(rs.create_research_record("1234", "空評価"))

        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: {c: {} for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names", lambda codes: {c: "" for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_name_prevs", lambda codes: {c: None for c in codes})
        monkeypatch.setattr(helpers, "compute_cell_styles", lambda row, today: {})
        monkeypatch.setattr(helpers, "_extract_indicators_for_portfolio", lambda stock: {})
        monkeypatch.setattr(helpers, "build_stock_chart_payload", lambda stock, market_db, mode: {"svg": "", "tooltip": ""})

        records = [
            self._make("3496", "3監"),
            self._make("1234", "3監"),
            self._make("9999", "3監"),
        ]
        rows = helpers.list_portfolio_with_indicators(records)
        by_code = {r["code_s"]: r for r in rows}

        assert by_code["3496"]["overall_rating"] == "S"
        assert by_code["1234"]["overall_rating"] == ""
        assert by_code["9999"]["overall_rating"] == ""

    @pytest.mark.parametrize(
        "sort_key, expected",
        [
            ("position", ["0003", "0001", "0004", "0002"]),
            ("rank", ["0002", "0003", "0001", "0004"]),
            ("gyoutai", ["0003", "0001", "0002", "0004"]),
            ("rating", ["0002", "0003", "0001", "0004"]),
            ("rs", ["0003", "0001", "0002", "0004"]),
        ],
        ids=["position", "rank", "gyoutai", "rating", "rs"],
    )
    def test_sort_key_switches_order(self, monkeypatch, sort_key, expected):
        prices = {"0001": 1000, "0003": 500, "0004": 10000}
        monkeypatch.setattr(
            helpers, "_bulk_get_stock_data",
            lambda codes: {c: {"price": prices[c]} for c in codes if c in prices},
        )
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names", lambda codes: {c: "" for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_name_prevs", lambda codes: {c: None for c in codes})
        monkeypatch.setattr(
            helpers,
            "_bulk_resolve_overall_ratings",
            lambda codes: {"0001": "B", "0002": "S", "0003": "A", "0004": ""},
        )
        monkeypatch.setattr(helpers, "compute_cell_styles", lambda row, today: {})
        monkeypatch.setattr(helpers, "_extract_indicators_for_portfolio", lambda stock: {})

        records = [
            {**self._make("0001", "1保", rank=30, themes=["人材"]), "qty": 100, "rs_raw": 80},
            {**self._make("0002", "2準", rank=5, themes=["半導体"]), "qty": 100, "rs_raw": 70},
            {**self._make("0003", "1保", rank=10, themes=["AI"]), "qty": 300, "rs_raw": 90},
            {**self._make("0004", "1保", rank=None, themes=[]), "qty": 0, "rs_raw": None},
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key=sort_key)
        assert [r["code_s"] for r in rows] == expected

    def test_sort_by_rs_change_1d_desc_none_last(self, monkeypatch, stub_externals):
        """issue #332: 前日比RSライン騰落率 降順、None は末尾、同値はコード順。"""
        import make_stock_db
        import make_market_db
        # market_db / topix_map を truthy にして compute_rs_line 経路を通す
        # (helpers は make_market_db / make_stock_db から遅延 import するため両モジュールを差し替え)
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: {"_": 1})
        monkeypatch.setattr(make_stock_db, "_topix_close_map", lambda mdb: {"_": 1})
        rs_lines = {
            "0001": [(date(2026, 6, 23), 102.0), (date(2026, 6, 22), 100.0)],
            "0002": [(date(2026, 6, 23), 99.0), (date(2026, 6, 22), 100.0)],
            "0003": [(date(2026, 6, 23), 102.0), (date(2026, 6, 22), 100.0)],
            "0004": [(date(2026, 6, 23), 100.0)],
        }
        monkeypatch.setattr(
            make_stock_db, "compute_rs_line",
            lambda stock, mdb, topix_map=None: rs_lines[stock["code_s"]],
        )
        monkeypatch.setattr(
            helpers, "_bulk_get_stock_data",
            lambda codes: {c: {"code_s": c} for c in codes},
        )
        records = [
            self._make("0002", "1保", rank=1),  # -1.0
            self._make("0004", "1保", rank=2),  # None → 末尾
            self._make("0003", "1保", rank=3),  # 2.0 (0001 と同値、コード順で後)
            self._make("0001", "1保", rank=4),  # 2.0
        ]
        rows = helpers.list_portfolio_with_indicators(records, sort_key="rs_change_1d")
        # 降順: 2.0 (0001, 0003 コード順) → -1.0 (0002) → None (0004) 末尾
        assert [r["code_s"] for r in rows] == ["0001", "0003", "0002", "0004"]

    # ===== issue #269: position_ratio 集計 =====
    @pytest.mark.parametrize(
        "records, prices, expected",
        [
            # ケース1: 1保 2銘柄、最大ポジションが 100%、もう1つが 25%
            (
                [
                    ("0001", "1保", 100),   # 1000 * 100 = 100000
                    ("0002", "1保", 25),    # 1000 * 25  = 25000
                ],
                {"0001": 1000, "0002": 1000},
                {"0001": 100.0, "0002": 25.0},
            ),
            # ケース2: 1保 と 2準 が混在 → 2準 は集計外、ratio=0
            (
                [
                    ("0001", "1保", 100),
                    ("0002", "2準", 100),
                ],
                {"0001": 500, "0002": 9999},
                {"0001": 100.0, "0002": 0.0},
            ),
            # ケース3: qty=0 / price=None → position_value=0、ratio=0
            (
                [
                    ("0001", "1保", 0),
                    ("0002", "1保", 100),
                ],
                {"0001": 1000, "0002": None},  # 0002 は price なし
                {"0001": 0.0, "0002": 0.0},
            ),
            # ケース4: 全 1保 が qty=0 → max=0 で全 ratio=0
            (
                [
                    ("0001", "1保", 0),
                    ("0002", "1保", 0),
                ],
                {"0001": 1000, "0002": 1000},
                {"0001": 0.0, "0002": 0.0},
            ),
        ],
        ids=["two-1ho-25pct", "exclude-2jun", "qty0-or-no-price", "all-zero"],
    )
    def test_position_ratio_computed(self, monkeypatch, records, prices, expected):
        # _bulk_get_stock_data を price 付き dict 返却に差し替え
        monkeypatch.setattr(
            helpers, "_bulk_get_stock_data",
            lambda codes: {c: ({"price": prices.get(c)} if prices.get(c) is not None else {}) for c in codes},
        )
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names", lambda codes: {c: "" for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_name_prevs", lambda codes: {c: None for c in codes})
        monkeypatch.setattr(helpers, "_bulk_resolve_overall_ratings", lambda codes: {c: "" for c in codes})
        monkeypatch.setattr(helpers, "compute_cell_styles", lambda row, today: {})
        monkeypatch.setattr(helpers, "_extract_indicators_for_portfolio", lambda stock: {})

        recs = [
            {"code_s": c, "status": s, "qty": q, "rank": None, "memo": {"gyoutai_themes": []}}
            for (c, s, q) in records
        ]
        rows = helpers.list_portfolio_with_indicators(recs)
        by_code = {r["code_s"]: r for r in rows}
        for code, exp_ratio in expected.items():
            assert by_code[code]["position_ratio"] == pytest.approx(exp_ratio), (
                f"{code}: expected ratio {exp_ratio}, got {by_code[code]['position_ratio']}"
            )


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


class TestPriceRsSparkline:
    """株価 + RSライン 統合スパークライン (issue #227)"""

    # --- compute_slope_per_day ---

    def test_slope_positive_linear_series(self):
        # 等差 100 → 110: 傾き 1/日, 末尾110で正規化 → ≒ 0.91%/日
        values = list(range(100, 111))  # 11点
        slope = helpers.compute_slope_per_day(values)
        assert slope is not None
        assert 0.85 < slope < 0.95

    def test_slope_negative_linear_series(self):
        values = list(range(110, 99, -1))  # 110→100
        slope = helpers.compute_slope_per_day(values)
        assert slope is not None
        assert slope < 0

    def test_slope_constant_series_is_zero(self):
        slope = helpers.compute_slope_per_day([100, 100, 100, 100])
        assert slope == 0.0

    def test_slope_too_few_points_returns_none(self):
        assert helpers.compute_slope_per_day([100]) is None
        assert helpers.compute_slope_per_day([]) is None

    # --- normalize_minmax ---

    def test_normalize_range_is_within_height(self):
        ys = helpers.normalize_minmax([1.0, 2.0, 3.0, 4.0], height=20)
        assert min(ys) == 0.0
        assert max(ys) == 20.0
        # 昇順入力 → SVG y は降順 (top = 0 が最大値)
        assert ys[0] > ys[-1]

    def test_normalize_constant_series_centers(self):
        ys = helpers.normalize_minmax([5.0, 5.0, 5.0], height=20)
        assert ys == [10.0, 10.0, 10.0]

    def test_normalize_empty_returns_empty(self):
        assert helpers.normalize_minmax([], height=20) == []

    # --- to_log_scale ---

    def test_log_scale_returns_natural_log(self):
        import math
        result = helpers.to_log_scale([100, 200, 400])
        assert abs(result[0] - math.log(100)) < 1e-9
        assert abs(result[1] - math.log(200)) < 1e-9
        assert abs(result[2] - math.log(400)) < 1e-9
        # 100→200 と 200→400 (どちらも +100%) は log 空間で同じ距離になる
        assert abs((result[1] - result[0]) - (result[2] - result[1])) < 1e-9

    def test_log_scale_empty_or_nonpositive_returns_empty(self):
        assert helpers.to_log_scale([]) == []
        assert helpers.to_log_scale([100, 0, 200]) == []
        assert helpers.to_log_scale([-1, 100]) == []

    # --- to_base_index ---

    def test_base_index_first_is_one(self):
        result = helpers.to_base_index([100, 110, 105, 120])
        assert result[0] == 1.0
        assert result[1] == 1.1
        assert abs(result[2] - 1.05) < 1e-9
        assert result[3] == 1.2

    def test_base_index_empty_or_zero_returns_empty(self):
        assert helpers.to_base_index([]) == []
        assert helpers.to_base_index([0, 10, 20]) == []
        assert helpers.to_base_index([-5, 10]) == []

    # --- normalize_shared_y ---

    def test_shared_y_aligns_start_points(self):
        # 株価: +20% 上昇, RS: +5% 上昇 → 同じ起点から終点が大きく乖離
        price = [100.0, 110.0, 120.0]
        rs = [1.0, 1.025, 1.05]
        ys = helpers.normalize_shared_y([price, rs], height=20)
        # 起点 (index=0) は両者とも先頭=1.0 に揃うので、共通スケール上で同じ y 座標
        assert ys[0][0] == ys[1][0]
        # 終点は乖離する (株価が 1.20、RSが 1.05)
        assert ys[0][-1] != ys[1][-1]
        # 株価の方が大きく上昇 → SVG y が小さい (上に伸びる)
        assert ys[0][-1] < ys[1][-1]

    def test_shared_y_constant_series_returns_center(self):
        ys = helpers.normalize_shared_y([[100, 100, 100], [1.0, 1.0, 1.0]], height=20)
        assert ys[0] == [10.0, 10.0, 10.0]
        assert ys[1] == [10.0, 10.0, 10.0]

    def test_shared_y_empty_input_returns_empty_each(self):
        ys = helpers.normalize_shared_y([[], []], height=20)
        assert ys == [[], []]

    # --- build_price_rs_chart_mini ---

    def _make_log(self, values, base_date=None):
        """新しい順の (date, value) タプル列を組み立てる (テスト用)"""
        from datetime import date as _d, timedelta
        if base_date is None:
            base_date = _d(2026, 5, 15)
        return [(base_date - timedelta(days=i), v) for i, v in enumerate(values)]

    def test_mini_chart_returns_svg_for_sufficient_data(self):
        price_log = self._make_log([110, 108, 106, 104, 102, 100])  # 新しい順
        rs_line = self._make_log([1.10, 1.08, 1.06, 1.04, 1.02, 1.00])
        svg, tooltip = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=False)
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "polyline" in svg
        assert "株価:" in tooltip
        assert "RSライン乖離:" in tooltip

    def test_mini_chart_insufficient_data_returns_dash(self):
        svg, _ = helpers.build_price_rs_chart_mini([], [], has_blue_dot=False)
        assert svg == "—"

    def test_mini_chart_blue_dot_uses_larger_circle(self):
        price_log = self._make_log([110, 108, 106, 104, 102, 100])
        rs_line = self._make_log([1.10, 1.08, 1.06, 1.04, 1.02, 1.00])
        svg_with, _ = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=True)
        svg_without, _ = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=False)
        # Blue Dot は #1976d2 (青) の大きい circle
        assert '#1976d2' in svg_with
        assert 'r="2.5"' in svg_with
        assert 'r="2.5"' not in svg_without

    def test_mini_chart_tooltip_includes_blue_dot_when_present(self):
        price_log = self._make_log([110, 108, 106, 104, 102, 100])
        rs_line = self._make_log([1.10, 1.08, 1.06, 1.04, 1.02, 1.00])
        _, tooltip = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=True)
        assert "新高値" in tooltip
        _, tooltip2 = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=False)
        assert "新高値" not in tooltip2

    # --- build_price_rs_chart_full ---

    def test_full_chart_renders_rs_line(self):
        """株価系列は廃止。RSライン (点線) のみ描画され tooltip は RSライン乖離を持つ。"""
        price_log = self._make_log(list(range(120, 100, -1)))  # 20点 (新しい順 = 降順)
        rs_line = self._make_log([1.20 - i * 0.01 for i in range(20)])
        svg, tooltip = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        assert "<svg" in svg
        # RS点線 (全期間 + 末尾5週重ね描き) = polyline は 2 本以上
        assert svg.count("<polyline") >= 2
        assert "stroke-dasharray" in svg  # RSライン点線
        assert "株価:" not in tooltip  # 株価行は廃止
        assert "RSライン乖離:" in tooltip

    # --- issue #332: 前日比 (1日比) は mini (portfolio) のみ tooltip に出す ---

    def test_mini_chart_tooltip_includes_prev_change_but_full_does_not(self):
        # rs_line 末尾2点 (最新 1.10 / 前日 1.08) → 前日比 +1.9%。mini にだけ出る。
        price_log = self._make_log([110, 108, 106, 104, 102, 100])
        rs_line = self._make_log([1.10, 1.08, 1.06, 1.04, 1.02, 1.00])
        _, mini_tooltip = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=False)
        assert "前日比:" in mini_tooltip
        assert "+1.9%" in mini_tooltip
        # full (詳細ページ週足) には出さない (issue #332: 対象外への波及防止)
        _, full_tooltip = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        assert "前日比:" not in full_tooltip

    @pytest.mark.parametrize("rs_values,expected", [
        ([1.00, 1.05], "+5.0%"),      # 古い順 [前日, 最新] → 上昇
        ([1.05, 1.00], "-4.8%"),      # 下落
        ([1.05], "—"),                # 2点未満
        ([0.0, 1.00], "—"),           # 前日値0 → 0除算回避
    ])
    def test_format_prev_change(self, rs_values, expected):
        assert helpers._format_prev_change(rs_values) == expected

    def test_change_from_desc_series_requires_exact_window(self):
        """N日比ソートは N+1 本未満なら短い期間で代替しない。"""
        series = [
            (date(2026, 6, 23), 110.0),
            (date(2026, 6, 22), 100.0),
            (date(2026, 6, 19), 90.0),
        ]
        assert helpers._change_from_desc_series(series, 1) == pytest.approx(10.0)
        assert helpers._change_from_desc_series(series, 2) == pytest.approx(22.2222222)
        assert helpers._change_from_desc_series(series, 5) is None

    def test_full_chart_includes_date_labels(self):
        price_log = self._make_log(list(range(120, 100, -1)))
        rs_line = self._make_log([1.20 - i * 0.01 for i in range(20)])
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        # 当日 = 5/15 ラベルが出る
        assert "05/15" in svg

    def test_build_payload_with_none_market_db_renders_rs_rank_history(self):
        """build_stock_chart_payload は market_db=None でも rs_rank_log があれば
        RS(0~99)履歴で SVG を返す (空チャート回帰を避ける)。

        株価系列廃止に伴い旧「価格のみフォールバック」契約を置き換える。
        market_db が無いと RSライン (対TOPIX) は算出できないが、rs_rank_log は
        stock 単体に持つので RS履歴 (右軸) は描ける。週足軸の土台に price_week_log を使う。
        """
        from datetime import date as _d, timedelta
        base = _d(2026, 5, 15)
        stock = {
            "price_log": [(base - timedelta(days=i), 100 + i) for i in range(20)],
            "price_week_log": [(base - timedelta(days=i * 7), 100 + i) for i in range(20)],
            # 直近 6 営業日分の RS(0~99) 履歴 (右軸に重畳される)
            "rs_rank_log": [(base - timedelta(days=i), 70 - i) for i in range(6)],
        }
        payload = helpers.build_stock_chart_payload(stock, market_db=None, mode="full")
        assert payload["svg"]  # 空文字でない
        assert "<svg" in payload["svg"]
        assert payload["blue_dot"] is False
        # RS履歴の右軸色 (紫) と 25 刻みスナップの軸目盛りが出る
        assert helpers._RS_RANK_COLOR in payload["svg"]
        assert "株価:" not in payload["tooltip"]  # 株価行は廃止
        # tooltip に RS(0~99) 現在値が出る (末尾 = 70)
        assert "RS(0~99): 70" in payload["tooltip"]

    @pytest.mark.parametrize("values,expected", [
        ([60, 72, 68, 94], (50, 99)),   # 50台~90台 → 50~99
        ([20, 35, 28, 40], (0, 50)),    # 0台~40台 → 0~50
        ([60, 70], (50, 75)),           # 1帯内 → 50~75 (1帯ぶん確保)
        ([95, 99], (75, 99)),           # 最上帯 → 75~99 (hi=99で上限clamp)
    ])
    def test_rs_rank_axis_bounds_snaps_to_25(self, values, expected):
        """右軸レンジは min-max を 25 刻み境界にスナップ。1帯内/最上帯も帯幅を確保。"""
        assert helpers._rs_rank_axis_bounds(values) == expected

    def test_full_chart_empty_when_no_rs_line_and_no_rs_rank(self):
        """株価線廃止後、RSライン不可 (rs_line 空) かつ RS履歴2点未満なら空SVG。
        週足 price_log が2本あっても軸・凡例だけのデータ無しチャート枠を出さない。
        """
        price_log = self._make_log(list(range(120, 100, -1)))  # 週足20点ぶん
        svg, tooltip = helpers.build_price_rs_chart_full(
            price_log, [], has_blue_dot=False, rs_rank_log=[])
        assert svg == ""
        assert tooltip == ""

    def test_full_chart_t20_label_matches_displayed_window(self):
        """price_log が _SPARK_LOOKBACK (20) を超える場合、左端ラベルは
        '表示窓内の最古日 (20日前)' であって '全履歴の最古日' ではない。
        (codex review 対応: 履歴が長い銘柄で左端ラベルがチャート期間とずれる回帰防止)
        """
        from datetime import date as _d, timedelta
        # 30営業日分の price_log (新しい順)。base_date=5/15
        base = _d(2026, 5, 15)
        price_log = [(base - timedelta(days=i), 100 + i) for i in range(30)]
        rs_line = [(base - timedelta(days=i), 1.0 + i * 0.01) for i in range(30)]
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        # 当日 = 5/15
        assert "05/15" in svg
        # 表示左端 = price_log[19] = 5/15 - 19日 = 4/26
        t20_date = (base - timedelta(days=19)).strftime("%m/%d")
        assert t20_date in svg
        # 全履歴最古 = price_log[29] = 5/15 - 29日 = 4/16 は出ないこと (回帰防止)
        t30_date = (base - timedelta(days=29)).strftime("%m/%d")
        assert t30_date not in svg

    def test_full_chart_blue_dot_renders_blue_circle(self):
        price_log = self._make_log(list(range(120, 100, -1)))
        rs_line = self._make_log([1.20 - i * 0.01 for i in range(20)])
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=True)
        # Blue Dot は r=4 の青円
        assert 'r="4.0"' in svg and '#1976d2' in svg

    def test_full_chart_empty_input_returns_empty(self):
        svg, tooltip = helpers.build_price_rs_chart_full([], [], has_blue_dot=False)
        assert svg == ""
        assert tooltip == ""

    def test_full_chart_renders_axis_labels(self):
        """左Y軸 = RSライン % (灰)、線・末尾現在値は RS系列色 (青)。株価系列は廃止。"""
        price_log = self._make_log(list(range(120, 100, -1)))  # 20点: 101..120
        rs_line = self._make_log([1.20 - i * 0.01 for i in range(20)])  # 1.01..1.20
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        # 左Y軸は % 表記
        assert "%" in svg
        # RS系列色 (青) が線・末尾ラベルに使われる
        assert "#1976d2" in svg
        # 凡例が新仕様 (RSライン左軸 / RS右軸) であること
        assert "RSライン" in svg and "RS (0~99/右軸)" in svg

    # --- codex review 対応: 5日基準のずれ / 短期履歴ガイドはみ出し ---

    def test_total_change_5day_matches_six_point_window(self):
        """_format_total_change(values, 5) は「5営業日前→今日」の変化を返す。

        既存の (20,5) 指標 (offset=5) と一致するよう、6点窓 (tail[0]→tail[-1]) で算出する。
        従来は 5点窓 = 4営業日変化になっていた回帰の防止。
        """
        # 7 点: 末尾から 6 本目 (index=1) → 今日 (index=6) で +6.0%
        values = [100.0, 100.0, 101.0, 102.0, 103.0, 104.0, 106.0]
        result = helpers._format_total_change(values, 5)
        assert result == "+6.0%"

    def test_total_change_20day_uses_21_point_window_when_available(self):
        """_format_total_change(values, 20) は 20営業日変化として 21点窓を取る。

        20点しか無い場合は全 20点で計算する (= 19営業日変化, 既存実装維持)。
        """
        # 21 点ちょうど: index=0 → index=20 で +20.0%
        values = [100.0 + i for i in range(21)]
        result = helpers._format_total_change(values, 20)
        assert result == "+20.0%"

    def test_mini_chart_t5_point_uses_5_business_days_ago(self):
        """mini チャートの t-5 点は 5営業日前 (= asc[-6]) を参照する。

        従来は asc[-5] (= 4営業日前) を取っていた回帰の防止。
        中間 y 座標が「5営業日前の値」に対応していることを確認する。
        """
        # 7 点入力 (新しい順): t-0=106, t-1=104, ... , t-5=100, t-6=99
        # asc は古い順 = [99, 100, 101, 102, 103, 104, 106]
        # t5 = asc[-6] = 100 (= 5営業日前)。 asc[-5]=101 (= 4営業日前) ではない。
        price_log = self._make_log([106, 104, 103, 102, 101, 100, 99])
        rs_line = self._make_log([1.06, 1.04, 1.03, 1.02, 1.01, 1.00, 0.99])
        svg, _ = helpers.build_price_rs_chart_mini(price_log, rs_line, has_blue_dot=False)
        # polyline が描画されている = 3点取得が成功
        assert "polyline" in svg
        # 中間点の y 座標を検証: log(99)=4.595, log(100)=4.605, log(101)=4.615, log(106)=4.663
        # 5営業日前=100 を採用すれば log 空間で「下から 2 番目」相当 (asc-internal 0-1 で 0.147)
        # 4営業日前=101 を採用すれば 0.293
        # mini パネルは 60% inner_h = (24-6)*0.6 = 10.8 高さ
        # 違いは小さいので、ここでは t-5 点で計算した値と直接比較する
        import math
        log_price = [math.log(v) for v in [99, 100, 101, 102, 103, 104, 106]]
        t5_expected = log_price[-6]  # = log(100)
        t5_wrong = log_price[-5]     # = log(101)
        # 正規化後の y を再現
        lo, hi = min(log_price), max(log_price)
        # mini の panel: inner_h = 24-6 = 18, price_panel_h = (18-1)*0.6 = 10.2
        panel_h = (18 - 1) * 0.6
        pad_y = 3
        y_t5_expected = pad_y + panel_h * (hi - t5_expected) / (hi - lo)
        y_t5_wrong = pad_y + panel_h * (hi - t5_wrong) / (hi - lo)
        # SVG に「中間点」の y 座標が含まれることを polyline points から抽出
        # points="X1,Y1 X2,Y2 X3,Y3" の真ん中 Y2 が y_t5_expected に近いことを確認
        import re
        m = re.search(r'<polyline points="([^"]+)" fill="none"\s+stroke="#[0-9a-f]+" stroke-width="1.5"', svg)
        assert m is not None, f"price polyline not found: {svg[:300]}"
        pts = m.group(1).split()
        assert len(pts) == 3
        _, y2_str = pts[1].split(",")
        y2 = float(y2_str)
        # 正解側 (5営業日前=100) との差は小さく、誤り側 (4営業日前=101) より近い
        assert abs(y2 - y_t5_expected) < abs(y2 - y_t5_wrong), (
            f"中間点 y={y2} が 5営業日前 (期待 {y_t5_expected:.2f}) ではなく "
            f"4営業日前 ({y_t5_wrong:.2f}) に近い"
        )

    def test_full_chart_t5_guide_omitted_for_short_history(self):
        """短期履歴 (2 本) の銘柄でも 5日ガイドが viewBox 外に飛び出さない。

        従来は x_t5 = pad_x - 3*inner_w 等 SVG 左外側に出てレイアウトを壊していた。
        本数が _SPARK_RECENT+1 に満たない場合は 5日ガイドを省略し、
        本体チャートだけは描画する。
        """
        from datetime import date as _d, timedelta
        base = _d(2026, 5, 15)
        # たった 2 点 (新しい順)
        price_log = [(base, 100), (base - timedelta(days=1), 95)]
        rs_line = [(base, 1.0), (base - timedelta(days=1), 0.95)]
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        # 本体ポリラインは描画されている
        assert "<polyline" in svg
        # ガイド線の x1 がすべて pad_x (= 36, 左端) 以上、x_today (= 36+inner_w) 以下に収まる
        import re
        # viewBox 0 0 400 120 / pad_left=36 / pad_right=8 / inner_w = 400-44 = 356
        # (重ね描き化で右側軸ラベルが不要になり pad_right 縮小)
        pad_left = 36
        pad_right = 8
        inner_w = 400 - pad_left - pad_right
        x_today = pad_left + inner_w  # = 392
        guides = re.findall(r'<line x1="([-0-9.]+)" y1="[0-9.]+" x2="[-0-9.]+" y2="[0-9.]+"\s+stroke="#e0e0e0"', svg)
        assert len(guides) >= 2  # 少なくとも t20 と today のガイド
        for gx in guides:
            x = float(gx)
            assert pad_left - 0.5 <= x <= x_today + 0.5, f"ガイド線が viewBox 外: x={x}"

    def test_full_chart_t5_label_omitted_for_short_history(self):
        """短期履歴 (3本) で 5日ラベルも省略される (t20 と重なる位置に出ない)。"""
        from datetime import date as _d, timedelta
        base = _d(2026, 5, 15)
        # 3 本 = 2 営業日変化
        price_log = [(base - timedelta(days=i), 100 + i) for i in range(3)]
        rs_line = [(base - timedelta(days=i), 1.0 + i * 0.01) for i in range(3)]
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        # 5/15 (today) ラベルは出る
        assert "05/15" in svg
        # 左端 = price_log[2] = 5/13
        assert "05/13" in svg

    def test_full_chart_t5_guide_present_for_normal_history(self):
        """通常 20 本データでは 5日ガイド (t5) が描画される。"""
        price_log = self._make_log(list(range(120, 100, -1)))  # 20点
        rs_line = self._make_log([1.20 - i * 0.01 for i in range(20)])
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=False)
        # ガイド線 3 本 (t20, t5, today) 描画されること
        import re
        guides = re.findall(r'stroke="#e0e0e0" stroke-width="0.5"', svg)
        assert len(guides) == 3
        # t-5 営業日前のラベル: 5/15 - 5 = 5/10
        assert "05/10" in svg

    # ==================================================
    # 週足チャート (issue #239) 用テスト
    # ==================================================
    def _make_week_log(self, values, base_friday=None, week_step=7):
        """新しい順の (date, value) タプル列を週足金曜日付で組み立てる"""
        from datetime import date as _d, timedelta
        if base_friday is None:
            base_friday = _d(2026, 5, 15)  # 金曜
        return [(base_friday - timedelta(days=i * week_step), float(v))
                for i, v in enumerate(values)]

    @pytest.mark.parametrize("case_id,has_new_daily_stock,has_new_daily_topix,empty_weekly,topix_weekly_leads,expect_provisional,expect_empty_svg", [
        ("a_caseA_provisional", True, True, False, False, True, False),
        # TOPIX 日足が週足末尾と同 ISO 週でも、銘柄日足が新しければ最新化する (>= 緩和)
        ("b_topix_daily_same_week", True, False, False, False, True, False),
        ("c_both_daily_same_week", False, False, False, False, False, False),
        ("d_caseC_no_weekly", True, True, True, False, False, True),
        # TOPIX 週足が当日分まで進んでいる非対称ケース。銘柄週足を基準に provisional を許可
        ("e_topix_weekly_leads", True, True, False, True, True, False),
    ])
    def test_full_chart_payload_provisional_requires_both_daily_logs(
        self, case_id, has_new_daily_stock, has_new_daily_topix, empty_weekly,
        topix_weekly_leads, expect_provisional, expect_empty_svg,
    ):
        """build_stock_chart_payload の mode='full': 仮終値の最新化は銘柄週足基準で判定。

        日足 ISO 週が銘柄週足の最新 ISO 週「以上」なら最新化する (>= 緩和)。TOPIX 日足が
        週足末尾と同 ISO 週でも銘柄日足が新しければ最新化する (b_topix_daily_same_week)。
        Case C (銘柄週足空) は空 SVG を返す (build_price_rs_chart_full の 2 点未満
        早期 return 仕様と整合、初回更新サイクル後に Case A/B 経路で自動復帰)。
        TOPIX 週足が当日分を含む非対称ケース (e_topix_weekly_leads) でも、
        銘柄週足の最新 ISO 週より日足が新しければ provisional を追加する。
        """
        from datetime import date as _d, timedelta
        # 20 週分の週足 (週足金曜日付、降順、5/15 が最新)
        weekly_values = [100 + i * 1.0 for i in range(20)]
        topix_weekly_values = [1000 + i * 5.0 for i in range(20)]
        stock_week = [] if empty_weekly else self._make_week_log(weekly_values)
        # TOPIX 週足: topix_weekly_leads=True なら銘柄週足より1週進んだ位置で組む
        latest_friday = _d(2026, 5, 15)
        new_day = latest_friday + timedelta(days=4)  # 翌週火曜 (= 仮終値想定)
        if empty_weekly:
            topix_week = []
        elif topix_weekly_leads:
            topix_week = self._make_week_log(topix_weekly_values, base_friday=new_day)
        else:
            topix_week = self._make_week_log(topix_weekly_values)
        # 日足: stock は新日付 / topix は条件次第
        daily_stock = [(new_day, 200.0)] if has_new_daily_stock else [(latest_friday, 120.0)]
        daily_topix = [(new_day, 1100.0)] if has_new_daily_topix else [(latest_friday, 1095.0)]

        stock = {"price_week_log": stock_week, "price_log": daily_stock}
        market_db = {"topix": {"price_week_log": topix_week, "price_log": daily_topix}}

        payload = helpers.build_stock_chart_payload(stock, market_db, mode="full")

        if expect_empty_svg:
            assert payload["svg"] == ""
            assert payload["tooltip"] == ""
            return
        assert payload["svg"] != ""
        assert "<svg" in payload["svg"]
        assert "週" in payload["tooltip"]  # 週足表記
        if expect_provisional:
            # 末尾日付ラベルが日足 new_day (5/19) の表記
            assert new_day.strftime("%m/%d") in payload["svg"]
        else:
            # 仮終値追加なし → 末尾日付ラベルは週足末尾 (5/15)
            assert latest_friday.strftime("%m/%d") in payload["svg"]

    @pytest.mark.parametrize("rs_values,expect_blue_dot", [
        # 末尾が過去 20 週中最大 (21 本、先頭が最大)
        ([1.50] + [1.40 - i * 0.01 for i in range(20)], True),
        # 末尾が最大ではない (先頭 < 過去のどれか)
        ([1.20] + [1.40 - i * 0.01 for i in range(20)], False),
    ])
    def test_full_chart_blue_dot_weekly_lookback_20(self, rs_values, expect_blue_dot):
        """Blue Dot は週足 21 本入力で「先頭 > max(過去 20 週)」のみ True"""
        price_log = self._make_week_log([100 + i for i in range(21)])
        rs_line = self._make_week_log(rs_values)
        svg, _ = helpers.build_price_rs_chart_full(price_log, rs_line, has_blue_dot=expect_blue_dot)
        # Blue Dot は r=4.0 の青円で描画される (helpers.py:_BLUE_DOT)
        if expect_blue_dot:
            assert 'r="4.0"' in svg
        else:
            assert 'r="4.0"' not in svg


class TestBuildTrendInfoMissing:
    """trend_template が欠損している銘柄を「完全通過 (◎)」と誤表示しないこと"""

    @pytest.mark.parametrize("stock,expected_expr", [
        # キー欠損 (DB に trend_template が無い既存銘柄)
        ({"price": 1000}, "—"),
        # 値が None (取得失敗で None が入っている)
        ({"trend_template": None}, "—"),
        # 旧フォーマットの文字列 "-" (移行残存)
        ({"trend_template": "-"}, "—"),
        # 正常: 空 list (= 不通過 0 件) は ◎ のまま
        ({"trend_template": []}, "◎"),
        # 正常: 1 件不通過は ◯
        ({"trend_template": ["RS"]}, "◯"),
    ])
    def test_missing_trend_returns_em_dash_not_circle(self, stock, expected_expr):
        info = helpers.build_trend_info(stock)
        assert info["expr"] == expected_expr


class TestExtractIndicatorsTrendTemplate:
    """portfolio 一覧用のトレンド表示加工を検証する"""

    def test_full_miss_x_is_blank_but_misses_are_kept(self):
        misses = [
            "pr>ma10", "pr>ma30,40", "ma30>ma40", "ma40Up",
            "ma10>ma30,40", "high(low)52", "RS",
        ]
        indicators = helpers._extract_indicators_for_portfolio({
            "trend_template": misses,
            "price_kairi_wma10": -5.0,
        })

        assert indicators["trend_template"] == ""
        assert indicators["trend_template_misses"] == misses
        assert ">×<" not in indicators["kairi_gauge_svg"]


class TestBuildTrendInfoGauge:
    """build_trend_info() の kairi_gauge_svg と tooltip 結合を検証する (issue portfolio-trend-gauge)"""

    @pytest.mark.parametrize(
        "kairi, misses, expect_marker, expect_marker_color, expect_unpass_tooltip",
        [
            # |kairi| < 10%: 中立 (黒)、不通過0件なので「不通過:」行なし (◎扱い)
            (5, [], True, "#000", False),
            # 健全帯 +12: 淡緑 (#9be29b)、不通過1件あるので「不通過:」行あり (◯扱い)
            (12, ["RS"], True, "#9be29b", True),
            # 小割れ -3: 中立 (黒)、不通過1件あるので「不通過:」行あり
            (-3, ["RS"], True, "#000", True),
            # +25 ちょうど: 濃緑 (#2e7d32)
            (25, [], True, "#2e7d32", False),
            # 範囲外 +30: クランプ + 濃緑
            (30, [], True, "#2e7d32", False),
            # マイナス -15: 淡薄赤 (#f4c7c3)
            (-15, ["RS"], True, "#f4c7c3", True),
            # マイナス -25: 濃薄赤 (#c62828)
            (-25, ["RS"], True, "#c62828", True),
            # データ無し: kairi=None、マーカーなし、記号のみ
            (None, [], False, None, False),
        ],
    )
    def test_gauge_svg_and_tooltip(
        self, kairi, misses, expect_marker, expect_marker_color, expect_unpass_tooltip,
    ):
        stock = {"price_kairi_wma10": kairi, "trend_template": misses}
        info = helpers.build_trend_info(stock)
        svg = info["kairi_gauge_svg"]
        # 必ず SVG が返る (kairi=None でも記号のみ表示)
        assert svg.startswith("<svg")
        # 記号は <text> として含まれる
        assert "<text" in svg
        # マーカー (現在値) の有無
        # SVG line 内訳: マーカー有りなら 2 本 (白縁 + 本体)、無しなら 0 本
        line_count = svg.count("<line")
        if expect_marker:
            assert line_count == 2
            assert expect_marker_color in svg
        else:
            assert line_count == 0
        # 廃止された overheat オレンジ色 (#e67e22) は出現しない
        assert "#e67e22" not in svg
        # tooltip は常に「10WMA乖離: ...」行を含む
        assert "10WMA乖離:" in info["tooltip"]
        # 不通過項目は ◯ のときのみ含まれる
        if expect_unpass_tooltip:
            assert "不通過:" in info["tooltip"]
        else:
            assert "不通過:" not in info["tooltip"]

    def test_triangle_down_tooltip_shows_passed_conditions(self):
        """△ は不通過ではなく、少数の通過項目を tooltip に出す"""
        misses = ["pr>ma10", "pr>ma30,40", "ma30>ma40", "ma40Up", "RS"]
        info = helpers.build_trend_info({"price_kairi_wma10": 0, "trend_template": misses})

        assert info["expr"] == "△"
        assert "通過: ma10>ma30,40,high(low)52" in info["tooltip"]
        assert "不通過:" not in info["tooltip"]


class TestBuildSprGaugeForStock:
    """個別銘柄 (price 形式の sell_pressure_ratio / stddev_volatility) から
    需給バランスゲージを組み立てるテスト (issue #247 portfolio_list 展開)"""

    def test_normal_renders_svg_and_tooltip(self):
        """正常: sprs/vols が揃っている → svg は <svg> 開始、SVG 内 <title> + セル全体 tooltip に
        買い集め評価が併記される (SVG <title> がホバー時に勝つため両方に出す)"""
        # price.get_spr_expr で 週diff=10 → B / 日diff=12 → A になる入力
        stock = {
            "sell_pressure_ratio": [48, 45, 60],
            "sell_pressure_ratio_w": [40, 42, 50],
            "stddev_volatility": [2.4, 2.6],
        }
        gauge = helpers._build_spr_gauge_for_stock(stock)
        assert gauge["svg"].startswith("<svg")
        # バー単体 <title> に SPR + 買い集め評価が併記される
        assert "SPR 48 ±2.4 (20日) 買い集めB" in gauge["svg"]
        assert "SPR 45 ±2.6 (5日) 買い集めA" in gauge["svg"]
        assert gauge["svg"].index("(5日)") < gauge["svg"].index("(20日)")
        # セル全体 tooltip にも (フォールバック用) 同等情報
        assert "SPR 48 ±2.4 (20日)" in gauge["tooltip"]
        assert "買い集め 週B 日A" in gauge["tooltip"]
        # 緑バーは週B 相当の中濃緑 (#9be29b)、赤バーは薄赤 (#f4c7c3) で統一
        assert "#9be29b" in gauge["svg"]
        assert "#f4c7c3" in gauge["svg"]

    def test_missing_data_returns_em_dash(self):
        """sprs/vols 欠損: svg は '—'、tooltip は空"""
        gauge = helpers._build_spr_gauge_for_stock({})
        assert gauge["svg"] == "—"
        assert gauge["tooltip"] == ""


class TestThemeNewsMdToHtml:
    """issue #165: theme-news markdown → HTML 変換 + サニタイズ"""

    def test_converts_markdown_and_sanitizes(self):
        """見出し / 箇条書き / 太字 / リンクが HTML 化され、危険タグはエスケープされる。"""
        src = (
            "## テーマA\n"
            "- **重要**: 業績好調\n"
            "- [出典](https://example.com)\n"
            "\n"
            "<script>alert(1)</script>\n"
        )
        out = helpers.theme_news_md_to_html(src)
        assert "<h2>テーマA</h2>" in out
        assert "<li><strong>重要</strong>: 業績好調</li>" in out
        assert '<a href="https://example.com">出典</a>' in out
        # script はエスケープされる (sanitize_html 経由)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_empty_input_returns_empty(self):
        assert helpers.theme_news_md_to_html("") == ""
        assert helpers.theme_news_md_to_html(None) == ""

    def test_inserts_br_before_zenkaku_middot(self):
        """skill 出力で `・` (全角中点) 区切りの長文 li は読みづらいので、
        テキスト中の `・` 前に <br> を挿入する。先頭 `・` (li 直後) には入れない。"""
        src = "- 地合い: 日経4日続落・6万円割れ達成・米10年金利4.68%\n"
        out = helpers.theme_news_md_to_html(src)
        # 文中の ・ 前には <br> が入る
        assert "日経4日続落<br>・6万円割れ達成" in out
        assert "6万円割れ達成<br>・米10年金利" in out

    def test_strips_leading_h1_title(self):
        """skill 出力先頭の `# 見出し` (h1) は /market summary 側で日付を出しているので
        冗長 + sanitize_html で許可外タグとしてエスケープされ生文字列が見える問題を防ぐ。"""
        src = "# テーマ急上昇ニュース要約（2026-05-20）\n\n## 市場全体\n- 本文\n"
        out = helpers.theme_news_md_to_html(src)
        assert "<h1>" not in out
        assert "&lt;h1&gt;" not in out
        # 後続の h2 は残る
        assert "<h2>市場全体</h2>" in out

    def test_sources_section_collapsed_in_details(self):
        """`## Sources` 以降は <details class="sources"> で折りたたまれる。"""
        src = (
            "## 1. 蓄電池\n- 材料\n\n"
            "## Sources\n- [link1](https://example.com/a)\n- [link2](https://example.com/b)\n"
        )
        out = helpers.theme_news_md_to_html(src)
        assert '<details class="sources">' in out
        assert "<summary>📎 Sources を表示</summary>" in out
        # Sources の中身 (リンク) は details の中に入っている
        i_details = out.index('<details class="sources">')
        i_link = out.index('https://example.com/a')
        assert i_details < i_link
        # 本編の h2 は details の外に残る
        assert "<h2>1. 蓄電池</h2>" in out

    def test_inserts_wbr_before_plus_and_arrow(self):
        """日本語/数字/% に続く `+` `→` の前に <wbr> を挟む (長文折返しヒント)。"""
        src = "- 反発+1.9%→翌日続伸\n"
        out = helpers.theme_news_md_to_html(src)
        assert "反発<wbr>+1.9%<wbr>→翌日続伸" in out

    @pytest.mark.parametrize(
        "src,must_contain,must_not_contain",
        [
            # 本文の ⟨N⟩ が anchor link 化され、Sources の <li>[N] に id が付く。
            # 旧履歴の [99] (構成銘柄数) は anchor 化されず素通りすることも同時確認。
            (
                "## 1. 半導体 (+3.8%, ↑1, [99])\n"
                "- 材料: 太陽誘電ストップ高 ⟨4⟩、SBG 40兆円 ⟨3⟩⟨8⟩\n\n"
                "## Sources\n"
                "- [3] [SBG40兆円](https://example.com/sbg)\n"
                "- [4] [太陽誘電](https://example.com/tau)\n"
                "- [8] [対中関税](https://example.com/tariff)\n",
                [
                    '<a href="#thn-src-4" class="thn-footnote">[4]</a>',
                    '<a href="#thn-src-3" class="thn-footnote">[3]</a>',
                    '<a href="#thn-src-8" class="thn-footnote">[8]</a>',
                    '<li id="thn-src-3">[3] ',
                    '<li id="thn-src-4">[4] ',
                    '<li id="thn-src-8">[8] ',
                    # 構成銘柄数 [99] は anchor 化されず素通り
                    "(+3.8%, ↑1, [99])",
                ],
                [
                    # 旧表記 [99] を anchor 化していない
                    "#thn-src-99",
                    'class="thn-footnote">[99]',
                ],
            ),
        ],
    )
    def test_inline_footnotes(self, src, must_contain, must_not_contain):
        """⟨N⟩ → anchor + Sources <li>[N] → id 付与。[99] (構成銘柄数) は影響なし。"""
        out = helpers.theme_news_md_to_html(src)
        for needle in must_contain:
            assert needle in out, f"expected {needle!r} in output: {out}"
        for needle in must_not_contain:
            assert needle not in out, f"unexpected {needle!r} in output: {out}"


class TestBuildGyosekiTooltips:
    """issue #204: gyoseki_quarity_expr から portfolio 列 tooltip を生成する。"""

    @pytest.mark.parametrize("expr,expected", [
        # 通常ケース (<C3>無し)
        (
            "[A]5±8%,2±6%[Q]-5±12%,1±8%",
            {
                "sales_growth": "5年平均: 5±8%",
                "profit_growth": "5年平均: 2±6%",
                "progress_diff": "4Q平均: 売上-5±12% / 利益1±8%",
            },
        ),
        # <C3> タグ付き
        (
            "[A]5±8%,2±6%[Q]-5±12%,1±8%<C3>",
            {
                "sales_growth": "5年平均: 5±8%",
                "profit_growth": "5年平均: 2±6%",
                "progress_diff": "4Q平均: 売上-5±12% / 利益1±8% [3Q連続利益率向上]",
            },
        ),
        # 空文字・パース失敗時は全空 (Jinja 側で title 属性が出ない想定)
        ("", {"sales_growth": "", "profit_growth": "", "progress_diff": ""}),
        ("invalid", {"sales_growth": "", "profit_growth": "", "progress_diff": ""}),
    ])
    def test_tooltip_format(self, expr, expected):
        assert helpers.build_gyoseki_tooltips(expr) == expected


class TestGetCurrentResearchData:
    """issue #219: get_current_research_data のテスト。

    stocks_shelve 未登録 / 必要キー欠落の境界条件と、
    正常系でグループ構造が返ることを確認する (各グループ内容の詳細は
    build_code_rank_row のテストでカバー)。
    """

    def test_missing_stock_returns_none(self, monkeypatch):
        # stocks_shelve から空 dict が返るケース
        monkeypatch.setattr(helpers, "get_stock_data", lambda code_s: {})
        assert helpers.get_current_research_data("9999") is None

    def test_missing_score_returns_none(self, monkeypatch):
        # score_gyoseki / shihyo_pt が欠落しているケース
        monkeypatch.setattr(
            helpers, "get_stock_data",
            lambda code_s: {"stock_name": "テスト", "overview": "概要"},
        )
        assert helpers.get_current_research_data("9999") is None

    def test_normal_returns_grouped_structure(self, monkeypatch):
        # 必要最小限の stock_data。build_code_rank_row の各 helper は
        # 空 dict でも空文字を返す前提なのでスコア計算が通れば構造が出る
        stock_data = {
            "stock_name": "テスト",
            "score_gyoseki": 50,
            "shihyo_pt": 40,
            "momentum_pt": 30,
            "funda_pt": 20,
            "stock_rank_log": [],
            "themes": "",
            "sector": "情報・通信業",
            "shihyo": {},
        }
        monkeypatch.setattr(helpers, "get_stock_data", lambda code_s: stock_data)
        import make_market_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: {"theme_rank": []})
        # get_major_theme は内部で get_market_db を直接呼ぶので別途モック
        monkeypatch.setattr(make_market_db, "get_major_theme", lambda themes: "")

        result = helpers.get_current_research_data("9999")
        assert result is not None
        # 戻り値は [(group_name, [(label, value), ...]), ...]
        assert isinstance(result, list)
        group_names = [g[0] for g in result]
        # スコアグループは確実に出る (総合PT が非零)
        assert "スコア" in group_names


class TestBuildPortfolioThemeSummary:
    """build_portfolio_theme_summary のテスト (issue #283)。

    rs_line 系 (compute_rs_line_changes) と DB バルク取得は monkeypatch で
    固定値に差し替え、集約ロジック (平均 / 除外 / ソート) を検証する。
    """

    def _record(self, code_s, themes, status="3監"):
        return {"code_s": code_s, "status": status,
                "memo": {"gyoutai_themes": themes}}

    def test_basic_aggregation(self, monkeypatch):
        """2 テーマ × 3 銘柄で momentum_pt 平均/最大が期待通り。"""
        records = [
            self._record("1111", ["半導体"]),
            self._record("2222", ["半導体"]),
            self._record("3333", ["防衛"]),
        ]
        stock_data = {
            "1111": {"stock_name": "A", "momentum_pt": 80},
            "2222": {"stock_name": "B", "momentum_pt": 60},
            "3333": {"stock_name": "C", "momentum_pt": 50},
        }
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: stock_data)
        monkeypatch.setattr(
            helpers, "_bulk_resolve_stock_names",
            lambda codes: {c: stock_data[c]["stock_name"] for c in codes},
        )
        import make_market_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: None)  # rs_line スキップ

        out = helpers.build_portfolio_theme_summary(records=records, sort_key="momentum")
        by_theme = {t["theme"]: t for t in out}
        assert by_theme["半導体"]["member_count"] == 2
        assert by_theme["半導体"]["momentum_pt_avg"] == 70.0  # (80+60)/2
        assert by_theme["半導体"]["momentum_pt_max"] == 80.0
        # 半導体 (avg 70) が 防衛 (avg 50) より先 (momentum 降順)
        assert out[0]["theme"] == "半導体"

    def test_same_code_in_two_themes(self, monkeypatch):
        """同一銘柄が 2 テーマに属する場合、両テーマで集計される。"""
        records = [self._record("1111", ["半導体", "AI"])]
        stock_data = {"1111": {"stock_name": "A", "momentum_pt": 80, "price_log": []}}
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: stock_data)
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names",
                            lambda codes: {"1111": "A"})
        import make_market_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: None)

        out = helpers.build_portfolio_theme_summary(records=records)
        themes = {t["theme"] for t in out}
        assert themes == {"半導体", "AI"}
        for t in out:
            assert t["member_count"] == 1
            assert t["momentum_pt_avg"] == 80.0

    def test_missing_momentum_excluded_but_counted(self, monkeypatch):
        """momentum_pt 欠損銘柄は集計から除外されるが member_count には含まれる。"""
        records = [
            self._record("1111", ["半導体"]),
            self._record("2222", ["半導体"]),  # momentum_pt 欠損
        ]
        stock_data = {
            "1111": {"stock_name": "A", "momentum_pt": 80, "price_log": []},
            "2222": {"stock_name": "B", "price_log": []},  # momentum_pt なし
        }
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: stock_data)
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names",
                            lambda codes: {c: stock_data[c]["stock_name"] for c in codes})
        import make_market_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: None)

        out = helpers.build_portfolio_theme_summary(records=records)
        t = out[0]
        assert t["member_count"] == 2          # 欠損も数える
        assert t["momentum_pt_avg"] == 80.0     # 欠損は平均から除外
        assert len(t["leaders"]) == 1           # momentum_pt がある銘柄のみ

    def test_dev_aggregation_excludes_none(self, monkeypatch):
        """短期の勢い: rs_line データ不足銘柄 (None) は平均から除外される。"""
        records = [
            self._record("1111", ["半導体"]),
            self._record("2222", ["半導体"]),
        ]
        stock_data = {
            "1111": {"stock_name": "A", "momentum_pt": 80, "price_log": []},
            "2222": {"stock_name": "B", "momentum_pt": 60, "price_log": []},
        }
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: stock_data)
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names",
                            lambda codes: {c: stock_data[c]["stock_name"] for c in codes})
        import make_market_db
        import make_stock_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: {"topix": {}})
        monkeypatch.setattr(make_stock_db, "_topix_close_map", lambda mdb: {"d": 1.0})
        # 公開 API compute_rs_line_changes を stock 名から (A, B, D) 乖離率を返すよう差し替え。
        # A=有効/B=None/D=None, A=有効/B=有効/D=有効 を返し分け
        dev_map = {"A": (2.0, None, None), "B": (4.0, 8.0, 1.0)}
        monkeypatch.setattr(
            make_stock_db, "compute_rs_line_changes",
            lambda stock, mdb, topix_map=None: dev_map[stock["stock_name"]],
        )
        out = helpers.build_portfolio_theme_summary(records=records)
        t = out[0]
        assert t["dev_a_avg"] == 3.0            # (2.0 + 4.0) / 2
        assert t["dev_b_avg"] == 8.0            # B のみ有効 (A の B は None で除外)
        assert t["dev_1d_avg"] == 1.0           # B のみ有効 (A の D は None で除外)

    @pytest.mark.parametrize("sort_key,expected_first", [
        ("momentum", "強"),    # momentum_pt 平均が高いテーマが先頭
        ("dev_1d", "急"),      # dev_1d (前日比) 平均が高いテーマが先頭
        ("dev_a", "急"),       # dev_a (短期の勢い) 平均が高いテーマが先頭
        ("dev_b", "急"),       # dev_b (20日乖離) 平均が高いテーマが先頭
    ])
    def test_sort_key_switch(self, monkeypatch, sort_key, expected_first):
        """sort_key で momentum / dev_1d / dev_a / dev_b の並び順が切り替わる。"""
        records = [
            self._record("1111", ["強"]),   # momentum 高, 勢い 低
            self._record("2222", ["急"]),   # momentum 低, 勢い 高
        ]
        stock_data = {
            "1111": {"stock_name": "S", "momentum_pt": 90, "price_log": []},
            "2222": {"stock_name": "Q", "momentum_pt": 40, "price_log": []},
        }
        monkeypatch.setattr(helpers, "_bulk_get_stock_data", lambda codes: stock_data)
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names",
                            lambda codes: {c: stock_data[c]["stock_name"] for c in codes})
        import make_market_db
        import make_stock_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: {"topix": {}})
        monkeypatch.setattr(make_stock_db, "_topix_close_map", lambda mdb: {"d": 1.0})
        dev_map = {"S": (1.0, 1.0, 0.5), "Q": (9.0, 9.0, 3.0)}
        monkeypatch.setattr(make_stock_db, "compute_rs_line_changes",
                            lambda stock, mdb, topix_map=None: dev_map[stock["stock_name"]])

        out = helpers.build_portfolio_theme_summary(records=records, sort_key=sort_key)
        assert out[0]["theme"] == expected_first

    def test_position_value_aggregation(self, monkeypatch):
        """ポジション集計: 1保のみ計上、テーマ無し1保は分母のみ、2テーマは50/50按分、max正規化。"""
        records = [
            {"code_s": "1111", "status": "1保", "qty": 100,
             "memo": {"gyoutai_themes": ["半導体"]}},        # 100,000
            {"code_s": "2222", "status": "1保", "qty": 10,
             "memo": {"gyoutai_themes": ["半導体", "AI"]}},   # 50,000 → 両テーマに25,000ずつ按分
            {"code_s": "3333", "status": "2準", "qty": 100,
             "memo": {"gyoutai_themes": ["防衛"]}},           # 2準 → 計上しない
            {"code_s": "4444", "status": "1保", "qty": 50,
             "memo": {"gyoutai_themes": []}},                 # テーマ無し 100,000 → 分母のみ
        ]
        stock_data = {
            "1111": {"stock_name": "A", "momentum_pt": 80, "price": 1000},
            "2222": {"stock_name": "B", "momentum_pt": 60, "price": 5000},
            "3333": {"stock_name": "C", "momentum_pt": 50, "price": 1000},
            "4444": {"stock_name": "D", "momentum_pt": 40, "price": 2000},
        }
        # codes でフィルタ: テーマ無し 1保 (4444) が fetch 対象に入ることも検証する
        monkeypatch.setattr(helpers, "_bulk_get_stock_data",
                            lambda codes: {c: stock_data[c] for c in codes if c in stock_data})
        monkeypatch.setattr(helpers, "_bulk_resolve_stock_names",
                            lambda codes: {c: stock_data[c]["stock_name"] for c in codes})
        import make_market_db
        monkeypatch.setattr(make_market_db, "get_market_db", lambda: None)

        out = helpers.build_portfolio_theme_summary(records=records)
        by_theme = {t["theme"]: t for t in out}
        # 分母 = 100,000 + 50,000 + 100,000 (テーマ無し) = 250,000
        assert by_theme["半導体"]["position_value"] == 125000.0  # 100,000 + 50,000/2
        assert by_theme["半導体"]["position_pct"] == 50.0
        assert by_theme["半導体"]["position_ratio"] == 100.0  # 最大テーマ
        assert by_theme["AI"]["position_value"] == 25000.0      # 50,000/2
        assert by_theme["AI"]["position_pct"] == 10.0
        assert by_theme["AI"]["position_ratio"] == 20.0
        assert by_theme["防衛"]["position_value"] == 0.0
        assert by_theme["防衛"]["position_ratio"] == 0.0


# ==================================================
# issue #253: signal セル tooltip/背景色・チャートマーカー
# ==================================================
class TestSignalDisplay:
    """_build_signal_display の強度/色・_resolve_signal_markers の週マップ"""

    def _anchor(self):
        # extract_signals は access_date_price を get_price_day() で anchor 化し、
        # かつ (today - anchor_day) で stale 判定する。テストは anchor を基準に
        # mmdd を作り delta を安定させる。
        # 素の datetime.now() だと CI(UTC) の午前実行で get_price_day の17時
        # カットオーバーにより anchor_day が前日(日曜)になり、曜日依存の按分
        # テストが崩れる。そこで「今日付近の直近平日 20:00」に正規化して
        # 実行時刻・TZ に依存させない (stale 判定も today-anchor が小さく通る)。
        from datetime import datetime, timedelta
        from ks_util import get_price_day
        today = datetime.now().date()
        wd = today.weekday()
        if wd >= 5:  # 土(5)/日(6) は直近金曜へ
            today -= timedelta(days=wd - 4)
        now = datetime(today.year, today.month, today.day, 20, 0)  # 平日20:00
        return now, get_price_day(now)

    def _stock_with_signal(self, kind, num, days_ago):
        anchor_now, anchor_day = self._anchor()
        from datetime import timedelta
        mmdd = (anchor_day - timedelta(days=days_ago)).strftime("%m/%d")
        key = "pocket_pivot" if kind == "ポ" else "breakout"
        return {key: ["%s,%d" % (mmdd, num)], "trend_template": [],
                "access_date_price": anchor_now}

    @pytest.mark.parametrize(
        "kind, num, days_ago, expect_word, alpha_high",
        [
            ("ブ", 200, 0, "強", True),    # per>=200 強・直近 → 濃い
            ("ブ", 199, 0, "中", False),   # per=199 中 → 強より薄い
            ("ポ", 0, 1, "強", True),      # MA10乖離0 強・直近
            ("ポ", -5, 6, "弱", False),    # 乖離-5 弱・古い → 薄い
        ],
    )
    def test_strength_and_alpha(self, kind, num, days_ago,
                                expect_word, alpha_high):
        stock = self._stock_with_signal(kind, num, days_ago)
        disp = helpers._build_signal_display(stock)
        assert expect_word in disp["tooltip"]
        # alpha を style 文字列から抽出 (rgba(...,A))
        import re
        m = re.search(r"rgba\(234,67,53,([0-9.]+)\)", disp["style"])
        assert m is not None
        alpha = float(m.group(1))
        assert (alpha >= 0.8) is alpha_high

    def test_format_signal_uses_recent_marks_only_but_keeps_full_title(self):
        """表示記号は直近ポ/ブのみ、title用全文は make_signal の signal を保持する"""
        from datetime import timedelta
        anchor_now, anchor_day = self._anchor()
        stock = {
            "pocket_pivot": ["%s,0" % (anchor_day - timedelta(days=1)).strftime("%m/%d")],
            "breakout": ["%s,180" % (anchor_day - timedelta(days=10)).strftime("%m/%d")],
            "trend_template": [],
            "access_date_price": anchor_now,
            "sell_pressure_ratio": [50, 80, 40, 2.5, 1.8],
            "rs_raw": 0.5,
        }
        mark, full = helpers._format_signal(stock)
        assert mark == "ポ/ブ"
        assert "[ポ]" in full
        assert "[ブ]" in full
        assert "[買過]" in full

    def test_resolve_markers_x_interp_and_drop(self):
        """発生日を週バー間で日割り按分し、10日超でも窓内なら描画、窓外はdrop"""
        from datetime import timedelta
        anchor_now, anchor_day = self._anchor()
        # 週足バー日付 (昇順): 直近4週 (各週の代表日として週初の月曜)
        monday = anchor_day - timedelta(days=anchor_day.weekday())  # 今週月曜
        window_dates = [monday - timedelta(weeks=k) for k in (3, 2, 1, 0)]
        xs = [10.0, 20.0, 30.0, 40.0]
        # ポ: 先週の半ば (バー間の按分で xs[2]=30 と xs[3]=40 の中間付近)
        po_day = (monday - timedelta(days=3)).strftime("%m/%d")  # 先週金曜
        # ブ: 11日前でもチャート窓内なら描画される
        old_day = (anchor_day - timedelta(days=11)).strftime("%m/%d")
        stock = {"pocket_pivot": ["%s,0" % po_day],
                 "breakout": ["%s,180" % old_day],
                 "trend_template": [], "access_date_price": anchor_now}
        markers = helpers._resolve_signal_markers(stock, window_dates, xs)
        kinds = {m["kind"]: m for m in markers}
        assert "ポ" in kinds
        assert "ブ" in kinds
        # 先週金曜は xs[2](先週月)と xs[3](今週月)の間 → 週バーにスナップせず按分
        assert 30.0 < kinds["ポ"]["x"] < 40.0
        assert 30.0 <= kinds["ブ"]["x"] < 40.0

    def test_chart_markers_render_and_size(self):
        """build_price_rs_chart_full: ポ三角/ブダイヤが線より後 (前面) に描画・強度でサイズ可変"""
        from datetime import timedelta
        anchor_now, anchor_day = self._anchor()
        price_log = [(anchor_day - timedelta(weeks=i), 1000 + i) for i in range(20)]
        mmdd = anchor_day.strftime("%m/%d")

        def _stock(pp=None, bo=None):
            s = {"trend_template": [], "access_date_price": anchor_now}
            if pp is not None:
                s["pocket_pivot"] = pp
            if bo is not None:
                s["breakout"] = bo
            return s

        # RSライン (polyline) を出すため rs_line を与える (株価系列は廃止済み)
        rs_line = [(anchor_day - timedelta(weeks=i), 1.0 + i * 0.01) for i in range(20)]
        svg, _ = helpers.build_price_rs_chart_full(
            price_log, rs_line, False,
            stock=_stock(pp=["%s,0" % mmdd], bo=["%s,180" % mmdd]))
        assert "#2e7d32" in svg and "#f57c00" in svg  # ポ緑・ブ橙
        # マーカー (polygon) は polyline 群より後 = 最前面
        assert svg.rindex("polygon") > svg.rindex("polyline")
        # tooltip に発生日 (M/D, ゼロ埋めなし) を明示する (週足チャートの
        # X 位置だけでは発生日が読み取りづらいため)
        sig_md = "%d/%d" % (anchor_day.month, anchor_day.day)
        assert "<title>ポ %s " % sig_md in svg
        assert "<title>ブ %s " % sig_md in svg
        # ポの強度でサイズが変わる: 強(乖離0, size6) vs 弱(乖離-5, size3)
        svg_strong, _ = helpers.build_price_rs_chart_full(
            price_log, rs_line, False, stock=_stock(pp=["%s,0" % mmdd]))
        svg_weak, _ = helpers.build_price_rs_chart_full(
            price_log, rs_line, False, stock=_stock(pp=["%s,-5" % mmdd]))
        assert svg_strong != svg_weak  # サイズ差で polygon 座標が変わる
        # stock なしなら polygon は出ない
        svg2, _ = helpers.build_price_rs_chart_full(price_log, [], False)
        assert "#f57c00" not in svg2

    def test_chart_marker_band_uses_signal_tooltip_only(self):
        """詳細チャートはRS hover面とシグナルマーカー帯を分離する。"""
        from datetime import timedelta
        anchor_now, anchor_day = self._anchor()
        price_log = [(anchor_day - timedelta(weeks=i), 1000 + i) for i in range(20)]
        rs_line = [(anchor_day - timedelta(weeks=i), 1.0 + i * 0.01) for i in range(20)]
        mmdd = anchor_day.strftime("%m/%d")
        stock = {
            "trend_template": [],
            "access_date_price": anchor_now,
            "breakout": ["%s,180" % mmdd],
        }

        svg, tooltip = helpers.build_price_rs_chart_full(price_log, rs_line, False, stock=stock)

        assert '<g pointer-events="none">' in svg
        assert '<rect x="0" y="0"' in svg
        assert "<title>%s</title></rect>" % html.escape(tooltip) in svg
        assert 'fill-opacity="0.001"' in svg
        assert "<title>ブ %d/%d " % (anchor_day.month, anchor_day.day) in svg


class TestBuildTrendInfoMa10:
    """トレンド列の10日MA乖離マーカー (通常は黒点線、30日10ma維持期間ありで赤太点線)"""

    @pytest.mark.parametrize("kairi_ma10,streak,expect_marker,expect_red", [
        (5.0, False, True, False),    # 通常: 黒の点線
        (5.0, True, True, True),      # 30日10ma維持期間あり: 赤の太点線
        (None, False, False, False),  # 10ma乖離なし: マーカー描かれない
    ])
    def test_ma10_marker(self, kairi_ma10, streak, expect_marker, expect_red):
        stock = {
            "trend_template": [],          # ◎ (全通過) で symbol が出る
            "price_kairi_wma10": 3.0,
            "price_kairi_ma10": kairi_ma10,
            "ma10_above_streak_30": streak,
        }
        info = helpers.build_trend_info(stock)
        svg = info["kairi_gauge_svg"]
        # 通常も赤も点線 (dasharray)。マーカー有無は乖離値の有無で決まる
        assert ("stroke-dasharray" in svg) is expect_marker
        assert ("#c62828" in svg) is expect_red  # 赤は streak のときだけ
        # tooltip に10日MA乖離行が入る
        assert "10日MA乖離:" in info["tooltip"]
        assert ("赤太点線: 10ma 30日維持中" in info["tooltip"]) is streak


# _classify_market_category (運用総額の市場別内訳)
# ==================================================
@pytest.mark.parametrize("market,is_nikkei225,expected", [
    ("東証Ｐ", True, "日経225"),        # 225優先
    ("東証Ｐ", False, "TOPIX"),          # プライム非225 (実DB短縮形)
    ("東証Ｇ", False, "グロース"),       # グロース (実DB短縮形)
    ("東証Ｇ", True, "日経225"),         # 225はグロースより優先 (実際上はまず無いが仕様確認)
    ("東証Ｓ", False, "その他"),         # スタンダード
    ("名証Ｍ", False, "その他"),         # 地方市場
    ("東証プライム", False, "TOPIX"),    # 長い表記も吸収
    ("東証グロース", False, "グロース"), # 長い表記も吸収
    ("", False, "その他"),               # market 空
    (None, False, "その他"),             # market None
])
def test_classify_market_category(market, is_nikkei225, expected):
    assert helpers._classify_market_category(market, is_nikkei225) == expected


def test_classify_market_category_legacy_nikkei225_cache(monkeypatch):
    """旧DBで is_nikkei225 が無い場合はHTMLキャッシュ判定で日経225に補完する。"""
    monkeypatch.setattr(
        helpers,
        "_is_nikkei225_from_cached_master_html",
        lambda code_s: code_s == "7203",
    )

    assert helpers._classify_market_category("東証Ｐ", None, code_s="7203") == "日経225"
    assert helpers._classify_market_category("東証Ｐ", None, code_s="9999") == "TOPIX"
    # 更新済みDBの明示 False は尊重し、キャッシュ補完しない
    assert helpers._classify_market_category("東証Ｐ", False, code_s="7203") == "TOPIX"


# ==================================================
# issue #361: 概算損益・成績サマリー
# ==================================================

def _ep(hold_date="2026-05-01", sell_date="2026-05-11", hold_price=1000,
        hold_qty=100, sell_price=1200, sell_qty=100, qty_changes=None):
    return {
        "hold_date": hold_date, "sell_date": sell_date,
        "hold_price": hold_price, "hold_qty": hold_qty,
        "sell_price": sell_price, "sell_qty": sell_qty,
        "qty_changes": qty_changes or [],
    }


@pytest.mark.parametrize("ep, expect", [
    # 単純: 1000→1200 = +20%、暦日10日
    (_ep(), {"return_pct": 20.0, "hold_days": 10, "profit_amount": 20000, "profit_per_share": 200}),
    # 単一 IN で hold_qty=None でも sell_qty があれば概算損益額を出す
    (_ep(hold_qty=None), {"return_pct": 20.0, "hold_days": 10, "profit_amount": 20000, "profit_per_share": 200}),
    # 株数が全く無い旧ログは損益率のみ出し、損益額は出さない
    (_ep(hold_qty=None, sell_qty=None), {"return_pct": 20.0, "hold_days": 10, "profit_amount": None, "profit_per_share": 200}),
    # 買い増し加重: 100株@1000 + 100株@1400 → 平均1200、売値1200 = 0%
    (_ep(hold_qty=100, sell_price=1200, sell_qty=200,
         qty_changes=[{"price": 1400, "after_qty": 200}]),
     {"return_pct": 0.0, "hold_days": 10, "profit_amount": 0, "profit_per_share": 0}),
    # 減玉あり (200→100) → None
    (_ep(hold_qty=200, qty_changes=[{"price": 1100, "after_qty": 100}]), None),
    # 買い増しがあるのに hold_qty=None (加重不能) → None
    (_ep(hold_qty=None, qty_changes=[{"price": 1400, "after_qty": 200}]), None),
    # 買い増しの price 欠損 → None
    (_ep(hold_qty=100, qty_changes=[{"price": None, "after_qty": 200}]), None),
    # 売却価格なし → None
    (_ep(sell_price=None), None),
])
def test_calc_episode_pl(ep, expect):
    result = helpers.calc_episode_pl(ep)
    if expect is None:
        assert result is None
    else:
        assert result is not None
        assert round(result["return_pct"], 4) == expect["return_pct"]
        assert result["hold_days"] == expect["hold_days"]
        assert result["profit_amount"] == expect["profit_amount"]
        assert result["profit_per_share"] == expect["profit_per_share"]


@pytest.mark.parametrize("pls, checks", [
    # 勝ち負け混在: +20%(amt100) 勝ち, -10%(amt100) 負け → win_rate50, payoff 20/10=2.0
    ([{"return_pct": 20, "hold_days": 5, "amount": 100},
      {"return_pct": -10, "hold_days": 15, "amount": 100}],
     {"win_rate": 50.0, "payoff_ratio": 2.0, "n_win": 1, "n_lose": 1}),
    # 0% は負け扱い
    ([{"return_pct": 0, "hold_days": 3, "amount": 100}],
     {"win_rate": 0.0, "n_win": 0, "n_lose": 1}),
    # 負け0件 → payoff None
    ([{"return_pct": 20, "hold_days": 5, "amount": 100}],
     {"win_rate": 100.0, "payoff_ratio": None, "n_win": 1, "n_lose": 0}),
])
def test_calc_trade_summary(pls, checks):
    s = helpers.calc_trade_summary(pls)
    assert s is not None
    for k, v in checks.items():
        assert s[k] == v


def test_calc_trade_summary_empty_returns_none():
    assert helpers.calc_trade_summary([]) is None
