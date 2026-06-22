#!/usr/bin/env python3

import sys
import shutil
from datetime import datetime, date, timedelta
import csv
from contextlib import contextmanager
import io
import traceback

from ks_util import *
import gyoseki
import rironkabuka
import shihyou
import price
import master
import make_market_db
import kessan
from db_shelve import get_stock_db as _get_stock_shelve_db, ShelveDB, STOCKS_SHELVE


def has_stock_data(stocks, code_s, latest=False):
    """
    DBに基本銘柄情報があるか？
    latest: 最新であることが必要かどうか
    """
    INTERVAL_DAY = 7  # 10
    # code = int(code)
    if code_s in stocks:
        if "stock_name" in stocks[code_s]:
            if latest:  # 最新だ
                timedelta = datetime.today() - stocks[code_s]["access_date"]
                if timedelta.days < INTERVAL_DAY:
                    # print "基本情報あり: %d日前"%timedelta.days
                    return True
                else:  # 最新でない
                    log_print("基本銘柄更新: %d日ぶり" % timedelta.days)
                    return False
            else:
                return True
    return False


def get_stock_master_data(stocks, code_s, upd=UPD_INTERVAL):
    """基本銘柄情報をDBから取得
    DBにない場合は通信またはキャッシュから取得
    dict,int,bool => dict
    Returns:
        dict<key, value>: 銘柄情報
    """
    # DBにある場合はそれを返す
    if code_s in stocks and upd < UPD_INTERVAL:
        if "stock_name" in stocks[code_s]:
            return stocks[code_s]

    return master.get_stock_master_data(code_s, upd)


def is_latest_price(stocks, code_s):
    """DB価格データ取得の日付現在日付から、
    最新価格データかどうかを返す
    """
    # 当日なら
    need_dt = get_price_day(datetime.today())
    price_dt = get_price_day(stocks[code_s]["access_date_price"])
    price_day = date(price_dt.year, price_dt.month, price_dt.day)
    need_day = date(need_dt.year, need_dt.month, need_dt.day)
    if need_day.weekday() == 5:
        need_day -= timedelta(1)
    elif need_day.weekday() == 6:
        need_day -= timedelta(2)
    # print " 価格データ 必要:%s DB最新:%s"%(need_day, price_day)
    if need_day <= price_day:
        return True, (need_day - price_day).days
    return False, (need_day - price_day).days


def has_price_data(stocks, code_s, latest=False):
    """
    DBに価格情報があるか？
    """
    # code = int(code)
    if code_s in stocks:
        if "sell_pressure_ratio" in stocks[code_s]:
            if latest:
                is_latest, interval_day = is_latest_price(stocks, code_s)
                if is_latest:
                    return True
                else:
                    log_print("価格更新: %d日ぶり" % interval_day)
                    return False
            else:
                return True
    return False


def get_price_data(stocks, code_s, upd=UPD_INTERVAL):
    """銘柄価格情報を通信またはキャッシュから取得"""
    # code = int(code)
    # DBにありなおかつ最新である場合はそれを返す
    if code_s in stocks:  # and not latest: #デバッグ用強制
        if "access_date_price" in stocks[code_s] and upd < UPD_INTERVAL:
            log_debug("DBに最新価格情報があるためそれを取得します")
            return stocks[code_s]
    # 価格データを新規更新
    stock = stocks.get(code_s, {})
    price_dict = price.get_price_data(code_s, stock, upd)
    # 関連銘柄内ランクを更新
    # ↓取得できない＆あまり意味がないので封印
    price_dict["rs_rank_log"] = update_rs_rank(stocks, code_s)

    # 変則的だがテーマは日々移ろうので指標とともにファンダポイントを計算
    themes = stocks[code_s].get("themes", "") if code_s in stocks else None
    if themes:
        funda_pt = master.calc_fundamental(code_s, themes)
        price_dict["funda_pt"] = funda_pt

    return price_dict


# ランクログ保持日数。詳細チャートの週足20週窓 (≈100営業日) の約半分をカバーし、
# RS(0~99)履歴を右端側に重畳できるようにする (rs_rank_log / stock_rank_log 共用)。
RANK_LOG_DAYS = 60


def update_stock_log(rank_log, rank):
    """ランクログを更新
    Returns: RANK_LOG_DAYS 個(日分)のランクログ(新しい日付が先)
    """
    date = get_price_day(datetime.today())
    ind = 0
    found = False
    for day, rs in rank_log:
        if day == date:
            rank_log[ind] = (day, rank)
            found = True
            break
        ind += 1
    if not found:
        rank_log.insert(0, (date, rank))
    rank_log = sorted(rank_log, key=lambda x: x[0], reverse=True)
    # print "ランクログ更新:", ind, date, rank
    return rank_log[0:RANK_LOG_DAYS]


def update_stock_rank(stock, rank):
    """銘柄ランクログを更新"""
    stock_rank_log = stock.get("stock_rank_log", [])
    rank_dict = {}
    rank_dict["stock_rank_log"] = update_stock_log(stock_rank_log, rank)
    stock.update(rank_dict)  # 更新


def update_rs_rank(stocks, code_s):
    """RSとRSログを更新"""
    if code_s not in stocks:
        return []
    stock = stocks[code_s]
    rs_rank_log = stock.get("rs_rank_log", [])
    # print "rs_rank_log:", rs_rank_log
    rs_rank = stock.get("momentum_pt")

    return update_stock_log(rs_rank_log, rs_rank)


# ==================================================
# RSライン: 銘柄終値/TOPIX終値 の生比率系列
# ==================================================
def _build_close_map(price_log):
    """(date, close) タプル列から日付→終値の dict を生成する。終値0や偽値は除外。"""
    return {dt: close for dt, close in price_log if close}


def _topix_close_map(market_db):
    """TOPIX の price_log から日付→終値の dict を生成する"""
    topix_log = market_db.get("topix", {}).get("price_log", []) if market_db else []
    return _build_close_map(topix_log)


def _topix_week_close_map(market_db):
    """TOPIX の price_week_log から ISO週キー → 終値の dict を生成する (週足版)。

    yfinance 週足は月曜ラベル、Kabutan 週足は金曜ラベル (週確定後) で曜日が
    揃わないため、日付完全一致ではなく ISO週 (年, 週番号) で正規化する。
    同じ週内に複数エントリがあれば「より新しい日付の終値」が勝つ (= 月曜より金曜)。
    """
    topix_log = market_db.get("topix", {}).get("price_week_log", []) if market_db else []
    week_map = {}
    for dt, close in topix_log:
        if not close:
            continue
        key = dt.isocalendar()[:2]
        # 日付降順入力で先に最新が入るため、新しい日付ほど上書きを避ける
        if key not in week_map:
            week_map[key] = close
    return week_map


def compute_rs_line_weekly(stock, market_db):
    """銘柄とTOPIXの週次終値系列から rs_line（生比率）を計算する純粋関数。

    詳細ページの株価+RS週足チャート専用 (issue #239)。
    日足版 compute_rs_line() と同じスキーマ (日付降順) を返す。
    日付完全一致ではなく ISO週 (年, 週番号) で銘柄/TOPIX を突合する
    (yfinance=月曜ラベル / Kabutan=金曜ラベルの曜日差を吸収)。

    Args:
        stock (dict): 銘柄DBの1銘柄分dict (price_week_log を持つ)
        market_db (dict): get_market_db() の戻り値 (topix.price_week_log を持つ)

    Returns:
        list[tuple[date, float]]: 週次 rs_line系列（日付降順）。
            銘柄系列の日付をそのまま使う (TOPIX 側は同じ ISO 週の終値で除算)。
            終値0や TOPIX 週欠落の週は除外。データ不足時は空リスト。
    """
    stock_log = stock.get("price_week_log", [])
    if not stock_log:
        return []
    topix_map = _topix_week_close_map(market_db)
    if not topix_map:
        return []
    rs_line = []
    for dt, stock_close in stock_log:
        topix_close = topix_map.get(dt.isocalendar()[:2])
        if not topix_close or not stock_close:
            continue
        rs_line.append((dt, float(stock_close) / float(topix_close)))
    return rs_line


def compute_rs_line_weekly_new_high_5d(stock, market_db, lookback=20):
    """Blue Dot 判定: 直近5日の日足RSの最高値が、過去 lookback 週の週足RS最高値を超えるか。

    issue #239 仕様。週途中の銘柄でも「今週のピークが週足新高値圏に達したか」を
    日足の解像度で先取りできる判定。
      - 直近5日の日足RS = 日足 price_log[:5] と TOPIX 日足 price_log の日付一致比率
      - 過去 lookback 週の週足RS = compute_rs_line_weekly() の [1:lookback+1] 区間
        (= 「今週」を含めない過去の週足ピーク)

    Args:
        stock (dict): 銘柄DB 1件 (price_log + price_week_log)
        market_db (dict): get_market_db() の戻り値 (topix.price_log + price_week_log)
        lookback (int): 過去比較する週数 (デフォルト 20)

    Returns:
        bool: max(直近5日の日足RS) > max(過去 lookback 週の週足RS) なら True。
            データ不足 (週足RS が lookback 本未満 or 日足RS が空) は False。
    """
    weekly_rs = compute_rs_line_weekly(stock, market_db)
    if len(weekly_rs) < lookback + 1:
        return False
    past_max = max(v for _, v in weekly_rs[1:lookback + 1])

    # 直近5日の日足 RS を計算 (日足 price_log と TOPIX 日足を日付一致で割る)
    daily_rs = compute_rs_line(stock, market_db)
    if not daily_rs:
        return False
    recent_max = max(v for _, v in daily_rs[:5])
    return recent_max > past_max


def compute_rs_line(stock, market_db, topix_map=None):
    """銘柄とTOPIXの日次終値系列から rs_line（生比率）を計算する純粋関数。

    Args:
        stock (dict): 銘柄DBの1銘柄分dict (price_log を持つ)
        market_db (dict): get_market_db() の戻り値 (topix.price_log を持つ)
        topix_map (dict, optional): 事前構築済みの {date: topix_close}。
            全銘柄ループから呼ぶ場合、銘柄ごとの再構築を避けるため事前に
            _topix_close_map() で1回だけ作って渡すと無駄が減る。

    Returns:
        list[tuple[date, float]]: rs_line系列（日付降順）。
            日付不一致や TOPIX/銘柄の終値が0の日は除外。
            データ不足時は空リスト。
    """
    stock_log = stock.get("price_log", [])
    if not stock_log:
        return []
    if topix_map is None:
        topix_map = _topix_close_map(market_db)
    if not topix_map:
        return []
    rs_line = []
    for dt, stock_close in stock_log:
        topix_close = topix_map.get(dt)
        if not topix_close or not stock_close:
            continue
        rs_line.append((dt, float(stock_close) / float(topix_close)))
    return rs_line


def compute_rs_line_changes(stock, market_db, topix_map=None):
    """rs_line の「今日 vs 直近 N 日移動平均」乖離率 A・B と前日比 D を%値で計算する (issue #283)。

    A = 直近 5 日平均乖離率、B = 直近 20 日平均乖離率。20 本に満たない場合は
    19,18,17,16,15 本平均で B を代替 (近似値)。
    D = 前日比 (1点比較)。当日の瞬間的な強さの把握用。

    Returns:
        tuple[float|None, float|None, float|None]: (短期A%, 中期B%, 前日比D%)
            - rs_line が 5本未満 → A=None
            - rs_line が 5本以上15本未満 → (A, None, D)
            - rs_line が 15本以上20本未満 → (A, B_approx, D) ※15-19本平均で代替
            - rs_line が 20本以上 → (A, B, D)
            - rs_line が 2本未満 → D=None
            移動平均 (D は前日値) が0の場合も None
    """
    rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
    a, b, _, d = _rs_line_changes_from_line(rs_line)
    return (a, b, d)


def _fmt_rs_change(v):
    """rs_line 騰落率を符号付き整数% に整形 (None は "-")"""
    return "-" if v is None else "%+d" % round(v)


def get_rs_line_changes_expr(stock, market_db, topix_map=None, rs_line=None):
    """rs_line の移動平均乖離率を CSV 表示用の '中期B%/短期A%' 文字列にする。

    A = 5日平均乖離率、B = 20日平均乖離率 (issue #283 で N日前比から MA 乖離率に変更)。
    rs_line を渡せば再計算をスキップする (CSV ループで複数の rs_line 系関数を
    呼ぶ際に共有するため)。

    Returns:
        str: 例 "+12/+5"。20日比が 15-19本平均で代替された場合は末尾 * を付ける
            (例: "+12*/+5")。両方計算不能なら "" 、片方のみなら "-/+5" 等
    """
    if rs_line is None:
        rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
    a, b, b_is_approx, _ = _rs_line_changes_from_line(rs_line)
    if a is None and b is None:
        return ""
    b_str = _fmt_rs_change(b)
    if b is not None and b_is_approx:
        b_str += "*"
    return "%s/%s" % (b_str, _fmt_rs_change(a))


def _rs_line_changes_from_line(rs_line):
    """rs_line 系列から「今日 vs 直近 N 日移動平均」の乖離率 A・B と前日比 D を計算する内部関数。

    A = 直近 5 日平均乖離率、B = 直近 20 日平均乖離率 (いずれも今日 rs_line[0] を含む)。
    1 点比較 (旧: N 日前の 1 点との比) ではヒゲ・急変でブレるため、基準を移動平均にして
    ブレを 1/N に薄め、勢い・過熱の度合いを安定して捉える (issue #283)。
    D = 前日比。window=1 の MA 乖離は恒等的に 0 になるため、D だけは
    rs_line[1] との 1 点比較とする (当日の瞬間的な強さの把握用)。

    Returns:
        tuple[float|None, float|None, bool, float|None]: (A, B, B が代替値か, D)
            乖離率 = (rs_line[0] - mean(直近 window 本)) / mean(直近 window 本) * 100。
            A は window=5 (5 本未満は None)。
            B は window=20、20 本未満なら 19,18,17,16,15 本平均で代替 (b_is_approx=True)。
            D = (rs_line[0] - rs_line[1]) / rs_line[1] * 100 (2 本未満は None)。
            平均 (D は前日値) が 0 の場合も None。
    """
    if not rs_line:
        return (None, None, False, None)
    current = rs_line[0][1]

    def _deviation(window):
        """今日 rs_line[0] と直近 window 本 (今日含む) の平均との乖離率%。"""
        if len(rs_line) < window:
            return None
        ma = sum(v for _, v in rs_line[:window]) / window
        if ma == 0:
            return None
        return (current - ma) / ma * 100

    d = None
    if len(rs_line) >= 2 and rs_line[1][1] != 0:
        d = (current - rs_line[1][1]) / rs_line[1][1] * 100

    a = _deviation(5)
    b = _deviation(20)
    if b is not None:
        return (a, b, False, d)
    # 20 本に満たないときは、データ長に収まる最大 window (15-19 本) の平均で代替する。
    # MA 版では _deviation(window) が None になるのは len < window のときだけなので、
    # 試すべき window は min(len, 19) の 1 つに定まる (15 本未満なら代替不可)。
    if len(rs_line) >= 15:
        return (a, _deviation(min(len(rs_line), 19)), True, d)
    return (a, None, False, d)


def compute_rs_line_new_high(stock, market_db, topix_map=None, lookback=20, rs_line=None):
    """rs_line[0] が直近 lookback 日の最高値を更新したかを判定する純粋関数。

    横ばい（同値）は False とする。連日同値の場合に毎日 True が立つのを防ぎ、
    「今日新高値を取った」イベントだけをタグ化するため。

    rs_line を渡せば再計算をスキップする。

    Args:
        stock (dict): 銘柄DB1件
        market_db (dict): マーケットDB
        topix_map (dict, optional): 事前構築済み TOPIX 終値マップ
        lookback (int): 比較対象日数（デフォルト20）
        rs_line (list, optional): 事前計算済みの rs_line 系列

    Returns:
        bool: rs_line[0] > max(rs_line[1:lookback+1]) なら True。
            データ不足 (rs_line が lookback+1 本未満) は False。
    """
    if rs_line is None:
        rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
    if len(rs_line) < lookback + 1:
        return False
    current = rs_line[0][1]
    return current > max(v for _, v in rs_line[1:lookback + 1])


def compute_rs_line_divergence(stock, market_db, topix_map=None,
                               offset=20, threshold=3.0, rs_line=None):
    """株価と rs_line の同期間騰落率の食い違い（ダイバージェンス）を判定する。

    rs_line[0] と rs_line[offset] の日付を基準に、銘柄 price_log から
    同日終値を引いて騰落率を算出する。インデックスではなく日付で揃えるのは、
    rs_line が TOPIX と日付一致した日だけ残るため、price_log と rs_line の
    [offset] 番目が同じ日とは限らないため。

    rs_line を渡せば再計算をスキップする。

    Returns:
        str: "bullish"（強気: 株価↓ rs_line↑）/ "bearish"（弱気: 株価↑ rs_line↓）/ ""
    """
    if rs_line is None:
        rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
    if len(rs_line) <= offset:
        return ""
    dt_now, rs_now = rs_line[0]
    dt_past, rs_past = rs_line[offset]
    if rs_past == 0:
        return ""
    rs_change = (rs_now - rs_past) / rs_past * 100

    price_map = _build_close_map(stock.get("price_log", []))
    price_now = price_map.get(dt_now)
    price_past = price_map.get(dt_past)
    if not price_now or not price_past:
        return ""
    price_change = (price_now - price_past) / price_past * 100

    if price_change <= -threshold and rs_change >= threshold:
        return "bullish"
    if price_change >= threshold and rs_change <= -threshold:
        return "bearish"
    return ""


def get_rank_log_expr(stock):
    """RSログを表示用に整形"""
    rs_rank_log = stock.get("rs_rank_log", [])
    if not rs_rank_log:
        return ""
    latest_date = rs_rank_log[0][0]
    # 0はエラー値なので除外
    # TODO: 日付でフィルターするべきかも
    rs_rank_ma5 = [
        log_entry[1]
        for log_entry in rs_rank_log[0:5]
        if log_entry[0] >= latest_date - timedelta(days=7)
    ]
    rs_rank_ma5 = [value for value in rs_rank_ma5 if value is not None and value > 0]
    if not rs_rank_ma5:
        return ""
    rs_rank_ma5 = sum(rs_rank_ma5) / len(rs_rank_ma5)
    rs_rank_ma20 = [
        log_entry[1]
        for log_entry in rs_rank_log[0:20]
        if log_entry[0] >= latest_date - timedelta(days=28)
    ]
    rs_rank_ma20 = [value for value in rs_rank_ma20 if value is not None and value > 0]
    if not rs_rank_ma20:
        return ""
    rs_rank_ma20 = sum(rs_rank_ma20) / len(rs_rank_ma20)
    return "%02d%02d" % (rs_rank_ma5, rs_rank_ma20)


def get_rank_log(stock, log_name, diff_day=0):
    """diff_day日前のrank_logを返す
    -> (day, rs)
    """
    rank_log = stock.get(log_name, ())
    if not rank_log:
        return ()
    day_first = rank_log[0][0]
    # print "day_first:", day_first
    for day, rs in rank_log:
        if (day_first - day).days >= diff_day:
            return day, rs
    return (None, 0)


# def get_relates_rank(stocks, code):
# 	"""
# 	関連銘柄内ランクを更新
# 	"""
# 	# ---- relates_rsを計算
# 	if stocks[code].has_key("relates"):
# 		relates = stocks[code]["relates"]
# 		rs_raws = []
# 		if stocks[code].has_key("rs_raw"):
# 			rs_raw = stocks[code]["rs_raw"]
# 			rs_raws.append(rs_raw)
# 			for relate in relates.split(","):
# 				try:
# 					if stocks.has_key(int(relate)):
# 						if stocks[int(relate)].has_key("rs_raw"):
# 							rs_raws.append(stocks[int(relate)]["rs_raw"])
# 						else:
# 							print "!!! 関連銘柄%sのRSはありません"%relate
# 					else:
# 						print "!!! 関連銘柄%sは銘柄DBにありません"%relate
# 				except ValueError:
# 					print "!!! 不正な関連銘柄です", relate
# 			# print relates, rs_raws
# 			rs_raws.sort(reverse=True)
# 			relates_rank = rs_raws.index(rs_raw)+1
# 			print "関連銘柄内ランク:", relates_rank
# 			return relates_rank
# 	return 0


def need_kessan_upd(stocks, code_s, dt_access):
    """アクセス時間の決算日超過のチェック"""
    kessan_upd = False
    dt_access2 = get_price_day(dt_access)
    tdy = get_price_day(datetime.today())
    try:
        # 決算日とアクセス時間の間隔を取得
        kessanbi = stocks[code_s].get("kessanbi", "")
        if kessanbi:
            dt_kessanbi = datetime.strptime(
                stocks[code_s]["kessanbi"], "%Y/%m/%d"
            ).date()
            # print "決算発表日付:", kessanbi, dt_kessanbi
            if tdy >= dt_kessanbi and dt_access2 < dt_kessanbi:
                log_print("決算日を過ぎているため更新", code_s, dt_kessanbi, dt_access2)
                # upd = UPD_FORCE
                kessan_upd = True
        kessan_mod_date = stocks[code_s].get("kessan_mod_date", "")
        if kessan_mod_date:
            dt_kessan_mod = datetime.strptime(
                stocks[code_s]["kessan_mod_date"], "%Y/%m/%d"
            ).date()
            # print "決算修正日付:", kessan_mod_date, "アクセス:", dt_kessan_mod, \
            #     dt_access2
            if tdy >= dt_kessan_mod and dt_access2 < dt_kessan_mod:
                log_print("決算修正があったため更新", code_s, dt_kessan_mod, dt_access2)
                kessan_upd = True
    except (KeyError, ValueError):
        log_print("決算データがない", code_s)
        pass
    return kessan_upd


_UPD_REASON_NONE = 0  # 更新不要
_UPD_REASON_TTL = 1  # TTL超過による更新
_UPD_REASON_KESSAN = 2  # 決算発表による更新
_UPD_REASON_NO_DATA = 3  # データなし


def has_active_dbdata(stocks, code_s, access_key, interval_day, latest):
    """DB上のデータ鮮度を確認する。
    Returns:
        tuple(bool, int): (データがあるか, 更新理由)
        更新理由は _UPD_REASON_* 定数
    """
    if code_s in stocks:
        # 対象(業績など)データアクセス時間と現在時間を比較
        if access_key in stocks[code_s]:
            dt_access = stocks[code_s][access_key]
            if latest:
                # 決算日超過のチェック
                kessan_upd = need_kessan_upd(stocks, code_s, dt_access)
                # アクセス日超過または決算更新
                timedelta = datetime.today() - dt_access
                if kessan_upd:
                    log_print("%s決算更新: %d日ぶり" % (access_key, timedelta.days))
                    return False, _UPD_REASON_KESSAN
                if timedelta.days >= interval_day:
                    log_print("%s更新: %d日ぶり" % (access_key, timedelta.days))
                    return False, _UPD_REASON_TTL
                else:
                    # print "業績あり: %d日前"%timedelta.days
                    return True, _UPD_REASON_NONE
            else:
                return True, _UPD_REASON_NONE
    return False, _UPD_REASON_NO_DATA


def has_gyoseki_data(stocks, code_s, latest=False):
    """DBに業績情報があるか
    15日経過するか、決算日をすぎている
    @param	latest
    Returns:
        tuple(bool, int): (データがあるか, 更新理由)
    """
    INTERVAL_DAY = 15
    return has_active_dbdata(
        stocks, code_s, "access_date_gyoseki", INTERVAL_DAY, latest
    )


def get_gyoseki_data(stocks, code_s, upd=UPD_INTERVAL):
    """
    業績データを通信またはキャッシュから取得する
    -> dict
    """
    if code_s in stocks:
        if "access_date_gyoseki" in stocks[code_s] and upd < UPD_INTERVAL:
            log_debug("DBから業績情報を取得します")
            return stocks[code_s]
    gyoseki_data = gyoseki.get_gyoseki_data(code_s, upd)
    return gyoseki_data


def has_rironkabuka_data(stocks, code_s, latest=False):
    """理論株価データがあるか？
    latest: Trueなら最新であるかを調査(一定期間アクセスがあったか)
    Returns:
        tuple(bool, int): (データがあるか, 更新理由)
    """
    INTERVAL_DAY = 15
    return has_active_dbdata(
        stocks, code_s, "access_date_rironkabuka", INTERVAL_DAY, latest
    )


def get_rironkabuka_data(stocks, code_s, upd=UPD_INTERVAL):
    """
    理論株価データを通信またはキャッシュから取得
    Returns:
        dict<key, value>: 更新するDBデータ
    """
    # code = int(code)
    if code_s in stocks:
        if "access_date_rironkabuka" in stocks[code_s] and upd < UPD_INTERVAL:
            log_debug("DBから理論株価情報を取得します")
            return stocks[code_s]
    stock = stocks[code_s] if code_s in stocks else None
    data = rironkabuka.get_rironkabuka_data(code_s, upd, stock)
    return data


def has_shihyo_data(stocks, code_s, latest=False):
    """指標データがあるか？
    Returns:
        tuple(bool, int): (データがあるか, 更新理由)
    """
    INTERVAL_DAY = 5
    return has_active_dbdata(
        stocks, code_s, "access_date_shihyo", INTERVAL_DAY, latest
    )


def get_shihyo_data(stocks, code_s, upd=UPD_INTERVAL):
    """指標データを通信またはキャッシュから取得
    Returns:
        dict<key, value>: 更新するDBデータ内容
    """
    latest = upd >= UPD_INTERVAL
    if code_s in stocks:
        if "access_date_shihyo" in stocks[code_s] and not latest:
            log_debug("DBから指標情報を取得します")
            return stocks[code_s]
    # 指標更新
    data = shihyou.get_shihyo_data(stocks, code_s, upd)
    return data


# ==================================================
# database
# ==================================================


def update_db(stocks, stock_data):
    """
    stocksのDBデータをstock_dataのcodeキーで更新する
    Args:
        stocks: 銘柄DB本体（dict または ShelveDB）
        stock_data(dict<key, value>): 更新したいdict
    """
    # 更新
    if "code_s" not in stock_data:
        if "code" not in stock_data:
            log_print("追加するレコードはcode_sキーを持たせてください")
            return
        else:
            code = stock_data["code"]
            stock_data["code_s"] = str(code)
            log_print("intコードをstrに変換:", code)
    code_s = stock_data["code_s"]
    # レコードにカラムをキーから抜き出し、stock_data要素を追加
    try:
        stock = stocks[code_s]
        if stock is None:
            stock = {}
    except KeyError:
        stock = {}
        log_print(str(code_s) + "は新規DB銘柄")
    # スクレイピング失敗時に空データで既存値を上書きしないよう保護するキー
    _PROTECTED_DICT_KEYS = {"shihyo"}
    _PROTECTED_LIST_KEYS = {
        "gyoseki_current", "gyoseki_quarter",
        "stddev_volatility", "sell_pressure_ratio", "sell_pressure_ratio_w",
        "price_log", "price_week_log",
    }
    # 0値での上書きを防止するキー（計算失敗時に0が返される）
    _PROTECTED_ZERO_KEYS = {
        "rironkabuka", "rironkabuka_up", "rironkabuka_down", "rironkabuka_preceding",
    }
    for k in list(stock_data.keys()):
        new_val = stock_data[k]
        if k.startswith("access_date_") and new_val is None:
            # スクレイピング失敗時: access_dateを削除して次回再取得を促す
            if k in stock:
                del stock[k]
                log_debug("%sを削除しました（次回再取得）" % k)
            continue
        if k in _PROTECTED_DICT_KEYS:
            # dictはキー単位でマージし、空dictでの上書きを防止
            if new_val:
                existing = stock.get(k, {})
                existing.update(new_val)
                stock[k] = existing
            elif k not in stock:
                # 新規銘柄では空dictでもキーを初期化（下流で KeyError を防ぐ）
                stock[k] = {}
            else:
                log_debug("%sが空のため既存データを保持します" % k)
        elif k in _PROTECTED_LIST_KEYS:
            # listは空リストでの上書きを防止
            if new_val:
                stock[k] = new_val
            elif k not in stock:
                stock[k] = []
            else:
                log_debug("%sが空のため既存データを保持します" % k)
        elif k in _PROTECTED_ZERO_KEYS:
            # 0値での上書きを防止（計算失敗時に既存値を保持）
            if new_val:
                stock[k] = new_val
            elif k in stock:
                log_debug("%sが0のため既存データを保持します" % k)
            else:
                stock[k] = new_val
        else:
            stock[k] = new_val
    log_debug("DB更新しました: ", code_s, list(stock_data.keys()))
    # 更新後のカラム表示
    print_dict(
        stock,
        ex_key=[
            "gyoseki_quarter",
            "gyoseki_current",
            "shihyo",
            "price_log",
            "rs_rank_log",
            "stock_rank_log",
        ],
    )
    stocks[code_s] = stock


def update_db_rows(code_s_list, upd=UPD_INTERVAL, tables=None, sync=True):
    """code_listで指定された銘柄のDB更新し、DB全体を返す
    Params:
        code_list: list<int>
        latest: bool 強制で最新データに更新する
        tables: list<str> 更新するテーブルを指定する[master/price/gyoseki/rironkabuka]
    Return:
        更新されたDB（dict形式でエクスポート）
    """
    if tables is None:
        tables = []
    latest = upd >= UPD_INTERVAL
    force = upd >= UPD_REEVAL
    log_print("update_tables:", tables, " 更新:", upd, "同期" if sync else "非同期")

    with _get_stock_shelve_db() as stocks_db:
        # yfinanceバッチプリフェッチ（price更新対象がある場合、DB参照で市場コード解決）
        if (not tables or "price" in tables) and code_s_list:
            try:
                price.prefetch_yfinance_batch(code_s_list, stocks=stocks_db)
            except Exception as e:
                log_warning("yfinanceバッチプリフェッチ失敗（個別取得にフォールバック）: %s" % e)
            try:
                price.prefetch_yfinance_weekly_batch(code_s_list, stocks=stocks_db)
            except Exception as e:
                log_warning("yfinance週足バッチプリフェッチ失敗（個別取得にフォールバック）: %s" % e)
        if sync:
            update_db_rows_sync(code_s_list, upd, tables, stocks_db, latest, force)
        else:
            update_db_rows_async(code_s_list, upd, tables, stocks_db, latest, force)
        stocks_db.sync()
        return stocks_db.export_to_dict()


def _sync_research_stock_name(code_s, *, new_name):
    """research_shelve の stock_name を排他更新する。

    research_shelve.sync_stock_name に委譲し、lost update リスクを
    research_shelve 側の flock 区間内に閉じ込める (issue #183)。
    例外は warning に握る — stock_name 同期失敗で銘柄更新全体を止めない。
    """
    import research_shelve
    try:
        returned_old = research_shelve.sync_stock_name(code_s, new_name)
    except Exception as e:
        log_warning("[stock_name_sync] %s: 同期失敗: %s" % (code_s, e))
        return
    if returned_old:
        log_print(
            "[stock_name_sync] %s: %s → %s (research_shelve 旧名退避)"
            % (code_s, returned_old, new_name)
        )


def _update_db_code(c_s, upd, tables, stocks, latest, force):
    """同期非同期共通のDB更新関数"""
    stock_data = {}
    if not tables or "master" in tables:
        if not has_stock_data(stocks, c_s, latest) or force:
            # 旧名を取得 (master更新で上書きされる前の値)
            try:
                old_name = (stocks[c_s] or {}).get("stock_name", "") if c_s in stocks else ""
            except (KeyError, TypeError):
                old_name = ""
            master_data = get_stock_master_data(stocks, c_s, upd)
            stock_data.update(master_data)
            new_name = (master_data or {}).get("stock_name", "")
            if (
                isinstance(new_name, str) and isinstance(old_name, str)
                and new_name.strip() and old_name.strip()
                and new_name.strip() != old_name.strip()
            ):
                _sync_research_stock_name(c_s, new_name=new_name.strip())
    if not tables or "price" in tables:
        if not has_price_data(stocks, c_s, latest) or force:
            stock_data.update(get_price_data(stocks, c_s, upd))
    if not tables or "gyoseki" in tables:
        has_data, reason = has_gyoseki_data(stocks, c_s, latest)
        if not has_data or force:
            # 決算更新時はファイルキャッシュも無視して最新を取得
            effective_upd = UPD_FORCE if reason == _UPD_REASON_KESSAN else upd
            stock_data.update(get_gyoseki_data(stocks, c_s, effective_upd))
    if not tables or "rironkabuka" in tables:
        has_data, reason = has_rironkabuka_data(stocks, c_s, latest)
        if not has_data or force:
            effective_upd = UPD_FORCE if reason == _UPD_REASON_KESSAN else upd
            stock_data.update(get_rironkabuka_data(stocks, c_s, effective_upd))
    if not tables or "shihyo" in tables:
        has_data, reason = has_shihyo_data(stocks, c_s, latest)
        if not has_data or force:
            effective_upd = UPD_FORCE if reason == _UPD_REASON_KESSAN else upd
            stock_data.update(get_shihyo_data(stocks, c_s, effective_upd))
    return stock_data


def update_db_rows_async(code_s_list, upd, tables, stocks, latest, force):
    """非同期版update_db_rows。
    (issue #43): ワーカー内で個別 Session を持たせてスレッドセーフ性を確保する。
    旧 use_requests_global_session() は requests.Session の Cookie Jar / コネクション
    プールを複数スレッドで共有してレースコンディションを起こすため使用しない。
    """
    from concurrent.futures import ThreadPoolExecutor

    MAX_WORKERS = 5  # スレッドワーカー数

    def _worker(c_s):
        # ThreadPoolExecutor の各ワーカーは別 ContextVar スコープなので、
        # ここで use_requests_session() を呼ぶとスレッドごとに独立した Session が
        # 生成され、http_get_html 経由で利用される。
        with use_requests_session():
            return _update_db_code(c_s, upd, tables, stocks, latest, force)

    # 並列通信実行
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # listで囲むことで結果待ち
        results = list(executor.map(_worker, code_s_list))
        # 結果をDBに反映
        for stock_data in results:
            if stock_data:
                update_db(stocks, stock_data)


def update_db_rows_sync(code_s_list, upd, tables, stocks, latest, force):
    with use_requests_session():
        for c in code_s_list:
            stock_data = _update_db_code(c, upd, tables, stocks, latest, force)
            if stock_data:
                update_db(stocks, stock_data)


def get_stock_db(code):
    """
    指定codeの銘柄DBデータを返す
    """
    with _get_stock_shelve_db() as db:
        return db.get(str(code), {})


def persist_stock_fields(stock_data_list):
    """指定銘柄の任意フィールドを shelve へ直接永続化する。

    update_db_rows の戻り値(export_to_dict した通常 dict)に update_db を
    かけてもメモリ上のコピーが変わるだけで shelve に保存されないため、
    永続化が必要な軽量フィールド更新はこの関数を経由する。

    Args:
        stock_data_list: list<dict> 各要素は code_s と更新カラムを持つ
    """
    if not stock_data_list:
        return
    with _get_stock_shelve_db() as db:
        for stock_data in stock_data_list:
            update_db(db, stock_data)
        db.sync()


@contextmanager
def print_to():
    output = io.StringIO()
    sys.stdout = output
    yield output
    sys.stdout = sys.__stdout__


@contextmanager
def print_to_file(fname):
    output = open(fname, "w")
    sys.stdout = output
    yield output
    output.close()
    sys.stdout = sys.__stdout__


def list_db(code_list=[]):
    with _get_stock_shelve_db() as stocks:
        _list_db_impl(stocks, code_list)


def _list_db_impl(stocks, code_list):
    """list_dbの実装"""
    code_s_list = [str(c) for c in code_list]
    with print_to() as out:
        for k in stocks.keys():
            if not code_s_list or k in code_s_list:
                v = stocks[k]
                log_print("[%s]" % k)
                print_dict(v, ex_key=["shihyo", "gyoseki_current", "gyoseki_quarter"])
    log_print(out.getvalue())


# ==================================================
# 項目カスタマイズ表示
# ==================================================


def get_trend_template_expr(stock):
    """
    銘柄DBデータから、トレンドテンプレートを返す
    """
    if "trend_template" not in stock:
        return "-"
    miss_count = len(stock["trend_template"])
    if miss_count == 0:
        return "◎"
    if miss_count <= 2:
        return "◯" + ",".join(stock["trend_template"])
    if miss_count <= 4:
        return "▲"
    if miss_count <= 6:
        return "△"
    return "×"  # 7件全 miss = 完全 Stage 4 崩壊 (trend_symbol_from_misses と整合)


def get_index_trend_template_expr(stock):
    """指数向けトレンドテンプレート簡略表記。

    個別銘柄向け get_trend_template_expr と異なり、
    通過率を分数で示し詳細はホバー (title属性) に逃がす。

    Returns:
        tuple: (display_str, miss_str) — display は "◎ 7/7" 等、
               miss_str は不通過項目をカンマ区切り (空なら "")
    """
    if "trend_template" not in stock:
        return ("-", "")
    misses = stock["trend_template"]
    miss_count = len(misses)
    pass_count = 7 - miss_count
    miss_str = ",".join(misses) if misses else ""
    if miss_count == 0:
        return ("◎ %d/7" % pass_count, miss_str)
    if miss_count <= 2:
        return ("◯ %d/7" % pass_count, miss_str)
    if miss_count <= 4:
        return ("▲ %d/7" % pass_count, miss_str)
    return ("△ %d/7" % pass_count, miss_str)


# trend_template (Minervini) の全条件名。calc_trend_template (price.py) が
# 不通過項目を misses として返すため、misses がこの全項目を含む = 1つも条件を
# 満たさない = 完全な Stage 4 崩壊、と判定する (issue #110/#111)。
# 件数は ks_util.TREND_TEMPLATE_CONDITION_COUNT (× 記号判定) と一致させる。
_TREND_TEMPLATE_ALL = {
    "pr>ma10", "pr>ma30,40", "ma30>ma40", "ma40Up",
    "ma10>ma30,40", "high(low)52", "RS",
}
assert len(_TREND_TEMPLATE_ALL) == TREND_TEMPLATE_CONDITION_COUNT


def _is_stage4(misses):
    """trend_template の不通過項目集合が「全条件 miss」= Stage 4 崩壊か判定する。

    部分的な不通過 (下落途中・Stage 1 底値圏など) は対象外。1つも条件を満たさない
    完全崩壊銘柄のみ True。ポケットピポット・ブレイク双方で同じ基準を使う (issue #110/#111)。
    """
    return _TREND_TEMPLATE_ALL <= misses


# シグナル表示で銘柄データ自体を stale とみなす上限 (access_date_price が今日からこの
# 日数を超えて古い銘柄はシグナルを出さない)。他タグ (新高値/押し) の 30 日と揃える。
_SIGNAL_STALE_DAYS = 30


def _signal_recent_delta(stock, mmdd):
    """価格更新日 (access_date_price) を基準にシグナル発生日の経過日数を返す。

    make_signal の tags 付与と extract_signals が同じ日付基準を使うための共通関数。
    経過日数は anchor_day (= access_date_price を get_price_day() で anchor 化した日)
    基準で算出する。calendar today 基準だと、金曜更新の銘柄を翌週末に見たとき
    access_date_price 当日のシグナルでも 8 日前扱いで落ちるため、価格更新が止まった
    銘柄・週末表示でも最新シグナルが残るよう anchor 基準に揃える。

    ただし anchor 基準だけだと「数年前に更新停止した銘柄の当時のシグナル」も delta=0
    で復活してしまうため、銘柄データ自体の鮮度 (今日 - anchor_day) が _SIGNAL_STALE_DAYS
    を超える銘柄は stale として除外する。これにより週末・数日停止は救いつつ古い銘柄は弾く。

    Returns:
        (int, date) | (None, None): (anchor_day からの経過日数, 年補完済みの発生日)。
        access_date_price 無し・銘柄データが stale なら (None, None)。
        ValueError は mmdd 不正時に送出 (呼び出し側でログ)。
    """
    access_date_price = stock.get("access_date_price")
    if not access_date_price:
        return None, None
    anchor_day = get_price_day(access_date_price)
    # 銘柄データ自体が古すぎる (更新停止) 場合はシグナルを出さない。
    if (datetime.today().date() - anchor_day).days > _SIGNAL_STALE_DAYS:
        return None, None
    sig_day = datetime.strptime(
        "%d/%s" % (anchor_day.year, mmdd), "%Y/%m/%d"
    ).date()
    if sig_day > anchor_day:
        sig_day = sig_day.replace(year=anchor_day.year - 1)
    delta = (anchor_day - sig_day).days
    # 発生日が anchor より極端に古い (年補完しても 1 年超) シグナルも除外。
    if delta > 366:
        return None, None
    return delta, sig_day


def extract_signals(stock, max_delta_days=10, include_extended=False):
    """表示対象ポ/ブシグナルを返す (issue #253/#310)。

    一覧 tooltip/背景色はデフォルトの 10 日以内だけを使い、詳細チャートは
    max_delta_days=None で make_signal と同じ元シグナルをチャート窓内に描ける。

    フィルタ:
      - ポ: trend_template に Stage4 崩壊系が含まれれば全除外、先頭最大3件走査。
      - ブ: 先頭1件のみ走査。
      - 各シグナル: access_date_price 基準で delta を計算し、
        max_delta_days が整数なら 0〜max_delta_days のものだけ採用。

    include_extended=True のときのみ breakout_extended (高値追い圏で正規ブレイクから
    弾かれた候補) を kind="ブ"・extended=True 付きで含める。詳細チャートマーカー専用で、
    シグナル列・一覧 tooltip 経路 (デフォルト False) には出さない。

    Returns:
        list[dict]: [{"kind","mmdd","num","sig_date","delta"}] (表示順)。
            extended 候補のみ "extended": True を持ち、num は MA10乖離% (正規ブレイクの
            num=出来高超過% とは意味が異なる)。
            sig_date は access_date_price 基準で年補完した発生日 (date)。
    """
    out = []
    pocket_pivot = stock.get("pocket_pivot", "")
    trend_template = stock.get("trend_template", [])
    misses = set(trend_template) if isinstance(trend_template, (list, tuple, set)) else set()
    # Stage 4 崩壊 (7条件全 miss) ではポ/ブともに無効化 (issue #110/#111)。
    stage4 = _is_stage4(misses)

    # (kind, sigs, extended) の3要素。extended は描画側で半透明中抜き表示する目印。
    sources = []
    if pocket_pivot and not stage4:
        sources.append(("ポ", list(pocket_pivot)[:3], False))  # 連続ポは最大3件
    breakout = stock.get("breakout", [])
    if breakout and not stage4:
        sources.append(("ブ", list(breakout)[:1], False))  # ブは最新1件のみ
    if include_extended and not stage4:
        breakout_ext = stock.get("breakout_extended", [])
        if breakout_ext:
            sources.append(("ブ", list(breakout_ext)[:3], True))  # extended は直近3件まで

    for kind, sigs, extended in sources:
        for sig in sigs:
            spl = str(sig).split(",")
            if len(spl) < 2:
                continue
            try:
                num = int(spl[1])
            except ValueError:
                continue
            try:
                delta, sig_date = _signal_recent_delta(stock, spl[0])
            except ValueError:
                log_warning("シグナル日付エラー", spl[0])
                continue
            if delta is None or delta < 0:
                continue
            if max_delta_days is not None and delta > max_delta_days:
                continue
            entry = {
                "kind": kind, "mmdd": spl[0], "num": num,
                "sig_date": sig_date, "delta": delta,
            }
            if extended:
                entry["extended"] = True
            out.append(entry)
    return out


def make_signal(stock, market_db=None, topix_map=None, rs_line=None):
    """銘柄DBデータから、シグナル情報を作成する。

    market_db を渡すと rs_line ベースのタグ (強乖 / 弱乖) も付与される。
    後方互換のため market_db=None ならスキップ。
    rs_line を渡せば再計算をスキップする。
    """
    today = datetime.today()
    signal = ""
    tags = []

    def get_recent_signal_delta(mmdd):
        """価格更新日に紐づく直近シグナルだけ日数差を返す (共通関数に委譲)。"""
        delta, _ = _signal_recent_delta(stock, mmdd)
        return delta

    # 新高値
    new_high = stock.get("new_high", "")
    if new_high:
        if "access_date_price" in stock:
            dt = stock.get("access_date_price", "")
            dt = get_price_day(dt)
            if (date.today() - dt).days <= 30:
                tags.append("".join(new_high))
    # 株探リスト掲載（本日のみ）
    kabutan_origin = stock.get("kabutan_origin", "")
    if kabutan_origin and stock.get("kabutan_origin_date"):
        dt = get_price_day(stock.get("kabutan_origin_date"))
        if (date.today() - dt).days <= 1:
            if "高" in kabutan_origin:
                tags.append("高")
            if "出" in kabutan_origin:
                tags.append("出")
            if "P" in kabutan_origin:
                tags.append("P")
    # 20MA押し
    pb20 = stock.get("pullback_20", "")
    if pb20:
        if "access_date_price" in stock:
            dt = stock.get("access_date_price", "")
            dt = get_price_day(dt)
            if (date.today() - dt).days <= 30:
                tags.append("押")
    # Stage 4 崩壊銘柄ではポケットピポット/ブレイクを無効化する (issue #110/#111)。
    # trend_template (週足) の7条件を1つも満たさない (全 miss) 場合のみ崩壊とみなす。
    # 部分的な不通過 (下落途中・Stage 1 底値圏) は対象外。
    # trend_template が空(◎)・キー欠落(週足取得失敗)ならベース形成中とみなし除外しない。
    trend_template = stock.get("trend_template", [])
    misses = set(trend_template) if isinstance(trend_template, (list, tuple, set)) else set()
    stage4 = _is_stage4(misses)
    # ポケットピポット
    pocket_pivot = stock.get("pocket_pivot", "")
    if pocket_pivot and not stage4:
        for i, sig in enumerate(pocket_pivot):
            if i >= 3:  # 連続ポケットピポットは最大3件まで表示 (issue #110)
                break
            spl = sig.split(",")
            try:
                delta_day = get_recent_signal_delta(spl[0])
                # mark = "★"  if delta_day < 3 else ""
            except ValueError:
                log_warning("ポケットピポット日付エラー", spl[0])
            if i == 0:
                signal += "\n[ポ]"
            signal += "%s(%s)," % (spl[0], spl[1])
    # ブレイクアウト
    breakout = stock.get("breakout", [])
    if not stage4:
        for brk in breakout:
            brkspl = brk.split(",")
            try:
                delta_day = get_recent_signal_delta(brkspl[0])
                # mark = "★"  if delta_day < 3 else ""
            except ValueError:
                log_warning("ブレイクアウト日付エラー", brkspl[0])
            signal += "[ブ]"
            signal += "%s(%s)," % (brkspl[0], brkspl[1])
            break  # 一つにしておく(最新日)
    # 売り圧力レシオ(5日)による買われ過ぎ売われすぎ
    sell_ratio = stock.get("sell_pressure_ratio", [])
    if sell_ratio:
        sell_ratio_5 = sell_ratio[1]
        if sell_ratio_5 >= 75:
            signal += "\n[買過]"
        elif sell_ratio_5 <= 25:
            signal += "\n[売過]"
    signal = signal.strip()

    # 売りシグナル
    # 50DMAを下回っていて、売り圧力レシオが45以下で、RSが70以上
    # つまり今まで上がっていたものが弱くなっている
    # rs_rank = stock.get("momentum_pt", 0)
    rs_raw = stock.get("rs_raw", 0)
    sell_ratio = stock.get("sell_pressure_ratio", [])
    if rs_raw >= 1.2:
        if sell_ratio:
            sell_ratio_20 = sell_ratio[0]
            warn = 0
            if sell_ratio_20 < 45:
                warn += 1
            kairi_wma10 = stock.get("price_kairi_wma10", 0)
            if kairi_wma10 < 0:
                warn += 1
            if warn >= 2:
                tags.append("売")
            elif warn == 1:
                tags.append("警")
    # 30日連続10日線上で一度利確基準に達した後、10日線割れに入った銘柄は早売タグ。
    kairi_ma10 = stock.get("price_kairi_ma10")
    if bool(stock.get("ma10_above_streak_30")) and isinstance(kairi_ma10, (int, float)) and kairi_ma10 < 0:
        tags.append("早売")

    # rs_line 新高値・ダイバージェンス（当日発生のみ）
    # list_all_db は更新対象外の銘柄もCSVに出すため、price_log が数日〜数週間古い
    # 銘柄が混じる。rs_line[0] が当日 (= 最新営業日 = TOPIX price_log[0]の日付) で
    # ある場合だけタグを立てる。古いキャッシュで連日タグが残るのを防ぐため。
    if market_db is not None:
        if rs_line is None:
            rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
        topix_log = market_db.get("topix", {}).get("price_log", [])
        latest_date = topix_log[0][0] if topix_log else None
        if rs_line and latest_date and rs_line[0][0] == latest_date:
            div = compute_rs_line_divergence(stock, market_db, rs_line=rs_line)
            if div == "bullish":
                tags.append("強乖")
            elif div == "bearish":
                tags.append("弱乖")

    # print signal, tags
    return signal, tags


def get_code_exp(code_s):
    code_s = str(code_s)
    KABUTAN_URL = "https://kabutan.jp/stock/chart?code=%s"
    return '=HYPERLINK("%s","%s")' % (KABUTAN_URL % code_s, code_s)


def get_stock_name_exp(stock):
    """
    銘柄名の表示用表現を返す
    """
    stock_name = stock.get("stock_name", "Unknown")
    corpo_url = stock.get("corporate_url", "")
    if corpo_url:
        stock_name = '=HYPERLINK("%s", "%s")' % (corpo_url, stock_name)
    return stock_name


def get_access_dates_expr(stock_data):
    """更新日表現を取得
    Args:
        stock_data (dict): 銘柄DBデータ
    Returns:
        str: 更新日文字列 "month/day|day|day".
    """
    month = None
    date = ""
    if "access_date_gyoseki" in stock_data:
        dt = stock_data["access_date_gyoseki"]
        date = "%s/%s" % (dt.month, dt.day)
        month = dt.month
    date_sh = ""
    if "access_date_shihyo" in stock_data:
        dt = stock_data["access_date_shihyo"]
        if month and month == dt.month:
            date_sh = dt.day
        else:
            date_sh = "%s/%s" % (dt.month, dt.day)
            month = dt.month
    date_pr = ""
    if "access_date_price" in stock_data:
        dt = stock_data["access_date_price"]
        dt = get_price_day(dt)
        if month and month == dt.month:
            date_pr = dt.day
        else:
            date_pr = "%s/%s" % (dt.month, dt.day)
    # TODO: 理論株価も必要なら
    date_exp = "%s|%s|%s" % (date, date_sh, date_pr)
    return date_exp


def get_vola_and_sell_press_expr(stock_data):
    try:
        # ボラティリティ
        vola = ",".join([str(int(v)) for v in stock_data.get("stddev_volatility", [])])
        # 売り圧力レシオと買い集め指数
        sprs = stock_data.get("sell_pressure_ratio", [])
        sprs_w = stock_data.get("sell_pressure_ratio_w", [])
        sell_press = price.get_spr_expr(sprs, sprs_w)
        # 50DMA(10WMA)との乖離率
        kairi_wma10 = stock_data.get("price_kairi_wma10", 0)
        sell_press += ", %+d" % (kairi_wma10)

    except TypeError:
        vola = ""
        sell_press = ""
    return vola, sell_press


def get_signal_tags_prevrank_expr(stock_data, market_db=None, topix_map=None, rs_line=None):
    tags = []  # タグ
    signal, tags = make_signal(stock_data, market_db=market_db,
                               topix_map=topix_map, rs_line=rs_line)

    # ---- 過去順位と株価上昇率
    try:
        rank0 = get_rank_log(stock_data, "stock_rank_log", 0)
        rank1 = get_rank_log(stock_data, "stock_rank_log", 1)
        rank5 = get_rank_log(stock_data, "stock_rank_log", 5)
        price_log = stock_data.get("price_log", [])
        pr0 = price.get_price_log(price_log, rank0[0])
        pr1 = price.get_price_log(price_log, rank1[0])
        pr5 = price.get_price_log(price_log, rank5[0])
        ratio1 = "%+d" % (100 * pr0 / pr1 - 100) if (pr0 != 0 and pr1 != 0) else ""
        ratio5 = "%+d" % (100 * pr0 / pr5 - 100) if (pr0 != 0 and pr5 != 0) else ""

        def get_arrow(v):
            if v == 0:
                return ""
            else:
                return "↑" if v > 0 else "↓"

        rank1_0 = rank1[1] - rank0[1]
        rank5_0 = rank5[1] - rank0[1]
        rank1_s = "%s%d" % (get_arrow(rank1_0), abs(rank1_0))
        rank5_s = "%s%d" % (get_arrow(rank5_0), abs(rank5_0))
        prev_rank = "%s(%s)|%s(%s)" % (rank1_s, ratio1, rank5_s, ratio5)

        # 急上昇をタグに入れる急
        if rank1_0 > rank1[1] * 0.30:
            tags.append("急")
        elif rank5_0 > rank5[1] * 0.30:
            tags.append("昇")
    except IndexError:
        prev_rank = ""

    tags = "/".join(tags)
    return signal, tags, prev_rank


# ==================================================
# モメンタムポイント手動キャリブレーション (issue #104)
# ==================================================
# 運用方針: 自動 (週次) 実行は採用しない。
# loc/scale が頻繁に動くと、同じ rs_raw でも momentum_pt が変わって code_rank が
# 揺れるため、基準ぶれによる評価の不安定化を避ける。相場局面が大きく変わったとき
# や Phase 2 切替時の再校正は、`python make_stock_db.py calibrate_momentum` を
# 手動実行する。

# 直近 N 日以内に rs_raw が更新された銘柄のみキャリブレーション対象とする。
# 短すぎるとサンプル不足、長すぎると相場環境の変化に追従できない。
MOMENTUM_CALIB_N_DAYS = 14
# 統計的に意味のある loc/scale を出すための最小サンプル数。
# 現状DBは 3,800 銘柄程度。500未満になる場合はデータ取得が壊れているサインとして扱う。
MOMENTUM_CALIB_MIN_SAMPLES = 500


def calibrate_momentum_pt(stocks=None, market_db=None, save=True):
    """モメンタムポイントの分布パラメータ (loc/scale) を実測してmarket_dbに保存する。

    log(rs_rel) = log(銘柄rs_raw / TOPIX rs_raw) の平均と標準偏差を、
    直近 MOMENTUM_CALIB_N_DAYS 日以内に rs_raw が更新された銘柄から実測する。

    Args:
        stocks (dict): 銘柄DB (code_s -> stock_data)。Noneならload_stock_db()。
        market_db (dict): マーケットDB。Noneならget_market_db()。
        save (bool): 結果をmarket_db['momentum_calib']に保存するか。

    Returns:
        dict | None: キャリブレーション結果 (loc/scale/sample_count/updated_at/n_days)。
                     最小サンプル数を満たさない場合はNone。
    """
    import math
    import statistics

    if stocks is None:
        stocks = load_stock_db()
    if market_db is None:
        market_db = make_market_db.get_market_db()

    topix = market_db.get("topix", {})
    topix_rs_raw = topix.get("rs_raw", 0)
    if not topix_rs_raw or topix_rs_raw <= 0:
        log_warning(
            "[momentum_calib] TOPIX rs_raw が取得できないためキャリブレーション中止"
        )
        return None

    # 直近 N 日の閾値 (rs_raw は週次価格更新時に書き込まれるため access_date_price をプロキシとする)
    today_dt = get_price_day(datetime.today())
    threshold = today_dt - timedelta(days=MOMENTUM_CALIB_N_DAYS)

    log_rels = []
    for stock in stocks.values():
        rs_raw = stock.get("rs_raw", 0)
        if not rs_raw or rs_raw <= 0:
            continue
        access_date = stock.get("access_date_price")
        if access_date is None:
            continue
        if get_price_day(access_date) < threshold:
            continue  # 古いrs_rawは除外
        rs_rel = rs_raw / topix_rs_raw
        if rs_rel <= 0:
            continue
        log_rels.append(math.log(rs_rel))

    sample_count = len(log_rels)
    log_print(
        "[momentum_calib] 対象銘柄: %d (直近%d日以内, TOPIX rs_raw=%.4f)"
        % (sample_count, MOMENTUM_CALIB_N_DAYS, topix_rs_raw)
    )

    if sample_count < MOMENTUM_CALIB_MIN_SAMPLES:
        log_warning(
            "[momentum_calib] サンプル数 %d < 最小要件 %d のためキャリブレーションを保存しません"
            % (sample_count, MOMENTUM_CALIB_MIN_SAMPLES)
        )
        return None

    loc_raw = statistics.mean(log_rels)
    scale_raw = statistics.stdev(log_rels)
    # scale 調整方針: 「実測値そのまま」 (issue #104 要件 §4)。
    # 将来 倍率調整 / 下限保証 に切り替える場合はここを差し替える。
    scale_final = _adjust_momentum_scale(scale_raw)

    calib = {
        "loc": loc_raw,
        "scale": scale_final,
        "sample_count": sample_count,
        "updated_at": datetime.now(),
        "n_days": MOMENTUM_CALIB_N_DAYS,
    }
    log_print(
        "[momentum_calib] loc=%.4f, scale=%.4f, n=%d"
        % (calib["loc"], calib["scale"], calib["sample_count"])
    )

    if save:
        market_db["momentum_calib"] = calib
        make_market_db._save_market_db(market_db)
        log_print("[momentum_calib] market_db['momentum_calib'] に保存しました")
    return calib


def _adjust_momentum_scale(scale_raw):
    """scale 調整関数。
    Phase 1 では実測値そのまま。将来、倍率調整 (scale_raw * α) や
    下限保証 (max(scale_raw, 下限値)) に切り替える場合はここを差し替える。
    """
    return scale_raw


# ==================================================
# DB一覧表示
# ==================================================

def compute_total_pt(gyoseki_pt, shihyo_pt, mom_pt, funda_pt):
    """総合PT を計算する。list_all_db のランキングと webapp 詳細ページの両方から使う。

    重み: 業績 40, 指標 20, モメンタム 25, ファンダ 15。
    """
    return int((40 * gyoseki_pt + 20 * shihyo_pt + 25 * mom_pt + 15 * funda_pt) / 100)


# code_rank.csv のヘッダ。build_code_rank_row の dict キーもこのリストと一致させる。
CODE_RANK_HEADERS = [
    "ポートフォリオ",
    "タグ",
    "決算日",
    "順位",
    "過去順位(1日/5日前)",
    "コード",
    "銘柄名",
    "セクター",
    "総合PT",
    "プロフィット/クォリティ",
    "バリュー/サイズ",
    "モメンタム(現在.20日比/5日比)",
    "ファンダメンタル",
    "更新日(業績|指標|価格)",
    "シグナル",
    "トレンドテンプレート",
    "ローソク足ボラティリティ(20,5)",
    "売り圧力レシオ(20,5) 買い集め(週,日) 50DMA乖離率",
    "業績(今季/今四半期 売上/営利成長率)",
    "進捗率(現四半期/売上(前年)利益(前年)",
    "指標(時価総額|PER|EVR|ROE|売上高営業利益率|有利子負債自己負債比率|自己資本比率)",
    "理論株価(乖離率|上限,下限))",
    "過去業績(5年増収増益 4Q増収増益率)",
    "信用(倍率|出来高買残比)",
    "テーマ",
    "概要",
]


def build_code_rank_row(
    code_s,
    stock_data,
    *,
    total_pt,
    gyoseki_pt,
    shihyo_pt,
    mom_pt,
    funda_pt,
    rank,
    pf_stocks,
    possess_list,
    market_db,
    topix_map=None,
):
    """code_rank.csv の 1行を dict 形式で構築する。

    戻り値の dict キーは ``CODE_RANK_HEADERS`` と一致する。
    順位/コード/銘柄名はリンク装飾を含まないテキストのみ。CSV書き出し時は
    ``_decorate_links_for_csv()`` で HYPERLINK を被せる。webapp 等の
    read-only 用途ではテキストのまま使える。
    """
    # ---- 更新日・概要・テーマ・決算日
    date_exp = get_access_dates_expr(stock_data)
    overview = stock_data.get("overview", "")
    themes = stock_data.get("themes", "")
    main_theme = make_market_db.get_major_theme(themes)
    kessanbi = kessan.get_kessanbi_expr(stock_data)
    # ---- テクニカル
    trend = get_trend_template_expr(stock_data)
    vola, sell_press = get_vola_and_sell_press_expr(stock_data)
    # ---- ポートフォリオ
    ports = []
    if code_s in pf_stocks:
        ports.append("監")
    if code_s in possess_list:
        ports.append("保")
    ports = "".join(ports)
    # ---- rs_line を1回だけ計算して下流で使い回す
    rs_line = compute_rs_line(stock_data, market_db, topix_map=topix_map)
    # ---- タグ、シグナル、過去順位
    signal, tags, prev_rank = get_signal_tags_prevrank_expr(
        stock_data, market_db=market_db, topix_map=topix_map, rs_line=rs_line
    )
    # ---- 指標・業績・理論株価
    indicator_expr = shihyou.get_shihyo_expr(stock_data)
    credit_expr = shihyou.get_credit_expr(stock_data)
    progress_expr, growth_exp = gyoseki.get_gyoseki_expr(stock_data)
    rironkabuka_expr = rironkabuka.get_rironkabuka_expr(stock_data)
    gyoseki_quarity_expr = gyoseki.get_gyoseki_quarity_expr(stock_data)
    # ---- その他
    sector = stock_data.get("sector", "")
    rs_log = get_rs_line_changes_expr(stock_data, market_db, rs_line=rs_line)
    momentum = "%d.%s" % (mom_pt, rs_log)
    # CSV の get_stock_name_exp に合わせて "Unknown" フォールバック
    stock_name = stock_data.get("stock_name", "Unknown")

    return {
        "ポートフォリオ": ports,
        "タグ": tags,
        "決算日": kessanbi,
        "順位": str(rank),
        "過去順位(1日/5日前)": prev_rank,
        "コード": code_s,
        "銘柄名": stock_name,
        "セクター": sector,
        "総合PT": total_pt,
        "プロフィット/クォリティ": gyoseki_pt,
        "バリュー/サイズ": shihyo_pt,
        "モメンタム(現在.20日比/5日比)": momentum,
        "ファンダメンタル": funda_pt,
        "更新日(業績|指標|価格)": date_exp,
        "シグナル": signal,
        "トレンドテンプレート": trend,
        "ローソク足ボラティリティ(20,5)": vola,
        "売り圧力レシオ(20,5) 買い集め(週,日) 50DMA乖離率": sell_press,
        "業績(今季/今四半期 売上/営利成長率)": growth_exp,
        "進捗率(現四半期/売上(前年)利益(前年)": progress_expr,
        "指標(時価総額|PER|EVR|ROE|売上高営業利益率|有利子負債自己負債比率|自己資本比率)": indicator_expr,
        "理論株価(乖離率|上限,下限))": rironkabuka_expr,
        "過去業績(5年増収増益 4Q増収増益率)": gyoseki_quarity_expr,
        "信用(倍率|出来高買残比)": credit_expr,
        "テーマ": main_theme,
        "概要": overview,
    }


def _decorate_links_for_csv(code_s, row_dict, stock_data):
    """build_code_rank_row の dict を CSV 1行 (list) に変換し、HYPERLINK を被せる。

    順位/コード/銘柄名の3列に既存の HYPERLINK 装飾を適用する。
    """
    URL_YAHOO_QUOTE = "https://finance.yahoo.co.jp/quote/%s.%s"
    market_code = get_market_code(stock_data)
    yahoo_url = URL_YAHOO_QUOTE % (code_s, market_code)
    decorated = dict(row_dict)
    decorated["順位"] = '=HYPERLINK("%s", "%s")' % (yahoo_url, row_dict["順位"])
    decorated["コード"] = get_code_exp(code_s)
    decorated["銘柄名"] = get_stock_name_exp(stock_data)
    return [decorated[h] for h in CODE_RANK_HEADERS]


def list_all_db(upload_csv=True, update_portforio=True):
    """DB内銘柄のランキングリスト
    Args:
        update_portforil(bool): 100位以内とポートフォリオのDBデータを更新するかどうか
    """
    # マーケットの更新
    market_db = make_market_db.update_market_db()
    # 銘柄DBロード
    stocks = load_stock_db()
    stocks_active = []
    log_print("DB内銘柄数", len(stocks))
    # delete_stocks = []
    for k, v in stocks.items():
        try:
            gyoseki_pt = int(v["score_gyoseki"])
            shihyo_pt = v["shihyo_pt"]
            # mom_pt = int((v.get('rs_raw', 0) - 1) * 100)
            mom_pt = v.get("momentum_pt", 0)
            funda_pt = v.get("funda_pt", 0)
            total_pt = compute_total_pt(gyoseki_pt, shihyo_pt, mom_pt, funda_pt)
            stocks_active.append((k, total_pt, gyoseki_pt, shihyo_pt, mom_pt, funda_pt))
        except KeyError as e:
            log_print("必要キー%sなし" % e, k, v.get("stock_name", ""))
            # delete_stocks.append(k)
            continue
    # return

    # 自分のポートフォリオロード
    import portfolio

    pf_stocks, possess_list = portfolio.parse_my_portforio()
    log_print("ポートフォリオ:", pf_stocks + possess_list)

    # 総合PTでソート
    stocks_active = sorted(stocks_active, key=lambda stock: stock[1], reverse=True)
    # ---- 100位以内とポートフォリオのDB情報を更新
    if update_portforio:
        # テーマ銘柄を更新に入れる
        theme_codes_s = []
        theme_rank_list = market_db["theme_rank"]
        # theme_rank_list, _, _, _ = make_market_db.get_theme_rank_list()
        for j, theme in enumerate(theme_rank_list):
            current = len(theme_codes_s)
            for i, s in enumerate(stocks_active):
                stock = stocks.get(s[0], {})
                themes = stock.get("themes", "")
                if theme in themes and theme not in theme_codes_s:
                    if i / 100 + j < 20:  # 一定以上の重要度
                        theme_codes_s.append(s[0])
            log_print("テーマ:%sの銘柄%d個" % (theme, len(theme_codes_s) - current))
            if len(theme_codes_s) > 100:
                break
        update_codes_s = theme_codes_s
        # 100位以内
        update_codes_s += [s[0] for i, s in enumerate(stocks_active) if i < 100]
        # 俺ポートフォリオ追加
        update_codes_s += pf_stocks + possess_list
        update_codes_s = list(set(update_codes_s))  # 重複解消
        # TODO: ETFや投資法人を除外する
        # update_codes_s = update_codes_s[:2] # デバッグ用に数を減らす
        # マスター,価格,業績,指標,理論株価を更新
        stocks = update_db_rows(
            update_codes_s,
            upd=UPD_INTERVAL,
            tables=["master", "price", "shihyo", "gyoseki", "rironkabuka"],
            sync=False,
        )  # UPD_INTERVAL/UPD_REEVAL
        # 個別でやるとき(テスト用強制)
        # stocks = update_db_rows(update_codes_s, upd=UPD_FORCE, tables=["rironkabuka"])

    # ---- 各銘柄のランクデータを更新
    log_print("---- 各銘柄のランクデータ更新")
    for i, elem in enumerate(stocks_active):
        stock = stocks[elem[0]]
        rank = i + 1
        update_stock_rank(stock, rank)
    save_stock_db(stocks)  # 更新した順位のDB保存

    # ---- テーマ別株価騰落率を計算してmarket_dbに保存
    log_print("---- テーマ別株価騰落率を計算")
    theme_momentum = make_market_db.calc_theme_price_momentum(stocks)
    market_db["theme_momentum"] = theme_momentum
    make_market_db._save_market_db(market_db)

    # ---- 銘柄ランキング用CSVファイル作成
    log_print("---- CSV項目作成")
    rank_csv = os.path.join(DATA_DIR, "code_rank_data/code_rank.csv")

    if os.path.exists(rank_csv):
        latest_csv_dt = get_file_datetime(rank_csv)
        tdy = datetime.today()
        if (tdy - latest_csv_dt).days >= 7:
            backup_csv = os.path.join(
                DATA_DIR,
                "code_rank_data/code_rank_%02d%02d%02d.csv"
                % (latest_csv_dt.year % 2000, latest_csv_dt.month, latest_csv_dt.day),
            )
            log_print("バックアップ:", backup_csv)
            shutil.copy(rank_csv, backup_csv)
    # CSV用項目作成
    # 全銘柄ループの前に TOPIX 終値マップを1回だけ構築（rs_line 計算用）
    topix_map = _topix_close_map(market_db)
    rows = []
    rows.append(list(CODE_RANK_HEADERS))

    for i, stock in enumerate(stocks_active):
        stock_data = stocks[stock[0]]
        row_dict = build_code_rank_row(
            stock[0],
            stock_data,
            total_pt=stock[1],
            gyoseki_pt=stock[2],
            shihyo_pt=stock[3],
            mom_pt=stock[4],
            funda_pt=stock[5],
            rank=i + 1,
            pf_stocks=pf_stocks,
            possess_list=possess_list,
            market_db=market_db,
            topix_map=topix_map,
        )
        rows.append(_decorate_links_for_csv(stock[0], row_dict, stock_data))
    # CSV書き込み
    with open(rank_csv, "w", encoding="utf-8") as f:  # python3対応
        rank_csv_w = csv.writer(f)
        rank_csv_w.writerows(rows)

    # GoogleDriveにアップロード（非同期、ファイルロックでプロセス間排他制御）
    if upload_csv:
        import googledrive

        googledrive.upload_csv_async(rank_csv, "code_rank")

    # テーマ騰落率入りのmarket_data.csvを再生成
    make_market_db.create_market_csv()

    # モメンタムポイントキャリブレーションは list_all_db では自動実行しない
    # (issue #104)。基準ぶれによる code_rank の不安定化を避けるため、
    # 必要時に `python make_stock_db.py calibrate_momentum` を手動実行する。


def get_market_code(stock_data):
    """銘柄DBデータから、マーケットコードを取得
    Args:
        stock_data (dict): 銘柄DBデータ
    Returns:
        str: マーケットコード "T" (東証), "S" (札証), "N" (名証), "F" (福証)
    """
    if not stock_data:
        return "T"  # デフォルトは東証

    market_code = "T"
    market_name = stock_data.get("market", "T")  # デフォルトはT(東証)
    if market_name == "札証":
        market_code = "S"
    elif market_name == "名証":
        market_code = "N"
    elif market_name == "福証":
        market_code = "F"
    return market_code


def load_stock_db():
    """stockDBのロード
    全データをdictとしてエクスポート
    """
    with _get_stock_shelve_db() as db:
        return db.export_to_dict()


def save_stock_db(stocks):
    """stockDBの保存
    dictを全置換（削除も反映）
    """
    with _get_stock_shelve_db() as db:
        db.replace_from_dict(stocks)


def delete_db_column(stocks, column):
    for k, stock in stocks.items():
        if column in stock:
            del stock[column]
        # print_dict(stock)


STOCK_PICKLE_PATH = os.path.join(DATA_DIR, "stock_data", "stock_%s.pickle")


def load_cacehd_stock_db(code_s, force=False):
    """基本テスト用
    個別コードのpickleを別途保存したものをロードする
    (str, bool) -> dict
    """
    stock_path = STOCK_PICKLE_PATH % code_s
    if not os.path.exists(stock_path) or force:
        with _get_stock_shelve_db() as db:
            stock = db.get(code_s, None)
        save_pickle(stock_path, stock)
        return stock
    stock = load_pickle(stock_path)
    return stock


def edit_db():
    backup_db()
    stocks = load_stock_db()
    # delete_db_column(stocks, "access_data")
    delete_db_column(stocks, "sell_pressure_ratio_20")
    delete_db_column(stocks, "sell_pressure_ratio_10")
    delete_db_column(stocks, "sell_pressure_ratio_5")
    delete_db_column(stocks, "PER")
    delete_db_column(stocks, "PSR")
    delete_db_column(stocks, "PBR")
    delete_db_column(stocks, "ROE")
    delete_db_column(stocks, "profit_margin")
    delete_db_column(stocks, "capital_ratio")
    delete_db_column(stocks, "debt_ratio")
    save_stock_db(stocks)


def backup_db():
    """shelve DBの全ファイル(.dat, .dir, .bak)をバックアップ"""
    for ext in [".dat", ".dir", ".bak"]:
        fpath = STOCKS_SHELVE + ext
        if os.path.exists(fpath):
            backup_file(fpath, 0)


def delete_delist_stocks(stocks):
    """上場廃止銘柄を削除する）"""
    for code_s, stock in list(stocks.items()):
        if stock.get("price", 0) == 0:
            log_print(code_s, stock.get("stock_name", "不明"), "は上場廃止")
            del stocks[code_s]

    delisted_codes = []
    continue_codes = []
    # acces_date_priceが半年以上前の銘柄を、上場廃止チェックする
    for code_s, stock in list(stocks.items()):
        dt_price = stock.get("access_date_price", None)
        if dt_price < datetime.today() - timedelta(
            days=180
        ):  # 最新価格が半年経過しているか？
            log_print(code_s, stock.get("stock_name", "不明"), "は上場廃止の可能性あり")
            # print_dict(stock, ex_key=["gyoseki_quarter", "gyoseki_current", "price_log", "rs_rank_log", "stock_rank_log"])
            parsed_data = get_stock_master_data(stocks, code_s, UPD_INTERVAL)
            if master.is_delist(parsed_data):
                log_print(code_s, stock.get("stock_name", "不明"), "は上場廃止")
                # del stocks[code_s]
                delisted_codes.append(code_s)
            else:
                log_print(code_s, stock.get("stock_name", "不明"), "は上場継続中")
                continue_codes.append(code_s)

    # log_print("上場廃止銘柄:", delisted_codes)
    # log_print("上場継続銘柄:", continue_codes)
    # 上場廃止銘柄を削除
    for code_s in delisted_codes:
        if code_s in stocks:
            log_print("削除:", code_s, stocks[code_s].get("stock_name", "不明"))
            del stocks[code_s]
        else:
            log_warning("上場廃止銘柄がDBに存在しません:", code_s)
    log_print("上場廃止銘柄の削除完了")


def reflesh_db():
    """stock_dbを適切な状態に更新する
    現状は上場廃止銘柄の削除
    """
    stocks = load_stock_db()
    log_print("DB内銘柄数:", len(stocks))
    # 上場廃止銘柄の削除
    delete_delist_stocks(stocks)
    # ETF系の削除
    etf_codes = load_etf_codes()
    for code_s in etf_codes:
        if code_s in stocks:
            log_print("ETF銘柄削除:", code_s, stocks[code_s].get("stock_name", "不明"))
            del stocks[code_s]
        # else:
        #    log_warning("ETF銘柄がDBに存在しません:", code_s)

    log_print("削除後DB内銘柄数:", len(stocks))

    # 削除後のデータ保存
    save_stock_db(stocks)


def load_etf_codes():
    etf_fpath = os.path.join(DATA_DIR, "ETF_code.txt")
    with open(etf_fpath, "r") as f:
        etf_codes = f.read().splitlines()
    # タブ区切りのETFコードを抽出
    # 例: "1554 上場インデックスファンド米国株"
    new_etf_codes = []
    for line in etf_codes:
        code = line.strip().split("\t")[0]  # タブ区切りのコード部分を取得
        if code:
            new_etf_codes.append(code)
    return new_etf_codes



def test():
    # code = 6560
    # stock_db = load_stock_db()
    # stock_data = stock_db[code]
    # rank_log = stock_data.get("stock_rank_log",[])
    # print rank_log
    # rank0 = get_rank_log(stock_data, "stock_rank_log", 0)
    # rank1 = get_rank_log(stock_data, "stock_rank_log", 1)
    # rank5 = get_rank_log(stock_data, "stock_rank_log", 5)
    # # print "Rank:", stock[0], rank0, rank1, rank5
    # price_log = stock_data.get("price_log",[])
    # print price_log
    # pr0 = price.get_price_log(price_log, rank0[0])
    # pr1 = price.get_price_log(price_log, rank1[0])
    # pr5 = price.get_price_log(price_log, rank5[0])

    # RSログ表示のテスト
    code = "9343"
    stock_data = load_cacehd_stock_db(code)
    log_print((get_rank_log_expr(stock_data)))

    # DBリフレッシュ用
    # stocks = load_stock_db()
    # print "before:", len(stocks), "個"



# ==================================================
# research_shelve スナップショット自動追記
# ==================================================

KESSAN_WINDOW_DAYS = 14


def _collect_trigger_dates(stock, today):
    """stock の決算実績日/修正日から KESSAN_WINDOW_DAYS 以内のトリガー日を返す。

    - kessan_jisseki_date (決算発表実績日)、kessanbi (決算発表日/次回予定日)、
      kessan_mod_date (決算修正日) を独立に窓判定し、窓内のものを候補にする。
      いずれも実イベントの日付なので、複数窓内なら複数返す
      (発表行・修正行を別スナップショットとして残すため)。
    - kessanbi も候補に残すのは、master 更新が shintakane.update_todays_kessan より
      先に走ると kessan_jisseki_date が前回分のまま (窓外) で kessanbi だけ今回決算日
      (窓内) になるケースがあり、これを取りこぼさないため。未来の次回予定日は
      days<0 で自動的に窓外になる。
    - 同日 (発表日==予定日==修正日 等) は1件に集約。
    """
    candidates = []
    for field in ("kessan_jisseki_date", "kessanbi", "kessan_mod_date"):
        date_str = stock.get(field, "")
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d").date()
        except ValueError:
            continue
        if 0 <= (today - dt).days <= KESSAN_WINDOW_DAYS:
            candidates.append(date_str)
    # 同日 (発表日==予定日==修正日 等) を集約しつつ順序は保持
    return list(dict.fromkeys(candidates))


def update_research_snapshots(*, db_path=None, code_filter=None):
    """ウォッチ銘柄のうち決算更新があったものにスナップショットを自動追記する。

    対象は `my_watch_list.txt` 記載のコード (通常 + H付き保有) の union に限定。
    kessanbi / kessan_mod_date が 14 日以内の銘柄のみが処理対象。
    ウォッチ銘柄でかつ決算ウィンドウ内でも research_shelve 未登録の場合は、
    空レコードを自動登録してから同一実行内でスナップショットも追記する。
    ウィンドウ外の未登録ウォッチ銘柄は登録しない (research DB を汚染しないため)。
    同じ date_yy_m のスナップショットが既にあればスキップ。

    Args:
        db_path: research_shelve の DB パス上書き (テスト用)
        code_filter: 銘柄コード集合を渡すと、ウォッチ集合との積に限定する。
                     None の場合はウォッチ集合全体が対象。
    """
    import research_shelve
    import portfolio

    # ウォッチ集合の構築 (通常コード + H付き保有)
    try:
        watch_codes, possess_codes = portfolio.parse_my_portforio()
    except FileNotFoundError:
        log_warning(
            "[research] my_watch_list.txt が見つからないためスナップショット自動追記をスキップ"
        )
        return set()
    watch_set = set(watch_codes) | set(possess_codes)
    if code_filter is not None:
        filter_set = set(code_filter)
        not_in_watch = filter_set - watch_set
        if not_in_watch:
            log_warning(
                f"[research] code_filter にウォッチ外銘柄が含まれるためスキップ: {sorted(not_in_watch)}"
            )
        watch_set = watch_set & filter_set

    stocks = load_stock_db()
    today = get_price_day(datetime.today())

    all_records = research_shelve.list_research_records(db_path=db_path)
    existing_records = {
        r.get("code_s", ""): r for r in all_records if r.get("code_s", "")
    }

    # ウォッチ集合 × 決算ウィンドウ内の銘柄だけを対象に、
    # 必要なら自動登録してからスナップショットを追記する
    added_count = 0
    count = 0
    skipped_existing = 0
    eligible_count = 0
    for code_s in watch_set:
        stock = stocks.get(code_s)
        if not stock:
            continue

        trigger_dates = _collect_trigger_dates(stock, today)
        if not trigger_dates:
            continue  # 決算ウィンドウ外なので何もしない (未登録でも登録しない)

        eligible_count += 1

        record = existing_records.get(code_s)
        if record is None:
            # 未登録かつ決算ウィンドウ内 → このタイミングで初めて登録
            stock_name = stock.get("stock_name", "")
            try:
                record = research_shelve.create_research_record(
                    code_s=code_s, stock_name=stock_name
                )
                research_shelve.upsert_research_record(record, db_path=db_path)
                existing_records[code_s] = record
                added_count += 1
            except Exception as e:
                log_warning(f"[research] ウォッチ銘柄の自動登録失敗: {code_s} {e}")
                continue

        # 同日の重複スナップショット (migration/manual の同日2件目等) も検出できるよう、
        # date_yy_m ごとに list で保持する
        existing_by_date = {}
        for s in record.get("snapshots", []):
            existing_by_date.setdefault(s["date_yy_m"], []).append(s)

        # 指標/理論株価は株価依存のため取得日 (today) をラベルにする
        acquired_date = research_shelve.to_date_yy_m(today)

        for trigger_date_str in trigger_dates:
            try:
                dt = datetime.strptime(trigger_date_str, "%Y/%m/%d").date()
                date_yy_m = research_shelve.to_date_yy_m(dt)

                # 同日に1件でも非auto (manual/migration等) があれば、
                # 後段の upsert_snapshot(overwrite_same_date=True) で消えてしまうため
                # 上書きせずスキップする。auto 同士のみ上書き許可。
                same_date = existing_by_date.get(date_yy_m, [])
                has_protected = any(
                    s.get("data_source") != "auto" for s in same_date
                )
                if has_protected:
                    skipped_existing += 1
                    continue

                progress_expr, growth_expr = gyoseki.get_gyoseki_expr(stock)
                ir_quant = growth_expr + progress_expr  # [A]...[Q]...[P]... の順
                quality_indicators = shihyou.get_shihyo_expr(stock)
                rironkabuka_kairi = rironkabuka.get_rironkabuka_expr(stock)

                snapshot = research_shelve.create_snapshot(
                    date_yy_m,
                    acquired_date=acquired_date,
                    ir_quant=ir_quant,
                    quality_indicators=quality_indicators,
                    rironkabuka_kairi=rironkabuka_kairi,
                    data_source="auto",
                )
                research_shelve.upsert_snapshot(
                    code_s, snapshot, overwrite_same_date=True, db_path=db_path,
                )
                existing_by_date[date_yy_m] = [snapshot]
                count += 1
            except Exception as e:
                log_warning(f"[research] スナップショット追記失敗: {code_s} {e}")

    log_print(
        f"[research] ウォッチ対象: {eligible_count} 銘柄が決算ウィンドウ内 "
        f"(ウォッチ総数 {len(watch_set)})"
    )
    if added_count:
        log_print(f"[research] ウォッチ銘柄の自動登録: {added_count} 件")
    log_print(
        f"[research] スナップショット自動追記: {count} 件追記"
        + (f", {skipped_existing} 件スキップ(既存)" if skipped_existing else "")
    )

    return watch_set


def update_pts_reactions(watch_set, today_date, *, stocks=None):
    """当日決算銘柄の kessan_comments に PTS 騰落率を追記する。

    - today_date: datetime.date (get_price_day の戻り値)
    - PTS CSV の日付と today_date が一致する場合のみ書き込み
      (load_pts_changes_for_date が日付一致を保証)
    - watch_set 制限で research_shelve 汚染を防ぐ
    - stocks=None なら自前で load_stock_db() を呼ぶ (呼び出し側の重複ロード回避)
    - PTS CSV 不在 / watch_set 空のときは warning + スキップ
    """
    import pts_data
    from webapp.helpers import upsert_kessan_pts_change

    if not watch_set:
        log_warning("[pts] watch_set が空のため PTS 反応の追記をスキップ")
        return

    pts_changes = pts_data.load_pts_changes_for_date(today_date)
    if not pts_changes:
        log_warning("[pts] 当日 PTS CSV が見つからないため PTS 反応の追記をスキップ")
        return

    if stocks is None:
        stocks = load_stock_db()

    today_str = today_date.strftime("%Y/%m/%d")
    written = 0
    # PTS データ件数は当日決算 ~10件、watch_set は ~325件。
    # 積を取って先に絞り込む方が走査件数が少なく意図が明確になる。
    for code_s in watch_set & pts_changes.keys():
        stock = stocks.get(code_s)
        if not stock or stock.get("kessanbi") != today_str:
            continue
        quarter = int(stock.get("kessan_quarter") or 0)
        try:
            upsert_kessan_pts_change(
                code_s, today_str, quarter, pts_changes[code_s]
            )
            written += 1
        except Exception as e:
            log_warning(f"[pts] PTS 反応の追記失敗: {code_s} {e}")
    log_print(f"[pts] PTS 反応を当日決算銘柄 {written} 件に追記")


def refresh_pts_reactions():
    """株探のPTSナイトランキングを最新取得し、当日決算銘柄の kessan_comments に PTS 騰落率を追記する。

    list_all_db や update --snapshot を回さず、PTS の取り直しと反映だけを行う
    軽量パスとして使う。手順:
      1. shintakane.get_todays_pts(force=True) で pts_YYMMDD.csv を最新化
      2. update_research_snapshots() で watch_set を取得
         (副作用: ウォッチ × 決算ウィンドウ内銘柄に当日の auto スナップショットを上書き保存。
          stocks の kabuka は前段の引け値のままで、PTS 価格は混入しない)
      3. update_pts_reactions(watch_set, today_date) で kessan_comments['pts'] を上書き

    運用前提: 株探PTSナイトランキングは引け後 (17時以降) に形成されるため、本コマンドも
    17時以降の実行を想定する。17時前は get_price_day() が前営業日扱いとなり、株探PTS CSV
    側も同じ前営業日のラベルで保存されるため両者は整合する (= 17時前の実行でも壊れない)。
    """
    import shintakane

    log_print("=" * 30)
    log_print("[pts] PTS 反応の再取り込みを開始します")
    shintakane.get_todays_pts(force=True)
    watch_set = update_research_snapshots()
    today_date = get_price_day(datetime.today())
    update_pts_reactions(watch_set or set(), today_date)
    log_print("[pts] PTS 反応の再取り込みを完了しました")


def refresh_stock(code_list):
    """指定銘柄の master/price/shihyo/gyoseki/rironkabuka を UPD_FORCE で強制再取得し、
    research_shelve の当日スナップショットも最新値で上書きする。

    `update CODE --snapshot` と内部処理はほぼ同じだが、引数必須で名前が直感的。
    決算速報 (kessan_quarter / kessan_mod_date) は別経路 (shintakane.update_todays_kessan)
    なので、必要なら shintakane.py を別途実行する。
    """
    if not code_list:
        log_warning("[refresh_stock] 銘柄コードが指定されていません")
        return
    codes = list(code_list)
    log_print("=" * 30)
    log_print(f"[refresh_stock] 強制再取得を開始します: {codes}")
    update_db_rows(codes, upd=UPD_FORCE, tables=None)
    # stocks DB だけ最新化しても research_shelve のスナップショットは古い ir_quant のまま
    # なので、決算ウィンドウ内銘柄については snapshot も上書き更新する
    update_research_snapshots(code_filter=codes)
    log_print("[refresh_stock] 強制再取得を完了しました")


def refresh_price(code_list):
    """指定銘柄の price のみを UPD_FORCE で強制再取得する。

    yfinance キャッシュ (.json) と stocks_shelve の price_log を更新する。
    master/shihyo/gyoseki/rironkabuka は再取得しないため Kabutan スクレイピング
    負荷が無く、price_log の取得期間設定 (period) 変更後の反映確認などに使う。
    """
    if not code_list:
        log_warning("[refresh_price] 銘柄コードが指定されていません")
        return
    codes = list(code_list)
    log_print("=" * 30)
    log_print(f"[refresh_price] price 強制再取得を開始します: {codes}")
    update_db_rows(codes, upd=UPD_FORCE, tables=["price"])
    log_print("[refresh_price] price 強制再取得を完了しました")


# ==================================================
# main
# ==================================================
def main():
    """株価DBを更新するメインスクリプト"""
    # raise NotImplementedError("main関数は実装されていません")

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", type=str, nargs="?", default="list_all_db", help="実行するコマンド"
    )
    parser.add_argument(
        "codes",
        type=str,
        nargs="*",
        default=[],
        help="update / list に渡す銘柄コード（複数可）。未指定時はソース内のデフォルトを使用",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="update 後にウォッチ銘柄のスナップショット自動追記を実行",
    )
    args = parser.parse_args()
    log_print("=" * 30)
    log_print("make_stock_db.pyを実行します", args)
    log_print("=" * 30)

    command = args.command
    # command = "edit"
    # command = "backup"
    # command = "list_all_db"  # デフォ
    # command = "update"
    # command = "update_all_db"
    # command = "list"
    # command = "reflesh"
    # command = "test"
    if command == "update":
        if args.codes:
            code_list = list(args.codes)
        else:
            code_list = "471A"
            # code_list = "2979 3226 4384 4434 4443 4448 4449 4475 4477 4478 4479 4480 4483 4485 4488 4490 4493 4599 6835 7071"
            code_list = code_list.split()
        # f = open("update_code_list.txt")
        # lines = f.readlines()
        # code_list = [l.strip() for l in lines]
        # f.close()
        tables = None
        # tables = ["master"]
        # tables = ["price"]
        # tables = ["shihyo"]
        # tables = ["gyoseki"]
        # tables = ["rironkabuka"]
        update_db_rows(
            code_list, upd=UPD_FORCE, tables=tables
        )  # UPD_FORCE/UPD_REEVAL/UPD_INTERVAL
        if args.snapshot:
            # update 対象の銘柄に絞ってスナップショットを追記する
            watch_set = update_research_snapshots(code_filter=code_list)
            today_date = get_price_day(datetime.today())
            update_pts_reactions(watch_set or set(), today_date)
    elif command == "list":
        # DB内銘柄情報表示
        if args.codes:
            code_list = list(args.codes)
        else:
            code_list = "4483"
            # code_list = "3242 3686 6058 6432 7435"
            code_list = code_list.split()
        list_db(code_list)
    elif command == "list_all_db":
        # DBの情報をランキングで表示する
        UPLOAD_CSV = True  # True/False
        UPDATE_PORTFOLIO = True
        list_all_db(UPLOAD_CSV, UPDATE_PORTFOLIO)
        watch_set = update_research_snapshots()
        today_date = get_price_day(datetime.today())
        update_pts_reactions(watch_set or set(), today_date)
    elif command == "edit":
        edit_db()
    elif command == "backup":
        backup_db()
    elif command == "update_all_db":
        # 対象コードを取得
        def get_code_list_from_db(min=1000, max=10000):
            stocks = load_stock_db()
            code_list = list(stocks.keys())  # [400:]
            code_list.sort()
            code_list = [c for c in code_list if c >= min and c <= max]
            return code_list

        # code_list = get_code_list_from_db(1500, 10000)
        code_list = get_code_list_from_db(1000, 10000)
        current = 0  # 途中からやるときはここを書き換え
        while current < len(code_list):
            num = 500
            current_code_list = code_list[current : current + num]
            log_print(
                "%d~%d/%dを更新します"
                % (current_code_list[0], current_code_list[-1], len(code_list))
            )
            # 何を更新する？
            # tables = ["gyoseki", "shihyo, "master"]
            tables = ["price"]
            # tables = ["master"]
            update_db_rows(
                current_code_list, upd=UPD_REEVAL, tables=tables
            )  # UPD_REEVAL/UPD_FORCE
            log_print(
                "%d/%dまで更新しました" % (current + num, len(code_list)),
                current_code_list[-3:],
            )
            current += num
            break  # とりあえずテスト
    elif command == "reflesh":  # DBをリフレッシュ(上場廃止銘柄を削除)
        backup_db()
        reflesh_db()
    elif command == "calibrate_momentum":
        # モメンタムポイント分布パラメータのキャリブレーション (issue #104)。
        # 手動運用: 相場環境が大きく変わった場合や Phase 2 切替時の再校正に使う。
        # 自動的な週次更新は行わない (基準ぶれによる評価ジャンプを避けるため)。
        calibrate_momentum_pt()
    elif command == "refresh_pts":  # PTSランキング再取得 + research_shelve への反映のみ
        refresh_pts_reactions()
    elif command == "refresh_stock":  # 指定銘柄の master/price/shihyo/gyoseki/rironkabuka を強制再取得
        if not args.codes:
            log_warning("refresh_stock: 銘柄コードを 1 つ以上指定してください")
        else:
            refresh_stock(list(args.codes))
    elif command == "refresh_price":  # 指定銘柄の price のみ強制再取得 (yfinanceキャッシュ + price_log)
        if not args.codes:
            log_warning("refresh_price: 銘柄コードを 1 つ以上指定してください")
        else:
            refresh_price(list(args.codes))
    elif command == "test":
        test()

    # 非同期アップロードの完了を待つ（list_all_db等で起動されたスレッド）
    import googledrive
    googledrive.wait_all_uploads()


# TODO: エラーを記述するようにせんと・・
if __name__ == "__main__":
    # TODO: 古い日付のタグは無効にしたい　全銘柄DB更新せず表示するときの判断でいいかも
    # TODO: 監視タグも
    # TODO: セクターのRSランキングを作成し、参照したい オニールのIBD
    # https://kabutan.jp/warning/?mode=9_1&market=0&capitalization=-1&stc=zenhiritsu&stm=1&col=zenhiritsu
    # ロガーの初期化
    logger = setup_logger("make_stock_db")
    # カレントディレクトリをこの.pyの場所に
    with chdir(os.path.abspath(os.path.dirname(__file__))):
        # main()
        try:
            main()
        except Exception as e:
            log_print("エラー発生", e)
            logger.exception(
                "Unhandled exception occurred:\n%s", traceback.format_exc()
            )
            raise e  # 開発実行時ブレークするため投げる
            # exit(1)  # エラー終了
