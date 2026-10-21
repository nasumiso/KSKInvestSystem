"""日本市場版 Fear & Greed Index (issue #212)。

CNN Fear & Greed Index の構造 (成分構成・等ウェイト平均・5段階 rating) に準拠しつつ、
各成分の正規化は独自実装 (直近2年の percentile rank) する日本市場版センチメント指数。

CNN 原典との差分 (運用上の限界は issue #212 参照):
  1. 成分数 7→5。Junk Bond Demand は日本市場に対応物が無く除外。
     Put/Call 比率は JPX の日次データが当日分しか置かれず、貯める仕組みが要るため見送り
     (#524)。調査結果は doc/spec/日本版FearGreed仕様.md。取れない成分は平均から除外する。
  2. Stock Price Strength が「52週」ではなく「年初来」基準 (データ源の都合)。
  3. Stock Price Breadth は CNN と同じく McClellan Summation Index (累積型) だが、
     出来高ベースではなく銘柄数ベース (issue #524)。daily2year.json の列 [19]〜[24]
     が値上がり・値下がりの出来高らしいが未確認のため、確認できるまで銘柄数で作る。
     差は ratio-adjusted ((値上がり-値下がり)/(値上がり+値下がり)×1000) で取り、
     東証プライムの銘柄数の増減で水準がずれないようにする。
  4. 正規化は CNN 非公開のため独自。外れ値1点でレンジが圧縮される min-max を避け、
     直近2年の percentile rank を採用 (単一の極値に頑健)。
  5. Safe Haven Demand の債券側は、価格指数が無料で取れないため、10年金利の変化に
     デュレーションを掛けた近似 (利息収入は無視)。

5成分:
  - Market Momentum     : 日経225 vs 125日移動平均の乖離率
  - Stock Price Strength: 東証プライム 新高値数 - 新安値数
  - Stock Price Breadth : 東証プライム 騰落の McClellan Summation Index (#524 で累積型に)
  - Market Volatility   : 日経VI とその50日移動平均の乖離率 (#524。高いほど Fear なので反転)
  - Safe Haven Demand   : 日経225 と 10年国債の20日騰落率の差 (#524。財務省の金利から近似)
"""

# 直近2年 min-max 正規化に使う履歴本数 (約2年 = 約490営業日)。
_NORM_WINDOW = 490
# モメンタムの移動平均日数 (CNN は 125日)。
_MA_DAYS = 125
# McClellan Oscillator の EMA 日数 (原典の 10% / 5% 平滑 = 19日 / 39日)。
_MCO_FAST = 19
_MCO_SLOW = 39
# Summation Index の先頭で正規化の履歴に使わない本数 (約半年)。EMA の初期値の影響が
# 残る区間を外す。39本だと、daily2year.json が1日進んで起点が動くだけでスコアが
# 最大5点ほど動いた (合成データで確認)。120本で1点未満になる。
_MCO_WARMUP = 120
# 日経VI を比べる移動平均の日数 (CNN は VIX の50日移動平均)。
_VI_MA_DAYS = 50
# 安全資産の需要: 株と債券の騰落率を比べる日数 (CNN は20日)。
_SAFE_HAVEN_DAYS = 20
# 10年国債の修正デュレーション (年)。金利1%の上昇で価格が約9%下がる近似。
_JGB10_DURATION = 9.0


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


def mcclellan_summation(advances, declines):
    """騰落数の列から McClellan Summation Index の列を返す (issue #524)。

    差 = (値上がり-値下がり)/(値上がり+値下がり)×1000 (ratio-adjusted)。
    Oscillator = 差の19日EMA - 39日EMA、Summation Index = Oscillator の累積。
    EMA の初期値は先頭の差。1日の急落は Oscillator 1日分しか累積値を動かさない。
    daily2year.json は日々先頭が1行ずつ落ちるので累積の起点も動くが、列全体が
    EMA の初期値の影響が消えたあとは列全体が定数だけずれるだけなので、
    同じ列の中で取る percentile は変わらない (立ち上がりは compute_component_breadth で外す)。
    """
    a_fast = 2.0 / (_MCO_FAST + 1)
    a_slow = 2.0 / (_MCO_SLOW + 1)
    fast = slow = None
    total = 0.0
    out = []
    for adv, dec in zip(advances, declines):
        n = adv + dec
        x = (adv - dec) / n * 1000.0 if n else 0.0
        if fast is None:
            fast = slow = x
        else:
            fast += a_fast * (x - fast)
            slow += a_slow * (x - slow)
        total += fast - slow
        out.append(total)
    return out


def compute_component_breadth(breadth_history):
    """Stock Price Breadth: 騰落の McClellan Summation Index を 0-100 化 (高い=Greed)。

    EMA の初期値の影響が残る先頭 _MCO_WARMUP 本は正規化の履歴に使わない。
    Returns: (score 0-100, raw=Summation Index) または None。
    """
    rows = [
        r for r in breadth_history
        if r.get("advances") is not None and r.get("declines") is not None
    ]
    if len(rows) < _MCO_WARMUP + 2:
        return None
    sums = mcclellan_summation(
        [r["advances"] for r in rows], [r["declines"] for r in rows]
    )[_MCO_WARMUP:]
    return normalize_percentile(sums[-1], sums[:-1][-_NORM_WINDOW:]), sums[-1]


def compute_component_breadth_daily(breadth_history):
    """旧方式 (#524 まで): その日の 値上がり - 値下がり を 0-100 化。比較用に残置。

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
    """Market Volatility: 日経VI の50日移動平均からの乖離率を 0-100 化し方向反転 (issue #524)。

    CNN と同じく、VI の水準ではなく「ふだんの VI より高いか」を見る。VI の水準そのものは
    相場の局面で何ヶ月も高止まりするため、水準だと Fear が続きやすい。
    Returns: (score 0-100=反転後, raw=日経VI) または None。raw は画面に VI の値を出すため。
    """
    vis = [r["nikkei_vi"] for r in vi_history if r.get("nikkei_vi") is not None]
    if len(vis) < _VI_MA_DAYS + 1:
        return None
    # 正規化に使う直近2年分 (+最新) だけ乖離率を出す (VI の履歴は 1990年からある)
    vis = vis[-(_NORM_WINDOW + _VI_MA_DAYS):]
    ratios = [
        (vis[i] / (sum(vis[i - _VI_MA_DAYS + 1:i + 1]) / _VI_MA_DAYS) - 1.0) * 100.0
        for i in range(_VI_MA_DAYS - 1, len(vis))
    ]
    raw_score = normalize_percentile(ratios[-1], ratios[:-1][-_NORM_WINDOW:])
    return 100.0 - raw_score, vis[-1]


def compute_component_safe_haven(breadth_history, jgb_history):
    """Safe Haven Demand: 日経225 と 10年国債の20日騰落率の差を 0-100 化 (issue #524)。

    株が債券より上がっている (安全資産が求められていない) ほど Greed。
    債券の騰落率 ≈ -デュレーション × 金利の変化 (%pt)。利息収入は無視する。
    株価と金利の両方がある日だけで比べる。金利は翌営業日に公表されるので、
    日次バッチ時点の最新は前営業日になる。
    Returns: (score 0-100, raw=株の騰落率 - 債券の騰落率 (%pt)) または None。
    """
    yields = {r["date"]: r["jgb10"] for r in jgb_history if r.get("jgb10") is not None}
    pairs = [
        (r["nikkei_close"], yields[r["date"]])
        for r in breadth_history
        if r.get("nikkei_close") is not None and r.get("date") in yields
    ]
    if len(pairs) < _SAFE_HAVEN_DAYS + 2:
        return None
    diffs = []
    for (c0, y0), (c1, y1) in zip(pairs, pairs[_SAFE_HAVEN_DAYS:]):
        stock = (c1 / c0 - 1.0) * 100.0
        bond = -_JGB10_DURATION * (y1 - y0)
        diffs.append(stock - bond)
    return normalize_percentile(diffs[-1], diffs[:-1][-_NORM_WINDOW:]), diffs[-1]


def compute_component_volatility_level(vi_history):
    """旧方式 (#524 まで): 日経VI の水準を 0-100 化し方向反転。比較用に残置。

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


def compute_fear_greed_jp(breadth_history, vi_history, jgb_history=()):
    """5成分から日本市場版 Fear & Greed の合成スコアを計算する。

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
    # 金利も同じ理由で揃える (#524)。
    as_of = breadth_history[-1].get("date") if breadth_history else None
    if as_of:
        if vi_history:
            vi_history = [v for v in vi_history if v.get("date", "") <= as_of] or vi_history
        jgb_history = [j for j in jgb_history if j.get("date", "") <= as_of]

    raw_components = {
        "momentum": compute_component_momentum(breadth_history),
        "strength": compute_component_strength(breadth_history),
        "breadth": compute_component_breadth(breadth_history),
        "volatility": compute_component_volatility(vi_history),
        "safe_haven": compute_component_safe_haven(breadth_history, jgb_history),
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


# 比較で各日のスコアを出すとき、正規化の履歴として最低限ほしい本数 (約半年)。
_COMPARE_MIN_HISTORY = 120


def _summarize_series(scores):
    """スコア列 (日付昇順) の振れ幅と rating の出現日数をまとめる。"""
    moves = [abs(b - a) for a, b in zip(scores, scores[1:])]
    ratings = [_rating(s) for s in scores]
    return {
        "days": len(scores),
        "mean_abs_move": sum(moves) / len(moves) if moves else 0.0,
        "max_abs_move": max(moves) if moves else 0.0,
        "extreme_fear_days": ratings.count("Extreme Fear"),
        "extreme_greed_days": ratings.count("Extreme Greed"),
        "rating_changes": sum(1 for a, b in zip(ratings, ratings[1:]) if a != b),
    }


def compare_methods(breadth_history, vi_history, jgb_history):
    """#524 の変更前後で、合成スコアと変えた成分の推移を比べる。

    変更前 = 4成分 (騰落は1日差分、VI は水準)。
    変更後 = 5成分 (騰落は McClellan 累積、VI は50日平均との乖離、安全資産を追加)。
    各日について、その日までのデータだけで計算し直す (日次バッチと同じ条件)。
    正規化の履歴が _COMPARE_MIN_HISTORY 本に満たない先頭の日は、新旧どちらも比較から外す
    (累積型は立ち上がりの _MCO_WARMUP 本も要るので、比較できるのは約1年分になる)。
    Returns: {系列名: 統計} 系列名は composite_old/new, breadth_old/new,
             volatility_old/new, safe_haven_new。
    """
    import bisect

    vi_dates = [v.get("date", "") for v in vi_history]
    jgb_dates = [j.get("date", "") for j in jgb_history]
    series = {}

    def add(name, comp):
        if comp is not None:
            series.setdefault(name, []).append(comp[0])

    for i in range(_MCO_WARMUP + _COMPARE_MIN_HISTORY, len(breadth_history)):
        b = breadth_history[: i + 1]
        as_of = b[-1]["date"]
        vi = vi_history[: bisect.bisect_right(vi_dates, as_of)]
        jgb = jgb_history[: bisect.bisect_right(jgb_dates, as_of)]
        mom, strength = compute_component_momentum(b), compute_component_strength(b)
        old = [mom, strength, compute_component_breadth_daily(b),
               compute_component_volatility_level(vi)]
        new = [mom, strength, compute_component_breadth(b),
               compute_component_volatility(vi), compute_component_safe_haven(b, jgb)]
        for name, comps in (("composite_old", old), ("composite_new", new)):
            valid = [c[0] for c in comps if c is not None]
            if valid:
                series.setdefault(name, []).append(sum(valid) / len(valid))
        add("breadth_old", old[2])
        add("breadth_new", new[2])
        add("volatility_old", old[3])
        add("volatility_new", new[3])
        add("safe_haven_new", new[4])
    return {name: _summarize_series(v) for name, v in series.items()}


def main():
    """`python fear_greed_jp.py compare`: #524 の変更前後の比較を表で出す。"""
    import sys

    import market_breadth
    from ks_util import log_print, log_warning

    if sys.argv[1:] != ["compare"]:
        log_print("usage: python fear_greed_jp.py compare")
        return
    breadth = market_breadth.fetch_market_breadth_daily()
    vi = market_breadth.fetch_nikkei_vi()
    try:
        jgb = market_breadth.fetch_jgb10()
    except Exception as e:
        log_warning("10年金利の取得に失敗 (安全資産を除いて比べる): %s" % e)
        jgb = []
    log_print("比較期間: %s 〜 %s (breadth %d 行)" % (
        breadth[_MCO_WARMUP + _COMPARE_MIN_HISTORY]["date"], breadth[-1]["date"], len(breadth)))
    result = compare_methods(breadth, vi, jgb)
    labels = [
        ("days", "日数"), ("mean_abs_move", "前日比の平均 (絶対値)"),
        ("max_abs_move", "前日比の最大 (絶対値)"),
        ("extreme_fear_days", "Extreme Fear (<25) の日数"),
        ("extreme_greed_days", "Extreme Greed (≥75) の日数"),
        ("rating_changes", "rating が変わった回数"),
    ]
    tables = (("composite", "合成スコア"), ("breadth", "騰落"), ("volatility", "日経VI"),
              ("safe_haven", "安全資産 (新設)"))
    for name, title in tables:
        old, new = result.get(name + "_old"), result.get(name + "_new")
        log_print("| %s | 変更前 | 変更後 |" % title)
        log_print("|---|---|---|")
        for key, jp in labels:
            cells = []
            for stats in (old, new):
                v = stats[key] if stats else None
                cells.append("—" if v is None else ("%.1f" % v if isinstance(v, float) else "%d" % v))
            log_print("| %s | %s | %s |" % (jp, cells[0], cells[1]))


if __name__ == "__main__":
    main()
