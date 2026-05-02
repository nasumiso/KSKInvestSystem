"""市場状態 State Machine (issue #117 Part A)

O'Neil/IBD原典に準拠した3状態モデル + 原典準拠DD/FTD判定 + ラリーアテンプト追跡を実装する。

3状態:
- confirmed_uptrend: 上昇トレンド確定
- uptrend_under_pressure: 上昇トレンドだが圧力下 (DD増加)
- market_in_correction: 調整相場

I/O は持たない純関数の集まり。market_db への永続化は make_market_db.py 側で行う。
詳細仕様は doc/requirements/market_state_machine_requirements.md を参照。
"""

# ==================================================
# 状態定数
# ==================================================
CONFIRMED_UPTREND = "confirmed_uptrend"
UPTREND_UNDER_PRESSURE = "uptrend_under_pressure"
MARKET_IN_CORRECTION = "market_in_correction"

ALL_STATES = (CONFIRMED_UPTREND, UPTREND_UNDER_PRESSURE, MARKET_IN_CORRECTION)

# ==================================================
# 判定パラメータ (将来差し替え可能にするためモジュール定数化)
# ==================================================

# DD 失効
DD_EXPIRY_DAYS = 25  # 25取引日経過で失効
DD_RECOVERY_RATIO = 1.05  # DD発生日終値の1.05倍以上に達したら失効

# 状態遷移しきい値
DD_THRESHOLD_TO_PRESSURE = 4  # confirmed → pressure: 直近25日内 DD ≥ 4
DD_THRESHOLD_TO_CORRECTION = 6  # confirmed/pressure → correction: DD ≥ 6
DD_THRESHOLD_TO_CONFIRMED_FROM_PRESSURE = 4  # pressure → confirmed: DD < 4 復帰

# FTD 判定 (固定閾値、IBD中央値)
FTD_GAIN_THRESHOLD = 1.0  # %、ラリー Day 4 以降の最低上昇率
FTD_MIN_DAYS_FROM_RALLY_START = 3  # Day 1 から3取引日後 = Day 4

# state_history 保持件数
STATE_HISTORY_MAX = 30


# ==================================================
# DD 失効
# ==================================================
def expire_distribution_days(dd_list, today_close, daily_history):
    """有効な DD のみを返す (純関数)。

    失効ルール:
    - 25取引日経過: daily_history で DD 日と当日のインデックス差 >= 25
    - 5%上昇: 当日終値 >= DD発生日終値 * 1.05
    - 境界: DD 日が daily_history に含まれない (窓外) → 失効扱い

    Args:
        dd_list: [(date_str, close_float), ...] DD のリスト
        today_close: float 当日終値
        daily_history: [date_str, ...] 直近の取引日リスト (新しい日が先頭)

    Returns:
        list: 失効していない DD のリスト (順序は元のまま)
    """
    if not dd_list or not daily_history:
        return []

    # daily_history の date → index の dict (新しい日 = index 0)
    history_index = {date: i for i, date in enumerate(daily_history)}
    today_idx = history_index.get(daily_history[0])  # 0 のはず

    valid = []
    for dd_date, dd_close in dd_list:
        # 5%上昇で失効
        if dd_close > 0 and today_close >= dd_close * DD_RECOVERY_RATIO:
            continue
        # daily_history に含まれない (窓外) → 失効扱い
        if dd_date not in history_index:
            continue
        # 25取引日経過で失効
        days_passed = history_index[dd_date] - today_idx
        if days_passed >= DD_EXPIRY_DAYS:
            continue
        valid.append((dd_date, dd_close))
    return valid


# ==================================================
# ラリーアテンプト追跡
# ==================================================
def update_rally_attempt(rally_meta, today, prev):
    """ラリーアテンプトの開始/リセット/維持を判定する (純関数)。

    Correction 状態のときに呼ばれることを想定。
    Confirmed/Pressure 状態では rally_meta はクリアする (呼び出し側で処理)。

    Args:
        rally_meta: dict {rally_attempt_start_date: str|None, rally_attempt_start_low: float|None}
        today: dict {date, close, low}
        prev: dict {date, close, low}

    Returns:
        dict: 更新後の rally_meta
    """
    start_date = rally_meta.get("rally_attempt_start_date")
    start_low = rally_meta.get("rally_attempt_start_low")

    # まだラリー開始していない: 当日 close > 前日 close で Day 1 確定
    if start_date is None:
        if today["close"] > prev["close"]:
            return {
                "rally_attempt_start_date": today["date"],
                "rally_attempt_start_low": today["low"],
            }
        return rally_meta

    # ラリー追跡中: 当日安値が start_low を下回ったらリセット
    if today["low"] < start_low:
        # リセット後、同日に新たな Day 1 を判定するかは仕様外 (次の取引日に再判定)
        return {
            "rally_attempt_start_date": None,
            "rally_attempt_start_low": None,
        }

    # 継続
    return rally_meta


# ==================================================
# FTD 判定
# ==================================================
def check_follow_through_day(today, prev, rally_meta, daily_history):
    """FTD (Follow-Through Day) 成立判定 (純関数)。

    成立条件:
    - rally_attempt_start_date is not None (ラリー追跡中)
    - Day 4 以降 (start から3取引日経過)
    - 当日安値 >= rally_attempt_start_low (リセット未発動)
    - 前日比 >= FTD_GAIN_THRESHOLD (固定 1.0%)
    - 当日出来高 > 前日出来高

    Args:
        today: dict {date, close, low, volume}
        prev: dict {close, volume}
        rally_meta: dict
        daily_history: [date_str, ...] 新しい日が先頭

    Returns:
        bool: FTD成立か
    """
    start_date = rally_meta.get("rally_attempt_start_date")
    start_low = rally_meta.get("rally_attempt_start_low")
    if not start_date or start_low is None:
        return False

    # 安値割れチェック
    if today["low"] < start_low:
        return False

    # Day 4 以降か
    history_index = {date: i for i, date in enumerate(daily_history)}
    if today["date"] not in history_index or start_date not in history_index:
        return False
    days_since_start = history_index[start_date] - history_index[today["date"]]
    if days_since_start < FTD_MIN_DAYS_FROM_RALLY_START:
        return False

    # 上昇率チェック
    if prev["close"] <= 0:
        return False
    pct_change = (today["close"] / prev["close"] - 1) * 100
    if pct_change < FTD_GAIN_THRESHOLD:
        return False

    # 出来高増チェック
    if today["volume"] <= prev["volume"]:
        return False

    return True


# ==================================================
# 状態遷移
# ==================================================
def derive_state(prev_state, valid_dd_count, ftd_today):
    """前日 state と当日の DD 数 / FTD成立から、新 state を計算する (純関数)。

    遷移ルール:
    - market_in_correction → confirmed_uptrend: FTD成立
    - confirmed_uptrend → uptrend_under_pressure: 有効DD ≥ 4 (かつ < 6)
    - confirmed_uptrend → market_in_correction: 有効DD ≥ 6
    - uptrend_under_pressure → market_in_correction: 有効DD ≥ 6
    - uptrend_under_pressure → confirmed_uptrend: 有効DD < 4
    - それ以外: 状態維持

    Args:
        prev_state: 前日の state (None の場合は初期判定)
        valid_dd_count: 有効DD数
        ftd_today: bool 当日 FTD 成立か

    Returns:
        tuple: (new_state, trigger_reason)
            trigger_reason: 遷移理由文字列 ("ftd" / "dd>=4" / "dd>=6" / "dd<4_recover" / "init" / "stay")
    """
    # 初回 (prev_state なし) は遷移ルールと同じ閾値で判定
    if prev_state is None or prev_state not in ALL_STATES:
        if valid_dd_count >= DD_THRESHOLD_TO_CORRECTION:
            return MARKET_IN_CORRECTION, "init"
        if valid_dd_count >= DD_THRESHOLD_TO_PRESSURE:
            return UPTREND_UNDER_PRESSURE, "init"
        return CONFIRMED_UPTREND, "init"

    if prev_state == MARKET_IN_CORRECTION:
        if ftd_today:
            return CONFIRMED_UPTREND, "ftd"
        return MARKET_IN_CORRECTION, "stay"

    if prev_state == CONFIRMED_UPTREND:
        if valid_dd_count >= DD_THRESHOLD_TO_CORRECTION:
            return MARKET_IN_CORRECTION, "dd>=6"
        if valid_dd_count >= DD_THRESHOLD_TO_PRESSURE:
            return UPTREND_UNDER_PRESSURE, "dd>=4"
        return CONFIRMED_UPTREND, "stay"

    if prev_state == UPTREND_UNDER_PRESSURE:
        if valid_dd_count >= DD_THRESHOLD_TO_CORRECTION:
            return MARKET_IN_CORRECTION, "dd>=6"
        if valid_dd_count < DD_THRESHOLD_TO_CONFIRMED_FROM_PRESSURE:
            return CONFIRMED_UPTREND, "dd<4_recover"
        return UPTREND_UNDER_PRESSURE, "stay"

    # 想定外
    return prev_state, "stay"


# ==================================================
# 状態履歴
# ==================================================
def append_state_history(history, today_date, new_state, trigger):
    """state_history に1件追加。直近 STATE_HISTORY_MAX 件のみ保持。

    Args:
        history: 既存の履歴 [(date, state, trigger), ...] (新しいが先頭)
        today_date: str
        new_state: str
        trigger: str

    Returns:
        list: 更新後の履歴
    """
    if not isinstance(history, list):
        history = []
    # 同じ日の既存エントリは置き換え (1日複数回計算される場合)
    history = [h for h in history if h[0] != today_date]
    history.insert(0, (today_date, new_state, trigger))
    return history[:STATE_HISTORY_MAX]


# ==================================================
# direction_signal 互換変換
# ==================================================
def to_direction_signal(state, today_date):
    """state と日付から direction_signal 文字列を生成する。
    既存形式 "<value>,YYMMDD" を継続、value のみ変更。
    """
    return "%s,%s" % (state, today_date)
