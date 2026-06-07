"""日本市場版 Fear & Greed Index (issue #212)。

CNN Fear & Greed Index の構造 (成分構成・等ウェイト平均・5段階 rating) に準拠しつつ、
各成分の正規化は独自実装 (直近2年の percentile rank) する日本市場版センチメント指数。

CNN 原典との差分 (運用上の限界は issue #212 参照):
  1. 成分数 6→4 (第1弾)。Junk Bond Demand は日本市場に対応物が無く除外。
     PCR / Safe Haven(JGB10年) はデータ源未確定のため第1弾では除外し、
     データが揃い次第 components に追加する (取れない成分は平均から除外)。
  2. Stock Price Strength が「52週」ではなく「年初来」基準 (データ源の都合)。
  3. Stock Price Breadth が出来高ベースではなく銘柄数ベース。
  4. 正規化は CNN 非公開のため独自。外れ値1点でレンジが圧縮される min-max を避け、
     直近2年の percentile rank を採用 (単一の極値に頑健)。

第1弾の4成分 (いずれも nikkei225jp.com 由来、新規データ源不要):
  - Market Momentum     : 日経225 vs 125日移動平均の乖離率
  - Stock Price Strength: 東証プライム 新高値数 - 新安値数
  - Stock Price Breadth : 東証プライム 値上がり数 - 値下がり数
  - Market Volatility   : 日経VI (高いほど Fear なので方向反転)
"""

# 直近2年 min-max 正規化に使う履歴本数 (約2年 = 約490営業日)。
_NORM_WINDOW = 490
# モメンタムの移動平均日数 (CNN は 125日)。
_MA_DAYS = 125


def normalize_percentile(value, history):
    """直近履歴に対する value の percentile rank を 0-100 で返す (issue #212)。

    「history 中で value 以下の割合」(中央順位法) を百分率にする。
    min-max と違い単一の外れ値に頑健で、レンジが極端な値1点に圧縮されない。
    history が空なら中立 50。同値が多いケースも中央順位で滑らかに扱う。
    """
    if not history:
        return 50.0
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    # 中央順位法: 同値は半分が下とみなす (value 自身を含めないため +0.5*equal)
    rank = (below + 0.5 * equal) / len(history)
    return max(0.0, min(100.0, rank * 100.0))


def normalize_min_max(value, history):
    """直近の min-max で 0-100 にスケールする (旧方式、参考用に残置)。

    history は正規化の基準となる過去値リスト (最新値 value は含めない想定)。
    履歴が空・最大最小が一致なら中立 50 を返す。0-100 にクリップする。
    """
    if not history:
        return 50.0
    lo, hi = min(history), max(history)
    if hi == lo:
        return 50.0
    score = (value - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, score))


def _ma(values):
    """末尾 _MA_DAYS 本の単純移動平均。データ不足なら全件平均。"""
    window = values[-_MA_DAYS:] if len(values) >= _MA_DAYS else values
    return sum(window) / len(window) if window else 0.0


def _series_to_scores(series, value_fn):
    """時系列 (日付昇順) から「各時点の指標値」を計算した数値リストを返す。

    value_fn(rows_up_to_i) -> float|None。None の行は除外。
    モメンタムのように過去に依存する指標を min-max 履歴化するために使う。
    """
    out = []
    for i in range(len(series)):
        v = value_fn(series[: i + 1])
        if v is not None:
            out.append(v)
    return out


def compute_component_momentum(breadth_history):
    """Market Momentum: 日経225 vs 125日MA の乖離率を 0-100 化 (高い=Greed)。

    breadth_history: [{date, nikkei_close, ...}, ...] 日付昇順。
    乖離率 = (終値 - 125日MA) / 125日MA * 100 を直近2年で min-max 正規化。
    """
    closes = [r["nikkei_close"] for r in breadth_history if r.get("nikkei_close") is not None]
    if len(closes) < 2:
        return None

    def kairi(window):
        if len(window) < 1:
            return None
        ma = _ma(window)
        if ma == 0:
            return None
        return (window[-1] - ma) / ma * 100.0

    hist = _series_to_scores([c for c in closes], lambda w: kairi(w))
    if not hist:
        return None
    latest = hist[-1]
    score = normalize_percentile(latest, hist[:-1][-_NORM_WINDOW:])
    return score, latest  # raw = 125日MA乖離率(%)


def compute_component_strength(breadth_history):
    """Stock Price Strength: 新高値 - 新安値 を 0-100 化 (高い=Greed)。

    Returns: (score 0-100, raw=新高値-新安値) または None。
    """
    diffs = [
        r["new_high"] - r["new_low"]
        for r in breadth_history
        if r.get("new_high") is not None and r.get("new_low") is not None
    ]
    if len(diffs) < 2:
        return None
    return normalize_percentile(diffs[-1], diffs[:-1][-_NORM_WINDOW:]), diffs[-1]


def compute_component_breadth(breadth_history):
    """Stock Price Breadth: 値上がり - 値下がり を 0-100 化 (高い=Greed)。

    Returns: (score 0-100, raw=値上がり-値下がり) または None。
    """
    diffs = [
        r["advances"] - r["declines"]
        for r in breadth_history
        if r.get("advances") is not None and r.get("declines") is not None
    ]
    if len(diffs) < 2:
        return None
    return normalize_percentile(diffs[-1], diffs[:-1][-_NORM_WINDOW:]), diffs[-1]


def compute_component_volatility(vi_history):
    """Market Volatility: 日経VI を 0-100 化し方向反転 (VI が高い=Fear)。

    vi_history: [{date, nikkei_vi}, ...] 日付昇順。
    Returns: (score 0-100=反転後, raw=日経VI) または None。
    """
    vis = [r["nikkei_vi"] for r in vi_history if r.get("nikkei_vi") is not None]
    if len(vis) < 2:
        return None
    raw_score = normalize_percentile(vis[-1], vis[:-1][-_NORM_WINDOW:])
    return 100.0 - raw_score, vis[-1]  # VI は高いほど Fear なので反転、raw=VI実値


def _rating(score):
    """0-100 スコアを CNN 準拠の 5 段階 rating に変換する。"""
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score < 55:
        return "Neutral"
    if score < 75:
        return "Greed"
    return "Extreme Greed"


def compute_fear_greed_jp(breadth_history, vi_history):
    """4成分から日本市場版 Fear & Greed の合成スコアを計算する。

    取得できなかった成分は None として平均から除外する (フォールバック)。
    全成分 None なら None を返す。

    Returns:
        dict|None: {
            "score": float, "rating": str,
            "components": {name: {"score": 0-100, "raw": float} | None},
        }
        components の各成分は score(0-100) と raw(元データ値) を持つ。
        未取得成分は None。
    """
    # VI履歴を breadth の最新日以前に揃える。
    # 揃えないと過去日の一括再計算時に VI 成分だけ最新 VI で正規化されてしまい、
    # 時系列スコア (history の前日比/5日前比) が不正になる (issue #212 セルフレビュー指摘)。
    if breadth_history and vi_history:
        as_of = breadth_history[-1].get("date")
        if as_of:
            vi_history = [v for v in vi_history if v.get("date", "") <= as_of] or vi_history

    raw_components = {
        "momentum": compute_component_momentum(breadth_history),
        "strength": compute_component_strength(breadth_history),
        "breadth": compute_component_breadth(breadth_history),
        "volatility": compute_component_volatility(vi_history),
    }
    valid = [c[0] for c in raw_components.values() if c is not None]
    if not valid:
        return None
    score = sum(valid) / len(valid)
    components = {}
    for k, c in raw_components.items():
        if c is None:
            components[k] = None
        else:
            components[k] = {"score": round(c[0], 1), "raw": round(c[1], 2)}
    return {
        "score": round(score, 1),
        "rating": _rating(score),
        "components": components,
    }
