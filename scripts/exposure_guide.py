#!/usr/bin/env python3
"""市場状態連動の運用比率 (エクスポージャー) ガイド (issue #362)。

「基準運用額 (市場中立時の標準運用総額) に対して今どれだけ張っているか」を
運用比率 (%) で表し、市場ステートから導いた目標レンジとの乖離を可視化する。

構成:
  1. 純関数群 (weighted_state / apply_modifiers / evaluate_exposure) — I/O を持たない
  2. I/O ヘルパー (read_*) — 指標 JSON / market_db を読む。鮮度切れは None
  3. 日次ログ (record_daily_log) と CLI

判定の考え方:
  - ベースレンジは保有額で加重した市場ステートが決める (順張り)
  - 過熱指標 (信用評価損益率・日本版F&G) は上限を削る方向にのみ効く (非対称)
  - 恐怖側での自動増枠はしない (裁量に残す)
"""

import argparse
import json
import os
from datetime import datetime, timedelta

import market_state
import portfolio_shelve as ps
from db_shelve import MARKET_SHELVE, ShelveDB
from ks_util import DATA_DIR, get_price_day, log_print, log_warning

# ==================================================
# 定数
# ==================================================

# ステート → スコア (加重平均用)。confirmed=1.0 / pressure=0.5 / correction=0.0
STATE_SCORES = {
    market_state.CONFIRMED_UPTREND: 1.0,
    market_state.UPTREND_UNDER_PRESSURE: 0.5,
    market_state.MARKET_IN_CORRECTION: 0.0,
}

# 加重平均スコア → ステートの丸め境界
SCORE_TO_STATE_THRESHOLDS = (
    (0.75, market_state.CONFIRMED_UPTREND),
    (0.25, market_state.UPTREND_UNDER_PRESSURE),
)

# 保有銘柄の market_category → 参照する market_db の指数キー。
# 「その他」(スタンダード等) は TOPIX 寄せ。
CATEGORY_TO_INDEX = {
    "日経225": "nikkei225",
    "TOPIX": "topix",
    "グロース": "mothers",
    "その他": "topix",
}

# ノーポジ時のフォールバック判定に使う指数 (TOPIX とグロース250 の悪い方)
FALLBACK_INDEXES = ("topix", "mothers")

# 指標の許容鮮度 (日数)。これより古ければ None にして判定から外す。
# 信用評価損益率は週次公表のため長めに取る (当日一致を求めると永久に記録できない)。
FRESHNESS_DAYS = {
    "credit_balance": 10,
    "fng_jp": 3,
    "fng_us": 3,
    "index": 3,
}

EXPOSURE_LOG_PATH = os.path.join(DATA_DIR, "code_rank_data", "exposure_log.json")


# ==================================================
# 1. 純関数群 (I/O を持たない)
# ==================================================

def _score_to_state(score):
    """加重平均スコアを 3 段階のステートに丸める。"""
    for threshold, state in SCORE_TO_STATE_THRESHOLDS:
        if score >= threshold:
            return state
    return market_state.MARKET_IN_CORRECTION


def weighted_state(category_values, index_states):
    """市場別保有額で加重した市場ステートを返す。

    category_values: {"TOPIX": 1234567.0, ...} 市場カテゴリ別の保有額
    index_states: {"topix": "confirmed_uptrend", ...} 指数別ステート

    ステート不明のカテゴリは加重から除外する (残ったカテゴリだけで平均)。
    ノーポジ / 全カテゴリのステート不明なら (None, None) を返し、
    呼び出し側が fallback_state に落とす。
    """
    total_weight = 0.0
    weighted_sum = 0.0
    for category, value in (category_values or {}).items():
        if not value or value <= 0:
            continue
        index_name = CATEGORY_TO_INDEX.get(category)
        state = (index_states or {}).get(index_name)
        score = STATE_SCORES.get(state)
        if score is None:
            continue  # ステート不明の指数は加重から除外
        total_weight += value
        weighted_sum += value * score
    if total_weight <= 0:
        return None, None
    score = weighted_sum / total_weight
    return _score_to_state(score), score


def fallback_state(index_states):
    """ノーポジ時のフォールバック: TOPIX とグロース250 の悪い方を返す。

    片方だけ欠損ならもう片方を使う。両方欠損なら (None, None)。
    """
    candidates = []
    for index_name in FALLBACK_INDEXES:
        state = (index_states or {}).get(index_name)
        score = STATE_SCORES.get(state)
        if score is not None:
            candidates.append((score, state))
    if not candidates:
        return None, None
    score, state = min(candidates, key=lambda x: x[0])
    return state, score


def apply_modifiers(range_pct, credit_eval_rate, fng_jp, modifiers):
    """過熱指標に応じて目標レンジの上限のみを削る (非対称)。

    恐怖側での増枠はしない。削った結果が下限を下回る場合は下限に丸める。
    指標値が None (取得失敗・鮮度切れ) の modifier は発動させない。

    返り値: (lower, upper, applied) — applied は発動した modifier 名のリスト
    """
    lower, upper = range_pct
    applied = []
    values = {"credit_eval_rate": credit_eval_rate, "fng_jp": fng_jp}
    for name, value in values.items():
        if value is None:
            continue
        mod = (modifiers or {}).get(name)
        if not mod:
            continue
        # 信用評価損益率・日本版F&G とも「threshold 以上で過熱」
        if value >= mod["threshold"]:
            upper -= mod["penalty"]
            applied.append(name)
    if upper < lower:
        upper = lower  # レンジ反転を防ぐ
    return lower, upper, applied


def evaluate_exposure(
    total_value,
    category_values,
    index_states,
    credit_eval_rate,
    fng_jp,
    settings,
):
    """運用比率と目標レンジを評価して表示・ログ共通の dict を返す。

    指標欠損でも例外を投げず、判定不能な項目を None にした dict を必ず返す
    (デイリー全体を止めない既存方針に合わせる)。
    """
    base_amount = settings.get("base_amount") or 0
    ranges = settings.get("ranges") or {}
    modifiers = settings.get("modifiers") or {}

    # ノーポジかどうかは保有額の合計で機械的に判定する。weighted_state が None を
    # 返すのは「ノーポジ」と「保有はあるが該当指数が鮮度切れ」の両方がありうり、
    # 後者を「(ノーポジ)」と表示すると誤情報になる (PR #423 レビュー指摘)。
    is_no_position = sum((category_values or {}).values()) <= 0

    state, score = weighted_state(category_values, index_states)
    state_is_fallback = False
    if state is None:
        state, score = fallback_state(index_states)
        state_is_fallback = True

    ratio_pct = None
    if base_amount > 0:
        ratio_pct = total_value / base_amount * 100.0

    result = {
        "total_value": total_value,
        "base_amount": base_amount,
        "ratio_pct": ratio_pct,
        "state": state,
        "state_score": score,
        "state_is_fallback": state_is_fallback if state is not None else False,
        "is_no_position": is_no_position,
        "range_lower": None,
        "range_upper": None,
        "modifiers_applied": [],
        "deviation_pct": None,
        "position": None,
    }
    if state is None:
        return result  # 市場ステート不明: ガイドを出さない

    base_range = ranges.get(state)
    if not base_range:
        return result
    lower, upper, applied = apply_modifiers(
        base_range, credit_eval_rate, fng_jp, modifiers
    )
    result["range_lower"] = lower
    result["range_upper"] = upper
    result["modifiers_applied"] = applied

    if ratio_pct is None:
        return result  # 基準運用額が未設定: レンジだけ出して乖離は出さない
    if ratio_pct > upper:
        result["deviation_pct"] = ratio_pct - upper
        result["position"] = "over"
    elif ratio_pct < lower:
        result["deviation_pct"] = ratio_pct - lower
        result["position"] = "under"
    else:
        result["deviation_pct"] = 0.0
        result["position"] = "within"
    return result


# ==================================================
# 2. I/O ヘルパー (鮮度チェック付き)
# ==================================================

def _is_fresh(date_str, kind, *, today=None):
    """date_str ("YYYY-MM-DD") が許容鮮度内なら True。"""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    base = today or get_price_day(datetime.now())
    return (base - d).days <= FRESHNESS_DAYS[kind]


def _read_json_history_tail(path):
    """JSON の history 末尾要素を返す。読めなければ None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    history = payload.get("history") or []
    return history[-1] if history else None


def read_credit_eval_rate(*, today=None):
    """信用評価損益率と元データ日付を返す (value, date)。鮮度切れは value=None。"""
    latest = _read_json_history_tail(
        os.path.join(DATA_DIR, "code_rank_data", "credit_balance.json")
    )
    if not latest:
        return None, None
    date_str = latest.get("date")
    value = latest.get("credit_eval_rate")
    if value is None:
        return None, date_str
    if not _is_fresh(date_str, "credit_balance", today=today):
        log_warning(
            "[exposure] 信用評価損益率が鮮度切れのため判定から除外 (date=%s)" % date_str
        )
        return None, date_str
    return value, date_str


def read_fng_jp(*, today=None):
    """日本版 Fear & Greed スコアと元データ日付を返す (value, date)。"""
    latest = _read_json_history_tail(
        os.path.join(DATA_DIR, "code_rank_data", "fear_greed_jp.json")
    )
    if not latest:
        return None, None
    date_str = latest.get("date")
    value = latest.get("score")
    if value is None:
        return None, date_str
    if not _is_fresh(date_str, "fng_jp", today=today):
        log_warning(
            "[exposure] 日本版F&G が鮮度切れのため判定から除外 (date=%s)" % date_str
        )
        return None, date_str
    return value, date_str


def read_fng_us(market_db, *, today=None):
    """CNN Fear & Greed スコアと取得日を返す (value, date)。

    判定には使わずログのみ (issue #362)。米センチメント→日本の稼働率は因果が
    一段遠く、指標を増やすと後から効果検証ができなくなるため。
    """
    fng = (market_db or {}).get("fear_and_greed") or {}
    access_date = fng.get("access_date")
    date_str = None
    if isinstance(access_date, datetime):
        date_str = access_date.date().isoformat()
    elif isinstance(access_date, str):
        date_str = access_date[:10]
    value = fng.get("score")
    if value is None:
        return None, date_str
    if not _is_fresh(date_str, "fng_us", today=today):
        return None, date_str
    return value, date_str


def _index_latest_date(entry):
    """指数エントリの最新日足の日付を "YYYY-MM-DD" で返す。無ければ None。

    price_log は日付降順 (`[0]` が最新) で、日次取得が失敗した指数は
    make_market_db 側で前日データを保持したままになるため、ここが据え置かれる。
    DB ファイルの mtime は取得失敗時も更新される (全指数まとめて保存するため)
    ので鮮度判定には使えない。
    """
    price_log = (entry or {}).get("price_log") or []
    if not price_log:
        return None
    latest = price_log[0][0]
    if isinstance(latest, str):
        return latest[:10]
    return latest.isoformat()  # date / datetime


def read_index_states(market_db, *, today=None):
    """指数別の market_state を返す (states, dates)。

    鮮度は指数ごとに判定し、古い指数だけを個別に除外する
    (一部の指数が取得失敗しても、生きている指数の判定は続ける)。
    dates は指数ごとの最新日足 {index_name: date_str} を返す (採用有無に関わらず
    候補日を記録する)。単一の代表日付に潰すと、一部の指数だけ古い日に
    ログを監査できなくなるため (PR #423 レビュー指摘)。
    """
    states = {}
    dates = {}
    for index_name in set(CATEGORY_TO_INDEX.values()) | set(FALLBACK_INDEXES):
        entry = (market_db or {}).get(index_name) or {}
        state = entry.get("market_state")
        if state not in STATE_SCORES:
            continue
        date_str = _index_latest_date(entry)
        dates[index_name] = date_str
        if not _is_fresh(date_str, "index", today=today):
            log_warning(
                "[exposure] %s が鮮度切れのため判定から除外 (最新日足=%s)"
                % (index_name, date_str)
            )
            continue
        states[index_name] = state
    return states, dates


# ==================================================
# 3. 日次ログ
# ==================================================

def _load_exposure_log():
    """exposure_log.json を読む。無ければ空の history。"""
    if not os.path.exists(EXPOSURE_LOG_PATH):
        return {"history": []}
    try:
        with open(EXPOSURE_LOG_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        log_warning("[exposure] exposure_log.json が読めないため新規作成する")
        return {"history": []}
    if not isinstance(payload.get("history"), list):
        return {"history": []}
    return payload


def _save_exposure_log(payload):
    os.makedirs(os.path.dirname(EXPOSURE_LOG_PATH), exist_ok=True)
    with open(EXPOSURE_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def build_daily_entry(*, db_path=None, today=None):
    """当日分のログ 1 行分の dict を組み立てる。市場ステート不明なら None。"""
    from webapp.helpers import summarize_hold_positions

    base_day = today or get_price_day(datetime.now())
    with ShelveDB(MARKET_SHELVE) as db:
        market_db = {k: db.get(k) for k in ("topix", "mothers", "nikkei225", "fear_and_greed")}

    index_states, index_dates = read_index_states(market_db, today=base_day)
    if not index_states:
        # ガイドの主軸が無く state=null だけの行には監査価値がないためスキップ
        log_warning("[exposure] 市場ステートを取得できないため日次ログをスキップ")
        return None

    credit_eval_rate, credit_date = read_credit_eval_rate(today=base_day)
    fng_jp, fng_jp_date = read_fng_jp(today=base_day)
    fng_us, fng_us_date = read_fng_us(market_db, today=base_day)

    summary = summarize_hold_positions(db_path=db_path)
    settings = ps.get_exposure_settings(db_path=db_path)
    evaluation = evaluate_exposure(
        summary["total_value"],
        summary["category_values"],
        index_states,
        credit_eval_rate,
        fng_jp,
        settings,
    )

    entry = {"date": base_day.isoformat()}
    entry.update(evaluation)
    entry["index_states"] = index_states
    entry["category_values"] = summary["category_values"]
    entry["credit_eval_rate"] = credit_eval_rate
    entry["fng_jp"] = fng_jp
    entry["fng_us"] = fng_us
    entry["source_dates"] = {
        "index": index_dates,  # {"topix": "2026-08-28", "mothers": "...", ...}
        "credit_balance": credit_date,
        "fng_jp": fng_jp_date,
        "fng_us": fng_us_date,
    }
    return entry


def record_daily_log(*, db_path=None, today=None):
    """当日の運用比率を exposure_log.json に記録する。同一日付は上書き。"""
    entry = build_daily_entry(db_path=db_path, today=today)
    if entry is None:
        return None
    payload = _load_exposure_log()
    history = [h for h in payload["history"] if h.get("date") != entry["date"]]
    history.append(entry)
    history.sort(key=lambda h: h.get("date") or "")
    payload["history"] = history
    _save_exposure_log(payload)
    log_print(
        "<---- エクスポージャーログ記録 date=%s ratio=%s state=%s"
        % (
            entry["date"],
            "—" if entry["ratio_pct"] is None else "%.1f%%" % entry["ratio_pct"],
            entry["state"],
        )
    )
    return entry


# ==================================================
# 4. CLI
# ==================================================

def _format_entry(entry):
    ratio = entry.get("ratio_pct")
    lower, upper = entry.get("range_lower"), entry.get("range_upper")
    dev = entry.get("deviation_pct")
    mods = entry.get("modifiers_applied") or []
    return "%s  %s万円  比率%s  %s  目標%s  乖離%s%s" % (
        entry.get("date"),
        format(int(round((entry.get("total_value") or 0) / 10000)), ","),
        "—" if ratio is None else "%.1f%%" % ratio,
        entry.get("state") or "—",
        "—" if lower is None else "%d〜%d%%" % (lower, upper),
        "—" if dev is None else "%+.1fpt" % dev,
        " [%s]" % ",".join(mods) if mods else "",
    )


def main():
    parser = argparse.ArgumentParser(
        description="市場状態連動の運用比率ガイド (issue #362)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("log", help="当日の運用比率を記録する (同一日付は上書き)")

    show = sub.add_parser("show", help="直近の記録と現在の評価を表示する")
    show.add_argument("-n", "--limit", type=int, default=10, help="表示件数")

    settings_parser = sub.add_parser("settings", help="設定の表示・更新")
    settings_parser.add_argument(
        "--set-base-amount", type=float, help="基準運用額 (円) を設定する"
    )

    args = parser.parse_args()

    if args.command == "log":
        entry = record_daily_log()
        if entry:
            print(_format_entry(entry))
        return

    if args.command == "show":
        payload = _load_exposure_log()
        for entry in payload["history"][-args.limit:]:
            print(_format_entry(entry))
        current = build_daily_entry()
        if current:
            print("--- 現在 (未記録) ---")
            print(_format_entry(current))
        return

    if args.command == "settings":
        if args.set_base_amount is not None:
            ps.set_exposure_settings({"base_amount": args.set_base_amount})
        settings = ps.get_exposure_settings()
        print("基準運用額: %s円" % format(int(settings["base_amount"]), ","))
        print("目標レンジ:")
        for state, rng in settings["ranges"].items():
            print("  %-24s %d〜%d%%" % (state, rng[0], rng[1]))
        print("過熱モディファイア (上限を削る):")
        for name, mod in settings["modifiers"].items():
            print(
                "  %-18s threshold>=%s → −%dpt"
                % (name, mod["threshold"], mod["penalty"])
            )
        return

    parser.print_help()


if __name__ == "__main__":
    main()
