#!/usr/bin/python
# -*- coding: utf-8 -*-
#================================================
# セクターDBを作成します。
#================================================

import re
from datetime import datetime, timedelta
import csv

import price
import googledrive
import make_stock_db

from ks_util import *


URL_THEME_RANK_KABUTAN = "https://kabutan.jp/info/accessranking/3_2"
MARKET_DB_PATH = "market_data/market_db.pickle"

def parse_theme_html(html):
	#print ux_cmd_head(html, 10)
	#<td class="acrank_url"><a href="/themes/?theme=デジタルトランスフォーメーション">デジタルトランスフォーメーション</a></td>
	themes = []
	for m in re.finditer(r'<td class="acrank_url"><a href=".*?">(.*?)</a></td>', html):
		themes.append(m.group(1))
	return themes

def get_timedelta_today(fname):
	"""
	fnameファイルの日付と今日の日付の日数を返す
	"""
	#TODO: utilに移動
	if not os.path.exists(fname):
		print "%sはありません"%fname
		return None
	stat = os.stat(fname)
	fdate = datetime.fromtimestamp(stat.st_mtime)
	today = datetime.today()
	return get_price_day(today)-get_price_day(fdate), fdate

def get_prev_fname(fname, cur_day=datetime.today()):
	"""
	fnameの日付より古い日付のファイルを返す
	"""
	# 今日の日付
	#today_csv = get_dekidakaup_day_txtname(today)+".csv"
	# 探す
	count = 0
	CountMax = 30
	name, ext = os.path.splitext(fname)
	#print fname, name, ext
	while count < CountMax:
		#print "注：今日の情報がありません",today_csv,count
		cur_day = cur_day - timedelta(1)
		fname = name+"_%02d%02d%02d"%(cur_day.year-2000,cur_day.month,cur_day.day)+ext
		#print "fname:", fname
		count += 1
		if os.path.exists(fname):
			break
	if count >= CountMax:
		print "!!!直前のファイルが見つかりません", fname
		return "", cur_day
	print "直前のファイル:", fname
	return fname, cur_day

def get_theme_rank_list():
	"""
	テーマランクデータをDBから取得する
	Returns:
		現在のランクデータ、数日前のランクデータ、日付、数日前の日付
	"""
	cach_path = "market_data/theme_rank.html"

	delta, cach_date = get_timedelta_today(cach_path)
	use_cache = delta.days < THEME_RANK_INTERVAL
	#print "cache: ", cach_path, cach_date
	#print "use_cache: ", use_cache, delta.days	
	#prev_path = backup_file(cach_path, INTERVAL_BACKUP)
	html = http_get_html(URL_THEME_RANK_KABUTAN, \
		cache_dir="market_data", cache_fname="theme_rank.html", use_cache=use_cache)
	theme_rank_list = parse_theme_html(html)
	# 今日のデータと直前のデータを比較して
	# 勢いを考慮した真のランキングデータを作成する
	# 直前日付のランクデータを取得
	prev_cache, prev_day = get_prev_fname(cach_path, cach_date-timedelta(2))
	if (cach_date-prev_day).days >= INTERVAL_BACKUP:
		backup_file(cach_path, 0)
	prev_html = file_read(prev_cache)
	prev_theme_rank_list = parse_theme_html(prev_html)

	return theme_rank_list, prev_theme_rank_list, cach_date, prev_day

THEME_RANK_INTERVAL = 1  # 再取得までの日数
INTERVAL_BACKUP = 3 # バックアップ日数

def make_theme_data(): #market_db=None
	"""テーマランクデータを作成
	"""
	print "テーマランクデータを作成します"
	theme_rank_list, prev_theme_rank_list, cach_date, _ = get_theme_rank_list()

	# print "今:", ".".join(theme_rank_list)
	# print "前:", ".".join(prev_theme_rank_list)
	theme_rank_dict = {v:i+1 for (i,v) in enumerate(theme_rank_list)}
	prev_theme_rank_dict = {v:i+1 for (i,v) in enumerate(prev_theme_rank_list)}
	theme_rank2 = {}
	for theme, rank in theme_rank_dict.items():
		#print theme, ":", rank
		moment = 0
		if prev_theme_rank_dict.has_key(theme):
			prev_rank = prev_theme_rank_dict[theme]
		else:
			prev_rank = 31
		moment = -(rank - prev_rank)
		print "  %s %d->%d"%(theme, prev_rank, rank)
		rank_pt = 31-rank + moment
		theme_rank2[theme] = rank_pt
		#print "%s=%d"%(theme,rank_pt)
	# Valueでソートしてその順番でリストに
	theme_rank2_sorted = sorted(theme_rank2.items(), key=lambda x:x[1], reverse=True)
	theme_rank2_list = [theme for theme,pt in theme_rank2_sorted]
	print "モメンタム順位:", ",".join(theme_rank2_list)
	# マーケットDBに保存
	# if not market_db:
	# 	market_db = load_pickle(MARKET_DB_PATH)
	market_db = {}
	market_db["theme_rank"] = theme_rank2_list
	#print "cach_date:", cach_date
	market_db["access_date_theme_rank"] = cach_date
	#save_pickle(MARKET_DB_PATH, market_db)
	return market_db

# def get_theme_rank():
# 	"""
# 	テーマランクデータを取得
# 	"""
# 	market_db = load_pickle(MARKET_DB_PATH)
# 	#print market_db["access_date_theme_rank"]
# 	if market_db.has_key("access_date_theme_rank") and \
# 	(datetime.today()-market_db["access_date_theme_rank"]).days < THEME_RANK_INTERVAL:
# 		print "ランクDB作成済み", market_db["access_date_theme_rank"]
# 	 	return market_db["theme_rank"]
# 	market_db = make_theme_data(market_db)
# 	return market_db["theme_rank"]

def get_major_theme(themes):
	"""
	銘柄テーマから、主要3テーマを取得する
	@themes 銘柄のテーマ: stock_dbの'themes'キー
	"""
	market_db = memoized_load_pickle(MARKET_DB_PATH)
	theme_rank = market_db["theme_rank"] # 現在のランキング
	theme_rank_dict = {v:i+1 for (i,v) in enumerate(theme_rank)}
	# DSU
	# 渡されたテーマの順位表
	#print themes
	if not themes:
		return ""
	themes = themes.split(",") #　リスト化
	themes_dict = {theme:theme_rank_dict.get(theme,31) for theme in themes}
	themes_sorted = sorted(themes_dict.items(), key=lambda x:x[1])
	#print "sorted:", themes_dict_sorted
	major_themes_rank = [theme for theme,rank in themes_sorted]
	#print ",".join(major_themes_rank)
	major_themes_rank = major_themes_rank[:3]
	#major_themes = ""
	# for th in major_themes_rank:
	# 	major_themes += "%s"%(th) # themes_dict[th]
	#print ",".join(major_themes_rank)
	return ",".join(major_themes_rank)

def make_db_common(code_s):
	"""DBデータ更新共通処理
	type: str -> dict<db>
	"""
	db = {}
	priced_dict = price.get_daily_price_kabutan(code_s)
	#print "mothers:", priced_dict
	db.update(priced_dict)
	pr=priced_dict.get("price",0)
	pricew_dict = price.get_weekly_price_data(code_s, [pr, pr, pr]) # 仮処理ではある
	#print pricew_dict
	print "RS_RAW=", pricew_dict.get("rs_raw", 0)
	db.update(pricew_dict)

	return db

def make_topix_db():
	code_s = "0010" # 株探ではTOPIXが0010
	db_dict = make_db_common(code_s)
	db = {}
	db["topix"] = db_dict
	return db

def make_mothers_db():
	code_s = "0012"
	db_dict = make_db_common(code_s)
	db = {}
	db["mothers"] = db_dict
	return db
def make_nikkei_db():
	code_s = "0000"
	db_dict = make_db_common(code_s)
	db = {}
	db["nikkei225"] = db_dict
	return db
def make_dow_db():
	code_s = "0800"
	db_dict = make_db_common(code_s)
	db = {}
	db["dow"] = db_dict
	return db
def make_nasdaq_db():
	code_s = "0802"
	db_dict = make_db_common(code_s)
	db = {}
	db["nasdaq"] = db_dict
	return db

def get_market_db():
	market_db = memoized_load_pickle(MARKET_DB_PATH)
	return market_db

def update_market_db():
	"""	マーケットDBを読み込んで最新に更新
	"""
	market_db = load_pickle(MARKET_DB_PATH)

	theme_db = make_theme_data()
	market_db.update(theme_db)
	
	topix_db = make_topix_db()
	market_db.update(topix_db)

	mothers_db = make_mothers_db()
	market_db.update(mothers_db)
	nikkei_db = make_nikkei_db()
	market_db.update(nikkei_db)
	nasdaq_db = make_nasdaq_db()
	market_db.update(nasdaq_db)

	save_pickle(MARKET_DB_PATH, market_db)
	print "MarketDB保存:", market_db.keys()
	return market_db

def create_market_csv(market_db=None, shintakane_theme_csv=""):
	"""市場DBから表示用CSVデータにする
	"""
	if not market_db:
		market_db = load_pickle(MARKET_DB_PATH)
	#print market_db.keys()
	csv_path = 'code_rank_data/market_data.csv'

	theme_rank_list, prev_theme_rank_list, _, prev_day = get_theme_rank_list()
	rows = []
	rows.append(["■ テーマランク"])
	row = ["ランク"]
	row.extend(market_db["theme_rank"]) # 現在と過去を考慮した総合ランク
	rows.append(row)
	time = market_db["access_date_theme_rank"].date()
	row = [str(time)]
	row.extend(theme_rank_list) # 当日テーマ
	rows.append(row)
	prev_time = prev_day.date()
	row = [prev_time]
	row.extend(prev_theme_rank_list) # 数日テーマ
	rows.append(row)
	# 新高値テーマを反映
	for row in shintakane_theme_csv:
		rows.append(row)

	#---- 各市場
	def get_db_row(db_name, market_name):
		try:
			db = market_db[db_name]
			trend_expr = make_stock_db.get_trend_template_expr(db)
			distribution_days = ",".join([s[3:] for s in db["distribution_days"]])
			followthrough_days = ",".join([s[3:] for s in db["followthrough_days"]])
			#diff = (sprs[2]-sprs[0])
			diff = db["spr_buygagher"]-db["spr_20"]
			eval = step_func(diff, [-10, -5, 0, 5, 10], ["E","D","C","B","A"])
			rows = []
			rows.append([market_name, db["rs_raw"], trend_expr, \
			distribution_days, followthrough_days, db["direction_signal"], \
				"%d,%d,%s"%(db["spr_20"],db["spr_5"], eval), "%.1f,%.1f"%(db["rv_20"],db["rv_5"]) \
					])
			return rows
		except KeyError as e:
			print "!!! 市場のDBデータ取得できず",db_name, market_name
			return []

	rows.append([])
	rows.append(["■市場"])
	rows.append(["市場名", "RS", "トレンド", "ディストリビューション", "フォロースルー","シグナル","売り圧力レシオ(20,5)","ローソク足ボラティリティ(20,5)"])
	rows.extend(get_db_row("topix","TOPIX"))
	rows.extend(get_db_row("mothers","マザーズ指数"))
	rows.extend(get_db_row("nikkei225","日経225"))
	rows.extend(get_db_row("nasdaq","NASDAQ"))
	#---- 決算日
	rows.append([])
	rows.append(["■決算日"])
	import kessan
	kessan_csv = kessan.make_kessan_csv()
	rows.extend(kessan_csv)
	#---- 適宜開示
	rows.append([])
	rows.append(["■適宜開示"])
	import disclosure
	disc_csv = disclosure.update_disclosure_all()
	rows.extend(disc_csv)

	with open(csv_path, "wb") as f:
		csv_w = csv.writer(f)
		csv_w.writerows(rows)

	# アップロード
	googledrive.upload_csv(csv_path, "market_data")

def update_shintakane_theme(stocks, code_list):
	themes_count = {}
	for code_s in code_list:
		if not stocks.has_key(code_s):
			continue
		stock = stocks[code_s]
		themes = stock.get("themes",[]) # "テーマ1,テーマ2,..."の形
		#print "themes:", themes
		for theme in themes.split(","):
			#print "theme:", theme
			if not theme:
				continue
			if not themes_count.has_key(theme):
				themes_count[theme] = 0
			themes_count[theme] += 1
	themes_count_sorted = (sorted(themes_count.items(), key=lambda x: x[1], reverse=True))
	for theme, count in themes_count_sorted[:30]:
		print theme, count
	return themes_count_sorted

def update_shintakane_theme_csv(stocks, today_list, past_list):
	print "新高値テーマの取得"
	today_counts = update_shintakane_theme(stocks, today_list)
	past_counts = update_shintakane_theme(stocks, past_list)
	csv = []
	today = ["当日"]
	today.extend(["%s(%d)"%(t[0],t[1]) for t in today_counts[:30]])
	csv.append(today)
	today = ["過去"]
	today.extend(["%s(%d)"%(t[0],t[1]) for t in past_counts[:30]])
	csv.append(today)
	return csv

def main():
	# for k, v in market_db.items():
	# 	print k, v
	#mothers_db = make_mothers_db()
	#nikkei_db = make_nikkei_db()
	# なぜか、株探でダウとS&Pの価格取得できないんじゃあ・・
	# やるならYahooだが面倒
	#dow_db = make_dow_db() 
	#nasdaq_db = make_nasdaq_db()

	market_db = update_market_db()
	create_market_csv()

	#theme_db = make_theme_data()
	# import make_stock_db
	# stocks = make_stock_db.load_stock_db()
	# codes = ["1841","2218","2801","1802","2068"]
	# codes2 = ["5842","1723","3558","9211","1770","6551","3816","7670","2397","9216","3649","2924"]
	# update_shintakane_theme_csv(stocks, codes, codes2)

if __name__ == '__main__':
	main()
