"""推定維持率 (信用の保証金率) の概算ロジック (issue #527)。

証券会社の保証金データは CSV で取れないので、ユーザーが入れた保証金率を基準値にし、
そのあとは残高 CSV (portfolio_shelve の position) と株価の変化から差分で推定する。
厳密ではなく概算。純粋関数だけを置く (DB・Flask に依存しない)。

前提 (spec に同じ内容を書く):
- 信用は買建のみ。売建がある証券会社は推定しない
- 信用建玉の評価損だけを保証金に算入する (SBI のヘルプが明記。評価益は算入しない)
- 現物は代用として掛目 0.8 で値動きを算入する (現物の全株が代用とは限らない近似)
- 基準日より後の信用返済の決済損益は、約定 (fill) から足す。入出金・金利・現物の売却代金は
  追わない (基準値を入れ直すと戻る)
- 銘柄単位の合算スナップショットなので、基準日の前後で同じ銘柄の株が入れ替わっても区別できない
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

BROKERS = ("楽天", "SBI")
# 代用有価証券の掛目 (一般的な値。銘柄ごとの掛目は追わない)
SUBSTITUTE_RATIO = 0.8
# 追証までの下落率を出す保証金率 (%)
THRESHOLDS = (30, 25, 20)
# 証券会社別の推定がこれを下回ったら注記する (%)
WARN_RATE_PCT = 30
HISTORY_LIMIT = 20


def build_snapshot(
    broker: str,
    positions: List[Dict[str, Any]],
    closes: Dict[str, Optional[float]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """証券会社1社分の残高 CSV を、銘柄単位 (全口座を合算) のスナップショットにする。

    Returns: (snapshot, reason)。推定できないときは (None, 理由)。
    snapshot = {"margin": {code: {qty, avg_price}}, "spot": {code: {qty, avg_price}},
                "closes": {code: 終値}, "tate_total": 建玉合計, "skipped": 終値が無い銘柄数}
    """
    margin: Dict[str, Dict[str, Any]] = {}
    spot: Dict[str, Dict[str, Any]] = {}
    for p in positions:
        if p.get("broker") != broker or not p.get("qty") or p["qty"] <= 0:
            continue
        kind = p.get("kind")
        if kind == "信用売建":
            return None, "信用売建があるため推定しません"
        if kind not in ("信用", "現物"):
            continue
        bucket = margin if kind == "信用" else spot
        entry = bucket.setdefault(p["code_s"], {"qty": 0, "cost": 0.0, "missing": False})
        entry["qty"] += p["qty"]
        if p.get("avg_price") is None:
            entry["missing"] = True
        else:
            entry["cost"] += p["qty"] * p["avg_price"]
    for code, e in margin.items():
        if e["missing"]:
            return None, f"{code} の信用建玉に建単価が無いため推定しません"
    tate_total = sum(e["cost"] for e in margin.values())
    if tate_total <= 0:
        return None, "信用建玉がありません"
    for code in margin:
        close = closes.get(code)
        if not (isinstance(close, (int, float)) and close > 0):
            # 建玉合計には入るのに評価損が出せないと、維持率が実際より低く出る
            return None, f"{code} の信用建玉の終値が取れないため推定しません"
    snapshot = {"margin": {}, "spot": {}, "closes": {}, "tate_total": tate_total, "skipped": 0}
    for kind_name, bucket in (("margin", margin), ("spot", spot)):
        for code, e in bucket.items():
            snapshot[kind_name][code] = {
                "qty": e["qty"],
                "avg_price": None if e["missing"] else e["cost"] / e["qty"],
            }
            close = closes.get(code)
            if isinstance(close, (int, float)) and close > 0:
                snapshot["closes"][code] = float(close)
            else:
                snapshot["skipped"] += 1
    return snapshot, None


def make_baseline(
    broker: str,
    rate_pct: float,
    snapshot: Dict[str, Any],
    base_day: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """入力した保証金率とスナップショットから、基準値のレコードを作る。"""
    closes = snapshot["closes"]
    return {
        "broker": broker,
        "date": base_day,
        "rate_pct": float(rate_pct),
        "tate_total": snapshot["tate_total"],
        "guarantee": rate_pct / 100.0 * snapshot["tate_total"],
        "margin": {c: {**v, "close": closes.get(c)} for c, v in snapshot["margin"].items()},
        "spot": {c: {"qty": v["qty"], "close": closes.get(c)} for c, v in snapshot["spot"].items()},
        "history": list(history or [])[-HISTORY_LIMIT:],
    }


def _margin_loss(qty: float, avg_price: float, close: float) -> float:
    """建玉の評価損 (0 以下)。評価益は 0 として算入しない。"""
    return min(0.0, qty * (close - avg_price))


def realized_since(fills: List[Dict[str, Any]], broker: str, base_day: str) -> Dict[str, Optional[float]]:
    """基準日より後の信用返済 (買建の返済) の決済損益を、銘柄ごとに合計する。

    損益は SBI が settle_pl、楽天が (約定単価 − 建単価) × 株数 (往復行の損益と同じ定義)。
    どちらも無い返済 (当日の未確定 CSV 由来) が1つでもある銘柄は None にする。
    基準日の約定は、基準日の残高 CSV と保証金率にすでに入っているので数えない。
    """
    result: Dict[str, Optional[float]] = {}
    for f in fills:
        if (f.get("broker") != broker or f.get("trade_kind") != "信用返済"
                or f.get("side") != "sell" or (f.get("trade_date") or "") <= base_day):
            continue
        if f.get("settle_pl") is not None:
            pl = float(f["settle_pl"])
        elif f.get("tate_price") is not None:
            pl = (f["price"] - f["tate_price"]) * f["qty"]
        else:
            pl = None
        prev = result.get(f["code_s"], 0.0)
        result[f["code_s"]] = None if pl is None or prev is None else prev + pl
    return result


def estimate(baseline: Dict[str, Any], snapshot: Dict[str, Any],
             realized: Optional[Dict[str, Optional[float]]] = None,
             ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """基準値と今のスナップショットから、受入保証金・維持率・追証までの下落率を推定する。

    realized: realized_since の結果。決済損益が分かる銘柄は、基準日の評価損の控除を外して
      決済損益に置き換える。分からない銘柄は、減った建玉の損を基準日の値で残す。
    Returns: (結果, 理由)。結果 = {"guarantee", "tate_total", "rate_pct", "spot_value",
      "skipped", "realized"}。
    """
    closes = snapshot["closes"]
    realized = realized or {}
    delta = 0.0
    realized_total = 0.0
    for code in set(baseline["margin"]) | set(snapshot["margin"]) | set(realized):
        base = baseline["margin"].get(code)
        now = snapshot["margin"].get(code)
        loss_base = (_margin_loss(base["qty"], base["avg_price"], base["close"])
                     if base and base.get("close") else 0.0)
        # 基準日の損のうち、今も残っている建玉の分
        remaining = min(1.0, (now["qty"] if now else 0) / base["qty"]) if base else 1.0
        stays = loss_base * remaining
        if now and closes.get(code):
            loss_now = _margin_loss(now["qty"], now["avg_price"], closes[code])
        else:
            loss_now = stays  # 終値が無ければ変化なしとして扱う
        pl = realized.get(code)
        if pl is not None:
            # 決済した分は評価損の控除が外れ、決済損益が現金で入る
            delta += loss_now - loss_base + pl
            realized_total += pl
        else:
            # 決済損益が分からなければ、減った分の損は基準日の値で残す (損益 ≒ 基準日の評価損)
            delta += loss_now - stays
    # 代用 (現物) の値動き。基準日からある株は基準日の終値から、増えた分は取得単価から測る
    spot_value = 0.0
    for code, now in snapshot["spot"].items():
        close = closes.get(code)
        if not close:
            continue
        spot_value += now["qty"] * close
        base = baseline["spot"].get(code)
        q0 = base["qty"] if base else 0
        q_keep = min(q0, now["qty"])
        q_new = max(0, now["qty"] - q0)
        if q_keep and base and base.get("close"):
            delta += SUBSTITUTE_RATIO * q_keep * (close - base["close"])
        if q_new:
            if now.get("avg_price") is None:
                return None, f"{code} の現物に取得単価が無いため推定しません"
            delta += SUBSTITUTE_RATIO * q_new * (close - now["avg_price"])
    guarantee = baseline["guarantee"] + delta
    tate_total = snapshot["tate_total"]
    # 信用建玉の時価。適正エリアで、建玉を減らす量を時価に直すのに使う (#480)
    margin_value = sum(v["qty"] * (closes.get(c) or v["avg_price"])
                       for c, v in snapshot["margin"].items())
    return {
        "guarantee": guarantee,
        "tate_total": tate_total,
        "rate_pct": guarantee / tate_total * 100.0,
        "spot_value": spot_value,
        "margin_value": margin_value,
        "skipped": snapshot["skipped"],
        "realized": realized_total,
    }, None


def fall_to_threshold(guarantee: float, tate_total: float, spot_value: float,
                      threshold_pct: float) -> float:
    """全保有が一律に下がるとして、保証金率が threshold_pct に届く下落率 (%)。0 以上。

    評価益のある建玉は下落でまず益が減るだけで保証金は減らないが、その分は無視する
    (早めに届く側に倒れる)。
    """
    denominator = tate_total + SUBSTITUTE_RATIO * spot_value
    if denominator <= 0:
        return 0.0
    return max(0.0, (guarantee - threshold_pct / 100.0 * tate_total) / denominator * 100.0)


def combine(per_broker: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """証券会社別の推定を合算して、総合の維持率と下落率を返す。"""
    if not per_broker:
        return None
    guarantee = sum(r["guarantee"] for r in per_broker.values())
    tate_total = sum(r["tate_total"] for r in per_broker.values())
    spot_value = sum(r["spot_value"] for r in per_broker.values())
    return {
        "guarantee": guarantee,
        "tate_total": tate_total,
        "spot_value": spot_value,
        "margin_value": sum(r["margin_value"] for r in per_broker.values()),
        "rate_pct": guarantee / tate_total * 100.0,
        "falls": {t: fall_to_threshold(guarantee, tate_total, spot_value, t) for t in THRESHOLDS},
        "low_brokers": [b for b, r in per_broker.items() if r["rate_pct"] < WARN_RATE_PCT],
    }


def check_baseline_inputs(
    broker: str,
    sources: List[Dict[str, Any]],
    price_dates: List[Optional[str]],
    now: datetime,
) -> Optional[str]:
    """基準値を保存してよいか。保存できないときは理由を返す。

    入力した保証金率の分母 (建玉合計) を、その日の残高 CSV と終値で作るため、
    証券会社の CSV 全ソースの取込日が今日で、終値も直近の営業日のものである必要がある。
    """
    from ks_util import recent_weekday  # 遅延 import

    today = now.date().isoformat()
    mine = [s for s in sources if s.get("broker") == broker]
    for kind in ("現物", "信用"):
        if not any(s.get("kind") == kind for s in mine):
            return f"{broker} の{kind} CSV が取り込まれていません"
    for s in mine:
        if s.get("as_of") != today:
            return (f"{broker} の{s.get('kind')} CSV が {s.get('as_of')} のままです。"
                    "今日の残高 CSV を取り込んでから入力してください")
    expected = recent_weekday(now).isoformat()
    if any(d is None or d < expected for d in price_dates):
        return "株価が最新の営業日になっていません。日次バッチ (19時) の後に入力してください"
    return None


# ===========================================
# 目標の維持率と、運用総額の適正エリア (issue #480)
# ===========================================

# 感情スコアの段 → 目標の維持率 (%)。投資メモの感情スコアシートの「資産比」(自己資本 ÷ 建玉)
STEP_TARGET_RATES = {-3: 50, -2: 60, -1: 70, 0: 80, 1: 90, 2: 100, 3: 120}
NEUTRAL_STEP = 0
# 感情スコアの選択肢 (段, 状態, 自己申告の目安)。投資メモの感情スコアシートから
EMOTION_CHOICES = (
    (-3, "極度の恐怖", "パニック"),
    (-2, "恐怖", "全体的に弱気・損切りしたくなる・買うのが怖い / 売り焦り: 一時的な悪材料だが需給悪化でとにかく売りたい"),
    (-1, "不安", "様子見したい・持ちたくない / 安堵: 下がるのを覚悟した後の戻りで売りたくなる"),
    (0, "中立", "落ち着いて判断できている"),
    (1, "強気", "少し強気で買いたくなる / 貪欲: 反発を上追いで買いたくなる"),
    (2, "興奮", "含み益が増えてワクワクしている・買い増ししたい / 好決算S高剥がれを拾いたくなる"),
    (3, "過信", "自分が相場を読めると錯覚している"),
)
# 適正エリアは目標の維持率 ± この幅 (pt)
AREA_BAND_PT = 5
# 日本版 F&G の Extreme の境界。この外側では逆張りで 1 段ずらす
FNG_EXTREME_FEAR = 25
FNG_EXTREME_GREED = 75
# F&G が Extreme でないとき、市場ステートで攻める側 (マイナス) にかける上限の段。
# 上昇確定は上限なし。ステートが取れない日は上昇圧力下と同じにする
STATE_STEP_FLOORS = {
    "confirmed_uptrend": None,
    "uptrend_under_pressure": -1,
    "market_in_correction": 0,
    None: -1,
}
# 感情に関係なく守る下限: 全保有が一律にこれだけ下がっても追証 (20%) にならない
FLOOR_FALL_PCT = 20
MARGIN_CALL_RATE_PCT = 20


def format_step(step: int) -> str:
    """段を表示用の文字列にする (+1 / 0 / −1)。"""
    return f"{step:+d}".replace("+0", "0").replace("-", "−")


def target_step(emotion: Optional[int], fng: Optional[float],
                state: Optional[str]) -> Tuple[int, List[str]]:
    """感情スコア・日本版 F&G・市場ステートから目標の段 (−3〜+3) を決める。

    emotion: 有効な感情スコア (期限切れ・未入力は None で、中立 0 として扱う)
    Returns: (段, 決まり方の説明の list)。例 (−1, ["感情 0", "F&G Extreme Fear (22) で −1"])
    """
    step = NEUTRAL_STEP if emotion is None else int(emotion)
    parts = ["感情 未入力 (0)" if emotion is None else f"感情 {format_step(step)}"]
    if fng is not None and fng < FNG_EXTREME_FEAR:
        step -= 1
        parts.append(f"F&G Extreme Fear ({fng:.0f}) で −1")
    elif fng is not None and fng >= FNG_EXTREME_GREED:
        step += 1
        parts.append(f"F&G Extreme Greed ({fng:.0f}) で +1")
    else:
        floor = STATE_STEP_FLOORS.get(state, STATE_STEP_FLOORS[None])
        if floor is not None and step < floor:
            step = floor
            parts.append(f"市場ステートの上限で {format_step(floor)}")
    return max(-3, min(3, step)), parts


def target_area(guarantee: float, tate_total: float, spot_value: float,
                margin_value: float, step: int) -> Dict[str, Any]:
    """目標の維持率から、運用総額 (現物時価 + 信用建玉の時価) の適正エリアを出す。

    建玉の増減は信用で行う前提。目標の建玉 B_t = 受入保証金 ÷ 目標維持率 との差を時価に直す:
    増やすときは時価で建つのでそのまま、減らすときは比例で返済するとみなして (時価 ÷ 建玉) を掛ける。
    返済時に評価益が実現して保証金が増える分は入れない (減らす量がやや多めに出る)。
    多め側の端は、全保有が一律に FLOOR_FALL_PCT 下がっても追証にならない建玉で頭打ちにする。
    """
    total = spot_value + margin_value

    def value_for_tate(target_tate: float) -> float:
        diff = target_tate - tate_total
        if diff < 0 and tate_total > 0:
            diff *= margin_value / tate_total
        return max(spot_value, total + diff)  # 信用を全部返済しても現物は残る

    def value_for_rate(rate_pct: float) -> float:
        return value_for_tate(guarantee / (rate_pct / 100.0))

    rate = STEP_TARGET_RATES[step]
    lower = value_for_rate(rate + AREA_BAND_PT)
    upper = value_for_rate(rate - AREA_BAND_PT)
    target = value_for_rate(rate)
    # fall_to_threshold と同じ式 (G − T·B) / (B + 0.8·S) ≥ 下落率 f を B について解く:
    # B ≤ (G − 0.8·f·S) / (T + f)
    t, f = MARGIN_CALL_RATE_PCT / 100.0, FLOOR_FALL_PCT / 100.0
    floor_value = value_for_tate((guarantee - f * SUBSTITUTE_RATIO * spot_value) / (t + f))
    floor_capped = upper > floor_value
    if floor_capped:
        upper = floor_value
        target = min(target, upper)
        lower = min(lower, upper)
    if total > upper:
        position, deviation = "over", total - upper
    elif total < lower:
        position, deviation = "under", total - lower
    else:
        position, deviation = "within", 0.0
    return {
        "step": step, "target_rate": rate, "total_value": total,
        "area_lower": lower, "area_upper": upper, "target_value": target,
        "neutral_value": min(value_for_rate(STEP_TARGET_RATES[NEUTRAL_STEP]), floor_value),
        "floor_capped": floor_capped, "position": position, "deviation": deviation,
    }
