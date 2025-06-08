#!/usr/bin/python
# -*- coding: utf-8 -*-
import sys
import os.path
import shutil
from datetime import datetime, date, timedelta
import re
import pickle
import csv

from ks_util import *
import gyoseki
import rironkabuka
import shihyou
import price
import master
import make_market_db
import kessan

def has_stock_data(stocks, code_s, latest=False):
	"""
	DBに基本銘柄情報があるか？
	latest: 最新であることが必要かどうか
	"""
	INTERVAL_DAY = 7 #10
	#code = int(code)
	if code_s in stocks:
		if "stock_name" in stocks[code_s]:
			if latest: # 最新だ
				timedelta = datetime.today()-stocks[code_s]["access_date"]
				if timedelta.days < INTERVAL_DAY:
					#print "基本情報あり: %d日前"%timedelta.days
					return True
				else: # 最新でない
					print("基本銘柄更新: %d日ぶり"%timedelta.days)
					return False
			else:
				return True
	return False

def get_stock_data(stocks, code_s, upd=UPD_INTERVAL):
	"""
	基本銘柄情報をDBから取得
	DBにない場合は新規取得する
	dict,int,bool => dict
	"""
	# yahooをやめて株探基本情報から取得
	#code = int(code)
	#print "get_stock_data", code, latest
	# DBにある場合はそれを返す
	if code_s in stocks and upd < UPD_INTERVAL:
		if "stock_name" in stocks[code_s]:
			return stocks[code_s]

	return master.get_stock_master_data(code_s, upd)

def is_latest_price(stocks, code_s):
	"""
	DB価格データ取得の日付現在日付から、
	最新価格データかどうかを返す
	"""
	# 当日なら
	need_dt = get_price_day(datetime.today())
	price_dt = get_price_day(stocks[code_s]["access_date_price"])
	price_day = date(price_dt.year,price_dt.month,price_dt.day)
	need_day = date(need_dt.year,need_dt.month,need_dt.day)
	if need_day.weekday() == 5:
		need_day -= timedelta(1)
	elif need_day.weekday() == 6:
		need_day -= timedelta(2)
	#print " 価格データ 必要:%s DB最新:%s"%(need_day, price_day)
	if need_day <= price_day:
		return True, (need_day-price_day).days
	return False, (need_day-price_day).days

def has_price_data(stocks, code_s, latest=False):
	"""
	DBに価格情報があるか？
	"""
	#code = int(code)
	if code_s in stocks:
		if "sell_pressure_ratio" in stocks[code_s]:
			if latest:
				is_latest, interval_day = is_latest_price(stocks, code_s)
				if is_latest:
					return True
				else:
					print("価格更新: %d日ぶり"%interval_day)
					return False
			else:
				return True
	return False

def get_price_data(stocks, code_s, upd=UPD_INTERVAL):
	"""銘柄価格情報を取得
	"""
	#code = int(code)
	# DBにありなおかつ最新である場合はそれを返す
	if code_s in stocks: #and not latest: #デバッグ用強制
		if "access_date_price" in stocks[code_s] and upd < UPD_INTERVAL:
			print("DBに最新価格情報があるためそれを取得します")
			return stocks[code_s]
	# 価格データを新規更新
	price_dict = price.get_price_data(code_s, upd)
	# 関連銘柄内ランクを更新
	# ↓取得できない＆あまり意味がないので封印
	#price_dict["relates_rank"] = get_relates_rank(stocks, code)
	price_dict["rs_rank_log"] = update_rs_rank(stocks, code_s)

	# 変則的だがテーマは日々移ろうので指標とともにファンダポイントを計算	
	themes = stocks[code_s].get("themes","") if code_s in stocks else None
	if themes:
		funda_pt = master.calc_fundamental(code_s, themes)
		#funda_pt = master.calc_fundamental(stocks, code)
		price_dict["funda_pt"] = funda_pt

	return price_dict

def update_stock_log(rank_log, rank):
	""" ランクログを更新
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
	rank_log = sorted(rank_log, key=lambda x:x[0], reverse=True)
	#print "ランクログ更新:", ind, date, rank
	return rank_log[0:20]

def update_stock_rank(stock, rank):
	""" 銘柄ランクログを更新
	"""
	stock_rank_log = stock.get("stock_rank_log", [])
	#print "stock_rank_log:", stock["code"], stock_rank_log
	rank_dict = {}
	rank_dict["stock_rank_log"] = update_stock_log(stock_rank_log, rank)
	stock.update(rank_dict) # 更新

def update_rs_rank(stocks, code_s):
	""" RSとRSログを更新
	"""
	if code_s not in stocks:
		return []
	stock = stocks[code_s]
	rs_rank_log = stock.get("rs_rank_log", [])
	#print "rs_rank_log:", rs_rank_log
	rs_rank = stock.get("momentum_pt")

	return update_stock_log(rs_rank_log, rs_rank)

def get_rank_log_expr(stock):
	""" RSログを表示用に整形
	"""
	rs_rank_log = stock.get("rs_rank_log", [])
	# rs_rank_ma1 = [l[1] for l in rs_rank_log[0:1]]
	# rs_rank_ma1 = sum(rs_rank_ma1)/len(rs_rank_ma1)
	if not rs_rank_log:
		return ""
	latest_date = rs_rank_log[0][0]
	# 0はエラー値なので除外
	#TODO: 日付でフィルターするべきかも
	rs_rank_ma5 = [l[1] for l in rs_rank_log[0:5] if l[0] >= latest_date-timedelta(days=7)]
	rs_rank_ma5 = [l for l in rs_rank_ma5 if l is not None and l > 0]
	if not rs_rank_ma5:
		return ""
	rs_rank_ma5 = sum(rs_rank_ma5)/len(rs_rank_ma5)
	rs_rank_ma20 = [l[1] for l in rs_rank_log[0:20] if l[0] >= latest_date-timedelta(days=28)]
	rs_rank_ma20 = [l for l in rs_rank_ma20 if l is not None and l > 0]
	if not rs_rank_ma20:
		return ""
	rs_rank_ma20 = sum(rs_rank_ma20)/len(rs_rank_ma20)
	return "%02d%02d"%(rs_rank_ma5,rs_rank_ma20)
	

def get_rank_log(stock, log_name, diff_day=0):
	""" diff_day日前のrank_logを返す
	-> (day, rs)
	"""
	rank_log = stock.get(log_name, ())
	if not rank_log:
		return ()
	day_first = rank_log[0][0]
	#print "day_first:", day_first
	for day, rs in rank_log:
		if (day_first-day).days >= diff_day:
			return day,rs
	return (None, 0)

# def get_relates_rank(stocks, code):
# 	"""
# 	関連銘柄内ランクを更新
# 	"""
# 	#---- relates_rsを計算
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
# 			#print relates, rs_raws
# 			rs_raws.sort(reverse=True)
# 			relates_rank = rs_raws.index(rs_raw)+1
# 			print "関連銘柄内ランク:", relates_rank
# 			return relates_rank
# 	return 0

def need_kessan_upd(stocks, code_s, dt_access):
	"""アクセス時間の決算日超過のチェック
	"""
	kessan_upd = False
	dt_access2 = get_price_day(dt_access)
	tdy = get_price_day(datetime.today())
	try:
		# 決算日とアクセス時間の間隔を取得
		kessanbi = stocks[code_s].get("kessanbi","")
		if kessanbi:
			dt_kessanbi = datetime.strptime(stocks[code_s]["kessanbi"], "%Y/%m/%d").date()
			#print "決算発表日付:", kessanbi, dt_kessanbi
			if tdy >= dt_kessanbi and dt_access2 < dt_kessanbi:
				print("決算日を過ぎているため更新", code_s, dt_kessanbi, dt_access2)
				#upd = UPD_FORCE
				kessan_upd = True
		kessan_mod_date = stocks[code_s].get("kessan_mod_date","")
		if kessan_mod_date:
			dt_kessan_mod = datetime.strptime(stocks[code_s]["kessan_mod_date"], "%Y/%m/%d").date()
			#print "決算修正日付:", kessan_mod_date, "アクセス:", dt_kessan_mod, dt_access2
			if tdy >= dt_kessan_mod and dt_access2 < dt_kessan_mod:
				print("決算修正があったため更新", code_s, dt_kessan_mod, dt_access2)
				kessan_upd = True
	except (KeyError, ValueError):
		print("決算データがない", code_s)
		pass
	return kessan_upd

def has_active_dbdata(stocks, code_s, access_key, interval_day, latest):
	#code = int(code)
	if code_s in stocks:
		# 対象(業績など)データアクセス時間と現在時間を比較
		if access_key in stocks[code_s]:
			dt_access = stocks[code_s][access_key]
			#print "has_gyoseki:", code, latest, dt_access
			if latest:
				# 決算日超過のチェック
				kessan_upd = need_kessan_upd(stocks, code_s, dt_access)
				# アクセス日超過または決算更新
				timedelta = datetime.today() - dt_access
				if timedelta.days >= interval_day or kessan_upd:
					print("%s更新: %d日ぶり"%(access_key, timedelta.days))
					return False
				else:
					#print "業績あり: %d日前"%timedelta.days
					return True
			else:
				return True
	return False

def has_gyoseki_data(stocks, code_s, latest=False):
	"""DBに業績情報があるか
	15日経過するか、決算日をすぎている
	@param	latest	
	"""
	INTERVAL_DAY = 15
	return has_active_dbdata(stocks, code_s, "access_date_gyoseki",INTERVAL_DAY, latest)

def get_gyoseki_data(stocks, code_s, upd=UPD_INTERVAL):
	"""
	業績データを取得するインターフェース
	-> dict
	"""
	#code = int(code)
	if code_s in stocks:
		if "access_date_gyoseki" in stocks[code_s] and upd < UPD_INTERVAL:
			print("DBから業績情報を取得します")
			return stocks[code_s]
	gyoseki_data = gyoseki.get_gyoseki_data(code_s, upd)
	return gyoseki_data

def has_rironkabuka_data(stocks, code_s, latest=False):
	"""
	理論株価データがあるか？
	latest: Trueなら最新であるかを調査(一定期間アクセスがあったか)
	"""
	INTERVAL_DAY = 15
	return has_active_dbdata(stocks, code_s, "access_date_rironkabuka",INTERVAL_DAY, latest)

def get_rironkabuka_data(stocks, code_s, upd=UPD_INTERVAL):
	"""
	Returns:
		dict<key, value>: 更新するDBデータ
	"""
	#code = int(code)
	if code_s in stocks:
		if "access_date_rironkabuka" in stocks[code_s] and upd < UPD_INTERVAL:
			print("DBから理論株価情報を取得します")
			return stocks[code_s]
	stock = stocks[code_s] if code_s in stocks else None
	data = rironkabuka.get_rironkabuka_data(code_s, upd, stock)
	return data

def has_shihyo_data(stocks, code_s, latest=False):
	INTERVAL_DAY = 5 # この設定は微妙
	#code = int(code)
	if code_s in stocks:
		# 指標データの取得日と現在との日付チェック
		if "access_date_shihyo" in stocks[code_s]:
			if latest:
				dt_access = stocks[code_s]["access_date_shihyo"]
				kessan_upd = need_kessan_upd(stocks, code_s, dt_access)
				timedelta = datetime.today()-dt_access
				if timedelta.days >= INTERVAL_DAY or kessan_upd:
					print("指標更新: %d日ぶり"%timedelta.days)
					return False
				else:
					#print "指標あり: %d日前"%timedelta.days
					return True
			else:
				return True
	return False

def get_shihyo_data(stocks, code_s, upd=UPD_INTERVAL):
	"""指標データの更新・取得
	Returns:
		dict<key, value>: 更新するDBデータ内容
	"""
	#code = int(code)
	latest = upd >= UPD_INTERVAL
	if code_s in stocks:
		if "access_date_shihyo" in stocks[code_s] and not latest:
			print("DBから指標情報を取得します")
			return stocks[code_s]
	# 指標更新
	data = shihyou.get_shihyo_data(stocks, code_s, upd)
	return data

#==================================================
# database
#==================================================
STOCKS_PICKLE = os.path.join(DATA_DIR, "stock_data", "stocks.pickle")
#PRICES_PICKLE = "stock_data/prices.pickle"
#TABLES = {"master":STOCKS_PICKLE, "price":PRICES_PICKLE}
def update_db(stocks, stock_data):
	"""
	stocksのDBデータをstock_dataのcodeキーで更新する
	Args:
		stocks(dict<int, dict>): 銘柄DB本体
		stock_data(dict<key, value>): 更新したいdict
	"""
	#print stock_data
	# 更新
	if "code_s" not in stock_data:
		if "code" not in stock_data:
			print("追加するレコードはcode_sキーを持たせてください")
			return
		else:
			code = stock_data["code"]
			stock_data["code_s"] = str(code)
			print("intコードをstrに変換:",code)
	code_s = stock_data["code_s"]
	# レコードにカラムをキーから抜き出しを追加
	try:
		stock = stocks[code_s]
	except KeyError:
		stock = {}
		print(str(code_s)+"は新規DB銘柄")
	for k in list(stock_data.keys()):
		stock[k] = stock_data[k]
	print("DB更新しました: ", code_s, list(stock_data.keys()))
	# 更新後のカラム表示
	print_dict(stock, ex_key=["gyoseki_quarter", "gyoseki_current", "shihyo", \
		"price_log", "rs_rank_log", "stock_rank_log"])
	stocks[code_s] = stock

def update_db_rows(code_s_list, upd=UPD_INTERVAL, tables=None):
	"""	code_listで指定された銘柄のDB更新し、DB全体を返す
	Params:
		code_list: list<int>
		latest: bool 強制で最新データに更新する
		tables: list<str> 更新するテーブルを指定する[master/price/gyoseki/rironkabuka]
	Return:
		更新されたDB
	"""
	#TODO: ETF系は更新はぶく？事前にはstocksデータないからここでしかわからん
	#print code_list
	# ロード
	table_pickle = STOCKS_PICKLE
	stocks = {}
	if os.path.exists(table_pickle):
		stocks = load_pickle(table_pickle)
		if not isinstance(stocks, dict):
			raise "!!![警告] stocksがdict型でありません"
	# 更新
	if tables is None:
		tables = []
	latest = upd >= UPD_INTERVAL
	force = upd >= UPD_REEVAL
	#print "update_tables:", tables, "強制更新:", latest
	print("update_tables:", tables, " 更新:", upd)
	#code_exists = []
	for c in code_s_list:
		stock_data = {}
		if not tables or "master" in tables:
			if not has_stock_data(stocks, c, latest) or force:
				stock_data.update(get_stock_data(stocks, c, upd))
		if not tables or "price" in tables:
			if not has_price_data(stocks, c, latest) or force:
				stock_data.update(get_price_data(stocks, c, upd))
		if not tables or "gyoseki" in tables:
			if not has_gyoseki_data(stocks, c, latest) or force:
				upd = UPD_FORCE # アクセス間隔以外でもみてるので、一反強制　TODO:やり方考える
				stock_data.update(get_gyoseki_data(stocks, c, upd))
		if not tables or "rironkabuka" in tables:
			if not has_rironkabuka_data(stocks, c, latest) or force:
				upd = UPD_FORCE # 業績と同じく一反強制
				stock_data.update(get_rironkabuka_data(stocks, c, upd))
		if not tables or "shihyo" in tables:
			if not has_shihyo_data(stocks, c, latest) or force:
				upd = UPD_FORCE # 業績と同じく一反強制
				stock_data.update(get_shihyo_data(stocks, c, upd))

		if stock_data:
			update_db(stocks, stock_data)
		# else:
		# 	try:
		# 		code_exists.append(str(c)+stocks[int(c)]["stock_name"])
		# 		#print "%d%sはDBに存在します"%(c, stocks[c]["stock_name"])
		# 	except KeyError:
		# 		print "%dは更新情報を取得できませんでした"%c

	# print "-"*30
	# print "DBに有効期限内データが存在:", " ".join(code_exists) 
	# print "-"*30

	# セーブ
	save_pickle(table_pickle, stocks)
	return stocks

def get_stock_db(code):
	"""
	指定codeの銘柄DBデータを返す
	"""
	stocks = memoized_load_pickle(STOCKS_PICKLE)
	return stocks.get(int(code), {})

from contextlib import contextmanager
import io
@contextmanager
def print_to():
	output = io.StringIO()
	sys.stdout = output
	yield output
	sys.stdout = sys.__stdout__

@contextmanager
def print_to_file(fname):
	output = open(fname, 'w')
	sys.stdout = output
	yield output
	output.close()
	sys.stdout = sys.__stdout__

def list_db(code_list=[]):
	stocks = load_pickle(STOCKS_PICKLE)
	#print code_list
	code_s_list = [str(c) for c in code_list]
	with print_to() as out:
		for k, v in stocks.items():
			if not code_s_list or k in code_s_list:
				print("[%s]"%k)
				print_dict(v, 
					ex_key=["shihyo", "gyoseki_current", "gyoseki_quarter"])
	print(out.getvalue())

#==================================================
# 項目カスタマイズ表示
#==================================================
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
		return "◯"+",".join(stock["trend_template"])
	if miss_count <= 4:
		return "▲"
	if miss_count <= 6:
		return "△"	
	return ""

def make_signal(stock):
	"""銘柄DBデータから、シグナル情報を作成する
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
			dt = datetime.strptime(str(today.year)+"/"+spl[0], "%Y/%m/%d")
			delta_day = (today-dt).days
		#mark = "★"  if delta_day < 3 else ""
			if delta_day <= 7 and delta_day >= 0:
				tags.append("ポ")
		except ValueError:
			print("!!! ポケットピポット日付エラー", spl[0])
		signal += "\n[ポ]"
		signal += "%s(%s),"%(spl[0], spl[1])
		break # 一つにしておく(最新日)
	# ブレイクアウト
	breakout = stock.get("breakout", [])
	for brk in breakout:
		brkspl = brk.split(",")
		try:
			dt = datetime.strptime(str(today.year)+"/"+brkspl[0], "%Y/%m/%d")
			delta_day = (today-dt).days
			#mark = "★"  if delta_day < 3 else ""
			if delta_day <= 7 and delta_day >= 0:
				tags.append("ブ")
		except ValueError:
			print("!!! ブレイクアウト日付エラー", brkspl[0])
		signal += "[ブ]"
		signal += "%s(%s),"%(brkspl[0], brkspl[1])
		break # 一つにしておく(最新日)
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
	#rs_rank = stock.get("momentum_pt", 0)
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


	#print signal, tags
	return signal, tags

def get_code_exp(code_s):
	code_s = str(code_s)
	KABUTAN_URL = "https://kabutan.jp/stock/chart?code=%s"
	return '=HYPERLINK("%s","%s")'%(KABUTAN_URL%code_s, code_s)
def get_stock_name_exp(stock):
	"""
	銘柄名の表示用表現を返す
	"""
	stock_name = stock.get('stock_name',"Unknown")
	corpo_url = stock.get("corporate_url", "")
	if corpo_url:
		stock_name = '=HYPERLINK("%s","%s")'%(corpo_url, stock_name)
	return stock_name

def get_access_dates_expr(stock_data):
	"""	更新日表現を取得
	Args:
		stock_data (dict): 銘柄DBデータ
	Returns:
		str: 更新日文字列 "month/day|day|day".
	"""
	date = ""
	if "access_date_gyoseki" in stock_data:
		dt = stock_data["access_date_gyoseki"]
		date = "%s/%s"%(dt.month,dt.day)
		month = dt.month
	date_sh = ""
	if "access_date_shihyo" in stock_data:
		dt = stock_data["access_date_shihyo"]
		if month == dt.month:
			date_sh = dt.day
		else:
			date_sh = "%s/%s"%(dt.month,dt.day)
			month = dt.month
	date_pr = ""
	if "access_date_price" in stock_data:
		dt = stock_data["access_date_price"]
		dt = get_price_day(dt)
		if month == dt.month:                
			date_pr = dt.day
		else:
			date_pr = "%s/%s"%(dt.month,dt.day)
	# TODO: 理論株価も必要なら
	date_exp = "%s|%s|%s"%(date,date_sh,date_pr)
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
		sell_press += ",%+d"%(kairi_wma10)

	except TypeError:
		vola = ""
		sell_press = ""
	return vola, sell_press

def get_signal_tags_prevrank_expr(stock_data):
	tags = [] # タグ
	signal, tags = make_signal(stock_data) # シグナル

	#---- 過去順位と株価上昇率
	try:
		rank0 = get_rank_log(stock_data, "stock_rank_log", 0)
		rank1 = get_rank_log(stock_data, "stock_rank_log", 1)
		rank5 = get_rank_log(stock_data, "stock_rank_log", 5)
		price_log = stock_data.get("price_log",[])
		pr0 = price.get_price_log(price_log, rank0[0])
		pr1 = price.get_price_log(price_log, rank1[0])
		pr5 = price.get_price_log(price_log, rank5[0])
		ratio1 = "%+d"%(100*pr0/pr1-100) if (pr0!=0 and pr1!=0) else ""
		ratio5 = "%+d"%(100*pr0/pr5-100) if (pr0!=0 and pr5!=0) else ""
		
		def get_arrow(v):
			if v == 0:
				return ""
			else:
				return "↑" if v > 0 else "↓"
		
		rank1_0 = rank1[1]-rank0[1]
		rank5_0 = rank5[1]-rank0[1]
		rank1_s = "%s%d"%(get_arrow(rank1_0), abs(rank1_0))
		rank5_s = "%s%d"%(get_arrow(rank5_0), abs(rank5_0))
		prev_rank = "%s(%s)|%s(%s)"%(rank1_s, ratio1, rank5_s, ratio5)
		
		# 急上昇をタグに入れる急
		if rank1_0 > rank1[1]*0.30:
			tags.append("急")
		elif rank5_0 > rank5[1]*0.30:
			tags.append("昇")
	except IndexError as e:
		prev_rank = ""

	tags = "/".join(tags)
	return signal, tags, prev_rank

#==================================================
# DB一覧表示
#==================================================
def list_all_db(upload_csv=True, update_portforio=True):
	"""DB内銘柄のランキングリスト
	Args:
		update_portforil(bool): 100位以内とポートフォリオのDBデータを更新するかどうか
	"""
	# マーケットの更新
	market_db = make_market_db.update_market_db()
	# 銘柄DBロード
	stocks = load_pickle(STOCKS_PICKLE)
	stocks_active = []
	print("DB内銘柄数", len(stocks))
	#delete_stocks = []
	for k, v in stocks.items():
		try:
			gyoseki_pt = int(v['score_gyoseki'])
			shihyo_pt = v['shihyo_pt']
			#mom_pt = int((v.get('rs_raw', 0)-1)*100)
			mom_pt = v.get('momentum_pt', 0)
			funda_pt = v.get('funda_pt', 0)
			total_pt = int((40*gyoseki_pt+20*shihyo_pt+25*mom_pt+15*funda_pt)/100)
			stocks_active.append((k, total_pt, gyoseki_pt, shihyo_pt, mom_pt, funda_pt))
		except KeyError as e:
			print("必要キー%sなし"%e, k, v.get('stock_name', ''))
			#delete_stocks.append(k)
			continue
	#return

	# 自分のポートフォリオロード
	import portfolio
	pf_stocks, possess_list = portfolio.parse_my_portforio()
	print("ポートフォリオ:", pf_stocks+possess_list)
	
	# 総合PTでソート
	stocks_active = sorted(stocks_active, key=lambda stock: stock[1], reverse=True)
	#---- 100位以内とポートフォリオのDB情報を更新
	if update_portforio:
		# テーマ銘柄を更新に入れる
		theme_count = 0
		theme_codes_s = []
		theme_rank_list = market_db["theme_rank"]
		#theme_rank_list, _, _, _ = make_market_db.get_theme_rank_list()
		for j, theme in enumerate(theme_rank_list):
			current = len(theme_codes_s)
			for i, s in enumerate(stocks_active):
				stock = stocks.get(s[0], {})
				themes = stock.get("themes", "")
				if theme in themes and not theme in theme_codes_s:
					if i/100 + j < 20: # 一定以上の重要度
						theme_codes_s.append(s[0])
			print("テーマ:%sの銘柄%d個"%(theme, len(theme_codes_s)-current))
			if len(theme_codes_s) > 100:
				break
		update_codes_s = theme_codes_s
		# 100位以内
		update_codes_s += [s[0] for i,s in enumerate(stocks_active) if i<100] 
		# 俺ポートフォリオ追加
		update_codes_s += (pf_stocks + possess_list)
		update_codes_s = list(set(update_codes_s)) # 重複解消
		#update_codes_s = update_codes_s[:2] # デバッグ用に数を減らす
		# マスター,価格,業績,指標,理論株価を更新
		stocks = update_db_rows(update_codes_s, upd=UPD_INTERVAL, tables=["master", "price", "shihyo", "gyoseki", "rironkabuka"]) #UPD_INTERVAL/UPD_REEVAL
		# 個別でやるとき(テスト用強制)
		#stocks = update_db_rows(update_codes_s, upd=UPD_FORCE, tables=["rironkabuka"])

	#---- 各銘柄のランクデータを更新
	print("---- 各銘柄のランクデータ更新")
	for i, elem in enumerate(stocks_active):
		stock = stocks[elem[0]]
		rank = i+1
		update_stock_rank(stock, rank)
	save_stock_db(stocks) # 更新した順位のDB保存

	#---- 銘柄ランキング用CSVファイル作成
	print("---- CSV項目作成")
	rank_csv = os.path.join(DATA_DIR, "code_rank_data/code_rank.csv")
	
	if os.path.exists(rank_csv):
		latest_csv_dt = get_file_datetime(rank_csv)
		tdy = datetime.today()
		if (tdy-latest_csv_dt).days >= 7:
			backup_csv = os.path.join(DATA_DIR, "code_rank_data/code_rank_%02d%02d%02d.csv"%\
			(latest_csv_dt.year%2000, latest_csv_dt.month, latest_csv_dt.day))
			print("バックアップ:", backup_csv)
			shutil.copy(rank_csv, backup_csv)
	# CSV用項目作成
	rows = []
	rows.append(["ポートフォリオ", "タグ", "決算日", "順位", "過去順位(1日/5日前)", "コード","銘柄名","セクター",\
		"総合PT","プロフィット/クォリティ","バリュー/サイズ","モメンタム(現在.5/20日過去)","ファンダメンタル",\
		"更新日(業績|指標|価格)", "シグナル", "トレンドテンプレート",\
		"ローソク足ボラティリティ(20,5)", "売り圧力レシオ(20,5) 買い集め(週,日) 50DMA乖離率",\
		"業績(今季/今四半期 売上/営利成長率)","進捗率(現四半期/売上(前年)利益(前年)","指標(時価総額|PER|PSR|ROE|売上高営業利益率|有利子負債自己負債比率|自己資本比率)","理論株価(乖離率|上限,下限))","過去業績(5年増収増益 4Q増収増益率)","信用(倍率|出来高買残比)",\
			"テーマ","概要"])

	for i, stock in enumerate(stocks_active):
		stock_data = stocks[stock[0]]
		# 更新日
		date_exp = get_access_dates_expr(stock_data)

		overview = ""
		if "overview" in stock_data:
			overview = stock_data.get('overview',"")
		main_theme = make_market_db.get_major_theme(stock_data.get("themes",""))
		# 決算日
		kessanbi = kessan.get_kessanbi_expr(stock_data)
		# トレンド、押し目
		trend = get_trend_template_expr(stock_data)

		# ボラティリティ、売り圧力レシオ・買い集め指数
		vola, sell_press = get_vola_and_sell_press_expr(stock_data)
		# 順位
		#buffet_url = "https://www.buffett-code.com/company/%s/library"%(stock[0])
		yahoo_url = "https://finance.yahoo.co.jp/quote/%s.T"%(stock[0])
		rank = i+1
		rank = '=HYPERLINK("%s","%d")'%(yahoo_url, rank)		
		#---- ポートフォリオ
		ports = []
		if stock[0] in pf_stocks:
			ports.append("監")
		if stock[0] in possess_list:
			ports.append("保")
		ports = "".join(ports)
		#---- タグ、シグナル
		signal, tags, prev_rank = get_signal_tags_prevrank_expr(stock_data)

		#---- 指標用の項目
		indicator_expr = shihyou.get_shihyo_expr(stock_data)
		credit_expr = shihyou.get_credit_expr(stock_data)

		#---- 業績用項目
		progress_expr, growth_exp = gyoseki.get_gyoseki_expr(stock_data)
		
		# 理論株価
		rironkabuka_expr = rironkabuka.get_rironkabuka_expr(stock_data)
		# 過去業績
		gyoseki_quarity_expr = gyoseki.get_gyoseki_quarity_expr(stock_data)

		#---- その他項目
		code = get_code_exp(stock[0])
		stock_name = get_stock_name_exp(stock_data)
		sector = stock_data.get("sector","")
		#relates_rank = stock_data.get("relates_rank", 0) # 関連銘柄内順位:封印
		rs_log = get_rank_log_expr(stock_data) # RSの表示
		momentum = "%d.%s"%(stock[4],rs_log)
		# 行要素作成
		rows.append([ports, tags, kessanbi, \
			str(rank), prev_rank, code, stock_name, sector, \
			stock[1],  stock[2], stock[3], momentum, stock[5], \
			date_exp, 
			signal, trend, \
			vola, sell_press, growth_exp, progress_expr, indicator_expr, rironkabuka_expr, gyoseki_quarity_expr, credit_expr,\
			main_theme, overview])
	# CSV書き込み
	with open(rank_csv, "w", encoding="utf-8") as f:  # python3対応
		rank_csv_w = csv.writer(f)
		rank_csv_w.writerows(rows)

	# GoogleDriveにアップロード
	if upload_csv:
		import googledrive
		googledrive.upload_csv(rank_csv, "code_rank")

def load_stock_db():
	"""stockDBのロード
	時間がかかる
	"""
	stocks = load_pickle(STOCKS_PICKLE)
	return stocks 

def save_stock_db(stocks):
	save_pickle(STOCKS_PICKLE, stocks)

def delete_db_column(stocks, column):
	for k, stock in stocks.items():
		if column in stock:
			del stock[column]
		#print_dict(stock)	

STOCK_PICKLE_PATH = os.path.join(DATA_DIR, "stock_data", "stock_%s.pickle")
def load_cacehd_stock_db(code_s, force=False):
	""" 基本テスト用
	個別コードのpickleを別途保存したものをロードする
	(str, bool) -> dict
	"""
	stock_path = STOCK_PICKLE_PATH%code_s
	if not os.path.exists(stock_path) or force:
		stocks = memoized_load_pickle(STOCKS_PICKLE)
		stock = stocks.get(code_s, None)
		save_pickle(stock_path, stock)
		return stock
	stock = load_pickle(stock_path)
	return stock

def edit_db():
	backup_db()
	stocks = load_stock_db()
	#delete_db_column(stocks, "access_data")
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
	backup_file(STOCKS_PICKLE, 0)

def reflesh_db():
	"""stock_dbを適切な状態に更新する
	現状は上場廃止銘柄の削除
	"""
	stocks = load_pickle(STOCKS_PICKLE)
	print("DB内銘柄数:", len(stocks))
	for code, stock in list(stocks.items()):
		if stock.get("price", 0) == 0:
			print(code, stock.get("stock_name","不明"), "は上場廃止")
			del stocks[code]
	print("削除後DB内銘柄数:", len(stocks))

	#code_list = portfolio.parse_my_portforio()
	save_pickle(STOCKS_PICKLE, stocks)

def convert_code_db():
	"""既存のintキーのstockDBを英数字コードキーにコンバート
	"""
	stocks_bk = load_pickle("stock_data/stocks_back.pickle")
	stocks = {}
	for code, stock in list(stocks_bk.items()):
		code_s = format(code, "04d")
		stocks[code_s] = stock
	save_stock_db(stocks)

def test():
	# code = 6560
	# stock_db = load_stock_db()
	# stock_data = stock_db[code]
	# rank_log = stock_data.get("stock_rank_log",[])
	# print rank_log
	# rank0 = get_rank_log(stock_data, "stock_rank_log", 0)
	# rank1 = get_rank_log(stock_data, "stock_rank_log", 1)
	# rank5 = get_rank_log(stock_data, "stock_rank_log", 5)
	# #print "Rank:", stock[0], rank0, rank1, rank5
	# price_log = stock_data.get("price_log",[])
	# print price_log
	# pr0 = price.get_price_log(price_log, rank0[0])
	# pr1 = price.get_price_log(price_log, rank1[0])
	# pr5 = price.get_price_log(price_log, rank5[0])

	# RSログ表示のテスト
	code = "9343"
	stock_data = load_cacehd_stock_db(code)
	print((get_rank_log_expr(stock_data)))

	# DBリフレッシュ用
	# stocks = load_stock_db()
	# print "before:", len(stocks), "個"
	# for code, stock in stocks.items():
	# 	if type(code) == int:
	# 		print "%dを削除"%code
	# 		del stocks[code]
	# 	else:
	# 		#print code
	# 		pass
	# print "after:", len(stocks), "個"
	# save_stock_db(stocks)

#==================================================
# pickleの文字コード変換
#================================================== 
def _fix_str(obj):
	if isinstance(obj, dict):
		return {_fix_str(k): _fix_str(v) for k, v in obj.items()}
	elif isinstance(obj, list):
		return [_fix_str(i) for i in obj]
	elif isinstance(obj, tuple):
		return tuple(_fix_str(i) for i in obj)
	elif isinstance(obj, set):
		return set(_fix_str(i) for i in obj)
	elif isinstance(obj, frozenset):
		return frozenset(_fix_str(i) for i in obj)
	elif isinstance(obj, str):
		try:
			# Python 3 では、Python 2 の str は latin1 として読み込まれている
			return obj.encode('latin1').decode('utf-8')
		except Exception:
			return obj
	else:
		return obj

def convert_pickle_latin1_to_utf8(old_path, new_path):
    """ 古いpickleファイルを読み込み、latin1からutf-8に変換して保存する
    Args:
    old_path (str): 変換元のpickleファイルパス
    new_path (str): 変換後のpickleファイルパス
    """
    with open(old_path, 'rb') as f:
        raw = pickle.load(f, encoding='latin1')
    fixed = _fix_str(raw)
    with open(new_path, 'wb') as f:
        pickle.dump(fixed, f) # protocol=4
    print("UTF-8変換完了:", new_path)

def convert_python2():
	STOCKS_PICKLE_PY2 = os.path.join(DATA_DIR, "stock_data", "stocks_py2.pickle")
	convert_pickle_latin1_to_utf8(
		STOCKS_PICKLE_PY2, STOCKS_PICKLE)
	
#==================================================
# main
#==================================================
def main():
	"""
	株価DBを更新するメインスクリプト
	"""
	#TODO: REEVALで必ず通信する？
	#TODO: リストで本日の価格を表示
	#command = "edit"
	#command = "backup"
	command = "list_all_db" # デフォ
	#command = "update"
	#command = "update_all_db"
	#command = "list"
	#command = "reflesh"
	#command = "test"
	#command = "convert_code"
	#command = "convert_python2"
	if command == "update":
		code_list = "4417"
		#code_list = "2979 3226 4384 4434 4443 4448 4449 4475 4477 4478 4479 4480 4483 4485 4488 4490 4493 4599 6835 7071"
		code_list = code_list.split()
		# f = open("update_code_list.txt")
		# lines = f.readlines()
		# code_list = [l.strip() for l in lines]
		# f.close()		
		tables = None 
		#tables = ["master"]
		#tables = ["price"]
		#tables = ["shihyo"]
		#tables = ["gyoseki"]
		#tables = ["rironkabuka"]
		update_db_rows(code_list, upd=UPD_FORCE, tables=tables) #UPD_FORCE/UPD_REEVAL/UPD_INTERVAL
	elif command == "list":
		# DB内銘柄情報表示
		code_list = "4483"
		#code_list = "3242 3686 6058 6432 7435"
		code_list = code_list.split()
		list_db(code_list)
	elif command == "list_all_db":
		# DBの情報をランキングで表示する
		UPLOAD_CSV = True # True/False
		UPDATE_PORTFOLIO = True
		list_all_db(UPLOAD_CSV, UPDATE_PORTFOLIO)
	elif command == "edit":
		edit_db()
	elif command == "backup":
		backup_db()
	elif command == "update_all_db":
		#　対象コードを取得
		def get_code_list_from_db(min=1000,max=10000):
			stocks = load_pickle(STOCKS_PICKLE)
			code_list = list(stocks.keys())#[400:]
			code_list.sort()
			code_list = [c for c in code_list if c>=min and c<= max]
			return code_list
		#code_list = get_code_list_from_db(1500, 10000)
		code_list = get_code_list_from_db(1000, 10000)
		current = 0 # 途中からやるときはここを書き換え
		while current < len(code_list):
			num = 500
			current_code_list = code_list[current:current+num]
			print("%d~%d/%dを更新します"%(current_code_list[0], current_code_list[-1], len(code_list)))
			# 何を更新する？
			#tables = ["gyoseki", "shihyo", "master"]
			tables = ["price"]
			#tables = ["master"]
			update_db_rows(current_code_list, upd=UPD_REEVAL, tables=tables) #UPD_REEVAL/UPD_FORCE
			print("%d/%dまで更新しました"%(current+num, len(code_list)), current_code_list[-3:])
			current += num
			break #とりあえずテスト
	elif command == "reflesh":
		# 価格が取得できないのは上場廃止銘柄
		backup_db()
		reflesh_db()
	elif command == "convert_code":
		convert_code_db()
	elif command == "test":
		test()
	elif command == "convert_python2":
		convert_python2()

# TODO: エラーを記述するようにせんと・・
if __name__ == '__main__':
	#TODO: 古い日付のタグは無効にしたい　全銘柄DB更新せず表示するときの判断でいいかも
	#TODO: 監視タグも
	#TODO: セクターのRSランキングを作成し、参照したい オニールのIBD
	#https://kabutan.jp/warning/?mode=9_1&market=0&capitalization=-1&stc=zenhiritsu&stm=1&col=zenhiritsu	
	#やりたいが保留
	# カレントディレクトリをこの.pyの場所に
	path = os.path.abspath(os.path.dirname(__file__))
	os.chdir(path)
	cwd = os.getcwd()
	main()
	os.chdir(cwd)
