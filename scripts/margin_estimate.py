"""推定維持率 (信用の保証金率) の概算ロジック (issue #527)。

証券会社の保証金データは CSV で取れないので、ユーザーが入れた保証金率を基準値にし、
そのあとは残高 CSV (portfolio_shelve の position) と株価の変化から差分で推定する。
厳密ではなく概算。純粋関数だけを置く (DB・Flask に依存しない)。

前提 (spec に同じ内容を書く):
- 信用は買建のみ。売建がある証券会社は推定しない
- 信用建玉の評価損だけを保証金に算入する (SBI のヘルプが明記。評価益は算入しない)
- 現物は代用として掛目 0.8 で値動きを算入する (現物の全株が代用とは限らない近似)。
  NISA 口座の現物は代用にならないので数えない (運用総額には数える)
- 基準日より後の信用返済の決済損益は、約定 (fill) から足す。入出金・金利・現物の売却代金は
  追わない (基準値を入れ直すと戻る)
- 銘柄単位の合算スナップショットなので、基準日の前後で同じ銘柄の株が入れ替わっても区別できない
"""

from datetime import date, datetime, timedelta
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
    nisa_value = 0.0
    for p in positions:
        if p.get("broker") != broker or not p.get("qty") or p["qty"] <= 0:
            continue
        kind = p.get("kind")
        if kind == "信用売建":
            return None, "信用売建があるため推定しません"
        if kind not in ("信用", "現物"):
            continue
        if kind == "現物" and "NISA" in (p.get("account") or ""):
            # NISA 口座の株は代用有価証券にならない。運用総額にだけ数える
            nisa_value += p["qty"] * (closes.get(p["code_s"]) or p.get("avg_price") or 0)
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
    snapshot = {"margin": {}, "spot": {}, "closes": {}, "tate_total": tate_total, "skipped": 0,
                "nisa_value": nisa_value}
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
    outside_cash: float = 0.0,
) -> Dict[str, Any]:
    """入力した保証金率とスナップショットから、基準値のレコードを作る。

    outside_cash: 保証金に入っていない現金 (SBI の預り金・スィープ口座、楽天の預り金など)。
      保証金率には効かず、自己資本 (equity) にだけ足す。次に入れ直すまで一定とみなす (#593)
    """
    closes = snapshot["closes"]
    return {
        "broker": broker,
        "date": base_day,
        "rate_pct": float(rate_pct),
        "tate_total": snapshot["tate_total"],
        "guarantee": rate_pct / 100.0 * snapshot["tate_total"],
        "margin": {c: {**v, "close": closes.get(c)} for c, v in snapshot["margin"].items()},
        "spot": {c: {"qty": v["qty"], "close": closes.get(c)} for c, v in snapshot["spot"].items()},
        "outside_cash": float(outside_cash),
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
        "nisa_value": snapshot.get("nisa_value", 0.0),
        "margin_value": margin_value,
        # 下落率を銘柄ごとに出すための建玉 (株数, 建単価, 終値)
        "margin_rows": [(v["qty"], v["avg_price"], closes[c])
                        for c, v in snapshot["margin"].items() if closes.get(c)],
        "skipped": snapshot["skipped"],
        "realized": realized_total,
        "outside_cash": baseline.get("outside_cash", 0.0),
    }, None


def fall_to_threshold(estimates: List[Dict[str, Any]], threshold_pct: float) -> float:
    """全保有が一律に下がるとして、保証金率が threshold_pct に届く下落率 (%)。0〜100。

    estimate() の結果 (1社分、または合算する複数社分) を受け取り、建玉ごとに時価で
    評価損を出し直して二分法で探す。評価益のある建玉は、益が消えるまで保証金を減らさない。
    """
    tate_total = sum(e["tate_total"] for e in estimates)
    if tate_total <= 0:
        return 0.0
    base = sum(e["guarantee"] for e in estimates)
    spot = sum(e["spot_value"] for e in estimates)
    rows = [r for e in estimates for r in e.get("margin_rows", [])]
    target = threshold_pct / 100.0 * tate_total

    def guarantee_at(x: float) -> float:
        loss = sum(_margin_loss(q, a, c * (1 - x)) - _margin_loss(q, a, c) for q, a, c in rows)
        return base + loss - SUBSTITUTE_RATIO * spot * x

    if guarantee_at(0.0) <= target:
        return 0.0
    if guarantee_at(1.0) > target:
        return 100.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if guarantee_at(mid) > target else (lo, mid)
    return hi * 100.0


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
        "nisa_value": sum(r.get("nisa_value", 0.0) for r in per_broker.values()),
        "outside_cash": sum(r.get("outside_cash", 0.0) for r in per_broker.values()),
        "margin_value": sum(r["margin_value"] for r in per_broker.values()),
        # 自己資本 (equity) で信用の評価益を足すのに使う (#593)
        "margin_rows": [row for r in per_broker.values() for row in r.get("margin_rows", [])],
        "rate_pct": guarantee / tate_total * 100.0,
        "falls": {t: fall_to_threshold(list(per_broker.values()), t) for t in THRESHOLDS},
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
# 張る量: ふだん 1.2倍と口座の上限、信用の底で買い向かい (issue #593)
# ===========================================
# 正本は doc/投資戦略.md の「張りたい量」「張れる量」、検証は #594 (money_rule_sim.py)。
# 倍率 = 運用総額 (現物時価 + NISA + 信用建玉の時価) ÷ 自己資本。

# 倍率の段 (小さい順)。今週の目標はこの段を週1段ずつ動く
LADDER = [0.8, 1.0, 1.1, 1.2, 1.4, 1.6, 2.0]
# 張りたい量: ふだんの倍率と、信用の底からの買い向かいの倍率・期間 (月)
BASE_MULTIPLE = 1.2
BOOST_MULTIPLE = 1.6
BOOST_MONTHS = 3
# 張れる量: 2.0 × (1 − 口座の高値からの下落率 ÷ 幅)、下限 0.8倍
CAP_MAX = 2.0
CAP_WIDTH = 0.20
CAP_FLOOR = 0.8
# 信用評価損益率の公表値は約1週遅れ。データ日 + この日数から使えるとみなす (#594 と同じ)
CREDIT_LAG_DAYS = 7
# 適正エリアは目標の運用総額 ± この割合
AREA_BAND_RATIO = 0.05
# 感情スコアの選択肢 (段, 状態, 自己申告の目安)。投資メモの感情スコアシートから。
# #593 から量の計算には使わず、日曜の見直しで記録するだけ
EMOTION_CHOICES = (
    (-3, "極度の恐怖", "パニック"),
    (-2, "恐怖", "全体的に弱気・損切りしたくなる・買うのが怖い / 売り焦り: 一時的な悪材料だが需給悪化でとにかく売りたい"),
    (-1, "不安", "様子見したい・持ちたくない / 安堵: 下がるのを覚悟した後の戻りで売りたくなる"),
    (0, "中立", "落ち着いて判断できている"),
    (1, "強気", "少し強気で買いたくなる / 貪欲: 反発を上追いで買いたくなる"),
    (2, "興奮", "含み益が増えてワクワクしている・買い増ししたい / 好決算S高剥がれを拾いたくなる"),
    (3, "過信", "自分が相場を読めると錯覚している"),
)
# 日本版 F&G の Extreme の境界。量には使わず、Extreme Fear は準備、Extreme Greed は保有の点検の合図
FNG_EXTREME_FEAR = 25
FNG_EXTREME_GREED = 75
# 信用評価損益率 (%) の5段階の境界 (#535)。一般的な目安。
# 底値圏 (−15 以下) で前週より改善した週が買い向かいの合図 (#593)
CREDIT_CEILING = -3.0
CREDIT_BULL = -6.0
CREDIT_NEUTRAL = -10.0
CREDIT_BOTTOM = -15.0
# 前週値として使う、最新との日付の離れの上限 (日)。表示の「改善」の判定に使う
CREDIT_PREV_MAX_GAP_DAYS = 10
# 日経平均の騰落 1% あたりの信用評価損益率の変化 (pt)。2026-04〜11 の30週の週次で傾き 0.71・相関 0.90。
# 今日の値の推定 (表示だけ。買い向かいの合図は公表値で決める)
CREDIT_NIKKEI_BETA = 0.71
# 張れる量とは別に守る頭打ち: 全保有が一律にこれだけ下がっても追証 (20%) にならない
FLOOR_FALL_PCT = 20
MARGIN_CALL_RATE_PCT = 20


def fng_label(fng: Optional[float]) -> Optional[str]:
    """日本版 F&G を CNN と同じ5区分の名前にする。Extreme の境界は判定と同じ (25未満 / 75以上)。"""
    if fng is None:
        return None
    if fng < FNG_EXTREME_FEAR:
        return "Extreme Fear"
    if fng < 45:
        return "Fear"
    if fng <= 55:
        return "Neutral"
    if fng < FNG_EXTREME_GREED:
        return "Greed"
    return "Extreme Greed"


def credit_label(rate: Optional[float]) -> Optional[str]:
    """信用評価損益率を5段階の名前にする (天井圏 / 強気 / 中立 / 弱気 / 底値圏)。"""
    if rate is None:
        return None
    if rate >= CREDIT_CEILING:
        return "天井圏"
    if rate > CREDIT_BULL:
        return "強気"
    if rate > CREDIT_NEUTRAL:
        return "中立"
    if rate > CREDIT_BOTTOM:
        return "弱気"
    return "底値圏"


def credit_prev_rate(history: List[Dict[str, Any]]) -> Optional[float]:
    """credit_balance.json の history から前週値を返す。最新と離れすぎていれば None。"""
    if len(history) < 2:
        return None
    latest, prev = history[-1], history[-2]
    try:
        gap = (datetime.strptime(latest["date"], "%Y-%m-%d")
               - datetime.strptime(prev["date"], "%Y-%m-%d")).days
    except (KeyError, TypeError, ValueError):
        return None
    return prev.get("credit_eval_rate") if gap <= CREDIT_PREV_MAX_GAP_DAYS else None


def credit_nowcast(rate: Optional[float], data_date: Optional[str],
                   price_log: List[Any]) -> Optional[Tuple[float, str, float]]:
    """公表値と日経平均の騰落から、今日の信用評価損益率を推す (推定値, 指数の日付, 騰落率%)。

    price_log: 日経平均の日足 (日付降順の (date, 終値))。公表日以前の終値が無い、または
    最新の日足が公表日より後でないときは None (呼び出し側は前週比で判定する)。
    """
    if rate is None or not data_date or not price_log:
        return None
    rows = [(d if isinstance(d, str) else d.isoformat())[:10] for d, _ in price_log]
    base = next((c for d, (_, c) in zip(rows, price_log) if d <= data_date), None)
    latest_date, latest = rows[0], price_log[0][1]
    if not base or latest_date <= data_date:
        return None
    move = (latest / base - 1) * 100
    return rate + CREDIT_NIKKEI_BETA * move, latest_date, move


def credit_improving(rate: Optional[float], prev: Optional[float],
                     now: Optional[float] = None) -> bool:
    """改善した (損益率が上がった) か。推定値 now があれば公表値と、無ければ前週値と比べる。"""
    if rate is not None and now is not None:
        return now > rate
    return rate is not None and prev is not None and rate > prev


def format_step(step: int) -> str:
    """段を表示用の文字列にする (+1 / 0 / −1)。"""
    return f"{step:+d}".replace("+0", "0").replace("-", "−")



def ladder_floor(x: float) -> float:
    """x 以下で一番高い段。x が一番下の段より小さければ一番下の段。"""
    below = [v for v in LADDER if v <= x + 1e-9]
    return below[-1] if below else LADDER[0]


def step_toward(current: float, target: float) -> float:
    """いまの段から目標の段へ、隣の段を1つだけ動く。"""
    i, j = LADDER.index(current), LADDER.index(ladder_floor(target))
    return LADDER[i + (j > i) - (j < i)]


def room_cap(drawdown: float, width: float = CAP_WIDTH) -> float:
    """張れる量: 2.0 × (1 − 口座の高値からの下落率 ÷ 幅)、下限 0.8倍。drawdown は 0〜1。"""
    return max(CAP_FLOOR, CAP_MAX * (1 - drawdown / width))


def equity(est: Dict[str, Any]) -> float:
    """推定 (estimate() の結果か、その合算) から自己資本を出す。

    受入保証金 G = 保証金の現金 + 0.8·代用の現物 − 信用の評価損 で、評価益を含まない。
    自己資本 = 現金 + 現物 + NISA + 信用の評価損益
             = G + 0.2·現物 + NISA + 信用の評価益 + 預り金 (保証金とは別。SBI だけ入力)。
    """
    gains = sum(max(0.0, q * (c - a)) for q, a, c in est.get("margin_rows") or [])
    return (est["guarantee"] + (1 - SUBSTITUTE_RATIO) * est["spot_value"]
            + est.get("nisa_value", 0.0) + gains + est.get("outside_cash", 0.0))


def _add_months(day: date, months: int) -> date:
    """暦の月を足す。月末を超える日はその月の末日に寄せる。"""
    y, m = divmod(day.month - 1 + months, 12)
    y, m = day.year + y, m + 1
    for d in range(day.day, 0, -1):
        try:
            return date(y, m, d)
        except ValueError:
            continue
    raise ValueError(day)


def boost_signals(credit_history: List[Dict[str, Any]]) -> List[str]:
    """買い向かいの合図が使えるようになった日 (データ日 + CREDIT_LAG_DAYS) の list (古い順)。

    合図は、公表値が底値圏 (−15% 以下) で、1つ前の公表値より改善した週 (#594 の _credit_confirms と同じ)。
    """
    rows = sorted((h for h in credit_history or []
                   if h.get("date") and h.get("credit_eval_rate") is not None),
                  key=lambda h: h["date"])
    out = []
    for prev, now in zip(rows, rows[1:]):
        rate = now["credit_eval_rate"]
        if rate <= CREDIT_BOTTOM and rate > prev["credit_eval_rate"]:
            out.append((date.fromisoformat(now["date"]) + timedelta(days=CREDIT_LAG_DAYS)).isoformat())
    return out


def boost_until(credit_history: List[Dict[str, Any]], day: str) -> Optional[str]:
    """day 以前の最後の合図から BOOST_MONTHS か月後の日。day がその日以前なら買い向かいの期間。

    合図が続けば延びる。合図のあとに悪化しても取り消さない (#594 の検証と同じ)。
    期間が終わっている・合図が無いなら None。
    """
    known = [s for s in boost_signals(credit_history) if s <= day]
    if not known:
        return None
    until = _add_months(date.fromisoformat(known[-1]), BOOST_MONTHS).isoformat()
    return until if day <= until else None


def want_multiple(credit_history: List[Dict[str, Any]], day: str) -> float:
    """張りたい量: ふだん 1.2倍、買い向かいの期間は 1.6倍。"""
    return BOOST_MULTIPLE if boost_until(credit_history, day) else BASE_MULTIPLE


def _quarter(day: str) -> Tuple[int, int]:
    return int(day[:4]), (int(day[5:7]) - 1) // 3


def drawdown_series(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """日次ログの equity を古い順にたどり、各行の高値と下落率 (0〜1) を返す。

    高値は更新したときだけ上げる。四半期が変わった最初の行の前で、高値を直前の行の資産に置き換える。
    equity_gap (保証金率の入れ直しで入った入出金などの差) がある行は、高値を同じ額だけずらす。
    equity が無い行は飛ばす。Returns: [{"date", "equity", "peak", "drawdown"}]
    """
    out: List[Dict[str, Any]] = []
    peak = None
    for h in sorted(history, key=lambda r: r.get("date") or ""):
        e = h.get("equity")
        if e is None or not h.get("date"):
            continue
        if peak is None:
            peak = e
        else:
            if _quarter(h["date"]) != _quarter(out[-1]["date"]):
                peak = out[-1]["equity"]
            peak += h.get("equity_gap") or 0.0
        peak = max(peak, e)
        out.append({"date": h["date"], "equity": e, "peak": peak,
                    "drawdown": 1 - e / peak if peak > 0 else 0.0})
    return out


def _week_start(day: str) -> str:
    """day を含む週の土曜 (週は土曜始まり。土日の見直しで来週の目標を出すため)。"""
    d = date.fromisoformat(day)
    return (d - timedelta(days=(d.weekday() - 5) % 7)).isoformat()


def week_targets(history: List[Dict[str, Any]], credit_history: List[Dict[str, Any]]
                 ) -> List[Dict[str, Any]]:
    """週ごとの目標の倍率を、日次ログから計算し直す (状態は持たない)。

    週は土曜始まり。ある週の目標は、前の週の最後の行で min(張りたい量, 張れる量) へ
    先週の目標から1段動かす。最初の週は、起点の行の実際の倍率以下で一番高い段を張れる量で頭打ちにする。
    history は equity と multiple がある行を使う (暫定行を含めてよい)。
    Returns: [{"week", "target", "want", "cap", "from"}] (from は判定に使った行の日付)
    """
    rows = [h for h in sorted(history, key=lambda r: r.get("date") or "")
            if h.get("equity") is not None and h.get("multiple") is not None]
    if not rows:
        return []
    dd = {r["date"]: r["drawdown"] for r in drawdown_series(rows)}
    last_of_week: Dict[str, Dict[str, Any]] = {}
    for h in rows:
        last_of_week[_week_start(h["date"])] = h
    weeks = sorted(last_of_week)
    first = rows[0]
    target = min(ladder_floor(first["multiple"]), ladder_floor(room_cap(dd[first["date"]])))
    out = [{"week": weeks[0], "target": target, "want": None, "cap": None, "from": first["date"]}]
    for prev_week in weeks:
        basis = last_of_week[prev_week]
        want = want_multiple(credit_history, basis["date"])
        cap = room_cap(dd[basis["date"]])
        target = step_toward(target, min(want, cap))
        nxt = (date.fromisoformat(prev_week) + timedelta(days=7)).isoformat()
        out.append({"week": nxt, "target": target, "want": want, "cap": cap, "from": basis["date"]})
    return out


def target_for(weeks: List[Dict[str, Any]], day: str) -> Optional[Dict[str, Any]]:
    """day を含む週の目標 (week_targets の1行)。無ければ None。"""
    ws = _week_start(day)
    return next((w for w in reversed(weeks) if w["week"] <= ws), None)


def target_area(guarantee: float, tate_total: float, spot_value: float,
                margin_value: float, target_total: float, nisa_value: float = 0.0) -> Dict[str, Any]:
    """目標の運用総額 (目標の倍率 × 自己資本) を中心に、運用総額の適正エリアを出す。

    適正エリアは目標 ± AREA_BAND_RATIO。多め側の端は、全保有が一律に FLOOR_FALL_PCT 下がっても
    追証にならない建玉で頭打ちにする (建玉の増減は信用で行う前提。減らすときは比例で返済するとみなす)。
    """
    # NISA の現物は代用にならないが、運用総額 (値動きを受ける額) には入る
    total = spot_value + nisa_value + margin_value

    def value_for_tate(target_tate: float) -> float:
        diff = target_tate - tate_total
        if diff < 0 and tate_total > 0:
            diff *= margin_value / tate_total
        return max(spot_value + nisa_value, total + diff)  # 信用を全部返済しても現物は残る

    lower = target_total * (1 - AREA_BAND_RATIO)
    upper = target_total * (1 + AREA_BAND_RATIO)
    target = target_total
    # 頭打ちは表示の下落率 (fall_to_threshold) より厳しめの線形式で決める。建玉が建単価で
    # 下がり、評価益も減ると見る (G − T·B) / (B + 0.8·S) ≥ 下落率 f を B について解く:
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
        "total_value": total, "area_lower": lower, "area_upper": upper, "target_value": target,
        "floor_capped": floor_capped, "position": position, "deviation": deviation,
    }
