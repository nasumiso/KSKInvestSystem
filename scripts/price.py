#!/usr/bin/python
# -*- coding: utf-8 -*-
from ks_util import *

from datetime import datetime, date, timedelta
import os, os.path
import re
import requests

#URL_PRICE_D_KABUTAN = 'https://kabutan.jp/stock/kabuka?code=%04d&ashi=day&page=%d'
URL_PRICE_D_KABUTAN = 'https://kabutan.jp/stock/kabuka?code=%s&ashi=day&page=%d'
#PRICE_D_FNAME_KABUTAN = "stock_data/kabutan/price/kabutan_price_d_%04d_%d.html"
PRICE_D_FNAME_KABUTAN = os.path.join(DATA_DIR, "stock_data/kabutan/price/kabutan_price_d_%s_%d.html")

INTERVAL_DAY_D = 1
INTERVAL_DAY_W = 7

def get_daily_html_kabutan(code_s ,cache=True):
	"""日次時系列価格データhtmlを取得する
	"""
	html = ""
	print("----> %sの日次価格情報を取得します・・"%code_s, cache)
	ind = 0
	price_fname = PRICE_D_FNAME_KABUTAN%(code_s,ind+1)
	#print price_fname
	# キャッシュファイルから取得
	if cache and os.path.exists(price_fname):
		# TODO: 最新情報がなければ取りに行く
		cach, cach_date = is_file_timestamp(price_fname, INTERVAL_DAY_D)
		if cach:
			html = open(price_fname, 'r').read()
			#htmls.append(html)
			#continue
			return html
		else:
 			print("キャッシュの期限切れ(%s)"%cach_date.date())
	# 株探から取得		
	try:
		url = URL_PRICE_D_KABUTAN%(code_s, ind+1)
		print("%sから株探から通信で取得.."%url)
		r = requests.get(url)
		html = r.text # .encode('utf-8') :python3では不要
		#htmls.append(html)
		open(price_fname, 'w').write(html)
	except requests.exceptions.ConnectionError as e:
		print("!!! %sにつながりません"%url)
		print(e)
		#break

	print("<---- 取得完了")
	return html

def get_price_interval_day(day1, day2):
	#TODO: 17時の時点でYahooの価格は更新されてない？
	day1 = get_price_day(day1)
	day2 = get_price_day(day2)
	return (day1 - day2).days

def is_file_timestamp(fname, interval_day):
	stat = os.stat(fname)
	cach_date = datetime.fromtimestamp(stat.st_mtime)
	print("cach_date:", cach_date, fname)
	delta = get_price_interval_day(datetime.today(), cach_date)
	if delta < interval_day: # キャッシュ使用可能
		return True, cach_date
	return False, cach_date

URL_PRICE_KABUTAN = 'https://kabutan.jp/stock/kabuka?code=%s&ashi=wek&page=%d'
URL_CREDIT_KABUTAN = 'https://kabutan.jp/stock/kabuka?code=%s&ashi=shin&page=%d'
#PRICE_FNAME_KABUTAN = "stock_data/kabutan/price/kabutan_price_%04d_%d.html"
PRICE_FNAME_KABUTAN = os.path.join(DATA_DIR, "stock_data/kabutan/price/kabutan_price_%s_%d.html")
def get_weekly_html(code_s, cache=True):
	"""株探から週次データを取得
	type: (str, bool) -> str
	"""
	htmls = []
	print("----> %sの週次価格情報を取得します・・"%code_s, cache)
	for ind in range(2):
		price_fname = PRICE_FNAME_KABUTAN%(code_s,ind+1)
		# キャッシュファイルから取得
		if cache and os.path.exists(price_fname):
			# TODO: 最新情報がなければ取りに行く
			cach, cach_date = is_file_timestamp(price_fname, INTERVAL_DAY_W)
			if cach:
				html = open(price_fname, 'r').read()
				htmls.append(html)
				continue
			else:
				print("キャッシュの期限切れ(%s)"%cach_date.date())
		# 株探から取得		
		try:
			url = URL_PRICE_KABUTAN%(code_s, ind+1)
			print("%sから株探から通信で取得.."%url)
			r = requests.get(url)
			html = r.text # .encode('utf-8') :python3では不要
			htmls.append(html)
			open(price_fname, 'w').write(html)
		except requests.exceptions.ConnectionError as e:
			print("!!! %sにつながりません"%url)
			print(e)
			break
	
	print("<---- 取得完了")
	return htmls

URL_PRICE_YAHOO = 'http://stocks.finance.yahoo.co.jp/stocks/history/?code=%s.T'
#PRICE_FNAME = "stock_data/yahoo/price/yahoo_price_%d.txt"
PRICE_FNAME = os.path.join(DATA_DIR, "stock_data/yahoo/price/yahoo_price_%s.txt")
def get_daily_data_yahoo(code_s, upd=UPD_INTERVAL):
	"""yahooファイナンスから価格データhtmlを取得する
	type: (str, bool) -> str
	Returns:
		str: htmlテキスト
	"""
	price_fname = PRICE_FNAME%code_s
	# キャッシュファイルから取得
	if os.path.exists(price_fname):
		use_cach = False
		if upd < UPD_INTERVAL:
			use_cach = True
		else:
			if upd == UPD_FORCE:
				use_cach = False
			else:
				# キャッシュの日付が期限内かチェック
				cache, _ = is_file_timestamp(PRICE_FNAME%code_s, INTERVAL_DAY_D)
				if cache:
					use_cach = True
		if use_cach:
			return open(price_fname, 'r').read()

	# Yahooから取得
	print("----> %sの価格情報をYahooから取得します・・"%code_s)
	try:
		url = URL_PRICE_YAHOO%code_s
		r = requests.get(url)
	except requests.exceptions.ConnectionError as e:
		print("!!! %sにつながりません"%url)
		print(e)
		return ""
	
	print("<---- 取得完了")
	text = r.text  # python3では.encode('utf-8')不要
	open(price_fname, 'w').write(text)
	return text

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
	buygather_list = [] # 買い集め用
	volume_lst = [p[5] for p in price_list]
	if len(volume_lst) > 0:
		volume_avg = sum(volume_lst)/len(volume_lst)
	else:
		volume_avg = 0	
	for i, price in enumerate(price_list):
		if i+1 >= len(price_list):
			break
		#print price
		prev = price_list[i+1] # 前日
		yousen = price[4]-prev[4] > 0
		val0 = price[1]-prev[4] # 前日終値→始値
		val1 = price[3]-price[1] if yousen else price[2]-price[1] # 始値→高値or安値
		val2 = price[2]-price[3] if yousen else price[3]-price[2] # 高値or安値→安値or高値
		val3 = price[4]-price[2] if yousen else price[4]-price[3] # 安値or高値→終値
		vals = [val0, val1, val2, val3]
		#print yousen, vals
		buy_count = sum([v for v in vals if v > 0]) #買い値幅
		sel_count = abs(sum([v for v in vals if v <= 0])) #売り値幅
		#print "buy-sel:", buy_count, sel_count
		# 5:出来高
		buy_stock = price[5]*buy_count/(buy_count+sel_count) if (buy_count+sel_count) != 0 else price[5]/2
		sel_stock = price[5]*sel_count/(buy_count+sel_count) if (buy_count+sel_count) != 0 else price[5]/2
		ratio = round((float)(sel_count)/buy_count,1) if buy_count != 0 else 0
		volatility = sum([abs(v) for v in vals]) # ローソク足ボラティリティ	
		#print buy_count, sel_count, ratio, price[5], buy_stock, sel_stock
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
			buy_sum += ratio_list[i][0] #0: buy_count
			sel_sum += ratio_list[i][1] #1: sel_count
		except IndexError as e:
			print("!!! 日付が足りないため%d日で計算します"%i)
			break
	print("%d日 買い:%d 売り:%d"%(day_count, buy_sum, sel_sum))
	# if buy_sum > 0:
	# 	ratio = (sel_sum*100) / buy_sum
	if buy_sum+sel_sum > 0:
		ratio = (int)(100*buy_sum/(buy_sum+sel_sum))
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
	#---- 売り圧力レシオの計算
	ratio_list, buygather_list = get_ratio_list(price_list)
	#print "ratio_list:", ratio_list
	# メモ：売り圧力レシオが116で売り
	DAY_L = 19
	DAY_S = 5
	ratios = [calc_ratio(ratio_list, DAY_L), calc_ratio(ratio_list, DAY_S)]
	ratios.append(calc_ratio(buygather_list, len(buygather_list)))

	#---- ローソク足ボラティリティの計算
	def calc_vola(day_count):
		"""
		(int)->(float)
		"""
		vol_avg = 0
		end_avg = 0
		for i in range(day_count):
			try:
				vol_avg += ratio_list[i][3] #1日ボラティリティ
				end_avg += price_list[i][4] #終値
			except IndexError as e:
				print("!!! 日付が足りないため%d日で計算します"%i)
				break
		vol_avg /= float(day_count)
		end_avg /= float(day_count)
		#print vol_avg, end_avg
		volatility = round(100.0*vol_avg/end_avg, 2) if end_avg > 0 else 0		
		return volatility, end_avg

	volatility_l, ma_l = calc_vola(DAY_L)
	volatility_s, ma_s = calc_vola(DAY_S)
	print("SRRレシオ(20,5):", ratios, \
	"ボラティリティ(20,5):", volatility_l, volatility_s)
	#print "ボラ(H,L)=(%d, %s)"%(int(ma_l*(1+volatility_l/100)), int(ma_l*(1-volatility_l/100)) )
	ratios.append(volatility_l)
	ratios.append(volatility_s)
	return ratios

def parse_price_d_html_kabutan(html):
	"""日次の株探価格htmlデータを解析する
	"""
	#print ux_cmd_head(html, 10)
	#---- 日次データの作成
	daily_price_list = []
	for m in re.finditer(r'<th scope="row"><time datetime=".*?">(.*?)</time></th>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td><span class=".*?">(.*?)</span></td>\r\n<td><span class=".*?">(.*?)</span></td>\r\n<td>(.*?)</td>', \
		html, re.DOTALL):
		daily_price_list.append(m.groups())
	#print "日次データ:", len(daily_price_list), daily_price_list
	
	#---- ディストリビューション
	distribution_day = [] # 日付のリスト
	followthrough_day = []
	# 前日より安く、出来高が増える日
	# 　前日より0.1%以下で上半分で引ける場合はカウントしない[モラレス]
	# 前日よりわずかに高くても下で引けていけばカウント[モラレス]
	# 　例：0.1%上昇で25%以下で引ける
	# 日付、始値、高値、安値、終値、前週比、前週比％、売買高
	count_day = 20
	target_days = list(reversed(daily_price_list[:count_day+1])) # インデックスが若いほど前の日にする
	# 平均出来高
	avg_vol = 0
	avg_len = 0
	for d in target_days:
		try:
			dv = int(d[7].replace(",","")) #売買高
			avg_vol += dv
			avg_len += 1
		except ValueError:
			pass
	if avg_len == 0:
		print("!!! デイリー価格解析できず")
		return {}
	avg_vol = avg_vol/avg_len
	current_day = target_days[-1][0]
	print(current_day, "のディストリビューションカウント")
	for d,pd in zip(target_days[1:], target_days[:-1]):
		try:
			dp = float(d[4].replace(",","")) #終値
			pdp = float(pd[4].replace(",",""))
			dv = int(d[7].replace(",","")) #売買高
			pdv = int(pd[7].replace(",",""))
			dph = float(d[2].replace(",","")) #始値
			dpl = float(d[3].replace(",","")) #高値
			dr = float(d[6].replace(",","")) #前日比パーセント
		except ValueError:
			#TODO: ここに来ているみたい
			print("%sはデータ取得できず"%d[0])
			continue
		#print d[0], "の解析"
		pr_pos = (dp-dpl)/(dph-dpl)
		#print "値幅位置:", pr_pos
		if dp < pdp: # 前日より安く出来高が増える
			if dv > pdv:
				if dr >= -0.1 and pr_pos >= 0.5:
					print("前日より0.1%以下で上半分で引ける場合はカウントしない", d[0])
				else:
					distribution_day.append(d[0])
					#print "ディストリビューション:", d[0]
		else:
			if dv > pdv:
				print("dr",dr, "pr_pos",pr_pos)
				if dr <= 0.1 and pr_pos <= 0.25:
					print("前日よりわずかに高くても下で引けていけばカウント", d[0])
					distribution_day.append(d[0])
					#print "ディストリビューション:", d[0]	
		# フォロースルー: 反転から4~7日目で(ここは判定していない)
		# 平均以上の出来高で1.7%以上の上昇
		# 本当はその時から過去20日の出来高にしないといけないが取得できない・・
		if dr >= 1.7 and dv >= avg_vol:
			followthrough_day.append(d[0])
	dic = {}
	dic["distribution_days"] = distribution_day
	dic["followthrough_days"] = followthrough_day
	print("ディストリビューション:", distribution_day)
	print("フォロースルー:", followthrough_day)

	#---- シグナル
	signal = "neutral"
	#TODO: とりあえず簡易
	if len(distribution_day) >= 5:
		signal = "sell"
	
	dic["direction_signal"] = signal+","+current_day
	print("シグナル:", dic["direction_signal"])

	#---- 売り圧力レシオ
	def to_numeric(str):
		if str == "－":
			return 0
		return int(float(str.replace(",","")))
	#TODO:
	# 日付、始値、高値、安値、終値、前週比、前週比％、売買高
	# ->日付、始値、高値、安値、終値、出来高
	price_list_spr = [[l[0],\
		to_numeric(l[1]),to_numeric(l[2]),to_numeric(l[3]),\
			to_numeric(l[4]),to_numeric(l[7])] for l in daily_price_list]
	spr_20, spr_5, spr_buygagher, rv_20, rv_5 = calc_sell_pressure_ratio(price_list_spr)
	dic["spr_20"] = spr_20
	dic["spr_5"] = spr_5
	dic["spr_buygagher"] = spr_buygagher
	dic["rv_20"] = rv_20
	dic["rv_5"] = rv_5
	
	return dic

def parse_pricew_htmls_kabutan(htmls, cur_prices=[]):
	""" 株探の週次データから
	RSを求める
	"""
	price_dict = {}
	#---- 週次データを集計
	def calc_weekly_price_list():
		# index: [0]日付、[1]始値、[2]高値、[3]安値、[4]終値、前週比、前週比％、[7]売買高
		weekly_price_list = [] # 週次価格データ
		for ind, html in enumerate(htmls):
			j = 0
			for m in re.finditer(r'<th scope="row"><time datetime=".*?">(.*?)</time></th>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>\r\n<td>(.*?)</td>', \
				html, re.DOTALL):
				if ind >= 1 and j == 0:
					# 変則的だが、2ページ以降の最初の要素は現在株価なので省く
					j += 1
					continue
				weekly_price_list.append(m.groups())
				#print m.groups()
				j += 1
		return weekly_price_list
	weekly_price_list = calc_weekly_price_list()
	try:
		prices = [int(float(p[4].replace(",",""))) for p in weekly_price_list]
		highs = [int(float(p[2].replace(",",""))) for p in weekly_price_list]
		lows = [int(float(p[3].replace(",",""))) for p in weekly_price_list]
	except (ValueError, ZeroDivisionError, IndexError):
		print("!!! 価格データがかけている")
		prices = highs = lows = []
		return price_dict
	#---- 10WMA(=50日MA)との乖離率(売りシグナルに使用)
	try:
		wma10 = sum(prices[:10])/10
		kairi = (prices[0]-wma10)*100/wma10
		price_dict["price_kairi_wma10"] = kairi
	except IndexError:
		print("!!! 10WMA乖離率計算できず")
		price_dict["price_kairi_wma10"] = 0
	print("10WMA乖離率:", price_dict["price_kairi_wma10"])

	#---- 売り圧力レシオ(買い集め指数)
	try:
		price_list_w = [[p[0], int(float(p[1].replace(",",""))), int(float(p[2].replace(",",""))),int(float(p[3].replace(",",""))),int(float(p[4].replace(",",""))),int(float(p[7].replace(",","")))] for p in weekly_price_list]
		ratio_list, buygather_list = get_ratio_list(price_list_w[:20])
		sell20 = calc_ratio(ratio_list, 20)
		#sell5 = calc_ratio(ratio_list, 5)
		buy_gather = calc_ratio(buygather_list, len(buygather_list))
		price_dict["sell_pressure_ratio_w"] = [sell20, 0, buy_gather]
		print("週次売り圧力レシオ:", price_dict["sell_pressure_ratio_w"])
	except ValueError:
		print("!!! 週次売り圧力レシオ計算できず")
		price_dict["sell_pressure_ratio_w"] = [0, 0, 0]

	#---- RSを求める
	def calc_rs():	
		# 週次データからRSを計算
		price_len = len(weekly_price_list)
		if not weekly_price_list:
			print("!!!! 週次データのない銘柄のようです")
			return 0,0
		if not cur_prices:
			p_cur = float(weekly_price_list[0][4].replace(",","")) #4:終値
		else:
			p_cur = cur_prices[0]
		# if not cur_prices:
		# 	try:
		# 		p_cur_high = float(weekly_price_list[0][2].replace(",","")) #2:高値
		# 	except ValueError:
		# 		print "!!! 高値のない銘柄"
		# 		p_cur_high = p_cur
		# else:
		# 	p_cur_high = cur_prices[1]
		past_prices = []
		print("現在終値 %s"%p_cur)
		for w in [13,26,39,52]:
			if w < price_len:
				past_prices.append(float(weekly_price_list[w][4].replace(",","")))
				#print "%sの終値 %s"%(weekly_price_list[w][0], weekly_price_list[w][4])
		weights = [0.4, 0.2, 0.2, 0.2]
		weights = weights[:len(past_prices)]
		total = sum(weights)
		weights = [w/total for w in weights]
		# print "past_prices:", past_prices
		# print "weights:", weights
		try:
			ratios = [float(p_cur)/p for p in past_prices]
			print("ratio:", [round(r,2) for r in ratios])
			rs_raw = 0
			for r, w in zip(ratios, weights):
				rs_raw += r*w
			rs_raw = round(rs_raw, 2)
		except ZeroDivisionError:
			rs_raw = 0
		if rs_raw == 0:
			print("!!! RSを計算できませんでした (%d個の過去データ)"%len(weekly_price_list))
			rs_raw = 1.0 # 標準値にする
		print("rs_raw:", rs_raw)
		return rs_raw, p_cur

	rs_raw, p_cur = calc_rs()
	price_dict["rs_raw"] = rs_raw

	#---- TOPIXとの比較でモメンタムポイントを計算
	def calc_momentum_pt():
		import make_market_db
		market_db = make_market_db.get_market_db()
		if "rs_raw" in market_db["topix"]:
			topix_rs_raw = market_db["topix"]["rs_raw"]
			if topix_rs_raw == 0:
				print("!!! TOPIXのRSが0のためモメンタムポイント計算できません")
				rs_rank = 0
			else:
				# TOPIXのRSと比較してモメンタムポイントを計算
				rs_rel = rs_raw/topix_rs_raw
				#print "rs_rel:", rs_rel, topix_rs_raw
				from scipy.stats import norm
				scale = 0.3 # code_rank実測のrs_raw標準偏差
				# 平均1.0、標準偏差0.3の上側確率
				rs_rank = int(100*(1-norm.sf(x=rs_rel, loc=1.0, scale=scale)))
				print("rs_rank:", rs_rank)
		else:
			rs_rank = 0
			print("!!! TOPIXのモメンタムポイント存在せず、計算できず")
		return rs_rank
	rs_rank = calc_momentum_pt()
	if rs_rank > 0: # 0はエラーのため更新しない
		price_dict["momentum_pt"] = rs_rank
	#---- トレンドテンプレート
	def calc_trend_template():
		# ・株価が30週MAと40週MAを上回る
		# ・30週MAは40週MAを上回る
		# ・40週MAは1ヶ月以上上昇トレンド
		# ・10週MAは30,40週MAを上回る
		# ・株価は10週MAを上回る
		# ・株価は52週安値より30％以上高く、52週高値より25％以内
		# ・レラティブストレングスが70以上
		try:
			ma30 = sum(prices[0:30])/len(prices[0:30])
			ma40 = sum(prices[0:40])/len(prices[0:40])
			if len(prices[1:41]) > 0:
				ma40_b = sum(prices[1:41])/len(prices[1:41])
			else:
				ma40_b = ma40
			ma10 = sum(prices[0:10])/len(prices[0:10])
			low52 = min(lows[0:52])
			high52 = max(highs[0:52])

			#print "---", p_cur, ma10, ma30, ma40, ma40_b, low52, high52, rs_rank, "---"
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
			if not (p_cur >= 1.3*low52 and p_cur >= 0.75*high52):
				misses.append("high(low)52")
			if not rs_rank >= 75:
				misses.append("RS")
			print("トレンドテンプレート:", misses)
			return misses			
		except (ValueError, ZeroDivisionError, IndexError):
			print("!!! 価格データがかけている")
			return []
	price_dict["trend_template"] = calc_trend_template()

	def calc_pullback_20():
		try:
			#---- 20MA押し
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
			for ind in range(len(prices)-WEEK): 
				ma4 = sum(prices[ind:ind+WEEK])/WEEK
				ma4list.append(ma4)
				kairi = 100*prices[ind]/ma4-100
				kairi_list.append(kairi)
				kairi_low.append(100*lows[ind]/ma4-100)
				#print "pr, ma4,kairi =", prices[ind], ma4, kairi
			KAIRI_CUR = (-1,3)
			# 現在価格乖離が一定以内(4MAの上かどうかも兼ねている)
			if kairi_list[0] >= KAIRI_CUR[0] and kairi_list[0] <= KAIRI_CUR[1]:
				# 直近安値の乖離率
				#min_low = min(lows[0:WEEK])
				# min_low_ind = lows[0:WEEK].index(min_low)
				# min_kairi_low = kairi_low[min_low_ind]
				min_kairi_low = min(kairi_low[0:WEEK])
				if min_kairi_low <= 1: # 直近安値が20MAに接近
					min_low_ind = kairi_low[0:WEEK].index(min_kairi_low)
					#kairi_sum = sum(kairi_list[kairi_low_min_ind:WEEK])
					kairi_list_plus = [l for l in kairi_list[min_low_ind+1:WEEK] if l>=0]
					kairi_sum = sum(kairi_list_plus)
					# 上方にいたかどうか
					up_week_count = len(kairi_list_plus)
					if kairi_sum >= 7*up_week_count:
						# 直近安値から上がりすぎていないか
						kairi_low2cur = 100*prices[0]/lows[min_low_ind]-100
						if kairi_low2cur <= 10:
							return "○"
			return ""
		# kairi_sum = sum(kairi_list[0:8])
		# print "20MA押し　乖離:%d 8週乖離:%d"%(kairi_list[0], kairi_sum)
		# if kairi_list[0] <= 3 and kairi_list[0] >= -2: # 20MAからこの%以内を許容
		# 	if kairi_sum >= 7*8: # これまで20MAを一定以上上回っていた
		# 		price_dict["pullback_20"] = "○"
		except (ValueError, ZeroDivisionError, IndexError):
			print("!!! 価格データがかけている")
			return ""
	price_dict["pullback_20"] = calc_pullback_20()

	#---- 新高値
	def calc_new_highs():
		# index: 日付、始値、高値、安値、終値、前週比、前週比％、売買高
		# weekly_price_list = [] # 週次価格データ
		# 2週前からの最高値
		new_highs = []
		try:			
			p_list_wk = [p for p in weekly_price_list[2:]]
			p_high_list = [float(p[2].replace(",","")) for p in p_list_wk]
			p_high = max(p_high_list)
			p_high_ind = p_high_list.index(p_high)
			#print "価格", p_cur, len(weekly_price_list)
			print("2週前以前新高値:%d %d週前(%s)"%(p_high, p_high_ind, p_list_wk[p_high_ind][0]))
			if p_cur >= p_high*0.95: # 2週前以前高値から5パー以内
				if p_high_ind >= 52-2: # 一年以上前
					new_highs.append("新")
				elif p_high_ind >= 12-2: # 3ヶ月以上前
					new_highs.append("直")
			# 最高値
			p_high = [float(p[2].replace(",","")) for p in weekly_price_list[1:]]
			p_max = max(p_high)
			#print "最高値", p_max
			if p_cur >= p_max:
				new_highs.append("最")
			print("新高値:", "".join(new_highs))
		except ValueError:
			print("!!! 新高値取得できず(価格データ不足)")
			pass
		return new_highs
	price_dict["new_high"] = calc_new_highs()

	return price_dict

# 株式分割調整	
def adjust_divide_price(price_list):
	for price in price_list:
		ratio = float(price[4])/price[6]
		if ratio>1:
			print("株式分割%d倍 %s %d->"%(ratio, price[0], price[4]), end=' ')
			price[1] = int(price[1]/ratio)
			price[2] = int(price[2]/ratio)
			price[3] = int(price[3]/ratio)
			price[4] = int(price[4]/ratio)
			print("%d"%price[4])

def parse_price_text_yahoo_old(text):
	m = re.search(r'<td class="stoksPrice">(.*?)</td>', text)
	try:
		price_current = int(float(m.group(1).replace(",","")))
	except:
		print("!!! デイリー価格情報がありません(フォーマット変更?)")
		return 0, []

	#---- 価格データの解析
	m = re.search(r'<table.*"boardFin yjSt marB6">(.*)</table>', text, re.DOTALL)
	#print m.group(1)
	rows = []
	for m in re.finditer(r'<td>(.*)</td>', text):
		#print m.group(1)
		rows.append(m.group(1))
	price_list = []
	r = iter(rows)
	while(True):
		try:
			# 日付、始値、高値、安値、終値、出来高、調整後終値
			row = [next(r), next(r), next(r), next(r), next(r), next(r), next(r)]
			#print row
			row = [_r.replace(',','') for _r in row]
			for i, _r in enumerate(row[1:]):
				row[1+i] = int(float(_r))
			#print "---", row, "---"
			price_list.append(row)
		except StopIteration as e:
			break
	adjust_divide_price(price_list)
	return price_current, price_list

def parse_price_text_yahoo_new(text):
	price_current = 0
	try:
		#m = re.search(r'<span class="_3rXWJKZF">(.*?)</span>', text)
		# 24.9フォーマット変わった
		m = re.search(r'<span class="StyledNumber__value__3rXW">(.*?)</span>', text)
		#最初の1つ目のspan classが現在価格情報
		price_current = int(float(m.group(1).replace(",","")))
		print("現在株価:", price_current)
	except:
		print("現在株価なし")
		#print "!!! デイリー価格情報がありません(フォーマット変更?)"
		#return 0, []

	price_list = []
	m = re.search(r'<tbody>(.*?)</tbody>', text)
	if not m:
		print("!!! デイリー価格情報リストがありません(フォーマット変更?)")
		return 0, []
	#print m.group(1)
	tbody = m.group(1)
	try:
		days = []
		# 日付を先に集める
		for m in re.finditer(r'<th scope="row".*?>(.*?)</th>', tbody):
			#print "日付:",m.group(1)
			days.append(m.group(1))
		# 24.9フォーマット変わった
		#for ind, m in enumerate(re.finditer(r'<tr class="_2ZqX1qip">(.*?)</tr>', tbody)):
		for ind, m in enumerate(re.finditer(r'<tr class="HistoryTable__row__2ZqX">(.*?)</tr>', tbody)):
			trlist = m.group(1)
			row = []
			# 始値、高値、安値、終値、出来高、調整後終値
			row.append(days[ind])
			#for m2 in re.finditer(r'<span class="_3rXWJKZF">(.*?)</span>', trlist):
			for m2 in re.finditer(r'<span class="StyledNumber__value__3rXW">(.*?)</span>', trlist):
				#print m2.group(1)
				row.append(m2.group(1))
			row = [row[0]]+[int(float(_r.replace(',',''))) for _r in row[1:]]
			#print "日次データ: ", row
			price_list.append(row)
	except Exception as e:
		print("!!! Yahoo価格リスト解析エラー（フォーマット変更？）", e)
	if price_current == 0:
		try:
			price_current = price_list[0][6]
			print("現在株価を履歴から取得", price_current)
		except IndexError:
			print("!!!現在株価取得できず")
	# 株式分割の調整
	adjust_divide_price(price_list)
	return price_current, price_list

def parse_price_text_yahoo(text):
	# ETFなどに使われてる古いっぽいフォーマット
	# 現在価格
	m = re.search(r'<td class="stoksPrice">(.*?)</td>', text)
	if m:
		print("Yahoo価格: 古いフォーマット")
		return parse_price_text_yahoo_old(text)
	# 新しいっぽいフォーマット	
	#print "len_price: ", len(price_list)
	print("Yahoo価格: 新しいフォーマット")	
	return parse_price_text_yahoo_new(text)


def parse_price_text(text):
	"""yahoo価格情報htmlから
	売り圧力レシオ、ローソク足ボラティリティの解析
	ポケットピポット、ブレイクアウト
	Args:
		text(str): yahooファイナンスhtml
	Returns:
		dict:
	"""
	price = {}
	price_current, price_list = parse_price_text_yahoo(text)
	if not price_list:
		return {},[] # 何も更新しない

	price["price"] = price_current
	#---- ここからprice_listを使って各種価格に伴う指標の計算
	#---- 売り圧力レシオとローソク足ボラティリティ
	#for p in price_list:
	#	print p
	# 売り圧力レシオの計算
	sell_pressure_ratio = calc_sell_pressure_ratio(price_list)
	
	# "20日,5日(,買い集め)"のフォーマット
	price["sell_pressure_ratio"] = sell_pressure_ratio[0:3]
	price["stddev_volatility"] = sell_pressure_ratio[3:5]
	
	# 出来高
	res_dict = calc_avg_volume_d(price_list)
	price["avg_volume_d"] = [res_dict["avg_volume_d20"], res_dict["avg_volume_d5"]]

	#---- ポケットピポット
	# 過去10日間で出来高の最も多い陽線の日
	# 出来高収縮では50ma、ブレイク後の上昇トレンドは10maを使うのが本来
	# TODO: 本当は50maを参照したいが、50日分のデータが必要..
	pockets = []
	for ind in range(10):
		# 過去10日間の出来高
		volumes = []
		prices = []
		# 10日の下落日出来高を集める
		for j in range(10):
			try:
				if price_list[ind+j][6] < price_list[ind+j+1][6]:
					volumes.append(price_list[ind+j][5])
				prices.append(price_list[ind+j][6])
			except IndexError:
				print("%d日目のデータなし"%j)
				break
		if len(prices) == 0:
			break
		ma10 = sum(prices)/len(prices)
		down_vol = max(volumes) if len(volumes)>0 else 0 # 下落日の出来高
		#down_vol = sum(volumes) # 下落日の出来高
		if price_list[ind][6] > price_list[ind+1][6]: # 上昇日
			vol = price_list[ind][5]
		else:
			continue
		ref_price = min(price_list[ind][3], price_list[ind+1][6]) # 安値と前日終値
		kairi = round(100*float(ref_price-ma10)/ma10,1) # 安値からのma10との乖離
		#print "down_vol=", down_vol, "vol=", vol, "ma10=", ma10, "kairi=", kairi
		if vol > down_vol and kairi <= 4:
			m = re.search(r"(\d*?)年(\d*?)月(\d*?)日", price_list[ind][0])
			day = m.group(2)+"/"+m.group(3)
			print("ポケットピポット:%s(%+d)"%(day, kairi))
			pockets.append("%s,%d"%(day,kairi))
		#break
	price["pocket_pivot"] = pockets
	#raise
	
	#---- ブレイクアウト
	breaks = []
	for ind in range(10):
		# 過去20日平均出来高
		avg_vol = 0
		avg_count = 0
		for j in range(20):
			if ind+1+j >= len(price_list):
				continue	
			avg_vol += price_list[ind+1+j][5] # +1:前日から
			avg_count += 1
		if avg_count == 0:
			continue
		avg_vol /= avg_count
		# 過去10日ma
		def get_ma10(diff=0):
			ma_count = 0
			ma10 = 0
			for j in range(10):
				if ind+j+diff >= len(price_list):
					continue
				ma10 += price_list[ind+j+diff][6] # 終値
				ma_count += 1
			if ma_count == 0:
				return 0
			ma10 /= ma_count
			return ma10
		# 当日安値と前日終値の安い方
		if price_list[ind][3] < price_list[ind+1][6]:
			ma10 = get_ma10(0)
			ref_price = price_list[ind][3]
		else:
			ma10 = get_ma10(1)
			ref_price = price_list[ind+1][6]
		if ma10 == 0:
			continue
		kairi = round(100*float(ref_price-ma10)/ma10, 1)
		vol = price_list[ind][5]
		#print "AVG:", ind, avg_vol, avg_count, vol, kairi
		if vol >= 1.5*avg_vol and kairi <= 5:
			#TODO: ボラティリティ的なブレイクアウトを見たほうが良いが、
			#ややめんどうなのでまずはma乖離で ローソク足ボラティリティを使えば良い
			if price_list[ind][6] > price_list[ind+1][6]:
				m = re.search(r"(\d*?)年(\d*?)月(\d*?)日", price_list[ind][0])
				day = m.group(2)+"/"+m.group(3)
				per = (100*vol/avg_vol-100)
				print("ブレイク:%s,%d"%(day,per))
				breaks.append("%s,%d"%(day,per))
	price["breakout"] = breaks
	#print breaks
	#---- 過去価格
	past_prices = []
	LOG_DAY = 10
	for ind in range(LOG_DAY):
		if ind >= len(price_list):
			continue
		m = re.search(r"(\d*?)年(\d*?)月(\d*?)日", price_list[ind][0])
		dt = datetime.strptime(m.group(1)+"/"+m.group(2)+"/"+m.group(3), "%Y/%m/%d").date()
		pr = price_list[ind][6] #int 終値
		past_prices.append((dt,pr))
		#day = m.group(2)+"/"+m.group(3)
	#print "過去価格", past_prices
	price["price_log"] = past_prices
	# TODO: 週次でやっている20MA押しをやりたい

	return price, [price_list[0][6], price_list[0][2], price_list[0][3]]

def get_price_log(price_list, dt):
	"""
	price_list: 過去価格データ　(日付、価格)タプルのリスト
	dt: 価格を欲しい日付(date型)
	(list, date) -> int
	"""
	if dt:
		for pr in price_list:
			#print pr[0], "<->", dt
			if pr[0] <= dt: #過去の日を許容する
				return pr[1]
	return 0

def get_price_data_yahoo(code_s, upd=UPD_INTERVAL):
	"""yahooから日次価格データをパースしてdictで返す
	type: (str, bool) -> dict
	Returns:
		dict<int, T>: 解析した価格情報
		list<int>: 現在価格
	"""
	cache = (upd <= UPD_REEVAL)
	price_text = get_daily_data_yahoo(code_s, upd)
	if not price_text:
		return {}, []
	#print ux_cmd_head(price_text, 3)
	print(">>>>> %sの価格データを解析 "%code_s)
	parsed_data, cur_prices = parse_price_text(price_text)
	stat = os.stat(PRICE_FNAME%code_s)
	date = datetime.fromtimestamp(stat.st_mtime)
	price = parsed_data.get("price",0)
	print("<<<<< 解析完了 ", price, date)
	# 情報を追加して返す
	#parsed_data["code"] = code_s
	set_db_code(parsed_data, code_s)
	if price > 0:
		parsed_data["access_date_price"] = date
	return parsed_data, cur_prices

def get_weekly_price_data(code_s, upd=UPD_INTERVAL, prices=[]):
	"""
	週次データからRSを返す
	type: str -> dict
	"""
	cache = (upd <= UPD_REEVAL)
	price_htmls = get_weekly_html(code_s, cache)
	if not price_htmls:
		print("!!! 株探から週次価格を取得できません")
		return {}
	#print ux_cmd_head(price_htmls[0], 3)
	print(">>>>> %sの週次価格データを解析 "%code_s)
	parsed_data_w = parse_pricew_htmls_kabutan(price_htmls, prices)
	# stat = os.stat(PRICE_FNAME%code)
	# date = datetime.fromtimestamp(stat.st_mtime)
	print("<<<<< 解析完了 ")
	return parsed_data_w

def get_daily_price_kabutan(code_s, upd=UPD_INTERVAL):
	"""	日次の価格データを取得する
	マーケットデータ作成時に呼ばれる
	"""
	cache = (upd <= UPD_REEVAL)
	price_html = get_daily_html_kabutan(code_s, cache)
	if not price_html:
		print("!!! 株探から日次価格を取得できません")
		return {}
	print(">>>>> %sの日次価格データを解析 "%code_s)
	parsed_data_d = parse_price_d_html_kabutan(price_html)
	print("<<<<< 解析完了 ")	
	return parsed_data_d

def get_price_data(code_s, upd=UPD_INTERVAL):
	"""	価格情報を取得して(yahooから)
	更新情報を返す
	type: (str,bool) -> dict
	"""
	# 日次データ
	parsed_data, cur_prices = get_price_data_yahoo(code_s, upd)
	# 週次データを取得してRSを求める
	parsed_data_w = get_weekly_price_data(code_s, upd=upd, prices=cur_prices)
	parsed_data.update(parsed_data_w)
	return parsed_data

def get_spr_expr(sprs, sprs_w):
	"""　売り圧力レシオと買い集めの文字列表現を返す
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
		diff = (sprs_w[2]-sprs_w[0])
		eval = step_func(diff, [-10, -5, 0, 5, 10], ["E","D","C","B","A"])
		exprs.append(eval)
	# 日次
	if len(sprs) >= 3:
		diff = (sprs[2]-sprs[0])
		eval = step_func(diff, [-10, -5, 0, 5, 10], ["E","D","C","B","A"])
		exprs.append(eval)
	sell_press = ",".join(exprs)
	return sell_press

def main():
	"""価格データを取得、解析する
	"""
	#TODO: <meta http-equiv="Pragma" content="no-cache">
	# となっているとき、取れていない
	#TODO: ローソク足ボラティリティの計算が疑問　終値の平均で割るところ
	#TODO: 1/5/20出来高変化率もほしい
	code_list = ["6580"]
	for code_s in code_list:
		print("-"*30)
		print("%sの価格を更新します"%code_s)
		price_dict = get_price_data(code_s, UPD_INTERVAL) #UPD_INTERVAL/UPD_REEVAL/UPD_FORCE
		print(price_dict)
		# td = datetime.today().date()
		# print get_price_log(price_dict["price_log"], td)
		# print get_price_log(price_dict["price_log"], td-timedelta(1))
		# print get_price_log(price_dict["price_log"], td-timedelta(5))

if __name__ == '__main__':
	main()
