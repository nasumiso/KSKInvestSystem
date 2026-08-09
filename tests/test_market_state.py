"""market_state.py のテスト (issue #117 Part A)"""

import pytest
import market_state as ms


# ==================================================
# expire_distribution_days
# ==================================================
class TestExpireDistributionDays:
    """DD 失効処理"""

    def _history(self, n=30):
        """直近 n 日のダミー履歴 (新しい日が先頭、YYMMDD)"""
        # 26/04/30, 26/04/29, ..., で先頭に並べる (休日無視のシンプル形式)
        return ["26/04/%02d" % (30 - i) for i in range(n)]

    def test_recent_dd_kept(self):
        """直近のDD (5日前) は維持される"""
        history = self._history(30)
        dd_list = [(history[5], 100.0)]  # 5日前のDD
        valid = ms.expire_distribution_days(dd_list, today_close=99.0, daily_history=history)
        assert valid == [(history[5], 100.0)]

    def test_25_days_old_expires(self):
        """25取引日経過したDDは失効"""
        history = self._history(30)
        dd_list = [(history[25], 100.0)]  # ちょうど25日前
        valid = ms.expire_distribution_days(dd_list, today_close=99.0, daily_history=history)
        assert valid == []

    def test_24_days_old_kept(self):
        """24取引日前のDDはまだ有効"""
        history = self._history(30)
        dd_list = [(history[24], 100.0)]
        valid = ms.expire_distribution_days(dd_list, today_close=99.0, daily_history=history)
        assert valid == [(history[24], 100.0)]

    def test_5pct_recovery_expires(self):
        """5%上昇で失効"""
        history = self._history(30)
        dd_list = [(history[3], 100.0)]
        valid = ms.expire_distribution_days(dd_list, today_close=105.0, daily_history=history)
        assert valid == []

    def test_below_5pct_kept(self):
        """5%未満の上昇では失効しない"""
        history = self._history(30)
        dd_list = [(history[3], 100.0)]
        valid = ms.expire_distribution_days(dd_list, today_close=104.99, daily_history=history)
        assert len(valid) == 1

    def test_dd_outside_history_expires(self):
        """daily_history に含まれないDDは失効扱い"""
        history = self._history(30)
        dd_list = [("25/01/01", 100.0)]  # 履歴範囲外
        valid = ms.expire_distribution_days(dd_list, today_close=99.0, daily_history=history)
        assert valid == []

    def test_empty_dd_list(self):
        """空リストは空のまま"""
        history = self._history(30)
        valid = ms.expire_distribution_days([], today_close=99.0, daily_history=history)
        assert valid == []


# ==================================================
# update_rally_attempt
# ==================================================
class TestUpdateRallyAttempt:
    """ラリーアテンプト追跡"""

    def test_day1_starts_when_higher_close(self):
        """前日より高引けで Day 1 開始"""
        meta = {"rally_attempt_start_date": None, "rally_attempt_start_low": None}
        today = {"date": "26/04/30", "close": 1000, "low": 990}
        prev = {"date": "26/04/29", "close": 980, "low": 970}
        result = ms.update_rally_attempt(meta, today, prev)
        assert result["rally_attempt_start_date"] == "26/04/30"
        assert result["rally_attempt_start_low"] == 990

    def test_no_start_when_lower_close(self):
        """前日より低引けでは Day 1 開始しない"""
        meta = {"rally_attempt_start_date": None, "rally_attempt_start_low": None}
        today = {"date": "26/04/30", "close": 970, "low": 960}
        prev = {"date": "26/04/29", "close": 980, "low": 970}
        result = ms.update_rally_attempt(meta, today, prev)
        assert result["rally_attempt_start_date"] is None

    def test_reset_on_low_break(self):
        """ラリー追跡中に Day 1 安値割れでリセット"""
        meta = {"rally_attempt_start_date": "26/04/25", "rally_attempt_start_low": 990}
        today = {"date": "26/04/30", "close": 985, "low": 980}  # low < 990
        prev = {"date": "26/04/29", "close": 988, "low": 985}
        result = ms.update_rally_attempt(meta, today, prev)
        assert result["rally_attempt_start_date"] is None
        assert result["rally_attempt_start_low"] is None

    def test_continue_when_low_holds(self):
        """ラリー追跡中、Day 1 安値を保てば継続"""
        meta = {"rally_attempt_start_date": "26/04/25", "rally_attempt_start_low": 990}
        today = {"date": "26/04/30", "close": 1010, "low": 995}  # low >= 990
        prev = {"date": "26/04/29", "close": 1005, "low": 1000}
        result = ms.update_rally_attempt(meta, today, prev)
        assert result["rally_attempt_start_date"] == "26/04/25"
        assert result["rally_attempt_start_low"] == 990


# ==================================================
# check_follow_through_day
# ==================================================
class TestCheckFollowThroughDay:
    """FTD 判定"""

    def _history(self, n=30):
        return ["26/04/%02d" % (30 - i) for i in range(n)]

    def test_ftd_succeeds_on_day4(self):
        """Day 4 (start から3日経過)、+1.5%、出来高増 → FTD成立"""
        history = self._history(30)
        rally_meta = {
            "rally_attempt_start_date": history[3],  # 3日前 = Day 1
            "rally_attempt_start_low": 990,
        }
        today = {"date": history[0], "close": 1015, "low": 1000, "volume": 1500}
        prev = {"close": 1000, "volume": 1000}
        assert ms.check_follow_through_day(today, prev, rally_meta, history) is True

    def test_ftd_fails_too_early(self):
        """Day 3 (まだ Day 4 に達していない) では成立しない"""
        history = self._history(30)
        rally_meta = {
            "rally_attempt_start_date": history[2],  # 2日前
            "rally_attempt_start_low": 990,
        }
        today = {"date": history[0], "close": 1015, "low": 1000, "volume": 1500}
        prev = {"close": 1000, "volume": 1000}
        assert ms.check_follow_through_day(today, prev, rally_meta, history) is False

    def test_ftd_fails_below_threshold(self):
        """Day 4、+0.9% (閾値1.0%未達) → 非成立"""
        history = self._history(30)
        rally_meta = {
            "rally_attempt_start_date": history[3],
            "rally_attempt_start_low": 990,
        }
        today = {"date": history[0], "close": 1009, "low": 1000, "volume": 1500}
        prev = {"close": 1000, "volume": 1000}
        assert ms.check_follow_through_day(today, prev, rally_meta, history) is False

    def test_ftd_fails_volume_decrease(self):
        """Day 4、上昇率十分でも出来高減なら非成立"""
        history = self._history(30)
        rally_meta = {
            "rally_attempt_start_date": history[3],
            "rally_attempt_start_low": 990,
        }
        today = {"date": history[0], "close": 1020, "low": 1000, "volume": 900}
        prev = {"close": 1000, "volume": 1000}
        assert ms.check_follow_through_day(today, prev, rally_meta, history) is False

    def test_ftd_fails_when_low_break(self):
        """Day 1 安値を割っていたら非成立"""
        history = self._history(30)
        rally_meta = {
            "rally_attempt_start_date": history[3],
            "rally_attempt_start_low": 990,
        }
        today = {"date": history[0], "close": 1015, "low": 985, "volume": 1500}  # low < 990
        prev = {"close": 1000, "volume": 1000}
        assert ms.check_follow_through_day(today, prev, rally_meta, history) is False

    def test_ftd_fails_no_rally_attempt(self):
        """ラリー追跡していなければ非成立"""
        history = self._history(30)
        rally_meta = {"rally_attempt_start_date": None, "rally_attempt_start_low": None}
        today = {"date": history[0], "close": 1020, "low": 1000, "volume": 1500}
        prev = {"close": 1000, "volume": 1000}
        assert ms.check_follow_through_day(today, prev, rally_meta, history) is False


# ==================================================
# derive_state
# ==================================================
class TestDeriveState:
    """状態遷移"""

    def test_init_when_few_dd(self):
        """初期状態: DD < 4 で confirmed_uptrend"""
        state, trigger = ms.derive_state(prev_state=None, valid_dd_count=2, ftd_today=False)
        assert state == ms.CONFIRMED_UPTREND
        assert trigger == "init"

    def test_init_when_4_dd(self):
        """初期状態: DD = 4 で uptrend_under_pressure"""
        state, trigger = ms.derive_state(prev_state=None, valid_dd_count=4, ftd_today=False)
        assert state == ms.UPTREND_UNDER_PRESSURE
        assert trigger == "init"

    def test_init_when_6_dd(self):
        """初期状態: DD >= 6 で market_in_correction"""
        state, trigger = ms.derive_state(prev_state=None, valid_dd_count=6, ftd_today=False)
        assert state == ms.MARKET_IN_CORRECTION
        assert trigger == "init"

    def test_correction_to_confirmed_via_ftd(self):
        """Correction → Confirmed: FTD成立"""
        state, trigger = ms.derive_state(
            prev_state=ms.MARKET_IN_CORRECTION, valid_dd_count=3, ftd_today=True
        )
        assert state == ms.CONFIRMED_UPTREND
        assert trigger == "ftd"

    def test_correction_stays(self):
        """Correction → Correction: FTD 未成立"""
        state, trigger = ms.derive_state(
            prev_state=ms.MARKET_IN_CORRECTION, valid_dd_count=3, ftd_today=False
        )
        assert state == ms.MARKET_IN_CORRECTION

    def test_confirmed_to_pressure_at_4dd(self):
        """Confirmed → Pressure: 有効DD = 4"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=4, ftd_today=False
        )
        assert state == ms.UPTREND_UNDER_PRESSURE
        assert trigger == "dd>=4"

    def test_confirmed_to_correction_at_6dd(self):
        """Confirmed → Correction: 有効DD = 6"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=6, ftd_today=False
        )
        assert state == ms.MARKET_IN_CORRECTION
        assert trigger == "dd>=6"

    def test_confirmed_stays_at_3dd(self):
        """Confirmed → Confirmed: 有効DD = 3 (4未満)"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=3, ftd_today=False
        )
        assert state == ms.CONFIRMED_UPTREND

    def test_pressure_to_correction_at_6dd(self):
        """Pressure → Correction: 有効DD = 6"""
        state, trigger = ms.derive_state(
            prev_state=ms.UPTREND_UNDER_PRESSURE, valid_dd_count=6, ftd_today=False
        )
        assert state == ms.MARKET_IN_CORRECTION

    def test_pressure_to_confirmed_recover(self):
        """Pressure → Confirmed: 有効DD < 4 で復帰"""
        state, trigger = ms.derive_state(
            prev_state=ms.UPTREND_UNDER_PRESSURE, valid_dd_count=3, ftd_today=False
        )
        assert state == ms.CONFIRMED_UPTREND
        assert trigger == "dd<4_recover"

    def test_pressure_stays_at_4dd(self):
        """Pressure → Pressure: 有効DD = 4 (Correction にも復帰にも当たらない)"""
        state, trigger = ms.derive_state(
            prev_state=ms.UPTREND_UNDER_PRESSURE, valid_dd_count=4, ftd_today=False
        )
        assert state == ms.UPTREND_UNDER_PRESSURE


# ==================================================
# is_below_10ma_clearly (issue #117 Part B)
# ==================================================
class TestIsBelow10maClearly:
    """週足10MA 明確割れ判定"""

    @pytest.mark.parametrize("kairi,expected", [
        (-0.5, False),   # 閾値より上
        (-1.0, True),    # 境界 (-1.0% で明確割れ)
        (-1.5, True),
        (1.0, False),
        (0, False),
        (None, False),   # 欠損・不正値は False
        ("foo", False),
    ])
    def test_is_below_10ma_clearly(self, kairi, expected):
        assert ms.is_below_10ma_clearly(kairi) is expected


# ==================================================
# derive_state with below_10ma (issue #117 Part B 補助遷移)
# ==================================================
class TestDeriveStateWith10maAux:
    """週足10MA 補助遷移ルール"""

    def test_confirmed_to_pressure_via_below_10ma(self):
        """DD<4 でも 10MA明確割れで pressure 降格"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=2, ftd_today=False,
            below_10ma=True,
        )
        assert state == ms.UPTREND_UNDER_PRESSURE
        assert trigger == "below_10ma"

    def test_confirmed_to_correction_via_dd4_and_below_10ma(self):
        """DD=4 + 10MA明確割れ で correction 降格 (correction優先)"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=4, ftd_today=False,
            below_10ma=True,
        )
        assert state == ms.MARKET_IN_CORRECTION
        assert trigger == "dd>=4_and_below_10ma"

    def test_confirmed_stays_pressure_when_dd4_above_10ma(self):
        """DD=4 + 10MA上 → pressure 降格 (correction にはならない、現状挙動維持)"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=4, ftd_today=False,
            below_10ma=False,
        )
        assert state == ms.UPTREND_UNDER_PRESSURE
        assert trigger == "dd>=4"

    def test_confirmed_to_correction_via_dd6_ignores_10ma(self):
        """DD≥6 なら 10MA関係なく correction (DD>=6が最優先)"""
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=6, ftd_today=False,
            below_10ma=False,
        )
        assert state == ms.MARKET_IN_CORRECTION
        assert trigger == "dd>=6"

    def test_pressure_stays_when_dd_low_below_10ma(self):
        """DD<4 だが 10MA明確割れ → pressure 維持 (復帰しない)"""
        state, trigger = ms.derive_state(
            prev_state=ms.UPTREND_UNDER_PRESSURE, valid_dd_count=2, ftd_today=False,
            below_10ma=True,
        )
        assert state == ms.UPTREND_UNDER_PRESSURE
        assert trigger == "stay"

    def test_pressure_to_confirmed_when_dd_low_and_above_10ma(self):
        """DD<4 + 10MA上 → confirmed 復帰"""
        state, trigger = ms.derive_state(
            prev_state=ms.UPTREND_UNDER_PRESSURE, valid_dd_count=2, ftd_today=False,
            below_10ma=False,
        )
        assert state == ms.CONFIRMED_UPTREND
        assert trigger == "dd<4_recover"

    def test_correction_to_confirmed_via_ftd_ignores_10ma(self):
        """FTD出れば10MA関係なく confirmed"""
        state, trigger = ms.derive_state(
            prev_state=ms.MARKET_IN_CORRECTION, valid_dd_count=10, ftd_today=True,
            below_10ma=True,
        )
        assert state == ms.CONFIRMED_UPTREND
        assert trigger == "ftd"

    def test_below_10ma_default_false_backward_compat(self):
        """引数省略で既存挙動維持 (10MAルール不発動)"""
        # confirmed + DD=2 + below_10ma 引数なし → confirmed 維持
        state, trigger = ms.derive_state(
            prev_state=ms.CONFIRMED_UPTREND, valid_dd_count=2, ftd_today=False,
        )
        assert state == ms.CONFIRMED_UPTREND
        assert trigger == "stay"


# ==================================================
# append_state_history
# ==================================================
class TestAppendStateHistory:
    """状態履歴管理"""

    def test_append_to_empty(self):
        """空履歴に追加"""
        h = ms.append_state_history([], "26/04/30", ms.CONFIRMED_UPTREND, "init")
        assert h == [("26/04/30", ms.CONFIRMED_UPTREND, "init")]

    def test_caps_at_max(self):
        """STATE_HISTORY_MAX 件で打ち切り"""
        h = [("26/04/%02d" % i, ms.CONFIRMED_UPTREND, "stay") for i in range(35)]
        h2 = ms.append_state_history(h, "26/05/01", ms.MARKET_IN_CORRECTION, "dd>=6")
        assert len(h2) == ms.STATE_HISTORY_MAX
        assert h2[0] == ("26/05/01", ms.MARKET_IN_CORRECTION, "dd>=6")

    def test_replaces_same_date(self):
        """同じ日付のエントリは置き換え"""
        h = [("26/04/30", ms.CONFIRMED_UPTREND, "init")]
        h2 = ms.append_state_history(h, "26/04/30", ms.MARKET_IN_CORRECTION, "dd>=6")
        assert len(h2) == 1
        assert h2[0] == ("26/04/30", ms.MARKET_IN_CORRECTION, "dd>=6")

    def test_handles_none_history(self):
        """history が None でも動く"""
        h = ms.append_state_history(None, "26/04/30", ms.CONFIRMED_UPTREND, "init")
        assert h == [("26/04/30", ms.CONFIRMED_UPTREND, "init")]

    def test_preserves_transition_trigger_on_same_day_stay_overwrite(self):
        """同日 2 回呼ばれた時、後から "stay" で上書きすると遷移系 trigger が失われる
        バグへの回帰防止。state が同じで既存が遷移系なら trigger を保持する。

        シナリオ: 当日 cron で under_pressure → confirmed_uptrend に遷移して "dd<4_recover"
        が記録される。同日に make_market_db が再実行されると、prev_state が既に
        confirmed になっているため derive_state は "stay" を返すが、append_state_history
        はそれを無視して既存 "dd<4_recover" を維持する。
        """
        h = [("26/05/14", ms.CONFIRMED_UPTREND, "dd<4_recover")]
        h2 = ms.append_state_history(h, "26/05/14", ms.CONFIRMED_UPTREND, "stay")
        assert h2[0] == ("26/05/14", ms.CONFIRMED_UPTREND, "dd<4_recover")

    def test_replaces_when_state_changes_on_same_day(self):
        """同日でも state が変わった場合は新規 entry で置換 (遷移を記録する)。"""
        h = [("26/05/14", ms.CONFIRMED_UPTREND, "stay")]
        h2 = ms.append_state_history(h, "26/05/14", ms.UPTREND_UNDER_PRESSURE, "dd>=4")
        assert h2[0] == ("26/05/14", ms.UPTREND_UNDER_PRESSURE, "dd>=4")


# ==================================================
# to_direction_signal
# ==================================================
class TestToDirectionSignal:
    """direction_signal 文字列生成"""

    def test_format(self):
        s = ms.to_direction_signal(ms.CONFIRMED_UPTREND, "26/04/30")
        assert s == "confirmed_uptrend,26/04/30"


# ==================================================
# format_state_label (Part B)
# ==================================================
class TestFormatStateLabel:
    """state を日本語ラベルに変換"""

    @pytest.mark.parametrize("state,expected", [
        (ms.CONFIRMED_UPTREND, "上昇トレンド"),
        (ms.UPTREND_UNDER_PRESSURE, "圧力下"),
        (ms.MARKET_IN_CORRECTION, "調整相場"),
        (None, ""),
        ("foo_bar", "foo_bar"),   # 未知の state はそのまま返す
    ])
    def test_format_state_label(self, state, expected):
        assert ms.format_state_label(state) == expected


# ==================================================
# find_state_transition_date (Part B)
# ==================================================
class TestFindStateTransitionDate:
    """state_history から現在 state の遷移日を抽出"""

    def test_basic(self):
        history = [
            ("26/05/01", ms.CONFIRMED_UPTREND, "stay"),
            ("26/04/30", ms.CONFIRMED_UPTREND, "stay"),
            ("26/04/15", ms.CONFIRMED_UPTREND, "ftd"),
            ("26/04/14", ms.MARKET_IN_CORRECTION, "stay"),
        ]
        assert ms.find_state_transition_date(history, ms.CONFIRMED_UPTREND) == "26/04/15"

    def test_single_match(self):
        history = [("26/05/01", ms.CONFIRMED_UPTREND, "init")]
        assert ms.find_state_transition_date(history, ms.CONFIRMED_UPTREND) == "26/05/01"

    def test_single_mismatch(self):
        history = [("26/05/01", ms.MARKET_IN_CORRECTION, "init")]
        assert ms.find_state_transition_date(history, ms.CONFIRMED_UPTREND) is None

    def test_empty(self):
        assert ms.find_state_transition_date([], ms.CONFIRMED_UPTREND) is None

    def test_no_match(self):
        history = [
            ("26/05/01", ms.MARKET_IN_CORRECTION, "stay"),
            ("26/04/30", ms.UPTREND_UNDER_PRESSURE, "dd>=6"),
        ]
        assert ms.find_state_transition_date(history, ms.CONFIRMED_UPTREND) is None

    def test_state_changed_today(self):
        """今日 state が変わった場合 → 今日の日を返す"""
        history = [
            ("26/05/01", ms.CONFIRMED_UPTREND, "ftd"),
            ("26/04/30", ms.MARKET_IN_CORRECTION, "stay"),
        ]
        assert ms.find_state_transition_date(history, ms.CONFIRMED_UPTREND) == "26/05/01"


# ==================================================
# extract_ftd_history (Part B)
# ==================================================
class TestExtractFtdHistory:
    """state_history から FTD 成立日を抽出"""

    def test_basic(self):
        history = [
            ("26/05/01", ms.CONFIRMED_UPTREND, "stay"),
            ("26/04/15", ms.CONFIRMED_UPTREND, "ftd"),
            ("26/04/14", ms.MARKET_IN_CORRECTION, "stay"),
            ("26/03/01", ms.CONFIRMED_UPTREND, "ftd"),
        ]
        assert ms.extract_ftd_history(history) == ["26/04/15", "26/03/01"]

    def test_max_count(self):
        history = [
            ("26/05/01", ms.CONFIRMED_UPTREND, "ftd"),
            ("26/04/01", ms.CONFIRMED_UPTREND, "ftd"),
            ("26/03/01", ms.CONFIRMED_UPTREND, "ftd"),
            ("26/02/01", ms.CONFIRMED_UPTREND, "ftd"),
        ]
        result = ms.extract_ftd_history(history, max_count=2)
        assert result == ["26/05/01", "26/04/01"]

    def test_no_ftd(self):
        history = [
            ("26/05/01", ms.CONFIRMED_UPTREND, "stay"),
            ("26/04/30", ms.UPTREND_UNDER_PRESSURE, "dd>=4"),
        ]
        assert ms.extract_ftd_history(history) == []

    def test_empty(self):
        assert ms.extract_ftd_history([]) == []


# ==================================================
# calc_rally_day (Part B)
# ==================================================
class TestCalcRallyDay:
    """ラリーアテンプト Day N 計算"""

    def test_day1_today(self):
        """ラリー開始日 = 当日 → Day 1"""
        history = ["26/04/30", "26/04/29", "26/04/28"]
        assert ms.calc_rally_day("26/04/30", history) == 1

    def test_day3(self):
        """ラリー開始から2日経過 → Day 3"""
        history = ["26/04/30", "26/04/29", "26/04/28", "26/04/27"]
        assert ms.calc_rally_day("26/04/28", history) == 3

    def test_day4(self):
        """ラリー開始から3日経過 → Day 4 (FTD候補開始)"""
        history = ["26/04/30", "26/04/29", "26/04/28", "26/04/27"]
        assert ms.calc_rally_day("26/04/27", history) == 4

    def test_no_rally_start(self):
        assert ms.calc_rally_day(None, ["26/04/30"]) is None

    def test_empty_history(self):
        assert ms.calc_rally_day("26/04/30", []) is None

    def test_start_not_in_history(self):
        """ラリー開始日が daily_history に無い (窓外) → None"""
        history = ["26/04/30", "26/04/29"]
        assert ms.calc_rally_day("26/03/01", history) is None
