#!/usr/bin/env python3	

import re
import requests

from ks_util import *

# def calc_growth(uriage_zenki, uriage_raiki):
# 	growth = (float(uriage_raiki)/uriage_zenki)**0.5 if uriage_zenki > 0 else 1.0
# 	growth = min(growth, 1.25)
# 	return growth

# def calc_rironkabuka(bps, eps, uriage_zenki, uriage_raiki):
# 	"""
# 	rironkabuka = F72+(G72*(B72)^$A$2/(1+$B$2/100)^$A$2-($B$2/100)*F72)/($B$2/100)
# 	"""
# 	growth = calc_growth(uriage_zenki, uriage_raiki)
# 	print "成長率：",round(growth,3)

# 	A2 = 5 # $1益反映年数
# 	B2 = 6.6 # $1引率
# 	C2 = 0.6 # $1期金利
# 	D2 = 6 # $1スクプレミアム
# 	val = bps+(eps*(growth**A2)/(1+B2/100)**A2-((B2+1)/100)*bps)/(B2/100)
# 	return int(val)

def get_gyoseki_data(code, cache=True):
	# TODO: このあたり関数化？
	fname = "stock_data/kabutan_gyoseki_%d.txt"%code
	if cache and os.path.exists(fname):
		text = ""
		with open(fname, 'r') as f:
			text = f.read()
		return text

	URL = "http://kabutan.jp/stock/finance?code=%d&mode=k"%code
	print("----> %dのkabutanから業績情報を取得します・・"%code)
	r = requests.get(URL)
	print("<---- 取得完了")
	text = r.text.encode('utf-8')
	with open(fname, 'w') as f:
		f.write(text)
	return text


def is_cache_latest(url, interval_day):	
	"""
	URLアクセス時のhtmlキャッシュがあるかどうか(汎用)
	"""
	cach_path = get_http_cachname(url)
	cach_path = os.path.join(KABUTAN_CACHE_DIR, cach_path)
	if not os.path.exists(cach_path):
		print("キャッシュがない:", cach_path)
		return False
	cach_date = get_file_datetime(cach_path)
	timedelta = datetime.today()-cach_date
	print("  キャッシュ:", cach_date)
	if timedelta.days < interval_day:
		return True
	else:
		return False


KABUTAN_CACHE_DIR = os.path.join(DATA_DIR, "stock_data", "kabutan")
KABUTAN_URL_CODE = "http://kabutan.jp/stock/finance?code=%s&mode=k"
KABUTAN_BASE_URL_CODE = "https://kabutan.jp/stock/?code=%s"


def get_kabutan_html(code_s, upd=UPD_INTERVAL):
	"""株探から決算情報htmlをDL取得する
	type: (str,bool)->str
	"""
	if not os.path.exists(KABUTAN_CACHE_DIR):
		os.mkdir(KABUTAN_CACHE_DIR)

	if upd == UPD_CACHE:
		use_cache = True
	elif upd == UPD_FORCE:
		use_cache = False
	else:
		INTERVAL_DAY = 5 # この設定は微妙
		use_cache = is_cache_latest(KABUTAN_URL_CODE%(str(code_s)), INTERVAL_DAY)
	html = http_get_html(KABUTAN_URL_CODE%(str(code_s)), 
		cache_dir=KABUTAN_CACHE_DIR, use_cache=use_cache)
	for c in range(3):
		if "Service Temporarily Unavailable" in html:
			if c >= 2:
				print("!!! やっぱりだめみたいなので中止")
				return {}
			print("取得エラーのため再度取得", c)
			import time
			time.sleep(c+1)
			html = http_get_html(KABUTAN_URL_CODE%(str(code_s)), 
				use_cache=False, cache_dir=KABUTAN_CACHE_DIR)
		else:
			break
	return html

def get_kabutan_base_html(code_s, upd=UPD_INTERVAL):
	"""
	株探から基本情報htmlを取得
	"""
	if not os.path.exists(KABUTAN_CACHE_DIR):
		os.mkdir(KABUTAN_CACHE_DIR)

	if upd == UPD_CACHE:
		use_cache = True
	elif upd == UPD_FORCE:
		use_cache = False
	else:
		use_cache = is_cache_latest(KABUTAN_BASE_URL_CODE%(str(code_s)), upd)

	html = http_get_html(KABUTAN_BASE_URL_CODE%(str(code_s)), 
		cache_dir=KABUTAN_CACHE_DIR, use_cache=use_cache)
	for c in range(3):
		if "Service Temporarily Unavailable" in html:
			if c >= 2:
				print("!!! やっぱりだめみたいなので中止")
				return {}
			print("取得エラーのため再度取得", c)
			import time
			time.sleep(c+1)
			html = http_get_html(KABUTAN_BASE_URL_CODE%(str(code_s)), use_cache=False, cache_dir=KABUTAN_CACHE_DIR)
		else:
			break
	return html

def get_kabutan_cachename(code_s):
	cache_fname = get_http_cachname(KABUTAN_BASE_URL_CODE%(str(code_s)))
	return os.path.join(KABUTAN_CACHE_DIR, cache_fname)

# def get_from_kabutan2(html):
# 	"""
# 	株探htmlから解析情報を返す
# 	"""
# 	# ------------------------------
# 	# eps,売上
# 	# ------------------------------
# 	year_tbl_m = re.search(r'<div class="title1">通期</div>.*?<table>(.*?)</table>', html, re.S)
# 	if not year_tbl_m:
# 		print "!!! 通期テーブルが取得できない（フォーマット変更？）"
# 		return {}
# 	year_tbl_html = year_tbl_m.group(1)
# 	table = []
# 	for year_row_m in re.finditer(r'<tr >(.*?)</tr>', year_tbl_html, re.S):
# 		# print "Hoge", year_row_m.group(1)
# 		values = []
# 		for val_m in re.finditer(r'<td.*?>(.*?)</td>', year_row_m.group(1)):
# 			val = val_m.group(1)
# 			values.append(val)
# 		# print values
# 		table.append(values)
# 	# eps 4列目
# 	if table[-1][4] == "－":
# 		eps = table[-2][4]
# 		print "来季EPSが取得できないため今季"
# 	else:
# 		eps = table[-1][4]
# 	print "eps:", eps
# 	# 売上 0列目
# 	if table[-1][0] == "－":
# 		raiki_uriage = table[-2][0]
# 		print "来季売上が取得できないため今季"
# 	else:
# 		raiki_uriage = table[-2][0]
# 	konki_uriage = table[-2][0]
# 	zenki_uriage = table[-3][0]
# 	print "売上:", zenki_uriage, konki_uriage, raiki_uriage
# 	# TODO: bps
# 	# ------------------------------
# 	# bps
# 	# ------------------------------
# 	year_tbl_m = re.search(r'<div class="cap1"><h3>財務 【実績】</h3></div>.*?<table>(.*?)</table>', html, re.S)
# 	if not year_tbl_m:
# 		print "!!! 財務テーブルが取得できない（フォーマット変更？）"
# 		return {}
# 	year_tbl_html = year_tbl_m.group(1)
# 	# print year_tbl_html
# 	table = []
# 	for year_row_m in re.finditer(r'<tr >(.*?)</tr>', year_tbl_html, re.S):
# 		# print "Hoge", year_row_m.group(1)
# 		values = []
# 		for val_m in re.finditer(r'<td>(.*?)</td>', year_row_m.group(1)):
# 			val = val_m.group(1)
# 			values.append(val)
# 		# print values
# 		table.append(values)
# 	# bps
# 	if table[-1][0] == "－":
# 		if table[-2][0] == "－":
# 			bps = "0"
# 			print "!! bpsが取得できない"
# 		else:
# 			bps = table[-2][0]
# 	else:
# 		bps = table[-1][0]
# 	print "bps:", bps
# 	dic = {}
# 	dic["bps"] = float(bps.replace(",",""))
# 	dic["eps"] = float(eps.replace(",",""))
# 	dic["uriage_zenki"] = float(zenki_uriage.replace(",",""))
# 	dic["uriage_konki"] = float(konki_uriage.replace(",",""))
# 	dic["uriage_raiki"] = float(raiki_uriage.replace(",",""))
# 	return dic

def get_from_kabutan3(html):
	"""株探htmlから理論株価計算のための解析情報を返す
	"""
	# ------------------------------
	# eps
	# ------------------------------
	# 予想経常利益/発行済株式数
	year_tbl_m = re.search(r'<div class="title1">通期</div>.*?<table>(.*?)</table>', html, re.S)
	if not year_tbl_m:
		print("!!! 通期テーブルが取得できない（フォーマット変更？）")
		return {}
	year_tbl_html = year_tbl_m.group(1)
	# 各期の数字をテーブルに
	table = []
	for year_row_m in re.finditer(r'<tr >(.*?)</tr>', year_tbl_html, re.S):
		values = []
		for val_m in re.finditer(r'<td.*?>(.*?)</td>', year_row_m.group(1)):
			val = val_m.group(1)
			values.append(val)
		table.append(values)
	def get_table_value(tbl, column, row, name=""):
		ind = -1
		try:
			if tbl[column][row] == "－":
				val = tbl[column-1][row]
				ind = column-1
				if val == "－":
					print("今季来季とも取得できないため0")
					val = "0"
					ind = -1
				else:
					print("来季の%s値が取得できないため今季"%name)
			else:
				val = tbl[column][row]
				ind = column
		except IndexError:
			print("テーブルの値が取得できないため0")
			val = "0"
		return val, ind
	# 経常利益
	isKonki = False
	keijo,ind = get_table_value(table, -1, 2, "経常利益")
	keijo = float(keijo.replace(',',''))
	if ind < -1:
		isKonki = True
	print("経常利益:", keijo)
	# 最終利益
	profit,ind = get_table_value(table, -1, 3, "最終利益")
	profit = float(profit.replace(',',''))
	if ind < -1:
		isKonki = True
	print("最終利益:", profit)
	# EPS
	eps,eps_column = get_table_value(table, -1, 4, "一株益")
	eps = float(eps.replace(',',''))
	if ind < -1:
		isKonki = True
	print("EPS:", eps)
	# 発行株式数を取得
	issued_stock = 0
	if eps != 0:
		profit_,_ = get_table_value(table, eps_column, 3, "最終利益")
		profit_ = float(profit_.replace(',',''))
		issued_stock = profit_/eps
	else:
		eps,_ = get_table_value(table, -2, 4, "一株益")
		eps = float(eps.replace(',',''))
		if eps != 0:			
			profit,_ = get_table_value(table, -2, 3, "最終利益")
			profit = float(profit.replace(',',''))
			issued_stock = profit/eps
	print("発行済株式数:", issued_stock) # 単位: 百万株
	# 修正EPS(経常利益から計算)
	if issued_stock > 0:
		mod_eps = 0.7*keijo/issued_stock # 単位: 円
	else:
		mod_eps = 0
	print("修正EPS:", mod_eps)

	# ------------------------------
	# bps
	# ------------------------------
	year_tbl_m = re.search(r'<div class="cap1"><h3>財務 【実績】</h3></div>.*?<table>(.*?)</table>', html, re.S)
	if not year_tbl_m:
		print("!!! 財務テーブルが取得できない（フォーマット変更？）")
		return {}
	year_tbl_html = year_tbl_m.group(1)
	table = []
	for year_row_m in re.finditer(r'<tr >(.*?)</tr>', year_tbl_html, re.S):
		values = []
		for val_m in re.finditer(r'<td>(.*?)</td>', year_row_m.group(1)):
			val = val_m.group(1)
			values.append(val)
		table.append(values)
	# 自己資本比率
	equity_ratio,_ = get_table_value(table, -1, 1, "equity_ratio")
	print("自己資本比率:", equity_ratio)
	try:
		equity_ratio = float(equity_ratio.replace(",",""))
	except ValueError as e:
		print("!!! 自己資本比率が取得できない", equity_ratio)
		return {}

	# 今季bps
	# bps = get_table_value(table, -1, 0, "bps")
	bps = table[-1][0]
	if bps == "－":
		# 自己資本
		self_asset = table[-1][3]
		if self_asset != "－" :
			if issued_stock > 0:
				print("自己資本:", self_asset)
				self_asset = float(self_asset.replace(",",""))	
				bps = self_asset/issued_stock
				print("BPSが未発表のため自己資本から計算")
		else:
			bps = table[-2][0]
			bps = float(bps.replace(",",""))
	else:
		bps = float(bps.replace(",",""))
	print("BPS:", bps)

	# ---- 価格
	#<span class="kabuka">2,478円</span>
	m = re.search(r'<span class="kabuka">(.*?)円</span>', html)
	try:
		val = m.group(1)
		price = int(float(val.replace(",","")))
	except (ValueError, AttributeError):
		print("　株価取得できず", val)
		price = 0

	dic = {}
	dic["bps"] = bps
	dic["mod_eps"] = mod_eps
	dic["equity_ratio"] = equity_ratio
	dic["price"] = price
	dic["isKonki"] = isKonki
	return dic

def calc_theory_price(bps, eps, equity_ratio, price=0, preceding_eps=None):
	"""はっしゃん式理論株価を計算して返す
	Args:
	Returns:
		tupple<int>: [理論株価, 理論株価上限, 理論株価下限, 先行理論株価]
	"""
	# 理論株価
	# BPS: 財務の1株純資産
	# 発行済株式数: 自己資本/BPS
	# EPS: (MPER)当期経常利益x0.7/発行済株式数
	# 自己資本比率: 自己資本/総資産
	# ROA: 当期純利益/総資産
	# 先行指標: EPSに見込み利益(進捗率をもとに)を使用
	discount_rate = step_func(equity_ratio, [0, 10, 33, 50, 67, 80], \
		[50,60,65,70,75,80])
	asset_value = bps*discount_rate/100 # 解散価値
	eps = min(eps, bps*0.6) # 過小資本銘柄の過剰評価防止
	if equity_ratio > 0:
		mbps = bps/(equity_ratio/100) # 1株総資産
	else:
		mbps = bps
	if mbps > 0:
		roa = abs(eps/mbps) # roaは割合として使うため正の数に
	else:
		roa = 0
	roa = min(roa, 0.3)
	zaimu_correction = step_func(equity_ratio, [33,40,50,66], \
		[1.5, 1.36, 1.2, 1])
	enterprise_value = eps * roa * 150 * zaimu_correction # 事業価値
	preceding_enterprise_value = None
	if preceding_eps is not None:
		preceding_enterprise_value = preceding_eps * roa * 150 * zaimu_correction
	# リーマンショックリスク補正率
	risk_correction = 1.0
	if price > 0 and bps > 0:
		pbr = price/bps
		risk_correction = step_func(pbr, \
			[0, 0.03, 0.20, 0.25, 0.33, 0.40, 0.50], \
			[0.01, 0.1, 0.33, 0.5, 0.66, 0.8, 1.0]) # 比率
		if risk_correction < 1:
			print("リスク補正:", risk_correction)
	theory_price = (asset_value + enterprise_value)*risk_correction
	theory_price_preceding = None
	if preceding_enterprise_value is not None:
		theory_price_preceding = (asset_value + preceding_enterprise_value)*risk_correction
		theory_price_preceding = max(int(theory_price_preceding), 0)
	theory_price_up = (asset_value + 2*enterprise_value)*risk_correction
	theory_price_down = asset_value*risk_correction
	return max(int(theory_price), 0), max(int(theory_price_up), 0), max(int(theory_price_down), 0), theory_price_preceding

def get_preceding_eps(eps, quarter, profit, profit_pre):
	"""
	Args:
		profit(int): 当期利益進捗率
		prifit_pre(int): 前期利益進捗率
	Returns:
		float or None: 進捗率ベース予想EPS
	"""
	# 通期進捗率予想を前期実績から
	if profit and profit_pre:
		progress_predict = profit + (100-profit_pre)
		return eps * progress_predict/100
	else:
		return None

def analyze_from_kabutan(code_s, upd=UPD_INTERVAL, stock=None):
	""" 株探業績htmlから理論株価を計算する
	Args:
		code(str): 銘柄コード
	Returns:
		tuple<int>: [理論株価、上限株価、下限株価、先行理論株価, 価格, 今期理論株価かどうか]
		"""
	# https://kabutan.jp/stock/finance?code=3825&mode=k
	# code = int(code)
	html = get_kabutan_html(code_s, upd)
	# dic = get_from_kabutan2(html)
	# htmlから理論株価に必要なデータ解析
	# BPS:財務の一株純資産(前期末) EPS:一株利益(今季)
	dic = get_from_kabutan3(html)
	calced = False
	if dic:
		# ---- 理論株価計算
		# 先行指標用の数値を取得
		preceding_eps = None
		if stock:
			import gyoseki
			progress = gyoseki.calc_progress_rate(stock)
			preceding_eps = get_preceding_eps(dic["mod_eps"], progress.get("quarter"), progress.get("profit"), progress.get("profit_pre"))
		# 必要な値を渡して理論株価計算
		try:
			theory_price = calc_theory_price(dic["bps"], dic["mod_eps"], dic["equity_ratio"], dic["price"], preceding_eps)
			print("理論株価:", theory_price)
			theory_price = theory_price+(dic["price"],dic["isKonki"])
			calced = True
		except TypeError:
			pass
	if not calced:
		print("!!! 理論株価計算できず")
		theory_price = (0,0,0,0,0, False)
	return theory_price
	


def get_rironkabuka_data(code_s, upd=UPD_INTERVAL, stock=None):
	"""指定codeの理論株価関連データを分析計算してdictに格納して返す
	Args:
		code(int): 銘柄コード
		upd(int): 更新頻度
	Returns:
		dict<str(key), any>(理論株価データ):
		key = rironkabuka, rironkabuka_up, rironkabuka_down, rironkabuka_preceding, access_date_rironkabuka ,code
	"""
	print("-"*5, "理論株価の計算 upd:", upd)
	tables = {}
	# 決算htmlを解析して理論株価に必要なデータを取得
	res = analyze_from_kabutan(code_s, upd, stock)
	tables["rironkabuka"] = res[0]
	tables["rironkabuka_up"] = res[1]
	tables["rironkabuka_down"] = res[2]
	tables["rironkabuka_preceding"] = res[3]
	print("="*5, "理論株価の計算完了", tables["rironkabuka"])
	# 理論株価作成時間の格納
	cach_path = get_http_cachname(KABUTAN_URL_CODE%(str(code_s)))
	cach_path = os.path.join(KABUTAN_CACHE_DIR, cach_path)
	tables["access_date_rironkabuka"] = get_file_datetime(cach_path)
	print("date:", tables["access_date_rironkabuka"])
	# tables["code"] = code
	set_db_code(tables, code_s)
	tables["isKonki"] = res[5]
	return tables

def get_rironkabuka_kairi_fromprice(theory_price, theory_price_up, theory_price_down, therory_price_preceding, price):
	"""理論株価乖離率の計算
	Args:
		theory_price(int):理論株価
	Returns:
		tuple<int>: 理論株価乖離率(%) (理論株価、上限、下限、先行)
	"""
	if not price:
		return 0,0,0,0
	kairi = 100*(theory_price-price)/price 
	kairi_up = 100*(theory_price_up-price)/price 
	kairi_down = 100*(theory_price_down-price)/price 
	if therory_price_preceding is not None and therory_price_preceding > 0:
		kairi_preceding = 100*(therory_price_preceding-price)/price 
	else:
		kairi_preceding = None
	return kairi, kairi_up, kairi_down, kairi_preceding

def get_rironkabuka_kairi(stock):
	"""銘柄データから理論株価乖離率を求める
	"""
	# 理論株価(乖離率,上限乖離,下限乖離,先行乖離)
	theory_price = stock.get("rironkabuka", 0)
	theory_price_up = stock.get("rironkabuka_up", 0)
	theory_price_down = stock.get("rironkabuka_down", 0)
	theory_price_preceding = stock.get("rironkabuka_preceding", 0)
	price = stock.get("price", 0)
	kairi_lst = get_rironkabuka_kairi_fromprice(theory_price, theory_price_up, theory_price_down, theory_price_preceding, price)
	return kairi_lst

def calc_theory_pt(code_s, stock=None):
	"""理論株価評価ポイントの計算
	"""
	theory_pt = theory_proceding_pt = theory_up_pt = theory_down_pt = 0
	theory_price = analyze_from_kabutan(code_s, UPD_CACHE, stock)
	theroy, theory_up, theory_down, theory_proceding = get_rironkabuka_kairi_fromprice(*theory_price[0:5]) # theory_priceの最後の要素は不要
	print("理論価格乖離: (%d %s %d %d)"%(theroy, str(theory_proceding) if theory_proceding is not None else "-", theory_up, theory_down))
	THEORY_MAX = 40
	THEORY_UP_MAX = 20
	THEORY_DOWN_MAX = 10
	theory_proceding_pt = 0
	if not (theory_price[4] == 0) and theory_price[0] != 0: # 価格がある
		if theory_proceding is not None:
			ratio = float(theory_price[3])/theory_price[0]
			if ratio > 1.2:
				theory_proceding_pt = 20
			elif ratio > 1.05:
				theory_proceding_pt = 10
			elif ratio > 0.95:
				theory_proceding_pt = 0
			else:
				theory_proceding_pt = -10
		# 実際のポイント計算
		theory_pt = step_func(theroy, [0, 20, 40, 60, 100], [20, 40, 60, 80, 100], 0)*THEORY_MAX/100
		# theory_proceding_pt = step_func(theory_proceding, [0, 20, 40, 60, 100], [20, 40, 60, 80, 100], 0)*THEORY_PROCEDING_MAX/100
		theory_up_pt = step_func(theory_up, [30, 60, 100, 150, 300], [20, 40, 60, 80, 100], 0)*THEORY_UP_MAX/100
		theory_down_pt = step_func(theory_down, [-60, -30, 0, 30], [25, 50, 75, 100], 0)*THEORY_DOWN_MAX/100
	else:
		print("!!! 価格がないため理論PT計算できず")
	# thoery_total_pt = int(0.5*theory_pt + 0.35*theory_up_pt + 0.15*theory_down_pt)
	print("理論価格pt: %d/%d"%(theory_pt, THEORY_MAX))
	print("理論価格先行pt: %d"%(theory_proceding_pt))
	print("理論価格上限pt: %d/%d"%(theory_up_pt, THEORY_UP_MAX))
	print("理論価格下限pt: %d/%d"%(theory_down_pt, THEORY_DOWN_MAX))
	return theory_pt+theory_proceding_pt+theory_up_pt+theory_down_pt

def _get_rironkabuka_expr(kairi, kairi_up, kairi_down, kairi_preceding, isKonki):
	"""理論株価乖離率の文字列表現を返す
	"""
	preceding_expr = "%d"%kairi_preceding if kairi_preceding is not None else "-" 
	isKonkiMark = "△" if isKonki else ""
	expr = "%s%d%%(%s%%)|%d%%,%d%%"%(isKonkiMark, kairi, preceding_expr, kairi_up, kairi_down)
	return expr

def get_rironkabuka_expr(stock):
	"""理論株価乖離率の文字列表現を返す
	"""
	kairi_list = get_rironkabuka_kairi(stock)
	isKonki = stock.get("isKonki", False) # 今期データか通期データか
	kairi_list = kairi_list+(isKonki,)
	return _get_rironkabuka_expr(*kairi_list)

def get_rironkabuka_expr2(record, price):
	# 理論株価文字列表現テスト用
	kairi_list = get_rironkabuka_kairi_fromprice(record["rironkabuka"], record["rironkabuka_up"], record["rironkabuka_down"], record["rironkabuka_preceding"], price)
	kairi_list = kairi_list+(record["isKonki"],)
	return _get_rironkabuka_expr(*kairi_list)

def main():
	# TODO: 理論株価PTや進捗率はDB保持にしたほうがよいかも
	# 3920, 4493, 4595, 2389, 7270, 5032, 6096, 4169,6195,7808,2410,9107,9264
	code_list = ["9343"]
	for code_s in code_list:
		import make_stock_db as db
		stock = db.load_cacehd_stock_db(code_s)
		upd = UPD_INTERVAL # UPD_FORCE
		record = get_rironkabuka_data(code_s, upd, stock)
		# calc_theory_pt(code, stock)

		# print get_rironkabuka_expr(stock)
		print(get_rironkabuka_expr2(record, stock.get("price")))

if __name__ == '__main__':
	main()
