"""出口ラインの純粋計算ロジック (issue #386)。"""

from typing import Any, Callable, Dict, List, Optional


def calc_sma_at(closes: List[float], window: int, index: int = 0) -> Optional[float]:
    """新しい日が先頭の終値列から index 日の単純移動平均を返す。"""
    if window <= 0 or index < 0 or len(closes) < index + window:
        return None
    values = closes[index:index + window]
    if not values:
        return None
    return sum(values) / window


def calc_ma_violation(
    closes: List[float],
    lows: List[Optional[float]],
    ma_at: Callable[[int], Optional[float]],
) -> Dict[str, Any]:
    """MA 割れと A日安値割れの確認状態を返す。

    終値列・安値列はいずれも新しい日が先頭。終値が MA を割った最初の日を
    A日とし、その後の安値が A日安値を下回ったときだけ confirmed とする。
    """
    result: Dict[str, Any] = {
        "breached": False,
        "pending": False,
        "confirmed": False,
        "ma_value": None,
        "a_day_low": None,
    }
    if not closes or not lows:
        return result
    ma0 = ma_at(0)
    if ma0 is None or ma0 == 0:
        return result
    result["ma_value"] = ma0
    if closes[0] >= ma0:
        return result
    result["breached"] = True

    a_day_idx = None
    for i, close in enumerate(closes):
        ma = ma_at(i)
        if ma is None:
            break
        if close >= ma:
            continue
        if i + 1 >= len(closes):
            break
        previous_ma = ma_at(i + 1)
        if previous_ma is not None and closes[i + 1] >= previous_ma:
            a_day_idx = i
            break
    if a_day_idx is None or a_day_idx >= len(lows):
        result["pending"] = True
        return result
    a_day_low = lows[a_day_idx]
    if a_day_low is None:
        result["pending"] = True
        return result
    result["a_day_low"] = a_day_low
    for i in range(a_day_idx):
        low = lows[i]
        if low is not None and low < a_day_low:
            result["confirmed"] = True
            return result
    result["pending"] = True
    return result


def calc_stop_loss_line(
    exit_rule: Optional[Dict[str, Any]],
    fills: List[Dict[str, Any]],
    *,
    kind: Optional[str] = None,
    is_short: bool = False,
) -> Optional[float]:
    """約定を時系列再生し、買い増し条件を反映した損切りラインを求める。"""
    if is_short or not isinstance(exit_rule, dict):
        return None
    stop_loss_pct = exit_rule.get("stop_loss_pct")
    if not isinstance(stop_loss_pct, (int, float)) or stop_loss_pct <= 0:
        return None
    allow_dca_lower = bool(exit_rule.get("allow_dca_lower"))
    held_qty = 0
    avg_cost = 0.0
    line = None
    # エピソード側で建玉を作る約定を同日中の返済・売却より先に正規化済み。
    # ここで seq 順に並べ直すと、その建玉順と残存原価が変わってしまう。
    for fill in fills:
        qty = fill.get("qty")
        price = fill.get("price")
        side = fill.get("side")
        trade_kind = fill.get("trade_kind") or ""
        if (not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0
                or not isinstance(price, (int, float)) or price <= 0):
            continue
        if kind == "信用":
            is_open = side == "buy" and trade_kind.startswith("信用新規")
            is_close = side == "sell" or trade_kind == "現引"
        else:
            is_open = side == "buy" and not trade_kind.startswith("信用返済")
            is_close = side == "sell"
        if is_open:
            new_qty = held_qty + qty
            avg_cost = (avg_cost * held_qty + price * qty) / new_qty
            held_qty = new_qty
            candidate = avg_cost * (1 - float(stop_loss_pct) / 100)
            line = candidate if allow_dca_lower or line is None else max(line, candidate)
        elif is_close:
            held_qty = max(0, held_qty - qty)
    return round(line, 4) if line is not None and held_qty > 0 else None


def evaluate_exit_signal(
    exit_rule: Optional[Dict[str, Any]],
    stock: Dict[str, Any],
    position: Dict[str, Any],
    previous_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """防御シグナルを評価する。"""
    if not isinstance(exit_rule, dict):
        return None
    price_log = stock.get("price_log") or []
    if not price_log:
        return None
    latest = price_log[0]
    if not isinstance(latest, (tuple, list)) or len(latest) < 2:
        return None
    signal_date, close = latest[0], latest[1]
    if not isinstance(close, (int, float)):
        return None
    reasons = []
    stop_line = position.get("stop_loss_line")
    if isinstance(stop_line, (int, float)) and close < stop_line:
        reasons.append("損切りライン割れ")
    ma_kind = exit_rule.get("ma_kind")
    ma_window = exit_rule.get("ma_window")
    violation = {}
    ma_label = ""
    if ma_kind == "day" and ma_window == 50:
        violation = stock.get("ma50_violation") or {}
        ma_label = "日足50MA"
    elif ma_kind == "week" and ma_window in (30, 40):
        violation = stock.get(f"wma{ma_window}_violation") or {}
        ma_label = f"週足{ma_window}MA"
    if isinstance(violation, dict) and violation.get("confirmed"):
        reasons.append(f"{ma_label}割れ確定")
    if reasons:
        return {"level": "防", "date": str(signal_date), "close": close, "reasons": reasons,
                "stop_loss_line": stop_line, "ma_value": violation.get("ma_value") if isinstance(violation, dict) else None}
    if isinstance(violation, dict) and violation.get("pending"):
        return {"level": "防予", "date": str(signal_date), "close": close,
                "reasons": [f"{ma_label}割れ予兆"], "stop_loss_line": stop_line,
                "ma_value": violation.get("ma_value")}
    if isinstance(previous_state, dict) and previous_state.get("triggered"):
        return {"level": "防歴", "date": str(signal_date), "close": close, "reasons": []}
    return None
