#!/usr/bin/python
# -*- coding: utf-8 -*-
#================================================
# 市場の新高値銘柄を分析
#================================================

#import xml.etree.ElementTree as xml # <-htmlはwellformedじゃないので解析不可
#from lxml import etree # lxmlは後で勉強する
from datetime import datetime
from datetime import timedelta
from datetime import date
import re
import csv

from ks_util import *
import make_sisu_data
import make_sector_data
import make_stock_db

URL_N225 = "http://finance.yahoo.com/q/hp?s=%5EN225+Historical+Prices"
URL_DOW = "http://finance.yahoo.com/q/hp?s=%5EDJI+Historical+Prices"

def is_latest_info(url):
	"""
	取得済みのURLキャッシュが最新か確認
	str -> bool[True:最新 False:最新でない]
	"""
	latest = False
	cach_path = "market_data/%s"%get_http_cachname(url)
	if os.path.exists(cach_path):
		cache_dt = get_file_datetime(cach_path)
		if cache_dt.hour < 16:
			cache_dt -= timedelta(1)
		now_dt = datetime.today()
		if now_dt.hour < 16:
			now_dt -= timedelta(1)
		print "最新html:", cache_dt.isoformat()[:13], "必要", now_dt.isoformat()[:13], "diff:",(now_dt-cache_dt)
		if (date(now_dt.year, now_dt.month, now_dt.day)-
			date(cache_dt.year, cache_dt.month, cache_dt.day)).days > 0:
		#if now_dt.day > cache_dt.day:
			latest = True
	else:
		print cach_path+"はない"
	return latest

def get_n225_data():
	use_cache = not is_latest_info(URL_N225)
	#print "use_cache", use_cache
	html = http_get_html(URL_N225, cache_dir="market_data", use_cache=use_cache) #latest=True
	#print ux_cmd_head(html)
	is_all_price = True
	price_list = make_sisu_data.parse_html_yahoo_us(html, is_all_price=is_all_price)
	# 本日の値
	m = re.search(r'<span class="time_rtq_ticker"><span id=".*?">(.*?)</span></span>', html)
	if m and not is_all_price:
		price = float(m.group(1).replace(",",""))
		if price != price_list[0][1]:
			price_list.insert(0, ["today", price])
	#print price_list[:3]
	for i, price in enumerate(price_list):
		try:
			if price_list[i-1][1]/price[1] >= 90 or price_list[i+1][1]/price[1] >= 90:
				print price, "は間違いデータ?"
				price_list[i][1] *= 100
				price_list[i][2] *= 100
				price_list[i][3] *= 100
				price_list[i][4] *= 100
				print "->", price_list[i]
		except IndexError:
			pass
	return price_list

def get_dow_data():
	use_cache = not is_latest_info(URL_DOW)
	html = http_get_html(URL_DOW, cache_dir="market_data", use_cache=use_cache) #latest=True
	is_all_price = True
	price_list = make_sisu_data.parse_html_yahoo_us(html, is_all_price=is_all_price)
	m = re.search(r'<span class="time_rtq_ticker"><span id=".*?">(.*?)</span></span>', html)
	if m and not is_all_price:
		price = float(m.group(1).replace(",",""))
		print "price:", price, price_list[0][1]
		if price != price_list[0][1]:
			price_list.insert(0, ["today", price])
	#print price_list[:3]
	return price_list

def analyze_market():
	"""
	日経平均とダウの10日移動平均を出力
	"""
	n225_data = get_n225_data()
	#price10 = [e[1] for e in n225_data[:10]]
	price10 = [e[4] for e in n225_data[:10]]
	n225_ma10 = int(average(price10))
	n225_current = int(n225_data[0][4])
	n225_mark = "◯" if n225_current>=n225_ma10 else "×"
	n225_sp_ratio = make_stock_db.calc_sell_pressure_ratio(n225_data)

	dow_data = get_dow_data()
	price10 = [e[4] for e in dow_data[:10]]
	dow_ma10 = int(average(price10))
	dow_current = int(dow_data[0][4])
	dow_mark = "◯" if dow_current>=dow_ma10 else "×"
	dow_sp_ratio = make_stock_db.calc_sell_pressure_ratio(dow_data)
	#print dow_sp_ratio

	# 表示
	print "%s日経平均:(10ma %+.1f%%) "%(n225_mark, (float)(n225_current-n225_ma10)*100/n225_ma10),
	print "%d(%+.1f%%) %d "%(n225_current, (n225_data[0][4]-n225_data[1][4])*100/n225_data[1][4], n225_ma10),
	print "SP_RATIO(20,10)=(%.1f %.1f) R_VOL=%.2f"%tuple(n225_sp_ratio)
	print "%sダウ　　:(10ma %+.1f%%) "%(dow_mark, (float)(dow_current-dow_ma10)*100/dow_ma10), 
	print "%d(%+.1f%%) %d "%(dow_current, (dow_data[0][4]-dow_data[1][4])*100/dow_data[1][4], dow_ma10),
	print "SP_RATIO(20,10)=(%.1f %.1f) R_VOL=%.2f"%tuple(dow_sp_ratio)

def read_csv_table(csv_name):
	csvr = csv.reader(open(csv_name))
	rows = []
	for row in csvr:
		rows.append(row)
		#print row
	return rows

def analyze_shintakane():
	"""
	新高値リストのセクター分析
	"""
	#pass
	# 新高値リストソースの作成
	current_day = datetime.today()
	counter = 0
	newhigh_list = []
	for _ in range(100):
		day_fmt = current_day.isoformat()[2:10].replace("-","")
		fname = "shintakane_data/shintakane_%s.csv"%day_fmt
		if os.path.exists(fname):
			table = read_csv_table(fname)
			newhigh_list.append([day_fmt, table])
			#print fname, counter, len(table)
			#break
			counter += 1

		current_day -= timedelta(1) # 前日
		if counter >= 30:
			break
	# 各期間での新高値リストを作成
	day1_code_list = []
	def get_code_list(days=1):
		code_list = []
		for i in range(days):
			try:
				table = newhigh_list[i][1]
				code_list.extend([int(row[1].split()[0]) for row in table])
			except IndexError as e:
				print "新高値リスト不足:", i, len(newhigh_list)
		code_list = list(set(code_list))
		return code_list
	day1_code_list = get_code_list(1)
	week1_code_list = get_code_list(5)
	month1_code_list = get_code_list(20)
	#print month1_code_list
	print "d,w,m=(%d %d %d)"%(len(day1_code_list), len(week1_code_list), len(month1_code_list))
	# 集計
	sector_tables = make_sector_data.load_pickle(make_sector_data.PATH_SECTOR_DB)
	sector_newhigh_list = {}
	for key in sector_tables.iterkeys():
		sector_newhigh_list[key] = [[0,[]] for _ in range(3)]
	sector_newhigh_list["unknown"] = [[0,[]]]*3

	for code in day1_code_list:
		sector = make_sector_data.get_sector_detail(code)
		row = sector_newhigh_list[sector][0]
		row[0] += 1
		row[1].append(code)
	for code in week1_code_list:
		sector = make_sector_data.get_sector_detail(code)
		row = sector_newhigh_list[sector][1]
		row[0] += 1
		row[1].append(code)
	for code in month1_code_list:
		sector = make_sector_data.get_sector_detail(code)
		row = sector_newhigh_list[sector][2]
		row[0] += 1
		row[1].append(code)
	#print sector_newhigh_list["unknown"]
	def print_list(lst):
		for l in lst:
			print l,
		print
	# 銘柄名わからんのは更新取得する
	print "-"*10, "銘柄名不明リスト"
	unknown_sector_list = sector_newhigh_list["unknown"][2][1]
	unknown_stockname_list = []
	for code in unknown_sector_list:
		if not make_stock_db.get_stock_db(code).has_key("stock_name"):
			unknown_stockname_list.append(code)
			#print "%d %s"%(code, make_stock_db.get_stock_db(code)["stock_name"])
	if unknown_stockname_list:
		stocks = make_stock_db.update_db_rows(unknown_sector_list, tables=["master"], latest=True)
	#print unknown_stockname_list
	print "-"*10, "銘柄名不明リスト終了"
	
	# unknownを除く
	sector_newhigh_list = {k:v for k,v in sector_newhigh_list.items() \
	if not v[0][0]==v[1][0]==v[2][0]==0 and k!="unknown"}
	def comp_val(x):
		v = x[1]
		avg = (x[1][0][0]*5+x[1][1][0]*3+x[1][2][0]*2)/10
		return avg*100/len(sector_tables[key])

	# セクター名わからんのは更新取得する
	#sector_newhight_list: k=セクター名 v=[[1d銘柄数、1d銘柄コード], 1w, 1m]
	unknown_sector_list = []
	for key, val in sector_newhigh_list.items():
		newhigh_lst_m = val[2][1]
		for code in newhigh_lst_m:
			if not make_stock_db.get_stock_db(code).has_key("sector_detail"):
				print "%dの詳細セクター更新"%code
				unknown_sector_list.append(code)
	#print unknown_sector_list
	stocks = make_stock_db.update_db_rows(unknown_sector_list, tables=["master"], latest=True) 
	#raise
	print "="*30
	print "-"*5, "セクター解析結果"
	print "="*30
	# 新高値セクター解析結果を表示
	for key, val in reversed(sorted( sector_newhigh_list.items(), \
		key=comp_val )):
		print key, val[0][0], val[1][0], val[2][0], "/%d"%\
		len(sector_tables[key]) if sector_tables.has_key(key) else 0
		code_name = [str(v)+make_stock_db.get_stock_db(v).get("stock_name","n/a") for v in val[1][1]]
		print "  ", 
		print_list(code_name)

def main():
	#args = ["market"]
	args = ["shintakane"]
	if "market" in args:
		analyze_market()
	if "shintakane" in args:
		analyze_shintakane()

if __name__ == '__main__':
	main()
