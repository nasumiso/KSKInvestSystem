"""推定維持率 (信用の保証金率) の概算ロジック (issue #527)。

証券会社の保証金データは CSV で取れないので、ユーザーが入れた保証金率を基準値にし、
そのあとは残高 CSV (portfolio_shelve の position) と株価の変化から差分で推定する。
厳密ではなく概算。純粋関数だけを置く (DB・Flask に依存しない)。

前提 (spec に同じ内容を書く):
- 信用は買建のみ。売建がある証券会社は推定しない
- 信用建玉の評価損だけを保証金に算入する (SBI のヘルプが明記。評価益は算入しない)
- 現物は代用として掛目 0.8 で値動きを算入する (現物の全株が代用とは限らない近似)
- 基準日以後の実現損益・入出金・金利・現物の売却代金は追わない (基準値を入れ直すと戻る)
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


def estimate(baseline: Dict[str, Any], snapshot: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """基準値と今のスナップショットから、受入保証金・維持率・追証までの下落率を推定する。

    Returns: (結果, 理由)。結果 = {"guarantee", "tate_total", "rate_pct", "spot_value", "skipped"}。
    """
    closes = snapshot["closes"]
    delta = 0.0
    # 信用建玉の評価損の変化。建玉が減った分の損は、決済しても保証金から消えないので基準日の値で残す
    for code in set(baseline["margin"]) | set(snapshot["margin"]):
        base = baseline["margin"].get(code)
        now = snapshot["margin"].get(code)
        loss_base = (_margin_loss(base["qty"], base["avg_price"], base["close"])
                     if base and base.get("close") else 0.0)
        # 基準日の損のうち、今も残っている建玉の分。減った分は決済しても保証金から消えない
        remaining = min(1.0, (now["qty"] if now else 0) / base["qty"]) if base else 1.0
        stays = loss_base * remaining
        if now and closes.get(code):
            loss_now = _margin_loss(now["qty"], now["avg_price"], closes[code])
        else:
            loss_now = stays  # 終値が無ければ変化なしとして扱う
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
    return {
        "guarantee": guarantee,
        "tate_total": tate_total,
        "rate_pct": guarantee / tate_total * 100.0,
        "spot_value": spot_value,
        "skipped": snapshot["skipped"],
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
    if not mine:
        return f"{broker} の残高 CSV が取り込まれていません"
    for s in mine:
        if s.get("as_of") != today:
            return (f"{broker} の{s.get('kind')} CSV が {s.get('as_of')} のままです。"
                    "今日の残高 CSV を取り込んでから入力してください")
    expected = recent_weekday(now).isoformat()
    if any(d is None or d < expected for d in price_dates):
        return "株価が最新の営業日になっていません。日次バッチ (19時) の後に入力してください"
    return None
