#!/usr/bin/env python3

from ks_util import *

from datetime import date, datetime, timedelta
import json
import os
import os.path
import re

import make_stock_db

import yfinance as yf

YFINANCE_CACHE_FNAME = os.path.join(
    DATA_DIR, "stock_data/yahoo/price/yfinance_price_%s.json"
)
YFINANCE_WEEKLY_CACHE_FNAME = os.path.join(
    DATA_DIR, "stock_data/yahoo/price/yfinance_price_w_%s.json"
)
YFINANCE_MONTHLY_CACHE_FNAME = os.path.join(
    DATA_DIR, "stock_data/yahoo/price/yfinance_price_m_%s.json"
)

URL_PRICE_D_KABUTAN = "https://kabutan.jp/stock/kabuka?code=%s&ashi=day&page=%d"
PRICE_D_FNAME_KABUTAN = os.path.join(
    DATA_DIR, "stock_data/kabutan/price/kabutan_price_d_%s_%d.html"
)

INTERVAL_DAY_D = 1
INTERVAL_DAY_W = 7
# 月足キャッシュのファイルTTL。_is_monthly_cache_fresh の内容判定と併用し実質月1更新 (issue #53)
INTERVAL_DAY_M = 28

# 月足評価 (issue #53) のブレイク判定窓。直近Nヶ月を除いた3年高値を基準線とし、
# 直近Nヶ月内の高値がそれを上回った月を break_month として記録する
MONTHLY_BREAK_WINDOW = 3

# ブレイクアウト extended 候補 (詳細チャートのみ表示)。正規ブレイクは MA10乖離
# +5%以内 (高値追いを避けるオニール/ミネルヴィニ規律) だが、出来高/急騰と上昇は
# 満たすのに乖離が +5% 超で弾かれた「高値追い圏」の候補を、行き過ぎ (クライマックス)
# を除く上限までは拾って半透明マーカーで可視化する。シグナル列には出さない。
BREAKOUT_EXTENDED_KAIRI_MAX = 25

# ブレイク検知の出来高平均日数 (issue #111)。20日では短期の急騰を平均が拾いやすく
# 基準が緩むため、より長期の平常出来高を基準にする。検知ループは直近10日を判定し
# 各日で前日から遡って平均を取るため、必要データは 10+30=40日。30日streak判定バッファのため period="3mo"≒60日
# (~41営業日) で全検知日が完全な30日平均を保てる上限値 (取得範囲は広げない)。
BREAKOUT_AVG_VOLUME_DAYS = 30


# モメンタムポイント動的キャリブレーション (issue #104) のデフォルト値。
# market_db['momentum_calib'] が無い・古い・サンプル不足の場合のフォールバック先。
# 値は #104 検証時の実測 (3,822 銘柄, log(rs_rel) の平均/標準偏差)。
MOMENTUM_CALIB_DEFAULT_LOC = -0.058
MOMENTUM_CALIB_DEFAULT_SCALE = 0.275
# フォールバック発動条件: サンプル数が下限未満、または updated_at がこの日数より前。
MOMENTUM_CALIB_MIN_SAMPLES = 500
# キャリブレーションは手動運用 (issue #104 Phase 1)。半年再校正なしで残っている
# 場合は流石に古いとみなしフォールバックする。loc/scale は週単位では動かず
# 月〜四半期スケールで変化する指標なので、半年が概ね妥当。
MOMENTUM_CALIB_MAX_AGE_DAYS = 180


def get_momentum_calib(market_db=None):
    """モメンタム計算用の loc/scale を取得する。

    market_db['momentum_calib'] が利用可能ならそれを返し、
    無い・古い・サンプル不足の場合はデフォルト値にフォールバックする。

    Args:
        market_db (dict): マーケットDB。Noneなら make_market_db.get_market_db()。

    Returns:
        tuple: (loc, scale, source) -- source は "calib" または "fallback"
    """
    if market_db is None:
        import make_market_db
        market_db = make_market_db.get_market_db()
    calib = market_db.get("momentum_calib")
    if not calib:
        return MOMENTUM_CALIB_DEFAULT_LOC, MOMENTUM_CALIB_DEFAULT_SCALE, "fallback"

    sample_count = calib.get("sample_count", 0)
    if sample_count < MOMENTUM_CALIB_MIN_SAMPLES:
        log_warning(
            "[momentum_calib] sample_count=%d がしきい値 %d 未満のためフォールバック"
            % (sample_count, MOMENTUM_CALIB_MIN_SAMPLES)
        )
        return MOMENTUM_CALIB_DEFAULT_LOC, MOMENTUM_CALIB_DEFAULT_SCALE, "fallback"

    updated_at = calib.get("updated_at")
    if updated_at is None:
        return MOMENTUM_CALIB_DEFAULT_LOC, MOMENTUM_CALIB_DEFAULT_SCALE, "fallback"
    age_days = (datetime.now() - updated_at).days
    if age_days > MOMENTUM_CALIB_MAX_AGE_DAYS:
        log_warning(
            "[momentum_calib] updated_at が %d 日前 (上限 %d) のためフォールバック"
            % (age_days, MOMENTUM_CALIB_MAX_AGE_DAYS)
        )
        return MOMENTUM_CALIB_DEFAULT_LOC, MOMENTUM_CALIB_DEFAULT_SCALE, "fallback"

    # loc/scale が欠落・非数値・scale<=0 の壊れたキャッシュにも備える。
    # 正規の calibrate_momentum_pt 経路では起きないが、スキーマ移行ミスや
    # 部分書き込みで一部キーだけ残った場合に KeyError で更新が止まらないよう保険。
    loc = calib.get("loc")
    scale = calib.get("scale")
    if not isinstance(loc, (int, float)) or not isinstance(scale, (int, float)) or scale <= 0:
        log_warning(
            "[momentum_calib] loc/scale が不正 (loc=%r, scale=%r) のためフォールバック"
            % (loc, scale)
        )
        return MOMENTUM_CALIB_DEFAULT_LOC, MOMENTUM_CALIB_DEFAULT_SCALE, "fallback"

    return loc, scale, "calib"


def calc_momentum_pt_value(rs_raw, market_db):
    """対数正規分布モデルで momentum_pt (0〜100) を算出する純粋関数。

    log(rs_rel) = log(rs_raw / topix_rs_raw) が正規分布 N(loc, scale^2) に
    従うと仮定し、その上側累積確率の補数を 0〜100 で返す。
    loc/scale は market_db['momentum_calib'] からの実測値 (無ければデフォルト)。

    Returns:
        int: momentum_pt (0〜100)。算出不能時は 0。
    """
    import math
    topix_rs_raw = market_db.get("topix", {}).get("rs_raw", 0)
    if topix_rs_raw <= 0:
        return 0
    rs_rel = rs_raw / topix_rs_raw
    if rs_rel <= 0:
        return 0
    loc, scale, _ = get_momentum_calib(market_db)
    if scale <= 0:
        return 0
    log_rs_rel = math.log(rs_rel)
    from scipy.stats import norm
    return int(100 * (1 - norm.sf(x=log_rs_rel, loc=loc, scale=scale)))


def get_daily_html_kabutan(code_s, cache=True):
    """日次時系列価格データhtmlを取得する"""
    html = ""
    log_print("----> %sの日次価格情報を株探から取得します・・" % code_s, cache)
    ind = 0
    price_fname = PRICE_D_FNAME_KABUTAN % (code_s, ind + 1)
    # キャッシュファイルから取得
    if cache and os.path.exists(price_fname):
        # 最新情報がなければ取りに行く
        cach, cach_date = is_file_timestamp(price_fname, INTERVAL_DAY_D)
        if cach:
            pass
        else:
            log_debug("キャッシュの期限切れ(%s)" % cach_date.date())
            cache = False
    # 株探から取得
    url = URL_PRICE_D_KABUTAN % (code_s, ind + 1)
    # html = http_get_html(url, use_cache=cache, cache_fname=price_fname)
    html = http_get_html_with_retry(url, use_cach=cache, cache_fname=price_fname)

    log_debug("<---- 取得完了")
    return html


def get_price_interval_day(day1, day2):
    # TODO: 17時の時点でYahooの価格は更新されてない？
    day1 = get_price_day(day1)
    day2 = get_price_day(day2)
    return (day1 - day2).days


def is_file_timestamp(fname, interval_day):
    """キャッシュファイルの更新日時をチェック"""
    stat = os.stat(fname)
    cach_date = datetime.fromtimestamp(stat.st_mtime)
    log_debug("cach_date:", cach_date, fname)
    delta = get_price_interval_day(datetime.today(), cach_date)
    if delta < interval_day:  # キャッシュ使用可能
        return True, cach_date
    return False, cach_date


URL_PRICE_KABUTAN = "https://kabutan.jp/stock/kabuka?code=%s&ashi=wek&page=%d"
URL_CREDIT_KABUTAN = "https://kabutan.jp/stock/kabuka?code=%s&ashi=shin&page=%d"
# PRICE_FNAME_KABUTAN = "stock_data/kabutan/price/kabutan_price_%04d_%d.html"
PRICE_FNAME_KABUTAN = os.path.join(
    DATA_DIR, "stock_data/kabutan/price/kabutan_price_%s_%d.html"
)


def get_weekly_html(code_s, cache=True):
    """株探から週次データを取得
    type: (str, bool) -> str
    Args:
        code_s(str): 銘柄コード
        cache(bool): キャッシュを使用するかどうか
    Returns:
        list<str>: htmlテキストのリスト
    """
    htmls = []
    log_print("----> %sの週次価格情報を株探から取得します・・" % code_s, cache)
    for ind in range(2):
        price_fname = PRICE_FNAME_KABUTAN % (code_s, ind + 1)
        # キャッシュファイルから取得
        if cache and os.path.exists(price_fname):
            # TODO: 最新情報がなければ取りに行く
            cach, cach_date = is_file_timestamp(price_fname, INTERVAL_DAY_W)
            if cach:
                pass
            else:
                log_debug("キャッシュの期限切れ(%s)" % cach_date.date())
                cache = False
        # 株探から取得
        url = URL_PRICE_KABUTAN % (code_s, ind + 1)
        # html = http_get_html(url, use_cache=cache, cache_fname=price_fname)
        html = http_get_html_with_retry(url, use_cach=cache, cache_fname=price_fname)
        htmls.append(html)

    log_debug("<---- 取得完了")
    return htmls




def calc_avg_volume_d(price_list):
    """平均出来高の計算
    Params:
        price_list(list< list<int(5)> >): (日付、始値、高値、安値、終値、出来高)のリスト
          20日分必要
    Returns:
        dict<str, int>: strはavg_volume_d20, avg_volume_d5
    """
    volume_lst = [l[5] for l in price_list]
    res = {}
    res["avg_volume_d20"] = int(average(volume_lst[0:20]))
    res["avg_volume_d5"] = int(average(volume_lst[0:5]))
    return res


# 計算に必要な価格データを収集


def get_ratio_list(price_list):
    ratio_list = []
    buygather_list = []  # 買い集め用
    volume_lst = [p[5] for p in price_list]
    if len(volume_lst) > 0:
        volume_avg = sum(volume_lst) / len(volume_lst)
    else:
        volume_avg = 0
    for i, price in enumerate(price_list):
        if i + 1 >= len(price_list):
            break
        # print price
        prev = price_list[i + 1]  # 前日
        yousen = price[4] - prev[4] > 0
        val0 = price[1] - prev[4]  # 前日終値→始値
        val1 = price[3] - price[1] if yousen else price[2] - price[1]  # 始値→高値or安値
        val2 = (
            price[2] - price[3] if yousen else price[3] - price[2]
        )  # 高値or安値→安値or高値
        val3 = price[4] - price[2] if yousen else price[4] - price[3]  # 安値or高値→終値
        vals = [val0, val1, val2, val3]
        # print yousen, vals
        buy_count = sum([v for v in vals if v > 0])  # 買い値幅
        sel_count = abs(sum([v for v in vals if v <= 0]))  # 売り値幅
        # print "buy-sel:", buy_count, sel_count
        # 5:出来高
        buy_stock = (
            price[5] * buy_count / (buy_count + sel_count)
            if (buy_count + sel_count) != 0
            else price[5] / 2
        )
        sel_stock = (
            price[5] * sel_count / (buy_count + sel_count)
            if (buy_count + sel_count) != 0
            else price[5] / 2
        )
        ratio = round((float)(sel_count) / buy_count, 1) if buy_count != 0 else 0
        volatility = sum([abs(v) for v in vals])  # ローソク足ボラティリティ
        # print buy_count, sel_count, ratio, price[5], buy_stock, sel_stock
        rec = [buy_stock, sel_stock, ratio, volatility]
        ratio_list.append(rec)
        # 平均以上の出来高なら買い集めリストに追加
        if price[5] > volume_avg and volume_avg > 0:
            buygather_list.append(rec)
    return ratio_list, buygather_list


def calc_ratio(ratio_list, day_count):
    """売り圧力レシオの計算
    Params:
        List< [買い総数、売り総数] >
        int
    """
    buy_sum = sel_sum = 0
    for i in range(day_count):
        try:
            buy_sum += ratio_list[i][0]  # 0: buy_count
            sel_sum += ratio_list[i][1]  # 1: sel_count
        except IndexError as e:
            log_warning(" 日付が足りないため%d日で計算します" % i)
            break
    log_debug("%d日 買い:%d 売り:%d" % (day_count, buy_sum, sel_sum))
    # if buy_sum > 0:
    # 	ratio = (sel_sum*100) / buy_sum
    if buy_sum + sel_sum > 0:
        ratio = (int)(100 * buy_sum / (buy_sum + sel_sum))
    else:
        ratio = 0
    return ratio


def calc_sell_pressure_ratio(price_list):
    """売り圧力レシオの計算
    Params:
        price_list(list< list<int(5)> >): (日付、始値、高値、安値、終値、出来高)のリスト 日付若い順
          20日分必要
    Returns:
        list< int(4) >: 20sp_ratio, 5sp_ratio, 20buygather, 20r_vol, 5r_vol(ローソク足ボラティリティ)
    """
    # ---- 売り圧力レシオの計算
    ratio_list, buygather_list = get_ratio_list(price_list)
    # print "ratio_list:", ratio_list
    # メモ：売り圧力レシオが116で売り
    DAY_L = 19
    DAY_S = 5
    ratios = [calc_ratio(ratio_list, DAY_L), calc_ratio(ratio_list, DAY_S)]
    ratios.append(calc_ratio(buygather_list, len(buygather_list)))

    # ---- ローソク足ボラティリティの計算
    def calc_vola(day_count):
        """
        (int)->(float)
        """
        vol_avg = 0
        end_avg = 0
        for i in range(day_count):
            try:
                vol_avg += ratio_list[i][3]  # 1日ボラティリティ
                end_avg += price_list[i][4]  # 終値
            except IndexError as e:
                log_warning(" 日付が足りないため%d日で計算します" % i)
                break
        vol_avg /= float(day_count)
        end_avg /= float(day_count)
        # print vol_avg, end_avg
        volatility = round(100.0 * vol_avg / end_avg, 2) if end_avg > 0 else 0
        return volatility, end_avg

    volatility_l, ma_l = calc_vola(DAY_L)
    volatility_s, ma_s = calc_vola(DAY_S)
    log_debug(
        "SRRレシオ(20,5):", ratios, "ボラティリティ(20,5):", volatility_l, volatility_s
    )
    # print "ボラ(H,L)=(%d, %s)"%(int(ma_l*(1+volatility_l/100)), int(ma_l*(1-volatility_l/100)) )
    ratios.append(volatility_l)
    ratios.append(volatility_s)
    return ratios


def parse_price_d_html_kabutan(html):
    """日次の株探価格htmlデータを解析する"""
    # ---- 日次データの作成
    daily_price_list = []
    for m in re.finditer(
        r'<th scope="row"><time datetime=".*?">(.*?)</time></th>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td><span class=".*?">(.*?)</span></td>[\r\n]+<td><span class=".*?">(.*?)</span></td>[\r\n]+<td>(.*?)</td>',
        html,
        re.DOTALL,
    ):
        daily_price_list.append(m.groups())

    dic = _calc_daily_indicators(daily_price_list)
    # Stalling Day を後付け検出するため raw データを内部的に保持
    if dic:
        dic["_daily_price_list_raw"] = daily_price_list
    return dic


def _is_stalling_day(d, pd, high52_weekly):
    """Stalling Day (停滞日) 判定 (O'Neil 原典準拠)

    条件 (すべて満たす):
    - 52週高値の97%以上 (高値圏)
    - 前日比が 0% 超 ~ +0.4% 未満 (微増)
    - 出来高が前日より増加
    - 終値が日足の下半分 (高値から引けが離れた)

    Args:
        d: 当日 8要素タプル (日付, 始値, 高値, 安値, 終値, 前日比, 前日比%, 売買高)
        pd: 前日 8要素タプル
        high52_weekly: 52週高値 (週足から計算済)
    Returns:
        bool: Stalling Day なら True
    """
    if not high52_weekly or high52_weekly <= 0:
        return False
    try:
        dh = float(d[2].replace(",", ""))
        dl = float(d[3].replace(",", ""))
        dp = float(d[4].replace(",", ""))
        dv = int(d[7].replace(",", ""))
        pdv = int(pd[7].replace(",", ""))
        dr = float(d[6].replace(",", ""))
    except (ValueError, IndexError):
        return False
    if dp < high52_weekly * 0.97:
        return False
    if not (0 < dr < 0.4):
        return False
    if dv <= pdv:
        return False
    if dh <= dl:
        return False
    return dp <= dl + (dh - dl) / 2


def add_stalling_days(dic, daily_price_list, high52_weekly):
    """既存の日次指標 dict に Stalling Day を追加検出する。

    `_calc_daily_indicators` で計算した distribution_days* の結果に、
    Stalling Day を後付けで追加する。通常DDと重複しない日のみ追加。
    日付順序は古い→新しい (元々の append 順) を保つ。

    Args:
        dic: _calc_daily_indicators の戻り値 dict
        daily_price_list: 元の日次価格リスト (新しい日が先頭)
        high52_weekly: 52週高値 (週足計算後に取得可能になる)
    Returns:
        dict: 同じ dict (in-place 更新)
    """
    if not high52_weekly or not daily_price_list or not dic:
        return dic
    count_day = 20
    target_days = list(reversed(daily_price_list[: count_day + 1]))
    if len(target_days) < 2:
        return dic
    existing_dd = set(dic.get("distribution_days", []))
    new_dd_dates = list(dic.get("distribution_days", []))
    new_dd_with_close = list(dic.get("distribution_days_with_close", []))
    for d, pd in zip(target_days[1:], target_days[:-1]):
        if d[0] in existing_dd:
            continue  # 既に通常DDとして計上されている
        if _is_stalling_day(d, pd, high52_weekly):
            try:
                dp = float(d[4].replace(",", ""))
            except ValueError:
                continue
            new_dd_dates.append(d[0])
            new_dd_with_close.append((d[0], dp))
            existing_dd.add(d[0])
            log_debug("Stalling Day:", d[0])
    dic["distribution_days"] = new_dd_dates
    dic["distribution_days_with_close"] = new_dd_with_close
    return dic


def calc_ma10_kairi_indicators(closes, lows=None, window=10):
    """終値リスト (新しい日が先頭) から 10日MA乖離率と30日10ma維持判定を計算する。

    Kabutan/yfinance 両系統で共通利用する。トレンド列の点線マーカー用。
    30日連続で終値が10maを維持する期間が保持データ窓内にあると赤実線に切替える。

    Args:
        closes: 終値 (int/float) のリスト、新しい日が先頭
        lows: 安値 (int/float or None) のリスト、新しい日が先頭。ma10_break_confirmed の計算に使用。
    Returns:
        dict: price_kairi_ma10 (float or None) / ma10_above_streak_30 (bool) / ma10_streak_ever (bool)
              / ma10_break_confirmed (bool)
    """
    res = {}
    # 乖離率は closes のみで計算する (安値の欠損に影響されない)
    if len(closes) >= window:
        ma10 = sum(closes[:window]) / window
        res["price_kairi_ma10"] = (closes[0] - ma10) * 100 / ma10 if ma10 else None
    else:
        res["price_kairi_ma10"] = None
    # 保持している価格データ (日足~40日) の範囲内に「30営業日連続で 10ma を維持した」
    # 期間があるかを判定する。現在も維持中なら ma10_above_streak_30=True (赤太点線)、
    # 達成実績はあるが現在は割れなら ma10_streak_ever=True (黒太点線)。
    # 達成期間が窓の外に流れたら両方 False に戻る。
    #
    # 連続維持の判定は Morales/Kacher の violation (浸透許容=porosity) に準拠する:
    #   - 終値 > 10ma なら維持。
    #   - 終値が10maを割っても、翌営業日の終値が翌営業日の10ma上に回復すれば維持
    #     (一時的な潜りは shakeout として救済)。
    #   - 翌営業日の終値も10ma以下なら violation 成立として切断。
    # 原典は「翌日以降」に前日安値を下回ることを violation とも読めるが、本指標は
    # 売りシグナルではなく利確基準の点灯なので、引けで10ma上に戻した事実を優先する。
    # 監視窓は「割れ日の翌営業日のみ」に固定する。
    # 各日 i の10ma = closes[i:i+10] の平均 (要 i+10 本)。リスト先頭が最新日なので、
    # 日 i の「翌営業日」は index i-1。
    # (原典は7週=35日だが取得範囲で収まる30日に調整。period="3mo"≒60日で余裕をもって判定)
    STREAK_DAYS = 30

    def _ma10(i):
        return sum(closes[i:i + window]) / window if len(closes) >= i + window else None

    def _streak_holds(i):
        ma = _ma10(i)
        if ma is None or ma == 0:
            return False  # 10ma 算出不可 = データ不足
        if closes[i] > ma:
            return True  # 終値が10ma上 → 維持
        # 終値が10maを割った日。翌営業日 (i-1) の終値がその日の10ma上に戻れば
        # porosity 救済で維持。戻らなければ violation 成立 → 切断。
        if i == 0:
            # 最新日の割れは翌営業日が未到来で violation 未確定。ここを維持側に倒すと
            # 当日 streak=True を立てた翌日に violation 確定で False へひっくり返る
            # (前日分の先取り誤判定)。確定情報のみで判定するため未確定は不成立とする。
            return False
        next_ma = _ma10(i - 1)
        if next_ma is None or next_ma == 0:
            return False
        return closes[i - 1] > next_ma

    # 各起点 s から STREAK_DAYS 連続で維持が成立する s があれば True
    had_streak = False
    streak_end_idx = None  # had_streak が成立した区間の末尾 (最新側) の index
    if len(closes) >= STREAK_DAYS + 9:
        max_start = len(closes) - (STREAK_DAYS + 9)  # s+STREAK_DAYS+9 が範囲内に収まる上限
        for s in range(max_start + 1):
            if all(_streak_holds(i) for i in range(s, s + STREAK_DAYS)):
                had_streak = True
                streak_end_idx = s  # リスト先頭が最新なので s が最新側
                break

    # 30日連続達成後に5日以上連続で10ma割れが発生していれば赤太点線をリセット。
    # streak 達成区間の最新端 (streak_end_idx) から現在 (index 0) に向かって
    # 連続割れ日数をカウントし、5日以上なら currently_holding を強制 False。
    # 黒太点線 (had_streak) は残す。
    BREAK_RESET_DAYS = 5
    long_break_occurred = False
    if had_streak and streak_end_idx is not None and streak_end_idx > 0:
        max_break = 0
        cur_break = 0
        for i in range(streak_end_idx - 1, -1, -1):  # streak末尾の翌日(新しい方)から現在へ
            ma = _ma10(i)
            if ma and closes[i] <= ma:
                cur_break += 1
                max_break = max(max_break, cur_break)
            else:
                cur_break = 0
        if max_break >= BREAK_RESET_DAYS:
            long_break_occurred = True

    # 現在も維持中 (最新日が10ma上) かどうかで赤太点線/黒太点線を分ける
    # 5日以上の連続割れがあった場合は赤太点線を出さない
    currently_holding = had_streak and _streak_holds(0) and not long_break_occurred
    res["ma10_above_streak_30"] = currently_holding
    # 達成実績はあるが現在は割れ → 黒太点線用
    res["ma10_streak_ever"] = had_streak and not currently_holding

    # 早売確定フラグ: ma10_streak_ever の状態で10ma割れ翌日以降に安値がA日安値を下回ったら True。
    # 10maを回復した場合はリセット。lows が渡されない場合は判定不可として False。
    # long_break_occurred で streak_ever=True になった場合でも現在10ma上なら判定しない。
    res["ma10_break_confirmed"] = False
    currently_above_ma10 = _streak_holds(0)
    if res["ma10_streak_ever"] and not currently_above_ma10 and lows and len(lows) >= 2:
        from exit_line import calc_ma_violation
        res["ma10_break_confirmed"] = bool(calc_ma_violation(closes, lows, _ma10)["confirmed"])

    return res


def calc_weekly_ma_violation(daily_price_list, weekly_price_list, window):
    """確定週だけの週足MAを基準に、日足系列で違反状態を判定する。"""
    from exit_line import calc_ma_violation

    if window not in (30, 40):
        raise ValueError("window must be 30 or 40")
    daily_dates = []
    closes = []
    lows = []
    for row in daily_price_list:
        dt = parse_date_str(row[0])
        if dt is None:
            return {"breached": False, "pending": False, "confirmed": False,
                    "ma_value": None, "a_day_low": None}
        try:
            closes.append(float(row[4].replace(",", "")))
            lows.append(float(row[3].replace(",", "")))
        except (ValueError, IndexError, AttributeError):
            return {"breached": False, "pending": False, "confirmed": False,
                    "ma_value": None, "a_day_low": None}
        daily_dates.append(dt)
    weekly = []
    for row in weekly_price_list:
        dt = parse_date_str(row[0])
        try:
            close = float(row[4].replace(",", ""))
        except (ValueError, IndexError, AttributeError):
            continue
        if dt is not None:
            weekly.append((dt, close))
    weekly.sort(reverse=True)

    def ma_at(index):
        if index >= len(daily_dates):
            return None
        week_start = daily_dates[index] - timedelta(days=daily_dates[index].weekday())
        # 当週足は未確定なので、日付ラベルが当週以前のバーは除外する。
        prior_closes = [close for dt, close in weekly if dt < week_start]
        if len(prior_closes) < window:
            return None
        return sum(prior_closes[:window]) / window

    return calc_ma_violation(closes, lows, ma_at)


def add_weekly_ma_violations(parsed_data, daily_price_list, weekly_price_list):
    """30/40週MAの違反結果を価格指標へ追加する。"""
    parsed_data["wma30_violation"] = calc_weekly_ma_violation(
        daily_price_list, weekly_price_list, 30
    )
    parsed_data["wma40_violation"] = calc_weekly_ma_violation(
        daily_price_list, weekly_price_list, 40
    )
    return parsed_data


def _calc_daily_indicators(daily_price_list):
    """daily_price_listから日次指標を計算する
    parse_price_d_html_kabutanから指標計算部分を分離。
    Kabutan HTML パスと yfinance パス共通で使用する。

    DD/FTD 判定は O'Neil 原典準拠:
    - 通常DD: 前日比 -0.2% 以下 + 出来高が前日より増加
    - FTD候補: 前日比 +1.0% 以上 + 出来高が前日より増加
      (ラリーアテンプト Day 4 以降の判定は market_state.py 側で行うため、ここでは候補日のみ拾う)

    Stalling Day は週足の high52_weekly が必要なため、本関数では検出せず
    `add_stalling_days()` を呼び出し側で別途実行する。

    direction_signal の最終値は make_market_db.py が market_state を計算してから上書きする。
    本関数では空文字をデフォルトとして入れておく (フィールド自体は後方互換で維持)。

    Args:
        daily_price_list: 8要素タプル(文字列)のリスト
            [0]日付, [1]始値, [2]高値, [3]安値, [4]終値, [5]前日比, [6]前日比%, [7]売買高
            カンマ区切り数値文字列、新しい日付が先頭
    Returns:
        dict: distribution_days / distribution_days_with_close / followthrough_days /
              daily_history / direction_signal / spr_20 / spr_5 / spr_buygagher / rv_20 / rv_5
    """
    # ---- ディストリビューション / フォロースルー候補
    distribution_day = []  # 日付のリスト (後方互換)
    distribution_day_with_close = []  # (date, close) タプルのリスト (新規、State Machine 用)
    followthrough_day = []
    count_day = 20
    target_days = list(
        reversed(daily_price_list[: count_day + 1])
    )  # インデックスが若いほど前の日にする
    if len(target_days) < 2:
        log_warning(" デイリー価格解析できず")
        return {}
    current_day = target_days[-1][0]
    log_debug(current_day, "のディストリビューションカウント")
    for d, pd in zip(target_days[1:], target_days[:-1]):
        try:
            dp = float(d[4].replace(",", ""))  # 終値
            dv = int(d[7].replace(",", ""))  # 売買高
            pdv = int(pd[7].replace(",", ""))
            dr = float(d[6].replace(",", ""))  # 前日比パーセント
        except ValueError:
            log_debug("%sはデータ取得できず" % d[0])
            continue
        # 通常 DD (原典準拠)
        if dr <= -0.2 and dv > pdv:
            distribution_day.append(d[0])
            distribution_day_with_close.append((d[0], dp))
        # FTD候補 (ラリーアテンプト判定は market_state.py 側で行う)
        if dr >= 1.0 and dv > pdv:
            followthrough_day.append(d[0])
    dic = {}
    dic["distribution_days"] = distribution_day
    dic["distribution_days_with_close"] = distribution_day_with_close
    dic["followthrough_days"] = followthrough_day
    # daily_history (新しい日が先頭) を State Machine の DD 失効判定で使う
    dic["daily_history"] = [d[0] for d in daily_price_list[:count_day + 1]]
    # 当日 OHLV をトップレベルキーに詰める。make_market_db._update_index_market_state
    # が new_index_db["price"|"low"|"high"|"volume"] を参照して FTD/ラリー判定を行うため、
    # kabutan/yfinance 両系統で確実にセットしておく (issue: state遷移FTD不動作)。
    # daily_price_list[0] = 当日 (新しい順、8要素タプル「日付,始値,高値,安値,終値,前日比,前日比%,売買高」)。
    try:
        head = daily_price_list[0]
        dic["price"] = int(float(head[4].replace(",", "")))   # 終値
        dic["low"] = int(float(head[3].replace(",", "")))
        dic["high"] = int(float(head[2].replace(",", "")))
        dic["volume"] = int(float(head[7].replace(",", "")))
    except (ValueError, IndexError, AttributeError, TypeError):
        # raw データが想定外なら明示的に None を残す (state machine 側が安全に短絡する)
        pass
    log_debug("ディストリビューション:", distribution_day)
    log_debug("フォロースルー候補:", followthrough_day)

    # ---- 10日MA乖離率 + 30日10ma維持判定 (短期ブレイク上昇時の利確基準)
    # トレンド列の点線マーカー用。daily_price_list は終値が d[4]・安値が d[3]。
    # streak 判定は終値回復ベースだが、呼び出し互換のため lows も渡す。
    try:
        closes = [int(float(d[4].replace(",", ""))) for d in daily_price_list]
    except (ValueError, IndexError):
        closes = []
    lows = []
    for d in daily_price_list:
        try:
            lows.append(int(float(d[3].replace(",", ""))))
        except (ValueError, IndexError):
            lows.append(None)
    dic.update(calc_ma10_kairi_indicators(closes, lows))
    from exit_line import calc_ma_violation, calc_sma_at
    dic["ma50_violation"] = calc_ma_violation(
        closes, lows, lambda i: calc_sma_at(closes, 50, i)
    )
    log_debug("10日MA乖離率:", dic["price_kairi_ma10"], "30日維持中:", dic["ma10_above_streak_30"], "維持実績あり:", dic["ma10_streak_ever"])

    # direction_signal は make_market_db.py が market_state を計算してから上書きする。
    # 計算前のデフォルト値として空文字を入れておく (後方互換のためフィールド自体は維持)。
    dic["direction_signal"] = ""

    # ---- 売り圧力レシオ
    def to_numeric(str):
        if str == "－":
            return 0
        return int(float(str.replace(",", "")))

    # 日付、始値、高値、安値、終値、前週比、前週比％、売買高
    # ->日付、始値、高値、安値、終値、出来高
    price_list_spr = [
        [
            l[0],
            to_numeric(l[1]),
            to_numeric(l[2]),
            to_numeric(l[3]),
            to_numeric(l[4]),
            to_numeric(l[7]),
        ]
        for l in daily_price_list
    ]
    spr_20, spr_5, spr_buygagher, rv_20, rv_5 = calc_sell_pressure_ratio(price_list_spr)
    dic["spr_20"] = spr_20
    dic["spr_5"] = spr_5
    dic["spr_buygagher"] = spr_buygagher
    dic["rv_20"] = rv_20
    dic["rv_5"] = rv_5

    # rs_line 計算で参照される。Kabutan/yfinance 両経路で同じ (date, int終値) 型にする
    # 20日前比較に必要な 21件 + バッファ。Kabutan HTML は元々 30 件返す
    LOG_DAY = 30
    past_prices = []
    for ind in range(min(LOG_DAY, len(daily_price_list))):
        dt = parse_date_str(daily_price_list[ind][0])
        if not dt:
            continue
        try:
            pr = int(float(daily_price_list[ind][4].replace(",", "")))
        except (ValueError, IndexError):
            continue
        past_prices.append((dt, pr))
    dic["price_log"] = past_prices

    return dic


def _calc_weekly_indicators(weekly_price_list, cur_prices=[]):
    """weekly_price_listから各種週次指標を計算する
    parse_pricew_htmls_kabutanから指標計算部分を分離。
    yfinanceパスとKabutanパス共通で使用する。
    Args:
        weekly_price_list: 8要素タプル(文字列)のリスト
            [0]日付, [1]始値, [2]高値, [3]安値, [4]終値, [5]前週比, [6]前週比%, [7]売買高
            カンマ区切り数値文字列、新しい日付が先頭
        cur_prices: [終値, 高値, 安値] の現在価格リスト
    Returns:
        dict: 指標dict (rs_raw, momentum_pt, trend_template, etc.)
    """
    price_dict = {}

    try:
        prices = [int(float(p[4].replace(",", ""))) for p in weekly_price_list]
        highs = [int(float(p[2].replace(",", ""))) for p in weekly_price_list]
        lows = [int(float(p[3].replace(",", ""))) for p in weekly_price_list]
    except (ValueError, ZeroDivisionError, IndexError):
        log_warning(" 価格データがかけている")
        prices = highs = lows = []
        return price_dict
    # ---- 10WMA(=50日MA)との乖離率(売りシグナルに使用)
    try:
        wma10 = sum(prices[:10]) / 10
        kairi = (prices[0] - wma10) * 100 / wma10
        price_dict["price_kairi_wma10"] = kairi
    except IndexError:
        log_warning(" 10WMA乖離率計算できず")
        price_dict["price_kairi_wma10"] = 0
    log_debug("10WMA乖離率:", price_dict["price_kairi_wma10"])

    # ---- 売り圧力レシオ(買い集め指数)
    try:
        price_list_w = [
            [
                p[0],
                int(float(p[1].replace(",", ""))),
                int(float(p[2].replace(",", ""))),
                int(float(p[3].replace(",", ""))),
                int(float(p[4].replace(",", ""))),
                int(float(p[7].replace(",", ""))),
            ]
            for p in weekly_price_list
        ]
        ratio_list, buygather_list = get_ratio_list(price_list_w[:20])
        sell20 = calc_ratio(ratio_list, 20)
        # sell5 = calc_ratio(ratio_list, 5)
        buy_gather = calc_ratio(buygather_list, len(buygather_list))
        price_dict["sell_pressure_ratio_w"] = [sell20, 0, buy_gather]
        log_debug("週次売り圧力レシオ:", price_dict["sell_pressure_ratio_w"])
    except ValueError:
        log_warning(" 週次売り圧力レシオ計算できず")
        price_dict["sell_pressure_ratio_w"] = [0, 0, 0]

    # ---- RSを求める
    def calc_rs():
        # 週次データからRSを計算
        price_len = len(weekly_price_list)
        if not weekly_price_list:
            log_warning("! 週次データのない銘柄のようです")
            return 0, 0
        if not cur_prices or (
            cur_prices[0] == 0 and cur_prices[1] == 0 and cur_prices[2] == 0
        ):
            # 現在価格がない場合は最新の週次データから取得
            p_cur = float(weekly_price_list[0][4].replace(",", ""))  # 4:終値
        else:
            p_cur = cur_prices[0]

        past_prices = []
        log_debug("現在終値 %s" % p_cur)
        for w in [13, 26, 39, 52]:
            if w < price_len:
                past_prices.append(float(weekly_price_list[w][4].replace(",", "")))
                # print "%sの終値 %s"%(weekly_price_list[w][0], weekly_price_list[w][4])
        weights = [0.4, 0.2, 0.2, 0.2]
        weights = weights[: len(past_prices)]
        total = sum(weights)
        weights = [w / total for w in weights]
        # print "past_prices:", past_prices
        # print "weights:", weights
        try:
            ratios = [float(p_cur) / p for p in past_prices]
            log_debug("ratio:", [round(r, 2) for r in ratios])
            rs_raw = 0
            for r, w in zip(ratios, weights):
                rs_raw += r * w
            rs_raw = round(rs_raw, 2)
        except ZeroDivisionError:
            rs_raw = 0
        if rs_raw == 0:
            log_print(
                "!!! RSを計算できませんでした (%d個の過去データ)"
                % len(weekly_price_list)
            )
            rs_raw = 1.0  # 標準値にする
        log_debug("rs_raw:", rs_raw)
        return rs_raw, p_cur

    rs_raw, p_cur = calc_rs()
    price_dict["rs_raw"] = rs_raw

    # ---- TOPIXとの比較でモメンタムポイントを計算 (対数正規分布モデル, issue #104 Phase 2)
    def calc_momentum_pt():
        import make_market_db

        market_db = make_market_db.get_market_db()
        topix = market_db.get("topix", {})
        if "rs_raw" not in topix:
            log_warning(" TOPIXのモメンタムポイント存在せず、計算できず")
            return 0
        rs_rank = calc_momentum_pt_value(rs_raw, market_db)
        if rs_rank <= 0:
            log_warning(" TOPIX/銘柄の rs_raw が不正のためモメンタムポイント計算できません")
        log_debug("rs_rank: %d" % rs_rank)
        return rs_rank

    rs_rank = calc_momentum_pt()
    if rs_rank > 0:  # 0はエラーのため更新しない
        price_dict["momentum_pt"] = rs_rank

    # ---- 52週高値/安値 (Stalling Day 判定で日足側からも参照する)
    try:
        if highs and lows:
            price_dict["high52_weekly"] = max(highs[0:52])
            price_dict["low52_weekly"] = min(lows[0:52])
    except (ValueError, IndexError):
        pass

    # ---- トレンドテンプレート
    def calc_trend_template():
        # ・株価が30週MAと40週MAを上回る
        # ・30週MAは40週MAを上回る
        # ・40週MAは1ヶ月以上上昇トレンド
        # ・10週MAは30,40週MAを上回る
        # ・株価は10週MAを上回る
        # ・株価は52週安値より30％以上高く、52週高値より25％以内
        # ・レラティブストレングスが70以上
        try:
            ma30 = sum(prices[0:30]) / len(prices[0:30])
            ma40 = sum(prices[0:40]) / len(prices[0:40])
            # 1週前比較だと週次ノイズで乱れるため4週前(≈1ヶ月)と比較する (issue #340 1-1)
            if len(prices[4:44]) > 0:
                ma40_b = sum(prices[4:44]) / len(prices[4:44])
            else:
                ma40_b = ma40
            ma10 = sum(prices[0:10]) / len(prices[0:10])
            low52 = min(lows[0:52])
            high52 = max(highs[0:52])

            # print "---", p_cur, ma10, ma30, ma40, ma40_b, low52, high52, rs_rank, "---"
            misses = []
            if not p_cur >= ma10:
                misses.append("pr>ma10")
            if not (p_cur >= ma30 and p_cur >= ma40):
                misses.append("pr>ma30,40")
            if not ma30 >= ma40:
                misses.append("ma30>ma40")
            if not ma40 >= ma40_b:
                misses.append("ma40Up")
            if not (ma10 >= ma30 and ma10 >= ma40):
                misses.append("ma10>ma30,40")
            if not (p_cur >= 1.3 * low52 and p_cur >= 0.75 * high52):
                misses.append("high(low)52")
            if not rs_rank >= 75:
                misses.append("RS")
            log_debug("トレンドテンプレート:", misses)
            return misses
        except (ValueError, ZeroDivisionError, IndexError):
            log_warning(" 価格データがかけている")
            # [] (空=◎完璧) ではなく None (欠損) を返す。
            # None のまま make_stock_db に渡すことでシグナル無効化される (issue #340 1-2)
            return None

    price_dict["trend_template"] = calc_trend_template()

    def calc_pullback_20():
        try:
            # ---- 20MA押し
            # 終値でなく、安値で見たほうが良い
            # 1,1(~3?)週前までの安値が20MA=4WMA乖離が一定以内で、
            # 2, 終値が4WMAを上回っている（1と4WMAのGC）
            # 3, 終値は4WMAから乖離しすぎていない
            # 4, 合計乖離は4固定でなく安値までの週数合計
            # 直近安値から反発しているかもみたい
            ma4list = []
            kairi_list = []
            kairi_low = []
            price_dict["pullback_20"] = ""
            WEEK = 5
            # 4週=20日MAを計算
            for ind in range(len(prices) - WEEK):
                ma4 = sum(prices[ind : ind + WEEK]) / WEEK
                ma4list.append(ma4)
                kairi = 100 * prices[ind] / ma4 - 100
                kairi_list.append(kairi)
                kairi_low.append(100 * lows[ind] / ma4 - 100)
                # print "pr, ma4,kairi =", prices[ind], ma4, kairi
            KAIRI_CUR = (-1, 3)
            # 現在価格乖離が一定以内(4MAの上かどうかも兼ねている)
            if kairi_list[0] >= KAIRI_CUR[0] and kairi_list[0] <= KAIRI_CUR[1]:
                # 直近安値の乖離率
                # min_low = min(lows[0:WEEK])
                # min_low_ind = lows[0:WEEK].index(min_low)
                # min_kairi_low = kairi_low[min_low_ind]
                min_kairi_low = min(kairi_low[0:WEEK])
                if min_kairi_low <= 1:  # 直近安値が20MAに接近
                    min_low_ind = kairi_low[0:WEEK].index(min_kairi_low)
                    # kairi_sum = sum(kairi_list[kairi_low_min_ind:WEEK])
                    kairi_list_plus = [
                        l for l in kairi_list[min_low_ind + 1 : WEEK] if l >= 0
                    ]
                    kairi_sum = sum(kairi_list_plus)
                    # 上方にいたかどうか
                    up_week_count = len(kairi_list_plus)
                    if kairi_sum >= 7 * up_week_count:
                        # 直近安値から上がりすぎていないか
                        kairi_low2cur = 100 * prices[0] / lows[min_low_ind] - 100
                        if kairi_low2cur <= 10:
                            return "○"
            return ""
        # kairi_sum = sum(kairi_list[0:8])
        # print "20MA押し　乖離:%d 8週乖離:%d"%(kairi_list[0], kairi_sum)
        # if kairi_list[0] <= 3 and kairi_list[0] >= -2: # 20MAからこの%以内を許容
        # 	if kairi_sum >= 7*8: # これまで20MAを一定以上上回っていた
        # 		price_dict["pullback_20"] = "○"
        except (ValueError, ZeroDivisionError, IndexError):
            log_warning(" 価格データがかけている")
            return ""

    price_dict["pullback_20"] = calc_pullback_20()

    # ---- 新高値
    def calc_new_highs():
        # index: 日付、始値、高値、安値、終値、前週比、前週比％、売買高
        # weekly_price_list = [] # 週次価格データ
        # 2週前からの最高値
        new_highs = []
        try:
            p_list_wk = [p for p in weekly_price_list[2:]]
            p_high_list = [float(p[2].replace(",", "")) for p in p_list_wk]
            p_high = max(p_high_list)
            p_high_ind = p_high_list.index(p_high)
            # print "価格", p_cur, len(weekly_price_list)
            log_debug(
                "2週前以前新高値:%d %d週前(%s)"
                % (p_high, p_high_ind, p_list_wk[p_high_ind][0])
            )
            if p_cur >= p_high * 0.95:  # 2週前以前高値から5パー以内
                if p_high_ind >= 52 - 2:  # 一年以上前
                    new_highs.append("新")
                elif p_high_ind >= 12 - 2:  # 3ヶ月以上前
                    new_highs.append("直")
            # 最高値
            p_high = [float(p[2].replace(",", "")) for p in weekly_price_list[1:]]
            p_max = max(p_high)
            # print "最高値", p_max
            if p_cur >= p_max:
                new_highs.append("最")
            log_debug("新高値:", "".join(new_highs))
        except ValueError:
            log_warning(" 新高値取得できず(価格データ不足)")
            pass
        return new_highs

    price_dict["new_high"] = calc_new_highs()

    # ---- 週足の生系列 (直近25週、(date, close) タプルの日付降順) を保存
    # 詳細ページの株価+RS週足チャート用 (issue #239)
    price_week_log = []
    for p in weekly_price_list[:25]:
        dt = parse_date_str(p[0])
        if dt is None:
            continue
        try:
            close = float(p[4].replace(",", ""))
        except (ValueError, IndexError):
            continue
        price_week_log.append((dt, close))
    price_dict["price_week_log"] = price_week_log

    return price_dict


def parse_pricew_htmls_kabutan(htmls, cur_prices=[]):
    """株探の週次HTMLデータからweekly_price_listを抽出し、指標を計算する"""

    # ---- 週次データを集計
    def calc_weekly_price_list():
        # index: [0]日付、[1]始値、[2]高値、[3]安値、[4]終値、前週比、前週比％、[7]売買高
        weekly_price_list = []  # 週次価格データ
        for ind, html in enumerate(htmls):
            if not html:
                log_warning("!!! 週次データが取得できていない", ind + 1, "ページ目")
                continue
            j = 0
            for m in re.finditer(
                r'<th scope="row"><time datetime=".*?">(.*?)</time></th>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>[\r\n]+<td>(.*?)</td>',
                html,
                re.DOTALL,
            ):
                if ind >= 1 and j == 0:
                    # 変則的だが、2ページ以降の最初の要素は現在株価なので省く
                    j += 1
                    continue
                weekly_price_list.append(m.groups())
                # print m.groups()
                j += 1
        return weekly_price_list

    weekly_price_list = calc_weekly_price_list()
    parsed_data = _calc_weekly_indicators(weekly_price_list, cur_prices)
    parsed_data["_weekly_price_list_raw"] = weekly_price_list
    return parsed_data




def _save_yfinance_cache(fname, price_current, price_list):
    """yfinanceキャッシュをJSON形式で保存する"""
    data = {
        "price_current": price_current,
        "price_list": price_list,
        "saved_at": datetime.now().isoformat(),
    }
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_yfinance_cache(fname):
    """yfinanceキャッシュをJSON形式で読み込む
    Returns:
        (price_current, price_list) or (None, None) if cache miss
    """
    if not os.path.exists(fname):
        return None, None
    try:
        with open(fname, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["price_current"], data["price_list"]
    except (json.JSONDecodeError, KeyError) as e:
        log_warning("yfinanceキャッシュ読み込みエラー:", e)
        return None, None


def _is_daily_cache_fresh(price_list):
    """日次キャッシュの先頭日付が直近取引日相当以上なら True。"""
    if not price_list:
        return False
    latest_dt = parse_date_str(str(price_list[0][0]))
    if latest_dt is None:
        return False
    need_dt = recent_weekday(datetime.today())
    return latest_dt >= need_dt


def _convert_df_to_price_list(df):
    """yfinance DataFrameを既存のprice_list形式に変換する
    auto_adjust=Trueで取得した場合、OHLCは分割・配当調整済み。
    adj_closeはcloseと同値になる。
    Args:
        df: yfinance historyのDataFrame
    Returns:
        list: [date_str, open, high, low, close, volume, adj_close] の7要素リスト
              新しい日付が先頭
    """
    price_list = []
    for idx in reversed(df.index):
        row = df.loc[idx]
        if any(row.get(col) != row.get(col) for col in ["Open", "High", "Low", "Close"]):
            # yfinance は当日行に Volume だけ入り OHLC が NaN のことがある。
            # repair=True でも復元できない行は、変換全体を落とさず除外する。
            continue
        # 日付を"YYYY年M月D日"形式に変換
        if hasattr(idx, "date"):
            dt = idx.date() if callable(idx.date) else idx.date
        else:
            dt = idx
        date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
        open_p = int(row["Open"])
        high_p = int(row["High"])
        low_p = int(row["Low"])
        close_p = int(row["Close"])
        volume = int(row["Volume"])
        # auto_adjust=TrueではAdj Closeカラムがないため、closeをそのまま使用
        # auto_adjust=Falseの場合はAdj Closeがあればそれを使用
        if "Adj Close" in df.columns:
            adj_close = int(row["Adj Close"])
        elif "Adjclose" in df.columns:
            adj_close = int(row["Adjclose"])
        else:
            adj_close = close_p
        price_list.append([date_str, open_p, high_p, low_p, close_p, volume, adj_close])
    return price_list


def get_daily_data_yfinance(code_s, stock={}, upd=UPD_INTERVAL):
    """yfinance APIで日次価格データを取得する
    Args:
        code_s: 銘柄コード文字列
        stock: 銘柄DB情報（マーケットコード判定用）
        upd: 更新レベル
    Returns:
        (price_current, price_list) — 現在価格と価格リスト
    """
    cache_fname = YFINANCE_CACHE_FNAME % code_s

    # キャッシュチェック
    if upd < UPD_FORCE and os.path.exists(cache_fname):
        if upd < UPD_INTERVAL:
            # UPD_CACHE: キャッシュがあればそのまま使用
            pc, pl = _load_yfinance_cache(cache_fname)
            if pc is not None:
                log_debug("yfinanceキャッシュ使用(UPD_CACHE): %s" % code_s)
                return pc, pl
        else:
            # UPD_INTERVAL: キャッシュの日付が期限内かチェック
            cache_ok, cach_date = is_file_timestamp(cache_fname, INTERVAL_DAY_D)
            if cache_ok:
                pc, pl = _load_yfinance_cache(cache_fname)
                if pc is not None and _is_daily_cache_fresh(pl):
                    log_debug("yfinanceキャッシュ使用(UPD_INTERVAL): %s" % code_s)
                    return pc, pl
                log_debug("yfinance日次キャッシュが古いため再取得します: %s" % code_s)

    # yfinance APIで取得
    ticker_symbol = _get_ticker_symbol(code_s, stock)

    log_print("----> %sの日次価格情報をyfinance(%s)から取得します" % (code_s, ticker_symbol))
    try:
        with sema:
            ticker = yf.Ticker(ticker_symbol)
            # 3mo: 30日streak判定には最大41本必要 (streak30日 + ma10算出9本 + 割れ日バッファ)。
            # 2mo≒40本では割れ日数によって streak を取り損なうため 3mo≒60本に拡張。
            df = ticker.history(period="3mo", auto_adjust=True, repair=True)
    except Exception as e:
        log_warning("yfinance取得エラー(%s): %s" % (code_s, e))
        return None, None

    if df is None or df.empty:
        log_warning("yfinanceデータなし: %s" % code_s)
        return None, None

    price_list = _convert_df_to_price_list(df)
    if not price_list:
        log_warning("yfinance価格リスト変換失敗: %s" % code_s)
        return None, None

    # 現在価格は最新の調整後終値
    price_current = price_list[0][6]  # adj_close

    # キャッシュに保存
    _save_yfinance_cache(cache_fname, price_current, price_list)
    log_print("<---- yfinance取得完了: %s 価格=%d データ数=%d" % (code_s, price_current, len(price_list)))
    return price_current, price_list


def _get_ticker_symbol(code_s, stock={}):
    """銘柄コードからyfinanceティッカーシンボルを生成する"""
    market_code = make_stock_db.get_market_code(stock)
    suffix = {"S": ".S", "N": ".N", "F": ".F"}.get(market_code, ".T")
    return code_s + suffix


def _parse_weekly_cache_date(date_str):
    """"YYYY年M月D日"形式の日付文字列をdate型に変換する。失敗時はNone。"""
    m = re.match(r"(\d+)年(\d+)月(\d+)日", date_str or "")
    if not m:
        return None
    from datetime import date as _date
    try:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _is_weekly_cache_fresh(weekly_price_list):
    """週足キャッシュが最新確定週を含んでいるかを判定する。

    yfinance週足バーは月曜ラベル。先頭行(最新バー)の月曜 + 4日 = 金曜。
    その金曜が、直近の確定済み金曜以降であれば、キャッシュは最新確定週を含んでいる。
    「直近の確定済み金曜」は price_day(18時前なら前日扱い) から曜日に応じて遡って算出する。
        月(0)→-3 火(1)→-4 水(2)→-5 木(3)→-6 金(4)→-0 土(5)→-1 日(6)→-2
    これは (wd - 4) % 7 で求められる(曜日4=金曜基準で直近の金曜までの戻り日数)。
    Args:
        weekly_price_list: _convert_weekly_df_to_kabutan_format形式のリスト
    Returns:
        bool: 確定週を含んでいればTrue
    """
    from datetime import timedelta
    if not weekly_price_list:
        return False
    head_date = _parse_weekly_cache_date(weekly_price_list[0][0])
    if head_date is None:
        return False
    price_day = get_price_day(datetime.now())
    wd = price_day.weekday()
    latest_confirmed_friday = price_day - timedelta(days=(wd - 4) % 7)
    bar_friday = head_date + timedelta(days=4)
    return bar_friday >= latest_confirmed_friday


def _convert_weekly_df_to_kabutan_format(df):
    """yfinance週足DataFrameをKabutan互換のweekly_price_list形式に変換する
    最新行が今週（未確定）の場合は除外する。
    Args:
        df: yfinance historyのDataFrame (interval="1wk")
    Returns:
        list[tuple[str, ...]]: 8要素タプルのリスト、新しい日付が先頭
            (日付, 始値, 高値, 安値, 終値, 前週比, 前週比%, 売買高)
            前週比/前週比%は指標計算で未使用のため"0"固定
    """
    from datetime import timedelta
    # get_price_day()は17:00前なら前日扱い。週足判定でも同じ基準日を使う
    price_day = get_price_day(datetime.now())

    weekly_price_list = []
    for idx in reversed(df.index):
        row = df.loc[idx]
        if hasattr(idx, "date"):
            dt = idx.date() if callable(idx.date) else idx.date
        else:
            dt = idx
        # yfinanceの週足バーは月曜ラベル。そのバーの金曜日 = dt + 4日。
        # 基準日(price_day)がバーの金曜日より前 → 今週はまだ未確定なので除外
        bar_friday = dt + timedelta(days=4)
        if price_day < bar_friday:
            continue
        date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
        open_p = "{:,}".format(int(row["Open"]))
        high_p = "{:,}".format(int(row["High"]))
        low_p = "{:,}".format(int(row["Low"]))
        close_p = "{:,}".format(int(row["Close"]))
        volume = "{:,}".format(int(row["Volume"]))
        weekly_price_list.append((date_str, open_p, high_p, low_p, close_p, "0", "0", volume))
    return weekly_price_list


def _is_monthly_cache_fresh(monthly_price_list):
    """月足キャッシュが最新確定月を含んでいるかを判定する (issue #53)。

    yfinance月足バーは月初(1日)ラベル。当月は未確定として変換時に除外されるため、
    先頭バーが「先月の月初」以降であればキャッシュは最新確定月を含んでいる。
    Args:
        monthly_price_list: _convert_monthly_df_to_kabutan_format形式のリスト
    Returns:
        bool: 確定月を含んでいればTrue
    """
    from datetime import timedelta
    if not monthly_price_list:
        return False
    head_date = _parse_weekly_cache_date(monthly_price_list[0][0])
    if head_date is None:
        return False
    price_day = get_price_day(datetime.now())
    prev_month_first = (price_day.replace(day=1) - timedelta(days=1)).replace(day=1)
    return head_date >= prev_month_first


def _convert_monthly_df_to_kabutan_format(df):
    """yfinance月足DataFrameをKabutan互換のmonthly_price_list形式に変換する (issue #53)
    yfinance月足バーは月初(1日)ラベルで、当月が部分バーとして含まれるため除外する。
    Args:
        df: yfinance historyのDataFrame (interval="1mo")
    Returns:
        list[tuple[str, ...]]: 8要素タプルのリスト、新しい日付が先頭
            (日付, 始値, 高値, 安値, 終値, 前月比, 前月比%, 売買高)
            前月比/前月比%は指標計算で未使用のため"0"固定
    """
    # get_price_day()は17:00前なら前日扱い。月足判定でも同じ基準日を使う
    price_day = get_price_day(datetime.now())

    monthly_price_list = []
    for idx in reversed(df.index):
        row = df.loc[idx]
        if hasattr(idx, "date"):
            dt = idx.date() if callable(idx.date) else idx.date
        else:
            dt = idx
        # 当月バーは未確定なので除外
        if (dt.year, dt.month) == (price_day.year, price_day.month):
            continue
        date_str = "%d年%d月%d日" % (dt.year, dt.month, dt.day)
        open_p = "{:,}".format(int(row["Open"]))
        high_p = "{:,}".format(int(row["High"]))
        low_p = "{:,}".format(int(row["Low"]))
        close_p = "{:,}".format(int(row["Close"]))
        volume = "{:,}".format(int(row["Volume"]))
        monthly_price_list.append((date_str, open_p, high_p, low_p, close_p, "0", "0", volume))
    return monthly_price_list


def _convert_daily_df_to_kabutan_format(df):
    """yfinance日足DataFrameをKabutan互換のdaily_price_list形式に変換する
    Args:
        df: yfinance historyのDataFrame (interval="1d")
    Returns:
        list[tuple[str, ...]]: 8要素タプルのリスト、新しい日付が先頭
            (日付, 始値, 高値, 安値, 終値, 前日比, 前日比%, 売買高)
            日付は Kabutan の日次表示と同じ "YY/MM/DD" 形式 (例: "26/04/24")。
            _html_market が distribution_days 等を `s[3:]` で表示用に切り取るため、
            この形式で揃える必要がある。
            前日比%は (close - prev_close) / prev_close * 100、前日比そのものは
            指標計算で未使用のため"0"固定
    """
    rows = []
    for idx in reversed(df.index):
        row = df.loc[idx]
        if hasattr(idx, "date"):
            dt = idx.date() if callable(idx.date) else idx.date
        else:
            dt = idx
        rows.append((dt, row))

    daily_price_list = []
    for i, (dt, row) in enumerate(rows):
        date_str = "%02d/%02d/%02d" % (dt.year % 100, dt.month, dt.day)
        open_p = "{:,}".format(int(row["Open"]))
        high_p = "{:,}".format(int(row["High"]))
        low_p = "{:,}".format(int(row["Low"]))
        close_p_int = int(row["Close"])
        close_p = "{:,}".format(close_p_int)
        volume = "{:,}".format(int(row["Volume"]))
        # 前日比%を計算 (前日 = リスト上で次のインデックス、より古い日)
        if i + 1 < len(rows):
            prev_close = int(rows[i + 1][1]["Close"])
            if prev_close != 0:
                ratio_pct = (close_p_int - prev_close) * 100.0 / prev_close
                ratio_pct_str = "{:.2f}".format(ratio_pct)
            else:
                ratio_pct_str = "0"
        else:
            # 最古の日は前日データなし → 計算不可。指標計算側で前日とのペアにならないので 0 でよい
            ratio_pct_str = "0"
        daily_price_list.append(
            (date_str, open_p, high_p, low_p, close_p, "0", ratio_pct_str, volume)
        )
    return daily_price_list


def get_us_index_daily(code_s, ticker_symbol, upd=UPD_INTERVAL):
    """米国指数の日次価格データをyfinanceから取得する
    日本指数の get_daily_price_kabutan と対になる関数。
    Args:
        code_s: キャッシュキー識別用の擬似コード ("_IXIC" / "_GSPC" など)
        ticker_symbol: yfinanceティッカー ("^IXIC" / "^GSPC" など)
        upd: 更新レベル
    Returns:
        dict: 日次指標 (price, distribution_days, direction_signal, spr_*, rv_* 等)
              失敗時は空dict
    """
    cache_fname = YFINANCE_CACHE_FNAME % code_s

    # キャッシュチェック
    if upd < UPD_FORCE and os.path.exists(cache_fname):
        cache_ok, _ = is_file_timestamp(cache_fname, INTERVAL_DAY_D)
        if upd < UPD_INTERVAL or cache_ok:
            pc, pl = _load_yfinance_cache(cache_fname)
            if pc is not None and pl:
                log_debug("yfinance米国指数日足キャッシュ使用: %s" % code_s)
                daily_price_list = [tuple(row) for row in pl]
                dic = _calc_daily_indicators(daily_price_list)
                if dic:
                    dic["price"] = int(pc)
                    dic["access_date_price"] = datetime.fromtimestamp(
                        os.stat(cache_fname).st_mtime
                    )
                    dic["_daily_price_list_raw"] = daily_price_list
                return dic

    log_print("----> %sの日次価格情報をyfinance(%s)から取得します" % (code_s, ticker_symbol))
    try:
        with sema:
            ticker = yf.Ticker(ticker_symbol)
            df = ticker.history(period="3mo", interval="1d", auto_adjust=True)
    except Exception as e:
        log_warning("yfinance米国指数日足取得エラー(%s): %s" % (code_s, e))
        return {}

    if df is None or df.empty:
        log_warning("yfinance米国指数日足データなし: %s" % code_s)
        return {}

    df = df.dropna(subset=["Close"])
    if df.empty:
        log_warning("yfinance米国指数日足データなし(NaN除去後): %s" % code_s)
        return {}

    daily_price_list = _convert_daily_df_to_kabutan_format(df)
    if not daily_price_list:
        log_warning("yfinance米国指数日足変換失敗: %s" % code_s)
        return {}

    log_print(">>>>> %sの日次価格データを解析(yfinance) " % code_s)
    dic = _calc_daily_indicators(daily_price_list)
    log_print("<<<<< 解析完了(yfinance) ")
    if not dic:
        return {}

    # 最新終値を price として保存 (国内指数の parse_price_d_html_kabutan は price を返さないが、
    # market_db には price キーが入る慣習なので明示的に詰めておく)
    try:
        latest_close = int(daily_price_list[0][4].replace(",", ""))
    except (ValueError, IndexError):
        latest_close = 0
    dic["price"] = latest_close
    dic["access_date_price"] = datetime.now()
    dic["_daily_price_list_raw"] = daily_price_list

    # キャッシュに保存
    _save_yfinance_cache(cache_fname, latest_close, daily_price_list)
    log_print("<---- yfinance米国指数日足取得完了: %s 価格=%d データ数=%d" % (
        code_s, latest_close, len(daily_price_list)))
    return dic


def get_us_index_weekly(code_s, ticker_symbol, upd=UPD_INTERVAL, prices=[]):
    """米国指数の週次価格データをyfinanceから取得して指標計算する
    日本指数の get_weekly_price_data と対になる関数。
    Args:
        code_s: キャッシュキー識別用の擬似コード
        ticker_symbol: yfinanceティッカー
        upd: 更新レベル
        prices: [終値, 高値, 安値] の現在価格リスト
    Returns:
        dict: 週次指標 (rs_raw, momentum_pt, trend_template 等)。失敗時は空dict
    """
    cache_fname = YFINANCE_WEEKLY_CACHE_FNAME % code_s

    # キャッシュチェック
    if upd < UPD_FORCE and os.path.exists(cache_fname):
        if upd < UPD_INTERVAL:
            _, pl = _load_yfinance_cache(cache_fname)
            if pl is not None:
                log_debug("yfinance米国指数週足キャッシュ使用(UPD_CACHE): %s" % code_s)
                weekly_price_list = [tuple(row) for row in pl]
                return _calc_weekly_indicators(weekly_price_list, prices)
        else:
            cache_ok, _ = is_file_timestamp(cache_fname, INTERVAL_DAY_W)
            if cache_ok:
                _, pl = _load_yfinance_cache(cache_fname)
                if pl is not None and _is_weekly_cache_fresh([tuple(row) for row in pl]):
                    log_debug("yfinance米国指数週足キャッシュ使用(UPD_INTERVAL): %s" % code_s)
                    weekly_price_list = [tuple(row) for row in pl]
                    return _calc_weekly_indicators(weekly_price_list, prices)

    log_print("----> %sの週次価格情報をyfinance(%s)から取得します" % (code_s, ticker_symbol))
    try:
        with sema:
            ticker = yf.Ticker(ticker_symbol)
            df = ticker.history(period="2y", interval="1wk", auto_adjust=True)
    except Exception as e:
        log_warning("yfinance米国指数週足取得エラー(%s): %s" % (code_s, e))
        return {}

    if df is None or df.empty:
        log_warning("yfinance米国指数週足データなし: %s" % code_s)
        return {}

    df = df.dropna(subset=["Close"])
    if df.empty:
        log_warning("yfinance米国指数週足データなし(NaN除去後): %s" % code_s)
        return {}

    weekly_price_list = _convert_weekly_df_to_kabutan_format(df)
    if not weekly_price_list:
        log_warning("yfinance米国指数週足変換失敗: %s" % code_s)
        return {}

    _save_yfinance_cache(cache_fname, None, weekly_price_list)
    log_print(">>>>> %sの週次価格データを解析(yfinance) " % code_s)
    parsed_data_w = _calc_weekly_indicators(weekly_price_list, prices)
    log_print("<<<<< 解析完了(yfinance) 週数=%d" % len(weekly_price_list))
    return parsed_data_w


def get_weekly_data_yfinance(code_s, stock={}, upd=UPD_INTERVAL):
    """yfinance APIで週次価格データを取得する
    Args:
        code_s: 銘柄コード文字列
        stock: 銘柄DB情報（マーケットコード判定用）
        upd: 更新レベル
    Returns:
        list[tuple[str, ...]] | None: Kabutan互換のweekly_price_list、失敗時None
    """
    cache_fname = YFINANCE_WEEKLY_CACHE_FNAME % code_s

    # キャッシュチェック
    if upd < UPD_FORCE and os.path.exists(cache_fname):
        if upd < UPD_INTERVAL:
            # UPD_CACHE: キャッシュがあればそのまま使用
            _, pl = _load_yfinance_cache(cache_fname)
            if pl is not None:
                log_debug("yfinance週足キャッシュ使用(UPD_CACHE): %s" % code_s)
                return [tuple(row) for row in pl]
        else:
            # UPD_INTERVAL: TTL内かつ内容が最新確定週を含んでいればキャッシュ使用
            cache_ok, cach_date = is_file_timestamp(cache_fname, INTERVAL_DAY_W)
            if cache_ok:
                _, pl = _load_yfinance_cache(cache_fname)
                if pl is not None and _is_weekly_cache_fresh([tuple(row) for row in pl]):
                    log_debug("yfinance週足キャッシュ使用(UPD_INTERVAL): %s" % code_s)
                    return [tuple(row) for row in pl]
                elif pl is not None:
                    log_debug("yfinance週足キャッシュ古い(最新確定週なし)、再取得: %s" % code_s)

    # yfinance APIで取得
    ticker_symbol = _get_ticker_symbol(code_s, stock)

    log_print("----> %sの週次価格情報をyfinance(%s)から取得します" % (code_s, ticker_symbol))
    try:
        with sema:
            ticker = yf.Ticker(ticker_symbol)
            df = ticker.history(period="2y", interval="1wk", auto_adjust=True)
    except Exception as e:
        log_warning("yfinance週足取得エラー(%s): %s" % (code_s, e))
        return None

    if df is None or df.empty:
        log_warning("yfinance週足データなし: %s" % code_s)
        return None

    # NaN行を除去
    df = df.dropna(subset=["Close"])
    if df.empty:
        log_warning("yfinance週足データなし(NaN除去後): %s" % code_s)
        return None

    weekly_price_list = _convert_weekly_df_to_kabutan_format(df)
    if not weekly_price_list:
        log_warning("yfinance週足価格リスト変換失敗: %s" % code_s)
        return None

    # キャッシュに保存（price_currentは週足では不要だがスキーマ互換のためNone）
    _save_yfinance_cache(cache_fname, None, weekly_price_list)
    log_print("<---- yfinance週足取得完了: %s データ数=%d週" % (code_s, len(weekly_price_list)))
    return weekly_price_list


def get_monthly_data_yfinance(code_s, stock={}, upd=UPD_INTERVAL):
    """yfinance APIで月次価格データを取得する (issue #53)
    Args:
        code_s: 銘柄コード文字列
        stock: 銘柄DB情報（マーケットコード判定用）
        upd: 更新レベル
    Returns:
        list[tuple[str, ...]] | None: Kabutan互換のmonthly_price_list、失敗時None
    """
    cache_fname = YFINANCE_MONTHLY_CACHE_FNAME % code_s

    # キャッシュチェック
    if upd < UPD_FORCE and os.path.exists(cache_fname):
        if upd < UPD_INTERVAL:
            # UPD_CACHE: キャッシュがあればそのまま使用
            _, pl = _load_yfinance_cache(cache_fname)
            if pl is not None:
                log_debug("yfinance月足キャッシュ使用(UPD_CACHE): %s" % code_s)
                return [tuple(row) for row in pl]
        else:
            # UPD_INTERVAL: TTL内かつ内容が最新確定月を含んでいればキャッシュ使用
            cache_ok, cach_date = is_file_timestamp(cache_fname, INTERVAL_DAY_M)
            if cache_ok:
                _, pl = _load_yfinance_cache(cache_fname)
                if pl is not None and _is_monthly_cache_fresh([tuple(row) for row in pl]):
                    log_debug("yfinance月足キャッシュ使用(UPD_INTERVAL): %s" % code_s)
                    return [tuple(row) for row in pl]
                elif pl is not None:
                    log_debug("yfinance月足キャッシュ古い(最新確定月なし)、再取得: %s" % code_s)

    # yfinance APIで取得
    ticker_symbol = _get_ticker_symbol(code_s, stock)

    log_print("----> %sの月次価格情報をyfinance(%s)から取得します" % (code_s, ticker_symbol))
    try:
        with sema:
            ticker = yf.Ticker(ticker_symbol)
            df = ticker.history(period="10y", interval="1mo", auto_adjust=True)
    except Exception as e:
        log_warning("yfinance月足取得エラー(%s): %s" % (code_s, e))
        return None

    if df is None or df.empty:
        log_warning("yfinance月足データなし: %s" % code_s)
        return None

    # NaN行を除去
    df = df.dropna(subset=["Close"])
    if df.empty:
        log_warning("yfinance月足データなし(NaN除去後): %s" % code_s)
        return None

    monthly_price_list = _convert_monthly_df_to_kabutan_format(df)
    if not monthly_price_list:
        log_warning("yfinance月足価格リスト変換失敗: %s" % code_s)
        return None

    # キャッシュに保存（price_currentは月足では不要だがスキーマ互換のためNone）
    _save_yfinance_cache(cache_fname, None, monthly_price_list)
    log_print("<---- yfinance月足取得完了: %s データ数=%dヶ月" % (code_s, len(monthly_price_list)))
    return monthly_price_list


def prefetch_yfinance_batch(code_s_list, stocks=None):
    """複数銘柄を一括ダウンロードしてキャッシュに保存する
    Args:
        code_s_list: 銘柄コード文字列のリスト
        stocks: 銘柄DB（市場コード解決用、Noneなら全て東証扱い）
    """

    # キャッシュが有効な銘柄はスキップ
    codes_to_fetch = []
    for code_s in code_s_list:
        cache_fname = YFINANCE_CACHE_FNAME % code_s
        if os.path.exists(cache_fname):
            cache_ok, _ = is_file_timestamp(cache_fname, INTERVAL_DAY_D)
            if cache_ok:
                _, pl = _load_yfinance_cache(cache_fname)
                if pl is not None and _is_daily_cache_fresh(pl):
                    continue
        codes_to_fetch.append(code_s)

    if not codes_to_fetch:
        log_print("yfinanceバッチ: 全銘柄キャッシュ有効、スキップ")
        return

    log_print("yfinanceバッチダウンロード: %d銘柄" % len(codes_to_fetch))

    # 銘柄コード→ティッカーシンボルのマッピングを構築
    code_to_ticker = {}
    for code_s in codes_to_fetch:
        stock = {}
        if stocks is not None:
            stock = stocks.get(code_s, {})
        code_to_ticker[code_s] = _get_ticker_symbol(code_s, stock)

    # 100銘柄ずつバッチ処理
    BATCH_SIZE = 100
    for batch_start in range(0, len(codes_to_fetch), BATCH_SIZE):
        batch_codes = codes_to_fetch[batch_start : batch_start + BATCH_SIZE]
        tickers = [code_to_ticker[c] for c in batch_codes]
        ticker_str = " ".join(tickers)

        log_print("yfinanceバッチ: %d/%d銘柄を一括取得中..." % (
            min(batch_start + BATCH_SIZE, len(codes_to_fetch)),
            len(codes_to_fetch),
        ))

        try:
            df = yf.download(
                ticker_str,
                # 3mo: 個別取得側 (get_daily_data_yfinance) と揃える。
                # 30日streak判定のバッファ確保のため 2mo→3mo に拡張。
                period="3mo",
                auto_adjust=True,
                repair=True,
                threads=True,
                progress=False,
            )
        except Exception as e:
            log_warning("yfinanceバッチ取得エラー: %s" % e)
            continue

        if df is None or df.empty:
            log_warning("yfinanceバッチデータなし")
            continue

        # 各銘柄のデータを個別にキャッシュに保存
        for code_s, ticker_s in zip(batch_codes, tickers):
            try:
                if len(batch_codes) == 1:
                    # 1銘柄の場合はMultiIndexにならない
                    single_df = df
                else:
                    # MultiIndexカラムから銘柄を抽出
                    single_df = df.xs(ticker_s, level=1, axis=1)
                if single_df.empty:
                    continue
                # NaNの行を除去
                single_df = single_df.dropna(subset=["Close"])
                if single_df.empty:
                    continue
                price_list = _convert_df_to_price_list(single_df)
                if price_list:
                    price_current = price_list[0][6]
                    cache_fname = YFINANCE_CACHE_FNAME % code_s
                    _save_yfinance_cache(cache_fname, price_current, price_list)
            except Exception as e:
                log_warning("yfinanceバッチ個別変換エラー(%s): %s" % (code_s, e))
                continue

        # バッチ間の待機（レートリミット対策）
        if batch_start + BATCH_SIZE < len(codes_to_fetch):
            import time
            log_print("yfinanceバッチ: 5秒待機（レートリミット対策）...")
            time.sleep(5)

    log_print("yfinanceバッチダウンロード完了")


def prefetch_yfinance_weekly_batch(code_s_list, stocks=None):
    """複数銘柄の週足データを一括ダウンロードしてキャッシュに保存する
    Args:
        code_s_list: 銘柄コード文字列のリスト
        stocks: 銘柄DB（市場コード解決用、Noneなら全て東証扱い）
    """

    # キャッシュが有効 かつ 最新確定週を含んでいる銘柄はスキップ
    codes_to_fetch = []
    for code_s in code_s_list:
        cache_fname = YFINANCE_WEEKLY_CACHE_FNAME % code_s
        if os.path.exists(cache_fname):
            cache_ok, _ = is_file_timestamp(cache_fname, INTERVAL_DAY_W)
            if cache_ok:
                _, pl = _load_yfinance_cache(cache_fname)
                if pl is not None and _is_weekly_cache_fresh([tuple(row) for row in pl]):
                    continue
        codes_to_fetch.append(code_s)

    if not codes_to_fetch:
        log_print("yfinance週足バッチ: 全銘柄キャッシュ有効、スキップ")
        return

    log_print("yfinance週足バッチダウンロード: %d銘柄" % len(codes_to_fetch))

    # 銘柄コード→ティッカーシンボルのマッピングを構築
    code_to_ticker = {}
    for code_s in codes_to_fetch:
        stock = {}
        if stocks is not None:
            stock = stocks.get(code_s, {})
        code_to_ticker[code_s] = _get_ticker_symbol(code_s, stock)

    # 100銘柄ずつバッチ処理
    BATCH_SIZE = 100
    for batch_start in range(0, len(codes_to_fetch), BATCH_SIZE):
        batch_codes = codes_to_fetch[batch_start : batch_start + BATCH_SIZE]
        tickers = [code_to_ticker[c] for c in batch_codes]
        ticker_str = " ".join(tickers)

        log_print("yfinance週足バッチ: %d/%d銘柄を一括取得中..." % (
            min(batch_start + BATCH_SIZE, len(codes_to_fetch)),
            len(codes_to_fetch),
        ))

        try:
            df = yf.download(
                ticker_str,
                period="2y",
                interval="1wk",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception as e:
            log_warning("yfinance週足バッチ取得エラー: %s" % e)
            continue

        if df is None or df.empty:
            log_warning("yfinance週足バッチデータなし")
            continue

        # 各銘柄のデータを個別にキャッシュに保存
        for code_s, ticker_s in zip(batch_codes, tickers):
            try:
                if len(batch_codes) == 1:
                    single_df = df
                else:
                    single_df = df.xs(ticker_s, level=1, axis=1)
                if single_df.empty:
                    continue
                single_df = single_df.dropna(subset=["Close"])
                if single_df.empty:
                    continue
                weekly_price_list = _convert_weekly_df_to_kabutan_format(single_df)
                if weekly_price_list:
                    cache_fname = YFINANCE_WEEKLY_CACHE_FNAME % code_s
                    _save_yfinance_cache(cache_fname, None, weekly_price_list)
            except Exception as e:
                log_warning("yfinance週足バッチ個別変換エラー(%s): %s" % (code_s, e))
                continue

        # バッチ間の待機（レートリミット対策）
        if batch_start + BATCH_SIZE < len(codes_to_fetch):
            import time
            log_print("yfinance週足バッチ: 5秒待機（レートリミット対策）...")
            time.sleep(5)

    log_print("yfinance週足バッチダウンロード完了")


def parse_date_str(s):
    """日付文字列を解析して date を返す（失敗なら None）。
    対応フォーマット: 'YYYY年M月D日', 'YYYY/MM/DD', 'YYYY-MM-DD', 'YY/MM/DD', 'YY-MM-DD', ISO

    Kabutan 株価HTMLの2桁年表記（例 '26/04/28'）は 20YY と解釈する。
    """
    if not s:
        return None
    # 1) YYYY年M月D日
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", s)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)}/{m.group(2)}/{m.group(3)}", "%Y/%m/%d"
            ).date()
        except Exception:
            return None
    # 2) YYYY/MM/DD or YYYY-MM-DD
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)}/{m.group(2)}/{m.group(3)}", "%Y/%m/%d"
            ).date()
        except Exception:
            return None
    # 3) YY/MM/DD or YY-MM-DD (Kabutan の2桁年表記)
    m = re.match(r"^\s*(\d{2})[/-](\d{1,2})[/-](\d{1,2})\s*$", s)
    if m:
        try:
            return datetime.strptime(
                f"20{m.group(1)}/{m.group(2)}/{m.group(3)}", "%Y/%m/%d"
            ).date()
        except Exception:
            return None
    # 4) ISO-ish fallback
    try:
        return datetime.fromisoformat(s.strip()).date()
    except Exception:
        return None


# ---- 週ブ (volume_dryup_breakout, issue #384) の判定パラメータ
# 「乾(出来高dry up)→増(5日間の出来高拡大)→抜(価格上抜け)」を検知する。
# 日次「ブ」(単日の出来高急増) と別軸で、複数営業日にわたる出来高拡大局面を拾う。
VDB_EXPAND_DAYS = 5          # 拡大期間 (直近営業日数): 出来高拡大・価格上抜けを評価
VDB_SETUP_DAYS = 20          # セットアップ期間 (拡大期間の直前): 通常出来高・dry up を評価
VDB_DRYUP_LOOKBACK = 10      # セットアップ期間のうち dry up を探す直近日数
VDB_MIN_DAYS = VDB_EXPAND_DAYS + VDB_SETUP_DAYS  # 必要データ数 (25営業日)
VDB_DRYUP_RATIO = 0.60       # dry up 判定 (基準比): この比率以下が2日以上
VDB_DRYUP_STRONG_RATIO = 0.45  # 強い dry up (基準比): この比率以下が1日以上でも可
VDB_EXPAND_AVG_RATIO = 1.5   # 増: 5日平均出来高が基準比この倍以上
VDB_EXPAND_DAY_RATIO = 1.2   # 増: 5日中この倍以上の日が
VDB_EXPAND_DAY_COUNT = 3     # 増: 上記を満たす日が3日以上


def _vdb_check(price_list, offset):
    """price_list[offset:] を最新日とみなし週ブ条件 (乾→増→抜) を判定する。

    Args:
        price_list: [date_str, open, high, low, close, volume, adj_close] (新しい日が先頭)
        offset: 最新日とみなす起点インデックス
    Returns:
        (per, dryup_pct) | None: 成立時は (5日平均出来高の基準比%, 最小出来高の基準比%)。
        不成立・データ不足・基準出来高0 なら None。
    """
    from statistics import median

    seg = price_list[offset:]
    if len(seg) < VDB_MIN_DAYS:
        return None
    expand = seg[:VDB_EXPAND_DAYS]                       # 直近5日
    setup = seg[VDB_EXPAND_DAYS:VDB_EXPAND_DAYS + VDB_SETUP_DAYS]  # 直前20日
    dryup_window = setup[:VDB_DRYUP_LOOKBACK]            # セットアップの直近10日

    setup_vols = [row[5] for row in setup]
    base_vol = median(setup_vols)                        # 基準出来高 (中央値)
    if base_vol <= 0:
        return None

    # 1. dry up (乾): 基準×0.60以下が2日以上、or 基準×0.45以下が1日以上
    dryup_vols = [row[5] for row in dryup_window]
    n_dryup = sum(1 for v in dryup_vols if v <= base_vol * VDB_DRYUP_RATIO)
    n_strong = sum(1 for v in dryup_vols if v <= base_vol * VDB_DRYUP_STRONG_RATIO)
    if not (n_dryup >= 2 or n_strong >= 1):
        return None

    # 2. 出来高拡大 (増): 5日平均≥基準×1.5 かつ 5日中3日以上が基準×1.2以上
    expand_vols = [row[5] for row in expand]
    avg5 = sum(expand_vols) / len(expand_vols)
    if avg5 < base_vol * VDB_EXPAND_AVG_RATIO:
        return None
    n_expand = sum(1 for v in expand_vols if v >= base_vol * VDB_EXPAND_DAY_RATIO)
    if n_expand < VDB_EXPAND_DAY_COUNT:
        return None

    # 3. 価格上抜け (抜): 最新終値 (発生日) がセットアップ20日終値高値を上回り、
    # かつ MA10 以上。5日中どこか1日が高値超えでは、その後レンジ内へ戻った銘柄を
    # 発生日 (最新日) の上抜けとして誤検知するため、最新終値そのもので判定する。
    setup_close_high = max(row[6] for row in setup)
    if seg[0][6] <= setup_close_high:
        return None
    ma10 = sum(seg[i][6] for i in range(10)) / 10        # 直近10日終値平均
    if seg[0][6] < ma10:
        return None

    per = int(round(100 * avg5 / base_vol))
    dryup_pct = int(round(100 * min(dryup_vols) / base_vol))
    return per, dryup_pct


def calc_volume_dryup_breakout(price_list):
    """週ブ (出来高dry up後の5日間出来高拡大ブレイクアウト) を検知する (issue #384)。

    直近10日を走査し、当日 (offset) で全条件が成立し、かつ前日 (offset+1) では
    成立していなかった「初成立日」だけを記録する (連日成立時の重複記録を防ぐ)。

    Returns:
        list[str]: ["MM/DD,per,dryup_pct"] (新しい日が先頭)。per=5日平均出来高の
            基準中央値比%、dryup_pct=セットアップ直近10日の最小出来高の基準比%。
    """
    out = []
    # 各 ind で当日 (ind) と前日 (ind+1) を判定する。隣接 ind 間で ind+1 の判定が
    # 重複するため、直前反復の当日結果を prev_res として持ち回し再計算を避ける。
    prev_res = _vdb_check(price_list, 0)  # ind=0 の当日結果 (最新日)
    for ind in range(10):
        res = prev_res  # = _vdb_check(price_list, ind)
        next_res = _vdb_check(price_list, ind + 1)  # 前日 (次反復では当日になる)
        prev_res = next_res
        if res is None:
            continue
        # 前日も成立していれば同一局面として重複記録しない (初成立日のみ)。
        if next_res is not None:
            continue
        per, dryup_pct = res
        dt = parse_date_str(price_list[ind][0])
        day = dt.strftime("%m/%d") if dt else price_list[ind][0]
        log_debug("週ブ:%s,%d,%d" % (day, per, dryup_pct))
        out.append("%s,%d,%d" % (day, per, dryup_pct))
    return out


def parse_price_text_from_list(price_current, price_list):
    """パース済みprice_listから各種指標を計算する
    yfinanceパスとHTMLパースパス共通で使用。
    Args:
        price_current(int): 現在価格
        price_list(list): [date_str, open, high, low, close, volume, adj_close] のリスト
                          新しい日付が先頭
    Returns:
        (dict, list): 指標dict, [終値, 高値, 安値]
    """
    price = {}
    # 現在価格を更新
    price["price"] = price_current
    # ---- ここからprice_listを使って各種価格に伴う指標の計算
    # ---- 売り圧力レシオとローソク足ボラティリティ
    # 売り圧力レシオの計算
    sell_pressure_ratio = calc_sell_pressure_ratio(price_list)

    # "20日,5日(,買い集め)"のフォーマット
    price["sell_pressure_ratio"] = sell_pressure_ratio[0:3]
    price["stddev_volatility"] = sell_pressure_ratio[3:5]

    # 出来高
    res_dict = calc_avg_volume_d(price_list)
    price["avg_volume_d"] = [res_dict["avg_volume_d20"], res_dict["avg_volume_d5"]]

    # ---- ポケットピポット
    # 過去10日間で出来高の最も多い陽線の日
    # 出来高収縮では50ma、ブレイク後の上昇トレンドは10maを使うのが本来。
    # 日足は period="2mo" (~40営業日) で MA50 には不足するため、現状データで
    # 計算可能な MA25 を併用し、ベース形成期の中期押し目も拾う (issue #110)。
    pockets = []
    for ind in range(10):
        # 過去10日間の出来高
        volumes = []
        prices = []
        # 10日の下落日出来高を集める
        for j in range(10):
            try:
                if price_list[ind + j][6] < price_list[ind + j + 1][6]:
                    volumes.append(price_list[ind + j][5])
                prices.append(price_list[ind + j][6])
            except IndexError:
                log_debug("%d日目のデータなし" % j)
                break
        if len(prices) == 0:
            break
        ma10 = sum(prices) / len(prices)
        # MA25 (25日分揃わなければ MA10 のみで判定)
        prices25 = []
        for j in range(25):
            try:
                prices25.append(price_list[ind + j][6])
            except IndexError:
                break
        ma25 = sum(prices25) / len(prices25) if len(prices25) >= 25 else None
        down_vol = max(volumes) if len(volumes) > 0 else 0  # 下落日の出来高
        # down_vol = sum(volumes) # 下落日の出来高
        if price_list[ind][6] > price_list[ind + 1][6]:  # 上昇日
            vol = price_list[ind][5]
        else:
            continue
        ref_price = min(price_list[ind][3], price_list[ind + 1][6])  # 安値と前日終値
        kairi = round(100 * float(ref_price - ma10) / ma10, 1)  # 安値からのma10との乖離
        # MA25 乖離。MA10 or MA25 のいずれかが近ければ extended でないとみなす
        kairi25 = round(100 * float(ref_price - ma25) / ma25, 1) if ma25 else None
        not_extended = kairi <= 4 or (kairi25 is not None and kairi25 <= 4)
        # print "down_vol=", down_vol, "vol=", vol, "ma10=", ma10, "kairi=", kairi
        if vol > down_vol and not_extended:
            dt = parse_date_str(price_list[ind][0])
            if dt:
                day = dt.strftime("%m/%d")
            else:
                # フォーマット不明なら元文字列を短縮して使う
                day = price_list[ind][0]
            log_debug("ポケットピポット:%s(%+d)" % (day, kairi))
            pockets.append("%s,%d" % (day, kairi))
        # break
    price["pocket_pivot"] = pockets
    # raise

    # ---- ブレイクアウト
    breaks = []
    breaks_ext = []  # 高値追い圏の extended 候補 (詳細チャートのみ)
    for ind in range(10):
        # 過去 BREAKOUT_AVG_VOLUME_DAYS 日平均出来高 (issue #111)
        avg_vol = 0
        avg_count = 0
        for j in range(BREAKOUT_AVG_VOLUME_DAYS):
            if ind + 1 + j >= len(price_list):
                continue
            avg_vol += price_list[ind + 1 + j][5]  # +1:前日から
            avg_count += 1
        if avg_count == 0:
            continue
        avg_vol /= avg_count

        # 過去10日ma
        def get_ma10(diff=0):
            ma_count = 0
            ma10 = 0
            for j in range(10):
                if ind + j + diff >= len(price_list):
                    continue
                ma10 += price_list[ind + j + diff][6]  # 終値
                ma_count += 1
            if ma_count == 0:
                return 0
            ma10 /= ma_count
            return ma10

        # 当日安値と前日終値の安い方
        if price_list[ind][3] < price_list[ind + 1][6]:
            ma10 = get_ma10(0)
            ref_price = price_list[ind][3]
        else:
            ma10 = get_ma10(1)
            ref_price = price_list[ind + 1][6]
        if ma10 == 0:
            continue
        kairi = round(100 * float(ref_price - ma10) / ma10, 1)
        vol = price_list[ind][5]
        # ストップ高張り付き対応 (issue #253): 値幅制限上限張り付き時は買いが
        # 集中しても約定せず出来高が逆に減るため、出来高基準だけでは検知できない。
        # 前日比 +20% 以上 (値幅制限上限相当) なら出来高条件をスキップして検知する。
        prev_close = price_list[ind + 1][6]
        chg_rate = (price_list[ind][6] - prev_close) / prev_close if prev_close else 0.0
        # print "AVG:", ind, avg_vol, avg_count, vol, kairi
        vol_or_surge = vol >= 1.5 * avg_vol or chg_rate >= 0.20
        rising = price_list[ind][6] > price_list[ind + 1][6]
        if vol_or_surge and rising:
            dt = parse_date_str(price_list[ind][0])
            day = dt.strftime("%m/%d") if dt else price_list[ind][0]
            if kairi <= 5:
                # TODO: ボラティリティ的なブレイクアウトを見たほうが良いが、
                # ややめんどうなのでまずはma乖離で ローソク足ボラティリティを使えば良い
                # ストップ高張り付き時は出来高が減り per が負になりうるため 0 床。
                # 保存値は常に非負で強度バケット (issue #253 webapp) が単調に振る舞う。
                per = max(100 * vol / avg_vol - 100, 0)
                log_debug("ブレイク:%s,%d" % (day, per))
                breaks.append("%s,%d" % (day, per))
            elif kairi <= BREAKOUT_EXTENDED_KAIRI_MAX:
                # 高値追い圏 (規律上は買わない) の extended 候補。kairi (乖離) に加え
                # 出来高超過率 per も保存し、詳細チャートで乖離 tooltip を出しつつ
                # マーカーサイズを通常ブレイクと同じ強度バケットで決められるようにする。
                # 形式: "MM/DD,kairi,per" (既存 "MM/DD,kairi" との後方互換のため末尾追加)。
                per = max(100 * vol / avg_vol - 100, 0)
                log_debug("ブレイクext:%s,%d,%d" % (day, round(kairi), per))
                breaks_ext.append("%s,%d,%d" % (day, round(kairi), per))
    price["breakout"] = breaks
    price["breakout_extended"] = breaks_ext
    # print breaks

    # ---- 週ブ (出来高dry up後の5日間出来高拡大ブレイクアウト, issue #384)
    price["volume_dryup_breakout"] = calc_volume_dryup_breakout(price_list)

    # ---- 過去価格
    past_prices = []
    # 20日前比較に必要な 21件 + バッファ
    LOG_DAY = 30
    for ind in range(LOG_DAY):
        if ind >= len(price_list):
            continue
        dt = parse_date_str(price_list[ind][0])
        if not dt:
            log_warning(" 日付解析失敗: %s" % price_list[ind][0])
            continue
        pr = price_list[ind][6]  # int 終値
        past_prices.append((dt, pr))
        # print "過去価格", past_prices
    price["price_log"] = past_prices
    # TODO: 週次でやっている20MA押しをやりたい

    # ---- 10日MA乖離率 + 30日10ma維持判定 (トレンド列の点線マーカー用)
    # この price_list は終値が [6]・安値が [3] (int 済み)。_calc_daily_indicators とは
    # カラム体系が異なる点に注意。calc_ma10_kairi_indicators のロジックを共通化。
    closes = [row[6] for row in price_list]
    lows = [row[3] for row in price_list]
    price.update(calc_ma10_kairi_indicators(closes, lows))

    return price, [price_list[0][6], price_list[0][2], price_list[0][3]]



def get_price_log(price_list, dt):
    """
    price_list: 過去価格データ　(日付、価格)タプルのリスト
    dt: 価格を欲しい日付(date型)
    (list, date) -> int
    """
    if dt:
        for pr in price_list:
            # print pr[0], "<->", dt
            if pr[0] <= dt:  # 過去の日を許容する
                return pr[1]
    return 0


def get_price_data_yahoo(code_s, stock, upd=UPD_INTERVAL):
    """yfinance APIから日次価格データをパースしてdictで返す
    Returns:
        dict<int, T>: 解析した価格情報
        list<int>: 現在価格
    """
    try:
        price_current, price_list = get_daily_data_yfinance(code_s, stock, upd)
        if price_current is None or not price_list:
            return {}, []
        log_print(">>>>> %sの価格データを解析(yfinance) " % code_s)
        parsed_data, cur_prices = parse_price_text_from_list(
            price_current, price_list
        )
        parsed_data["_daily_price_list_raw"] = price_list
        price_val = parsed_data.get("price", 0)
        # キャッシュの更新日時を取得
        cache_fname = YFINANCE_CACHE_FNAME % code_s
        if os.path.exists(cache_fname):
            stat = os.stat(cache_fname)
            update_date = datetime.fromtimestamp(stat.st_mtime)
        else:
            update_date = datetime.now()
        log_print("<<<<< 解析完了(yfinance) ", price_val, update_date)
        set_db_code(parsed_data, code_s)
        if price_val > 0:
            parsed_data["access_date_price"] = update_date
        return parsed_data, cur_prices
    except Exception as e:
        log_warning("yfinance価格取得失敗(%s): %s" % (code_s, e))
        return {}, []


# 市場指数コード（Kabutan固有、yfinanceでは無効）
_MARKET_INDEX_CODES = {"0000", "0010", "0012", "0800", "0802"}


def get_weekly_price_data(code_s, stock={}, upd=UPD_INTERVAL, prices=[]):
    """週次データからRSを返す
    一般銘柄はyfinance優先、市場指数はKabutan固定。
    Args:
        code_s: 銘柄コード文字列
        stock: 銘柄DB情報（マーケットコード判定用）
        upd: 更新レベル
        prices: [終値, 高値, 安値] の現在価格リスト
    Returns:
        dict: 指標dict
    """
    # 市場指数コードはyfinanceティッカーに変換できないためKabutan固定
    if code_s not in _MARKET_INDEX_CODES:
        weekly_price_list = get_weekly_data_yfinance(code_s, stock, upd)
        if weekly_price_list is not None:
            log_print(">>>>> %sの週次価格データを解析(yfinance) " % code_s)
            parsed_data_w = _calc_weekly_indicators(weekly_price_list, prices)
            log_print("<<<<< 解析完了(yfinance) ")
            parsed_data_w["_weekly_price_list_raw"] = weekly_price_list
            return parsed_data_w
        log_print("yfinance週足取得失敗、Kabutanフォールバック: %s" % code_s)

    # Kabutanパス（市場指数 or yfinanceフォールバック）
    cache = upd <= UPD_REEVAL
    price_htmls = get_weekly_html(code_s, cache)
    if not price_htmls:
        log_warning(" 株探からも週次価格を取得できません")
        return {}
    log_print(">>>>> %sの週次価格データを解析(Kabutan) " % code_s)
    parsed_data_w = parse_pricew_htmls_kabutan(price_htmls, prices)
    log_print("<<<<< 解析完了(Kabutan) ")
    return parsed_data_w


def _calc_monthly_position(monthly_price_list, price_current=0):
    """月足リストから月足位置評価の特徴量を計算する (issue #53)

    shelve には月足全系列でなく本特徴量のみ保存する (DB肥大防止)。
    タグ判定 (月低/月破/月高) の閾値は解釈層 (make_stock_db.judge_monthly_position)
    が持ち、本関数は事実 (高値・安値・位置・ブレイク月) のみを返す。

    Args:
        monthly_price_list: _convert_monthly_df_to_kabutan_format形式のリスト (新しい月が先頭)
        price_current: 現在株価 (0なら最新月終値で代用)
    Returns:
        dict | None: 特徴量dict。計算不能 (データなし・レンジゼロ) 時はNone
    """
    from statistics import median
    if not monthly_price_list:
        return None
    try:
        highs = [float(p[2].replace(",", "")) for p in monthly_price_list]
        lows = [float(p[3].replace(",", "")) for p in monthly_price_list]
        closes = [float(p[4].replace(",", "")) for p in monthly_price_list]
    except (ValueError, IndexError):
        log_warning(" 月足価格データが欠けている")
        return None

    high_10y = max(highs)
    low_10y = min(lows)
    if high_10y <= low_10y:
        return None
    range_10y = high_10y - low_10y

    p_cur = price_current if price_current else closes[0]
    pos_10y_pct = round((p_cur - low_10y) / range_10y * 100, 1)

    # ブレイク基準線: 直近3ヶ月を除く過去3年 (バー3〜35) の高値
    w = MONTHLY_BREAK_WINDOW
    prior_highs = highs[w:w + 36]
    high_3y_prior = max(prior_highs) if prior_highs else None

    # break_month: 直近3ヶ月内で月高値が基準線を上回った最新月 ("YYYY-MM")
    break_month = None
    if high_3y_prior is not None:
        for i in range(min(w, len(highs))):
            if highs[i] > high_3y_prior:
                dt = _parse_weekly_cache_date(monthly_price_list[i][0])
                if dt is not None:
                    break_month = "%04d-%02d" % (dt.year, dt.month)
                break

    # 低位滞留度: 直近3年 (直近3ヶ月除く) の月終値レンジ位置%の中央値。
    # 閾値は焼き込まず中央値スカラーで保存し、較正を解釈層の定数変更だけで済ませる
    prior_closes = closes[w:w + 36]
    pos_3y_median_pct = None
    if prior_closes:
        pos_3y_median_pct = round(
            median((c - low_10y) / range_10y * 100 for c in prior_closes), 1
        )

    return {
        "months": len(monthly_price_list),
        "high_10y": high_10y,
        "low_10y": low_10y,
        "pos_10y_pct": pos_10y_pct,
        "high_3y_prior": high_3y_prior,
        "break_month": break_month,
        "pos_3y_median_pct": pos_3y_median_pct,
        "updated_at": datetime.now().strftime("%Y-%m-%d"),
    }


def get_monthly_price_data(code_s, stock={}, upd=UPD_INTERVAL, prices=[]):
    """月足位置評価の特徴量を取得する (issue #53)

    「取得失敗」と「計算不能」を区別する:
    - 取得失敗 → {} を返しキーをemitしない → update_db が既存値を保持し次回リトライ
    - 計算不能 → {"monthly_position": None} で明示的に無効化 (古い評価の残留防止)
    update_db の保護キーには入れない (dictのキー単位マージだと古い break_month が
    残留するため、スナップショット丸ごと置換が正)。
    Args:
        code_s: 銘柄コード文字列
        stock: 銘柄DB情報（マーケットコード判定用）
        upd: 更新レベル
        prices: [終値, 高値, 安値] の現在価格リスト
    Returns:
        dict: {"monthly_position": dict | None} または {} (評価対象外・取得失敗)
    """
    # 市場指数は月足評価の対象外
    if code_s in _MARKET_INDEX_CODES:
        return {}
    monthly_price_list = get_monthly_data_yfinance(code_s, stock, upd)
    if monthly_price_list is None:
        return {}
    price_current = prices[0] if prices else 0
    mp = _calc_monthly_position(monthly_price_list, price_current)
    return {"monthly_position": mp}


def get_daily_price_kabutan(code_s, upd=UPD_INTERVAL):
    """日次の価格データを取得する
    マーケットデータ作成時に呼ばれる
    """
    cache = upd <= UPD_REEVAL
    price_html = get_daily_html_kabutan(code_s, cache)
    if not price_html:
        log_warning(" 株探から日次価格を取得できません")
        return {}
    log_print(">>>>> %sの日次価格データを解析 " % code_s)
    parsed_data_d = parse_price_d_html_kabutan(price_html)
    log_print("<<<<< 解析完了 ")
    return parsed_data_d


def get_price_data(code_s, stock={}, upd=UPD_INTERVAL):
    """価格情報を取得して(yahooから)
    更新情報を返す
    type: (str,bool) -> dict
    """
    # 日次データ
    parsed_data, cur_prices = get_price_data_yahoo(code_s, stock, upd)
    # 週次データを取得してRSを求める
    parsed_data_w = get_weekly_price_data(code_s, stock=stock, upd=upd, prices=cur_prices)
    daily_price_list = parsed_data.pop("_daily_price_list_raw", None)
    weekly_price_list = parsed_data_w.pop("_weekly_price_list_raw", None)
    if daily_price_list and weekly_price_list:
        add_weekly_ma_violations(parsed_data, daily_price_list, weekly_price_list)
    parsed_data.update(parsed_data_w)
    # 月足位置評価 (issue #53)
    parsed_data.update(get_monthly_price_data(code_s, stock=stock, upd=upd, prices=cur_prices))
    return parsed_data


def get_spr_expr(sprs, sprs_w):
    """売り圧力レシオと買い集めの文字列表現を返す
    Params: list 0:20日SPR 1:5日SPR 2:買い集めSPR
    Returns: str 文字列表現
    """
    if not sprs:
        return ""
    # 売り圧力レシオ
    exprs = [str(v) for v in sprs[:2]]
    # 買い集め
    # 週次
    if sprs_w:
        diff = sprs_w[2] - sprs_w[0]
        eval = step_func(diff, [-10, -5, 0, 5, 10], ["E", "D", "C", "B", "A"])
        exprs.append(eval)
    # 日次
    if len(sprs) >= 3:
        diff = sprs[2] - sprs[0]
        eval = step_func(diff, [-10, -5, 0, 5, 10], ["E", "D", "C", "B", "A"])
        exprs.append(eval)
    sell_press = ",".join(exprs)
    return sell_press


def main():
    """価格データを取得、解析する"""
    # TODO: <meta http-equiv="Pragma" content="no-cache">
    # となっているとき、取れていない
    # TODO: ローソク足ボラティリティの計算が疑問　終値の平均で割るところ
    # TODO: 1/5/20出来高変化率もほしい
    code_list = ["215A"]
    for code_s in code_list:
        log_print("-" * 30)
        log_print("%sの価格を更新します" % code_s)
        stock = make_stock_db.load_cacehd_stock_db(code_s)
        price_dict = get_price_data(
            code_s, stock, UPD_INTERVAL
        )  # UPD_INTERVAL/UPD_REEVAL/UPD_FORCE
        log_print(price_dict)
        # td = datetime.today().date()
        # print get_price_log(price_dict["price_log"], td)
        # print get_price_log(price_dict["price_log"], td-timedelta(1))
        # print get_price_log(price_dict["price_log"], td-timedelta(5))


if __name__ == "__main__":
    setup_logger("price")
    main()
