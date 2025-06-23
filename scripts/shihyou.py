#!/usr/bin/env python3

import re

# from scipy import stats
# import numpy as np

from ks_util import *

import rironkabuka

# ==================================================
# 株探から指標データを取得
# ================================================== 


def get_from_kabutan(html):
	# 財務テーブルを取得
	# re.S:"."に改行を含める ?:non greedy(最小マッチング)
	print("---> 財務テーブル取得")
	zaimu_table_html = re.search(r"財務 【実績】.*?<table>(.*?)</table>", html, re.S)
	if not zaimu_table_html:
		print("!!! kabutanHTML解析エラー フォーマット変わってる　もしくはインデックスETF")
		return {}
	zaimu_table_html = zaimu_table_html.group(1) if zaimu_table_html else ""
	def get_table_html(table_html):
		try:
			ms_td = re.findall(r"<tr >.*?</tr>", table_html, re.DOTALL) # なんかスペースあいてる
		except IndexError as e:
			print("!!! 財務情報がありません")
		if not ms_td:	
			print("!!! kabutanHTML解析エラー")
			return {}
		return ms_td
	ms_td = get_table_html(zaimu_table_html)
	latest_zaimu_html = ms_td[-1]
	# print latest_zaimu_html
	print("<--- 財務テーブル取得完了")

	# 取得した財務htmlから
	# 有利子負債倍率を取得
	def get_table_row(html):
		ReStr_Td = r"<td.*?>(.*?)</td>"
		msitr = re.finditer(ReStr_Td, html, re.DOTALL)
		items = [m.group(1) for m in msitr]
		# print "items:", items
		# return (items[1], items[5]) # 5: 有利子負債倍率の列番号(期の項目はないため)
		return items
	items = get_table_row(latest_zaimu_html)
	jiko_ratio = items[1]
	debut = items[5]
	def get_table_prev(val, item_ind):
		try:		
			if val.find("－") >= 0:
				print("有利子負債倍率一つ前を見る")
				items = get_table_row(ms_td[-2])
				val = items[item_ind]
				if val.find("－") >= 0:
					val = 0
				else:
					val = float(val)
		except (ValueError, IndexError) as e:
			print("!!! 有利子負債倍率か自己資本比率の値がありません")
			val = 0
		return val
	debut = get_table_prev(debut.replace(",",""), 5)
	jiko_ratio = get_table_prev(jiko_ratio, 1)
	print("有利子負債自己資本比率:", debut)
	print("自己資本比率:", jiko_ratio)
	shiyo_data = {}
	shiyo_data["debt_ratio"] = float(debut)
	shiyo_data["capital_ratio"] = float(jiko_ratio)

	# ---- ROE, 売上営業利益率取得
	profit_html_m = re.search(r'<table>.*?<th scope="col" class="fb_02">　ＲＯＥ</th>.*?<tbody>(.*?)</tbody>.*?</table>', html, re.S)
	if not profit_html_m:
		print("!!! 収益性htmlのフォーマットが変わっている？")
		return shiyo_data
	profit_html = profit_html_m.group(1)
	profit_htmls = get_table_html(profit_html)
	def get_roe_and_profit_margin(prof_html):
		items = get_table_row(prof_html)
		# ROEはインデックス3, 利益率はインデックス2(期はカウントしない)
		try:
			roe = float(items[3])
			profit_margin = float(items[2])
		except ValueError:
			roe = 0
			profit_margin = 0
		return roe, profit_margin
	roe, profit_margin = get_roe_and_profit_margin(profit_htmls[-1])
	if roe == 0 or profit_margin == 0:
		print("ROE一つ前を見る")
		try:
			roe, profit_margin = get_roe_and_profit_margin(profit_htmls[-2])
		except IndexError:
			pass
	print("ROE:", roe)
	print("売上営業利益率:", profit_margin)
	shiyo_data["ROE"] = roe
	shiyo_data["profit_margin"] = profit_margin

	return shiyo_data

def parse_jikasogaku_kabutan(html):
	"""
	株探htmlから時価総額(億円)を取得する
	"""
	jikasogaku_m = re.search(r'<td colspan="2" class="v_zika2">(.*)<span>億円</span></td>', html)
	if not jikasogaku_m:
		print("!!! 時価総額の値が取得できません(フォーマット変更？または上場廃止)")
		return 0
	else:
		if jikasogaku_m.group(1).find("兆") >= 0:
			# 11<span>兆</span>5899
			# 兆対応
			jikasogaku_m = re.search(r'(.*)<span>兆</span>(.*)', jikasogaku_m.group(1))
			return float(jikasogaku_m.group(1))*10000+float(jikasogaku_m.group(2).replace(",",""))
		else:
			return float(jikasogaku_m.group(1).replace(",",""))

def get_from_kabutan_base(html, shiyo_data):
	# 時価総額
	jikasogaku = parse_jikasogaku_kabutan(html)
	if jikasogaku == 0:
		shiyo_data["jikasogaku"] = 0
		return shiyo_data # その後PER,PSR計算できないので
	else:
		shiyo_data["jikasogaku"] = jikasogaku #億円
		print("時価総額(億円):", shiyo_data["jikasogaku"])

	# ---- PER
	stockinfo_m = re.search(r'<div id="stockinfo_i3">(.*?)</div>', html, re.DOTALL)
	per = 0.0
	need_per = False
	if stockinfo_m:
		# <td>48.8<span>倍</span></td>
		# per_m = re.search(r'<td>(.*)<span>倍</span></td>', stockinfo_m.group(1))
		per_ms = re.finditer(r'<td>(.*)<span.*?倍</span></td>', stockinfo_m.group(1))
		# だいぶ変則的だが、PERが5桁の場合「倍」が省略されるためその場合はとばす
		# iteratorはリセットや長さがわからないため再度finditer
		per_ms_len = sum(1 for _ in per_ms)
		per_ms = re.finditer(r'<td>(.*)<span.*?倍</span></td>', stockinfo_m.group(1))
		# print "per_ms:", per_ms, per_ms_len
		if per_ms and per_ms_len >= 3:
			try:
				per_m =  next(per_ms)
				if per_m:
					per = float(per_m.group(1))
					print("PER:", per)
					shiyo_data['PER'] = per
			except ValueError:
				print("PER計算できない", per_m.group(1))
				shiyo_data['PER'] = 0
				need_per = True # PERを実績から計算する
		else:
			print("!!! PER取得できず(フォーマット変更？)")
			need_per = True
		# PBRも
		pbr_m = next(per_ms)
		if pbr_m:
			try:
				pbr = float(pbr_m.group(1))
				print("PBR:", pbr)
				shiyo_data['PBR'] = pbr
			except ValueError:
				print("PBR取得できず", pbr_m.group(1))
		# 信用倍率、信用買残、信用売残
		name = "credit_ratio"
		try:
			dividend_nd = next(per_ms)
			if dividend_nd:
				try:
					val = float(dividend_nd.group(1))
					print(name+":", val)
					shiyo_data[name] = val
				except ValueError:
					print(name+"取得できず", dividend_nd.group(1))
		except StopIteration:
			print(name+"取得できず")
		margin_m = re.search(r'<h2 class="mgt6">信用取引&nbsp;\(単位:千株\)</h2>\r\n<table>(.*?)</table>', html, re.DOTALL)
		if margin_m:
			margin_html = margin_m.group(1)
			margin_itr = re.finditer('<td>(.*?)</td>', margin_html) 
			if margin_itr:
				try:
					margin_m2 = next(margin_itr)
					# tmp = margin_m2.group(1) # 売り残
					td = margin_m2.group(1)
					shiyo_data["credit_sell"] = int(float(td.replace(",","")))*1000 # 買い残
					margin_m2 = next(margin_itr)
					td = margin_m2.group(1)
					shiyo_data["credit_buy"] = int(float(td.replace(",","")))*1000 # 買い残
					margin_m2 = next(margin_itr)
					# tmp = margin_m2.group(1) # 信用倍率
				except StopIteration:
					pass
			
		# 配当利回り"dividend_yield"
		stockinfo_m_text = stockinfo_m.group(1)
		m = re.search(r'<td>(.*)<span.*?％</span></td>', stockinfo_m_text)
		if m:
			try:
				val = float(m.group(1))
				print("配当利回り:", val)
				shiyo_data["dividend_yield"] = val
			except ValueError:
				print("配当利回り取得できず", m.group(1))

	if per == 0.0 and not need_per:
		print("!!! PER取得できず(フォーマット変更？)")
		
	# ---- PSR
	psr = 0.0
	gyoseki_m = re.search(r'<div class="gyouseki_block">\r\n<div class="title">(.*?)</table>\r\n</div>', html, re.DOTALL)
	uriage_lst = []
	profit = 0
	if gyoseki_m:
		# print gyoseki_m.group(0)
		latest_term = ""
		for row_m in re.finditer(r'<tr>\r\n    <th scope=\'row\'><span class="kubun1">(.*?)</span>(.*?)</th>(.*?)</tr>', \
			gyoseki_m.group(1),re.DOTALL):
			# print row_m.group(1), row_m.group(2)
			# 一列目が売上高なので最初に見つかった<td></td>
			# print row_m.group(1), row_m.group(2)
			uriage_m = re.search(r'<td>(.*?)</td>\r\n    <td>(.*?)</td>\r\n    <td>(.*?)</td>', row_m.group(3))
			cur_term = row_m.group(2).replace("&nbsp;", "")
			try:
				uriage = float(uriage_m.group(1).replace(",",""))
				latest_term = cur_term
				uriage_lst.append(uriage)
			except ValueError:
				print("  売上高取得できず:", uriage_m.group(1), cur_term)
			
			try:
				profit = float(uriage_m.group(3)) #最終益
				# latest_term = cur_term
			except ValueError:
				print("  最終益取得できず:", uriage_m.group(3), cur_term)
			try:
				keijo = float(uriage_m.group(2)) # 経常益
			except ValueError:
				keijo = 0
				print("   経常益取得できず", uriage_m.group(2), cur_term)
			# print uriage
		if uriage_lst:
			uriage = uriage_lst[-1] #直近
			if uriage > 0:
				psr = round(jikasogaku/uriage, 1)
				print("PSR: %.1f 直近売上高(億円): %.1f(%s)"%(psr,uriage, latest_term))
			else:
				print("売上が0のためPSR計算できず")
		if need_per and profit != 0:
			per = round(jikasogaku/profit, 1)
			print("PERを実績から計算: ", per, "<- 最終益=%.1f億"%profit)
			if per > 0:
				shiyo_data['PER'] = per
		mper = 0
		if keijo != 0 and profit != 0:
			if profit >= keijo*0.6 and profit <= keijo*0.7:
				mper = round(jikasogaku/profit, 1)
				mper = max(mper, 0)
			else:
				print("修正PERを適用")
				mper = round(jikasogaku/(keijo*0.65), 1)
				mper = max(mper, 0)
			print("MPER:", mper)
		if mper != 0:
			shiyo_data['MPER'] = mper
		else:
			shiyo_data['MPER'] = per
	if psr == 0.0:
		print("!!! PSR取得できず(フォーマット変更？)")
	else:
		shiyo_data['PSR'] = psr
	return shiyo_data

def analyze_from_kabutan(code_s, upd=UPD_INTERVAL):
	# code = int(code)
	# 自己資本比率
	import rironkabuka
	html = rironkabuka.get_kabutan_html(code_s, upd)
	shiyo_data = get_from_kabutan(html)
	# 時価総額
	html = rironkabuka.get_kabutan_base_html(code_s, upd)
	shiyo_data = get_from_kabutan_base(html, shiyo_data)
	# print shiyo_data
	return shiyo_data

def calc_shihyo_pt(code_s, upd=UPD_INTERVAL, stock={}):
	"""指標計算本体
	"""
	# 理論株価をどう絡めるか？
	# PER, PBRは理論株価に含まれる PSRは外して良い
	# ROEも含まれる 営業利益率は含むべき
	# code = int(code)
	# 自己資本比率、時価総額、株主比率
	shiyo = analyze_from_kabutan(code_s, upd)
	# TODO: 0のときの5年計算してしまうため
	# print shiyo_data
	JIKASOGAKU_MAX = 30
	DEBT_RATIO_MAX = 30 # 減点用
	# PER_MAX = 25
	# PBR_MAX = 10
	# PSR_MAX = 35
	# 時価総額(小型株ファクター)
	# 500億>300億>100億
	jikasogaku_pt = 0
	if "jikasogaku" in shiyo:
		if shiyo["jikasogaku"] <= 10000/100:
			jikasogaku_pt = JIKASOGAKU_MAX
		elif shiyo["jikasogaku"] <= 30000/100:
			jikasogaku_pt = JIKASOGAKU_MAX/2
		elif shiyo["jikasogaku"] <= 50000/100:
			jikasogaku_pt = JIKASOGAKU_MAX/3
	# 発行株式数(小型株ファクター)

	# 有利子負債自己資本比率(クォリティファクター)
	debt_ratio_pt = 0
	if "debt_ratio" in shiyo:
		if shiyo["debt_ratio"] >= 0:
			debt_ratio_pt = step_func((shiyo["debt_ratio"]), [0.0, 0.5, 1.0, 3.0], [DEBT_RATIO_MAX/3, 0, -DEBT_RATIO_MAX/2, -DEBT_RATIO_MAX])
		else:
			debt_ratio_pt = -DEBT_RATIO_MAX
	else:
		print("!!! 有利子負債自己資本比率データがありません")
		debt_ratio_pt = 0
	# 理論株価
	# 理論価格乖離率[0, 20, 40, 60, 100]
	# 上限[30, 60, 100, 150, 300]
	# 下限[-60, -30, 0, 30]
	thoery_total_pt = rironkabuka.calc_theory_pt(code_s, stock)
	
	# PER(バリューファクター)
	# per_pt = 0
	# if shiyo.has_key("MPER"): # PER->MPERに
	# 	if shiyo["MPER"] > 0:
	# 		per_pt = step_func(shiyo["MPER"], [0, 30, 60], [PER_MAX, PER_MAX/2, 0])
	# # PBR(バリューファクター)
	# pbr_pt = 0
	# if shiyo.has_key("PBR"):
	# 	pbr_pt = step_func(shiyo["PBR"], [0, 1], [PBR_MAX, 0])
	# # PSR(バリューファクター)
	# psr_pt = 0
	# if shiyo.has_key("PSR"):
	# 	psr_pt = step_func(shiyo["PSR"], [0, 0.75, 2.5, 10], [PSR_MAX, PSR_MAX/2, PSR_MAX/4, 0])

	print("時価総額pt: %d/%d"%(jikasogaku_pt, JIKASOGAKU_MAX))
	print("有利子負債自己資本比率pt: %d/%d"%(debt_ratio_pt, -DEBT_RATIO_MAX))
	# print "PER pt: %d/%d"%(per_pt, PER_MAX)
	# print "PBR pt: %d/%d"%(pbr_pt, PBR_MAX)
	# print "PSR pt: %d/%d"%(psr_pt, PSR_MAX)
	# TODO: ROEと売上営業利益率は株探から取得可能(クォリティファクター)
	# TODO: 配当利回りも？

	# shiyo_pt = jikasogaku_pt + debt_ratio_pt+ per_pt+psr_pt
	shiyo_pt = jikasogaku_pt + debt_ratio_pt+ thoery_total_pt
	shiyo_pt = int(cramp(shiyo_pt, 0, 100))
	print("----------")
	print("指標PT:", shiyo_pt)
	print("----------")
	return shiyo_pt, shiyo

CACHE_DIR_KABUTAN = os.path.join(DATA_DIR, "stock_data", "kabutan")
URL_CODE_KABUTAN = "http://kabutan.jp/stock/finance?code=%s&mode=k"

def get_shihyo_data(stocks, code_s, upd=UPD_INTERVAL):
    """
    指標の更新・取得
    type: (dict<stock_db>, str, bool) -> dict<stock>
    """
    print("-" * 5, "指標の取得計算 upd=", upd)
    tables = {}
    stock = stocks[code_s] if code_s in stocks else {}
    # わかりにくいがここで通信している
    shihyo_pt, shihyo_data = calc_shihyo_pt(code_s, upd, stock)
    print("-" * 5, "指標の取得計算完了")
    # 指標データの取得元ファイルの日付を格納
    cache_path = os.path.join(
        CACHE_DIR_KABUTAN, get_http_cachname(URL_CODE_KABUTAN % str(code_s))
    )
    tables["access_date_shihyo"] = get_file_datetime(cache_path)
    print("date_shihyo:", tables["access_date_shihyo"])
    tables["shihyo_pt"] = shihyo_pt
    # 指標データ登録
    tables["shihyo"] = shihyo_data
    # tables["PER"] = shihyo_data["PER"]
    # tables["PBR"] = shihyo_data["PBR"]
    # tables["PSR"] = shihyo_data["PSR"]
    # tables["ROE"] = shihyo_data["ROE"]
    # tables["profit_margin"] = shihyo_data["profit_margin"]
    # tables["debt_ratio"] = shihyo_data["debt_ratio"]
    # tables["capital_ratio"] = shihyo_data["capital_ratio"]

	tables["code_s"] = code_s

	return tables

def get_credit_expr(stock_data):
	# 信用倍率
	ind_credit = ""
	if "credit_ratio" in stock_data["shihyo"]:
		ind_credit = str(round(stock_data["shihyo"]["credit_ratio"], 2))
	# 出来高買い残倍率
	volume_creditbuy_expr = ""
	if "credit_buy" in stock_data["shihyo"]:
		credit_buy = stock_data["shihyo"].get("credit_buy")
		# 価格更新時に更新される平均出来高
		avg_volume_dat = stock_data.get("avg_volume_d",[]) 
		if avg_volume_dat and avg_volume_dat[0]>0:
			avg_volume = avg_volume_dat[0] # 0: 20日のもの
			volume_creditbuy = float(credit_buy)/avg_volume
			if volume_creditbuy < 1:
				volume_creditbuy_expr = "%.2f"%volume_creditbuy
			elif volume_creditbuy < 10:
				volume_creditbuy_expr = "%.1f"%volume_creditbuy
			else:
				volume_creditbuy_expr = "%d"%volume_creditbuy
	return "売%s,出%s"%(ind_credit,volume_creditbuy_expr)

def get_shihyo_expr(stock_data):
	def get_indicator_exp(key, keta=0):
		if key in stock_data["shihyo"]:
			try:
				if keta == 0:
					return str(int(stock_data["shihyo"][key]))
				else:
					return str(round(stock_data["shihyo"][key], keta))
			except TypeError:
				return "Error:"+str(stock[0])
		return ""

	ind_marketcap = "%d"%int(stock_data.get("market_cap",0))
	if ind_marketcap == 0:
		ind_marketcap = "-"
	ind_per = "%s"%get_indicator_exp("MPER")
	ind_pbr = "%s"%get_indicator_exp("PBR", 1)
	ind_psr = "%s"%get_indicator_exp("PSR", 1)
	ind_roe = "%s"%get_indicator_exp("ROE")
	ind_margin = "%s%%"%get_indicator_exp("profit_margin")
	ind_debt = "%s"%get_indicator_exp("debt_ratio", 2)
	ind_capital = "%s%%"%get_indicator_exp("capital_ratio")
	# ind_credit = "%s"%get_indicator_exp("credit_ratio", 2)
	ind_dividend_yield = "%s"%get_indicator_exp("dividend_yield", 1)
	indicator = "%s億 PER%s PBR%s PSR%s 配当%s ROE%s 利益率%s 負債%s 自己%s"%(\
		ind_marketcap, ind_per, ind_pbr, ind_psr, ind_dividend_yield,\
		ind_roe, ind_margin, \
		ind_debt, ind_capital)
	return indicator

def main():
	"""
	指標:PERやPSRなど経営上の指標　を取得、分析する
	"""
	# code_list = [1768,1959,1820,1793,1352,2152,1801,1812,1332,1764,11827,1782,1899,1905,1301,1810,1824,2109,1946,2162,1720,1788,1934,1870,1376,1911,1515,1885,1939,1941]
	# code_list = [3038,2301,6095,8001,8031,8035,3668]
	code_list = ["9509"] # 5034,4436,7808, 2780,6083
	# import make_stock_db
	# stocks = make_stock_db.load_stock_db()
	for code_s in code_list:
		print("-"*30)
		print("%sの指標を計算します"%code_s)
		import make_stock_db as db
		stock = db.load_cacehd_stock_db(code_s)
		shiyo_pt, shihyo = calc_shihyo_pt(code_s, UPD_REEVAL, stock) # UPD_FORCE
		print(shihyo)
		# print get_shihyo_expr(stock)
		# print get_credit_expr(stock)

if __name__ == '__main__':
	# TODO: 9272ブティックス 取得できてない？
	#!!! 不正な関連銘柄です
	#もでてる
	main()