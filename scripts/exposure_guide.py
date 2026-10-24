#!/usr/bin/env python3
"""目標の維持率と、運用総額の適正エリア (issue #362 → #480)。

推定維持率 (#527) を物差しにして、感情スコア (自己申告 7段階) と日本版 F&G から
目標の維持率を決め、運用総額 (現物時価 + 信用建玉の時価) の適正エリアを出す。
計算は margin_estimate.target_step / target_area (純関数)、ここは入力を集める I/O と日次ログ。

判定の考え方 (doc/spec/推定維持率仕様.md):
  - 感情スコアの段が主。F&G が Extreme なら逆張りで 1 段ずらす (恐怖で張り、熱狂で落とす)
  - F&G が Extreme でない日は、保有額で加重した市場ステートで攻める側に上限をかける (順張り)
  - 感情に関係なく、全保有が一律に 20% 下がっても追証にならない建玉までに頭打ち

構成:
  1. 純関数群 (weighted_state / fallback_state / evaluate_target) — I/O を持たない
  2. I/O ヘルパー (read_* / build_margin_state / build_evaluation) — 鮮度切れは None
  3. 日次ログ (record_daily_log) と CLI
"""

import argparse
import json
import os
from datetime import datetime, timedelta

import margin_estimate
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

# 感情スコアの有効期限 (入力日からの営業日数)。過ぎたら中立 0 として扱う
EMOTION_VALID_WEEKDAYS = 5

# 保有銘柄 (1保) の数量から出した運用総額と、残高 CSV の運用総額がこれ以上ずれたら注記する
QTY_MISMATCH_RATIO = 0.01

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


def evaluate_target(margin_state, emotion, fng_jp, state, hold_total_value):
    """目標の維持率と、運用総額の適正エリアを評価して表示・ログ共通の dict を返す (issue #480)。

    margin_state: build_margin_state() の戻り値
    emotion: read_emotion() の戻り値 (未入力なら None)
    state: 保有額で加重した市場ステート (取れなければ None)
    hold_total_value: 保有銘柄 (1保) の数量 × 終値の合計。残高 CSV とのずれの注記に使う

    適正エリアは、残高 CSV に保有がある証券会社 (楽天・SBI) の推定がすべて成立したときだけ出す。
    片方だけの数字で全体の過不足を出さないため。出せないときは area_* を None にして理由を返す。
    """
    score = emotion["score"] if emotion and not emotion.get("expired") else None
    step, parts = margin_estimate.target_step(score, fng_jp, state)
    result = {
        "emotion_score": score,
        "emotion_date": (emotion or {}).get("date"),
        "emotion_expired": bool(emotion and emotion.get("expired")),
        "fng_jp": fng_jp,
        "state": state,
        "step": step,
        "step_parts": parts,
        "target_rate": margin_estimate.STEP_TARGET_RATES[step],
        "margin_rate_pct": None,
        "total_value": None,
        "hold_total_value": hold_total_value,
        "qty_mismatch": False,
        "area_lower": None,
        "area_upper": None,
        "target_value": None,
        "neutral_value": None,
        "floor_capped": False,
        "position": None,
        "deviation": None,
        "missing": margin_state["missing"],
    }
    combined = margin_state["combined"]
    if margin_state["missing"] or not combined:
        return result
    area = margin_estimate.target_area(
        combined["guarantee"], combined["tate_total"], combined["spot_value"],
        combined["margin_value"], step)
    result.update({k: area[k] for k in (
        "total_value", "area_lower", "area_upper", "target_value", "neutral_value",
        "floor_capped", "position", "deviation")})
    result["margin_rate_pct"] = combined["rate_pct"]
    total = area["total_value"]
    result["qty_mismatch"] = bool(
        total and abs((hold_total_value or 0) - total) / total >= QTY_MISMATCH_RATIO)
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


def read_emotion(*, db_path=None, today=None):
    """最新の感情スコアを返す (issue #480)。未入力なら None。

    入力日から EMOTION_VALID_WEEKDAYS 営業日 (平日) を過ぎたら expired=True にし、
    評価では中立 0 として扱う (古い感情で目標を出さない)。
    """
    rows = ps.list_emotion_scores(db_path=db_path)
    if not rows:
        return None
    latest = rows[-1]
    day = (today or datetime.now(ps.JST).date()).isoformat()
    return {**latest,
            "expired": day > ps._add_weekdays(latest["date"], EMOTION_VALID_WEEKDAYS)}


def margin_inputs(*, db_path=None):
    """推定維持率に使う残高 CSV の建玉と、銘柄ごとの最新終値・その日付を返す (issue #527)。"""
    from webapp.helpers import _bulk_get_stock_data

    positions = ps.list_positions(db_path=db_path)
    codes = sorted({p["code_s"] for p in positions})
    closes, price_dates = {}, {}
    for code, stock in _bulk_get_stock_data(codes).items():
        log = (stock or {}).get("price_log") or []
        if log:
            closes[code] = log[0][1]
            price_dates[code] = str(log[0][0])
    return positions, closes, price_dates


def build_margin_state(*, db_path=None):
    """楽天・SBI の推定維持率と、その合算を組み立てる (issue #527 / #480)。

    Returns: {"rows": 証券会社別の表示行, "combined": 合算 (推定できた証券会社だけ) か None,
              "missing": 残高 CSV に保有があるのに推定できない証券会社 [{"broker", "reason"}]}
    """
    positions, closes, _ = margin_inputs(db_path=db_path)
    baselines = ps.list_margin_baselines(db_path=db_path)
    sources = ps.list_position_sources(db_path=db_path)
    fills = ps.list_fills(db_path=db_path) if baselines else []
    rows, estimates, missing = [], {}, []
    for broker in margin_estimate.BROKERS:
        csv_dates = [s["as_of"] for s in sources if s.get("broker") == broker and s.get("as_of")]
        base = baselines.get(broker)
        history = (base or {}).get("history") or []
        row = {
            "broker": broker, "csv_as_of": min(csv_dates) if csv_dates else None,
            "base_date": (base or {}).get("date"), "base_rate": (base or {}).get("rate_pct"),
            "last_gap": history[-1] if history else None,
            "rate_pct": None, "reason": None, "falls": {}, "skipped": 0,
        }
        snapshot, reason = margin_estimate.build_snapshot(broker, positions, closes)
        if snapshot is None:
            row["reason"] = reason
        elif base is None:
            row["reason"] = "保証金率が未入力です"
        else:
            est, reason = margin_estimate.estimate(
                base, snapshot, margin_estimate.realized_since(fills, broker, base["date"]))
            if est is None:
                row["reason"] = reason
            else:
                row.update(rate_pct=est["rate_pct"], skipped=est["skipped"], realized=est["realized"], falls={
                    t: margin_estimate.fall_to_threshold(
                        est["guarantee"], est["tate_total"], est["spot_value"], t)
                    for t in margin_estimate.THRESHOLDS})
                estimates[broker] = est
        if row["rate_pct"] is None and any(p.get("broker") == broker for p in positions):
            missing.append({"broker": broker, "reason": row["reason"]})
        rows.append(row)
    return {"rows": rows, "combined": margin_estimate.combine(estimates), "missing": missing}


def build_evaluation(market_db, *, db_path=None, today=None, margin_state=None):
    """今の目標の維持率と適正エリアを、画面と日次ログ共通の dict で返す (issue #480)。

    市場ステートが取れない日も評価を出す (F&G が Extreme の日はステートを使わないため)。
    """
    from webapp.helpers import summarize_hold_positions

    base_day = today or get_price_day(datetime.now())
    index_states, index_dates = read_index_states(market_db, today=base_day)
    summary = summarize_hold_positions(db_path=db_path)
    state, _ = weighted_state(summary["category_values"], index_states)
    state_is_fallback = False
    if state is None:
        state, _ = fallback_state(index_states)
        state_is_fallback = state is not None
    fng_jp, fng_jp_date = read_fng_jp(today=base_day)
    if margin_state is None:
        margin_state = build_margin_state(db_path=db_path)
    evaluation = evaluate_target(
        margin_state, read_emotion(db_path=db_path), fng_jp, state, summary["total_value"])
    evaluation.update({
        "state_is_fallback": state_is_fallback,
        "is_no_position": summary["total_value"] <= 0,
        "index_states": index_states,
        "index_dates": index_dates,
        "category_values": summary["category_values"],
        "fng_jp_date": fng_jp_date,
    })
    return evaluation


def build_daily_entry(*, db_path=None, today=None):
    """当日分のログ 1 行分の dict を組み立てる。"""
    base_day = today or get_price_day(datetime.now())
    with ShelveDB(MARKET_SHELVE) as db:
        market_db = {k: db.get(k) for k in ("topix", "mothers", "nikkei225", "fear_and_greed")}
    evaluation = build_evaluation(market_db, db_path=db_path, today=base_day)
    credit_eval_rate, credit_date = read_credit_eval_rate(today=base_day)
    fng_us, fng_us_date = read_fng_us(market_db, today=base_day)

    entry = {"date": base_day.isoformat()}
    entry.update(evaluation)
    entry["source_dates"] = {
        "index": entry.pop("index_dates"),  # {"topix": "2026-08-28", "mothers": "...", ...}
        "credit_balance": credit_date,
        "fng_jp": entry.pop("fng_jp_date"),
        "fng_us": fng_us_date,
    }
    # 判定には使わない参考値 (#362 から記録を続けている)
    entry["credit_eval_rate"] = credit_eval_rate
    entry["fng_us"] = fng_us
    return entry


def record_daily_log(*, db_path=None, today=None):
    """当日の目標の維持率と適正エリアを exposure_log.json に記録する。同一日付は上書き。"""
    entry = build_daily_entry(db_path=db_path, today=today)
    payload = _load_exposure_log()
    history = [h for h in payload["history"] if h.get("date") != entry["date"]]
    history.append(entry)
    history.sort(key=lambda h: h.get("date") or "")
    payload["history"] = history
    _save_exposure_log(payload)
    log_print("<---- エクスポージャーログ記録 " + _format_entry(entry))
    return entry


# ==================================================
# 4. CLI
# ==================================================

def _man(value):
    return "—" if value is None else format(int(round(value / 10000)), ",")


def _format_entry(entry):
    if "target_rate" not in entry:  # issue #480 より前の行 (基準運用額に対する運用比率)
        ratio = entry.get("ratio_pct")
        return "%s  %s万円  比率%s  %s (旧方式)" % (
            entry.get("date"), _man(entry.get("total_value")),
            "—" if ratio is None else "%.1f%%" % ratio, entry.get("state") or "—")
    rate = entry.get("margin_rate_pct")
    return "%s  運用総額%s万円  維持率%s  段%s (%s) 目標%d%%  適正%s〜%s万円  %s%s" % (
        entry.get("date"), _man(entry.get("total_value")),
        "—" if rate is None else "%.1f%%" % rate,
        margin_estimate.format_step(entry["step"]), "、".join(entry.get("step_parts") or []),
        entry["target_rate"], _man(entry.get("area_lower")), _man(entry.get("area_upper")),
        entry.get("position") or "—",
        " [推定できない: %s]" % ",".join(m["broker"] for m in entry["missing"])
        if entry.get("missing") else "",
    )


def main():
    parser = argparse.ArgumentParser(
        description="目標の維持率と運用総額の適正エリア (issue #362 / #480)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("log", help="当日分を記録する (同一日付は上書き)")

    show = sub.add_parser("show", help="直近の記録と現在の評価を表示する")
    show.add_argument("-n", "--limit", type=int, default=10, help="表示件数")

    args = parser.parse_args()

    if args.command == "log":
        print(_format_entry(record_daily_log()))
        return

    if args.command == "show":
        payload = _load_exposure_log()
        for entry in payload["history"][-args.limit:]:
            print(_format_entry(entry))
        print("--- 現在 (未記録) ---")
        print(_format_entry(build_daily_entry()))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
