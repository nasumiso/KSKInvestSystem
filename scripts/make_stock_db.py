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


def update_stock_log(rank_log, rank):
    """ランクログを更新
    Returns: 20個(日分)のランクログ(新しい日付が先)
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
    return rank_log[0:20]


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
    """rs_line の 5日前比 A・20日前比 B 騰落率を%値で計算する。

    Returns:
        tuple[float|None, float|None]: (短期A%, 中期B%)
            - rs_line が 6本未満 → (None, None)
            - rs_line が 6本以上21本未満 → (A, None)
            - rs_line が 21本以上 → (A, B)
            past値が0の場合も None
    """
    rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
    return _rs_line_changes_from_line(rs_line)


def _fmt_rs_change(v):
    """rs_line 騰落率を符号付き整数% に整形 (None は "-")"""
    return "-" if v is None else "%+d" % round(v)


def get_rs_line_changes_expr(stock, market_db, topix_map=None, rs_line=None):
    """rs_line 騰落率を CSV 表示用の '中期B%/短期A%' 文字列にする。

    rs_line を渡せば再計算をスキップする (CSV ループで複数の rs_line 系関数を
    呼ぶ際に共有するため)。

    Returns:
        str: 例 "+12/+5"。両方計算不能なら "" 、片方のみなら "-/+5" 等
    """
    if rs_line is None:
        rs_line = compute_rs_line(stock, market_db, topix_map=topix_map)
    a, b = _rs_line_changes_from_line(rs_line)
    if a is None and b is None:
        return ""
    return "%s/%s" % (_fmt_rs_change(b), _fmt_rs_change(a))


def _rs_line_changes_from_line(rs_line):
    """rs_line 系列から 5日前比 A・20日前比 B 騰落率を計算する内部関数"""
    if not rs_line:
        return (None, None)
    current = rs_line[0][1]

    def _change(offset):
        if len(rs_line) <= offset:
            return None
        past = rs_line[offset][1]
        if past == 0:
            return None
        return (current - past) / past * 100

    return (_change(5), _change(20))


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
        "price_log",
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


def _update_db_code(c_s, upd, tables, stocks, latest, force):
    """同期非同期共通のDB更新関数"""
    stock_data = {}
    if not tables or "master" in tables:
        if not has_stock_data(stocks, c_s, latest) or force:
            stock_data.update(get_stock_master_data(stocks, c_s, upd))
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
    return ""


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


def make_signal(stock, market_db=None, topix_map=None, rs_line=None):
    """銘柄DBデータから、シグナル情報を作成する。

    market_db を渡すと rs_line ベースのタグ (R高 / 強乖 / 弱乖) も付与される。
    後方互換のため market_db=None ならスキップ。
    rs_line を渡せば再計算をスキップする。
    """
    today = datetime.today()
    signal = ""
    tags = []

    # 新高値
    new_high = stock.get("new_high", "")
    if new_high:
        if "access_date_price" in stock:
            dt = stock.get("access_date_price", "")
            dt = get_price_day(dt)
            if (date.today() - dt).days <= 30:
                tags.append("".join(new_high))
    # 20MA押し
    pb20 = stock.get("pullback_20", "")
    if pb20:
        if "access_date_price" in stock:
            dt = stock.get("access_date_price", "")
            dt = get_price_day(dt)
            if (date.today() - dt).days <= 30:
                tags.append("押")
    # ポケットピポット
    pocket_pivot = stock.get("pocket_pivot", "")
    for sig in pocket_pivot:
        spl = sig.split(",")
        try:
            dt = datetime.strptime(str(today.year) + "/" + spl[0], "%Y/%m/%d")
            delta_day = (today - dt).days
            # mark = "★"  if delta_day < 3 else ""
            if delta_day <= 7 and delta_day >= 0:
                tags.append("ポ")
        except ValueError:
            log_warning("ポケットピポット日付エラー", spl[0])
        signal += "\n[ポ]"
        signal += "%s(%s)," % (spl[0], spl[1])
        break  # 一つにしておく(最新日)
    # ブレイクアウト
    breakout = stock.get("breakout", [])
    for brk in breakout:
        brkspl = brk.split(",")
        try:
            dt = datetime.strptime(str(today.year) + "/" + brkspl[0], "%Y/%m/%d")
            delta_day = (today - dt).days
            # mark = "★"  if delta_day < 3 else ""
            if delta_day <= 7 and delta_day >= 0:
                tags.append("ブ")
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
            if compute_rs_line_new_high(stock, market_db, rs_line=rs_line):
                tags.append("R高")
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
# DB一覧表示
# ==================================================


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
            total_pt = int(
                (
                    40 * gyoseki_pt
                    + 20 * shihyo_pt
                    + 25 * mom_pt
                    + 15 * funda_pt  # noqa: E226,E501
                )
                / 100
            )
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
    rows.append(
        [
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
    )

    for i, stock in enumerate(stocks_active):
        stock_data = stocks[stock[0]]
        # 更新日
        date_exp = get_access_dates_expr(stock_data)

        overview = ""
        if "overview" in stock_data:
            overview = stock_data.get("overview", "")
        themes = stock_data.get("themes", "")
        main_theme = make_market_db.get_major_theme(themes)
        # 決算日
        kessanbi = kessan.get_kessanbi_expr(stock_data)
        # トレンド、押し目
        trend = get_trend_template_expr(stock_data)

        # ボラティリティ、売り圧力レシオ・買い集め指数
        vola, sell_press = get_vola_and_sell_press_expr(stock_data)
        # 順位
        # buffet_url = "https://www.buffett-code.com/company/%s/library" % (stock[0])
        # TODO: 福証などでは.Fになる
        # URL_YAHOO_QUOTE = "https://finance.yahoo.com/quote/%s.%s"
        URL_YAHOO_QUOTE = "https://finance.yahoo.co.jp/quote/%s.%s"
        market_code = get_market_code(stock_data)
        yahoo_url = URL_YAHOO_QUOTE % (stock[0], market_code)

        rank = i + 1
        rank = '=HYPERLINK("%s", "%d")' % (yahoo_url, rank)
        # ---- ポートフォリオ
        ports = []
        if stock[0] in pf_stocks:
            ports.append("監")
        if stock[0] in possess_list:
            ports.append("保")
        ports = "".join(ports)
        # rs_line を1回だけ計算して下流の関数で使い回す
        rs_line = compute_rs_line(stock_data, market_db, topix_map=topix_map)
        # ---- タグ、シグナル
        signal, tags, prev_rank = get_signal_tags_prevrank_expr(
            stock_data, market_db=market_db, topix_map=topix_map, rs_line=rs_line
        )

        # ---- 指標用の項目
        indicator_expr = shihyou.get_shihyo_expr(stock_data)
        credit_expr = shihyou.get_credit_expr(stock_data)

        # ---- 業績用項目
        progress_expr, growth_exp = gyoseki.get_gyoseki_expr(stock_data)

        # 理論株価
        rironkabuka_expr = rironkabuka.get_rironkabuka_expr(stock_data)
        # 過去業績
        gyoseki_quarity_expr = gyoseki.get_gyoseki_quarity_expr(stock_data)

        # ---- その他項目
        code = get_code_exp(stock[0])
        stock_name = get_stock_name_exp(stock_data)
        sector = stock_data.get("sector", "")
        # relates_rank = stock_data.get("relates_rank", 0) # 関連銘柄内順位:封印
        rs_log = get_rs_line_changes_expr(stock_data, market_db, rs_line=rs_line)
        momentum = "%d.%s" % (stock[4], rs_log)
        # 行要素作成
        rows.append(
            [
                ports,
                tags,
                kessanbi,
                str(rank),
                prev_rank,
                code,
                stock_name,
                sector,
                stock[1],
                stock[2],
                stock[3],
                momentum,
                stock[5],
                date_exp,
                signal,
                trend,
                vola,
                sell_press,
                growth_exp,
                progress_expr,
                indicator_expr,
                rironkabuka_expr,
                gyoseki_quarity_expr,
                credit_expr,  # noqa: E501
                main_theme,
                overview,
            ]
        )
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
    """stock の kessanbi / kessan_mod_date から KESSAN_WINDOW_DAYS 以内のトリガー日を返す。

    両方が窓内の場合は新しい方のみ採用 (stocks データが最新イベント時点の値しか
    保持しないため、古い方に書くと履歴捏造になる)。
    """
    candidates = []
    for date_field in ("kessanbi", "kessan_mod_date"):
        date_str = stock.get(date_field, "")
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d").date()
            if 0 <= (today - dt).days <= KESSAN_WINDOW_DAYS:
                candidates.append((dt, date_str))
        except ValueError:
            pass
    if len(candidates) >= 2:
        candidates.sort(reverse=True)
        return [candidates[0][1]]
    return [c[1] for c in candidates]


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

        for trigger_date_str in trigger_dates:
            try:
                dt = datetime.strptime(trigger_date_str, "%Y/%m/%d")
                date_yy_m = f"{dt.year % 100}.{dt.month}.{dt.day}"

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
