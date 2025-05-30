#!/usr/bin/python
# -*- coding: utf-8 -*-
import re
import os
#from tabnanny import check

from ks_util import *

CACHE_DIR = os.path.join(DATA_DIR, "stock_data/kabutan/")
URL_CODE = "http://kabutan.jp/stock/finance?code=%s&mode=k"

def parse_kabutan_account2(html):
	"""
	株探の業績ページhtmlから理論株価を計算
	type: str -> dict
	"""
	tables = {}
	# 通期データ
	print "[今季業績の解析]"
	year_tbl_m = re.search(r'<div class="title1">通期</div>.*?<table>(.*?)</table>', html, re.S)
	if not year_tbl_m:
		print "!!! 通期テーブルが取得できない（フォーマット変更？）"
		return tables
	year_tbl_html = year_tbl_m.group(1)

	def parse_gyoseki_html_table(tbl_html, tble_name):
		#print year_tbl_html
		print "----", tble_name, "の解析"
		table = []

		# htmlの列要素は↓
		# 決算期	売上高	営業益	経常益	最終益	修正1株益	１株配 発表日
		# テーブルの期ごとの行要素を抽出
		if tble_name == "gyoseki_current":
			table_re = r'<tr >(.*?)</tr>'
		elif tble_name == "gyoseki_quarter":	# 何故かタグが違うので対応
			table_re = r'<tr  >(.*?)</tr>'
		for row_year_m in re.finditer(table_re, tbl_html, re.S):
			# テーブルの行項目を処理
			ldict = {}
			row_year_html = row_year_m.group(1)
			#print "通期html:", row_year_html
			# 決算期
			if tble_name == "gyoseki_current":
				# 微妙にフォーマット違うんだよなあ・・
				kessanki_re = r'<th scope="row" >(.*)(\d\d\d\d\.\d\d).*</th>'
			elif tble_name == "gyoseki_quarter":
				#print "Hoge", row_year_html
				kessanki_re = r'<th scope="row">(.*)(\d\d\.\d\d\-\d\d).*</th>'
			term_m = re.search(kessanki_re, row_year_html)
			if not term_m:
				print "!!! 決算期を取得できない(フォーマット？)"		
			if term_m.group(1).find("予") >= 0:
				ldict["kessanki"] = "予"+term_m.group(2)
			else:
				ldict["kessanki"] = term_m.group(2)
			#print "----決算期:", ldict["kessanki"]
			# 売上高以降はtd項目で
			# 列項目をリストに
			values = []
			for val_m in re.finditer(r'<td.*?>(.*?)</td>', row_year_html):
				val = val_m.group(1)
				values.append(val)
			# 売上高
			val_uriagedaka = values[0]
			if val_uriagedaka == "－":
				#print "  !!! 売上高未発表"
				ldict["uriagedaka"] = "－"
			else:
				try:
					ldict["uriagedaka"] = int(val_uriagedaka.replace(",",""))
				except ValueError:
					print "  !!! 売上高値が不正のため中断", val_uriagedaka.replace(",","")
					break
			#print "売上高:", ldict["uriagedaka"]
			# 営業益、経常益
			val_eigyo = values[1]
			val_keijo = values[2]
			if val_eigyo == "－" and val_keijo == "－":
				print "  !!!営業利益、経常利益未発表", ldict["kessanki"]
				ldict["keijoeki"] = "－"
				ldict["eigyoeki"] = "－"
			elif val_eigyo == "－":
				print "  !!! 営業利益未発表のため経常利益で代用"
				ldict["keijoeki"] = int(float(val_keijo.replace(",","")))
				ldict["eigyoeki"] = ldict["keijoeki"]
			elif val_keijo == "－":
				print "  !!! 経常利益未発表のため営業利益で代用"
				ldict["eigyoeki"] = int(float(val_eigyo.replace(",","")))
				ldict["keijoeki"] = ldict["eigyoeki"]
			else:
				ldict["eigyoeki"] = int(float(val_eigyo.replace(",","")))
				ldict["keijoeki"] = int(float(val_keijo.replace(",","")))
			#print "営業益、経常益:", ldict["eigyoeki"], ldict["keijoeki"]
			# 最終益
			val_profit = values[3]
			if val_profit == "－":
				#print "  !!! 最終益未発表"
				ldict["profit"] = "－"
			else:
				ldict["profit"] = int(float(val_profit.replace(",","")))
			#print "最終益:", ldict["profit"]
			# 一株益
			val_profit_per1	= values[4]
			if val_profit_per1 == "－":
				#print "  !!! 一株益未発表"
				ldict["profit_per1"] = 0
			else:
				ldict["profit_per1"] = float(val_profit_per1.replace(",",""))
			#print "一株益:", ldict["profit_per1"]

			if tble_name == "gyoseki_current":
				if values[5] == "－":
					#print "  !!! 一株配当未発表"
					ldict["diviednd_per1"] = 0
				else:
					ldict["diviednd_per1"] = float(values[5].replace(",",""))
				#print "一株配当:", ldict["diviednd_per1"]
			elif tble_name == "gyoseki_quarter": # 3ヶ月業績テーブル
				if values[5] == "－":
					#print "  !!! 売上高営業利益率未発表"
					ldict["uriage_eigyo_ratio"] = 0							
				else:
					ldict["uriage_eigyo_ratio"] = float(values[5].replace(",",""))
				#print "売上高営業利益率:", ldict["uriage_eigyo_ratio"]
			# 日付
			ldict["date"] = (values[6])

			# 欲しい列要素は、
			# 決算期/売上高/営業益/経常益/最終益/一株益/一株配当or売上営業利益率
			ldict_list = [ldict["kessanki"], ldict["uriagedaka"], ldict["eigyoeki"], \
						ldict["keijoeki"], ldict["profit"], ldict["profit_per1"], \
						ldict.get("diviednd_per1", ldict.get("uriage_eigyo_ratio",""))]
			print "|", str(ldict_list).decode('string_escape')
			table.append(ldict_list)
		# for文終了
		
		#---- 前期比/前年同期比の行
		ldict = {}
		if tble_name == "gyoseki_current":
			ratio_m = re.search(r'<tr>\n<th scope="row">前期比</th>(.*?)</tr>', tbl_html, re.S)
			ratio_html = ratio_m.group(1)
		elif tble_name == "gyoseki_quarter":			
			ratio_m = re.search(r'<tr>\r\n<th scope="row">前年同期比</th>(.*?)</tr>', tbl_html, re.S)
			ratio_html = ratio_m.group(1)
		#print ratio_html
		ratio_rows = []
		for ratio_row in re.finditer(r'<td>(.*?)</td>', ratio_html):
			val_m = re.search(r'[\d\+\-\.－]+', ratio_row.group(1))
			val = val_m.group(0) if val_m else ""
			ratio_rows.append(val)
		ldict["kessanki"] = "前期比" if tble_name == "gyoseki_current" else "前年同期比"
		ldict["uriagedaka"] = (ratio_rows[0])
		ldict["eigyoeki"] = (ratio_rows[1])
		ldict["keijoeki"] = (ratio_rows[2])
		ldict["profit"] = (ratio_rows[3])
		ldict["profit_per1"] = (ratio_rows[4])
		if tble_name == "gyoseki_current":
			ldict["diviednd_per1"] = ratio_rows[5]
		elif tble_name == "gyoseki_quarter":
			ldict["uriage_eigyo_ratio"] = ratio_rows[5]
		ldict["date"] = ratio_rows[6]
		for k,v in ldict.items():
			if v=="赤拡":
				print "赤拡は-20%"
				ldict[k] = -20
		#print str(ratio_rows).decode('string_escape')
		ldict_list = [ldict["kessanki"], ldict["uriagedaka"], ldict["eigyoeki"], \
					ldict["keijoeki"], ldict["profit"], ldict["profit_per1"], \
					ldict["diviednd_per1"] if tble_name == "gyoseki_current" else ldict["uriage_eigyo_ratio"]]
					#ldict.get("diviednd_per1", ldict.get("uriage_eigyo_ratio","")

		print "前期比行:", str(ldict_list).decode('string_escape')
		table.append(ldict_list)
		# 決算期、売上高、営業益、経常益、純利益、一株純利益、分割調整後一株利益or売上営業利益
		return table

	table_year = parse_gyoseki_html_table(year_tbl_html, "gyoseki_current")

	# "ー"数値修正(怪しいので封印)
	for row in reversed(table_year):
		# 前年同期比/前期比以外の行
		if row[0] == "前年同期比" or row[0] == "前期比":
			continue
		if row[1] == "－":
			print "!!! 売上高がないのでこの期のデータは無効", row[0]
			table_year.remove(row)

	# tablesへの通期、四半期テーブルの格納
	tables["gyoseki_current"] = table_year

	# 四半期データ
	quarter_tbl_m = re.search(r'<div class="title1">3ヵ月決算【実績】</div>.*?<table>(.*?)</table>', html, re.S)
	if not quarter_tbl_m:
		print "!!! 四半期テーブルが取得できない（フォーマット変更？）"
		return tables
	quarter_tbl_html = quarter_tbl_m.group(1)
	table_quarter = parse_gyoseki_html_table(quarter_tbl_html, "gyoseki_quarter")
	tables["gyoseki_quarter"] = table_quarter
	return tables

#TODO: utilに移動
def calc_growth_rate2(cur, next):
	"""
	赤字を考慮した成長率計算
	return: +1.2:20%成長 0.9:-10%成長
	"""	
	#print "cur, nex", cur, next
	if cur > 0 and next > 0:
		return float(next)/cur
	else:
		# 伸びた数字を分子とし、平均値を分母とする
		tmp = (abs(next)+abs(cur))
		if tmp == 0:
			return 1.0
		else:
			return float((next-cur)/(tmp*0.5))

def average_compound(growth):
	"""
	幾何平均 
	In: 0.2(20%), -0.1(-10%)など0を基準とした割合を渡す
	Out: 1.2(20%) 0.9(-10%)
	"""
	def calc(x,y):
		return max((1+x), 0)* max((1+y), 0)
		#return max(x, 0)* max(y, 0)
	return reduce(calc, growth)**(1.0/len(growth))

def average_compound2(growth):
	avg = sum(growth)/len(growth)
	return avg
def calc_cagr(values):
	if (len(values)-1) <= 0:
		return 0
	first = values[0]
	if first <= 0:
		return 0
	latest = float(values[-1])
	cagr = (latest/first)**(1.0/(len(values)-1)) - 1
	return cagr

def check_table(code_s, table_current, table_quarter):
	"""業績データの不足をチェック"""
	# table_quarter = tables.get("gyoseki_quarter", [])
	# table_current = tables.get("gyoseki_current", [])
	#print str(code), "gyoseki_table データ数(term quater):", len(table_current), len(table_quarter)
	# 期数:3 四半期数:2を最低限とする
	# if len(table_quarter) < 3 and len(table_current) < 2:
	if len(table_current) < 1 and len(table_quarter) < 1:
	 	print "!!! 業績を解析できていません(期数不足) cur=%d qua=%d"%\
	 	(len(table_current), len(table_quarter))
	 	return False
	if (not table_quarter) or len(table_quarter[0]) < 4:
		term_count = 0 if (not table_quarter) else len(table_quarter[0])
		print "!!! 業績を解析できていません(四半期項目数不足) %d<8"%\
		(term_count)
		return False
	if len(table_current[0]) < 4:
		print "!!! 業績を解析できていません(年期項目数不足) %d<5"%\
		(len(table_current[0]))
		return False
	# 8四半期分のデータを補充
	if len(table_quarter) >= 1:
		while len(table_quarter) <= 8:
			print "  !!! 四半期業績の期数が足りないため過去データを加えました"
			table_quarter.insert(0, table_quarter[0])
			print table_quarter[0]
			#table_quarter.insert(0, [table_quarter[0][0], 0,0,0,0,0,0])
			#print [table_quarter[0][0]
	return True, table_quarter

def calc_progress_rate(stock):
	"""DBに保持する決算データから進捗率を計算する
	Args:
		stock(dict): 通期・四半期業績データ
	Retruns:
		dict: キー:quarter/sales/profit/sales_pre/profit_pre
	"""
	table_quarter = stock.get("gyoseki_quarter", "")
	table_current = stock.get("gyoseki_current", "")
	#---- 進捗率
	# if not check_table(tables):
	# 	print "決算データ不足で進捗率取得できず"
	# 	return 0, 0, 0, 0, 0
	# table_quarter = tables.get("gyoseki_quarter", [])
	# table_current = tables.get("gyoseki_current", [])
	ret = {}
	#code = stock.get("code", 0) 
	code_s = stock.get("code_s", "")
	if not check_table(code_s, table_current, table_quarter):
		print "決算データ不足で進捗率取得できず", code_s
		return ret
	predict_data = []
	for term in table_current:
		for t in term:
			if str(t).find("予") >= 0:
				predict_data = term
				break
	#print predict_data
	if not predict_data:
		#return ret
		# 最新の年度+1年をいったん予想年度とする
		try:
			predict_data = table_current[-2]
			exists_predict = False
		except IndexError:
			print "会社予想と今期データなく進捗率取得できず", code_s
			return ret
	else:
		exists_predict = True
	predict_term = predict_data[0].replace("予", "")
	predict_sales = predict_data[1]
	predict_profit = predict_data[2]

	#print "会社予想期:", predict_term, predict_sales, predict_profit
	from datetime import datetime, date, timedelta
	tmp = predict_term.split(".")
	if exists_predict:
		predict_term_date = datetime(int(tmp[0]), int(tmp[1]), 1)
	else:
		predict_term_date = datetime(int(tmp[0])+1, int(tmp[1]), 1)

	#---- 四半期決算データを2次元配列に配置 第x四半期 y年前
	def create_quarter_data(predict_term_date):
		quarter_data = [[[]]*4 for i in range(3)] # x:第x四半期 y:y年前の四半期決算データ
		for ind, data in enumerate(table_quarter):
			term = data[0]
			tmp = term.split(".")
			try:
				year = int(tmp[0])
			except ValueError:
				# 前年比の項目なのでスルー
				continue
			tmp2 = tmp[1].split("-")
			# 年越しチェック
			if int(tmp2[0]) > int(tmp2[1]):
				year += 1
			month = int(tmp2[1])
			term_date = datetime(2000+year, month, 1)
			month_diff_total = (predict_term_date.year-term_date.year)*12+(predict_term_date.month-term_date.month)
			year_diff = month_diff_total/12
			month_diff = (month_diff_total%12)/3
			quarter = -month_diff+4
			#if ind == len(table_quarter)-2:
				#print term_date.strftime("%Y年%m月"), "%d年前第%d四半期"%(year_diff, quarter)
			try:
				quarter_data[year_diff][quarter-1] = data
			except IndexError:
				print "!!!進捗率取得で不明なエラー(フォーマット不正？)", code_s
		return quarter_data
	
	quarter_data = create_quarter_data(predict_term_date)

	# 最新データの四半期がいつかを取得
	latest_quarter = sum(len(v)>0 for v in quarter_data[0])
	# 1Qまたは予想データなければここでQだけ返して終了
	if latest_quarter == 0 or not exists_predict:
		#print "第1四半期を発表されていないため進捗率なし"
		ret["quarter"] = latest_quarter
		if not exists_predict:
			print "会社予想がないため進捗率取得できず(第%d四半期)"%(latest_quarter), code_s
		return ret	

	#---- 進捗中の売上と利益を計算
	prog_sales = 0
	prog_profit = 0
	try:
		for q in range(latest_quarter):
			qdata = quarter_data[0][q]
			prog_sales += qdata[1]
			prog_profit += qdata[2]
	except (IndexError, TypeError) as e:
		print "当期進捗率に必要なデータが不足しています"
	# 前年も同様に計算
	prog_sales_pre_total = prog_profit_pre_total = 0
	prog_sales_pre = prog_profit_pre = 0
	try:
		for q in range(4):
			qdata = quarter_data[1][q]
			if not qdata:
				#print "前年の進捗率はありません"
				break
			prog_sales_pre_total += qdata[1]
			prog_profit_pre_total += qdata[2]
	except TypeError:
		print "!!!前期進捗率で不明なエラー(フォーマット不正？)"
	try:
		if not prog_sales_pre_total == 0:	
			for q in range(latest_quarter):
				qdata = quarter_data[1][q]
				prog_sales_pre += qdata[1]
				prog_profit_pre += qdata[2]
	except IndexError:
		print "!!!進捗に必要な四半期データの不足エラー"
	
	#---- 進捗率計算
	sales_per = 100*prog_sales/predict_sales if predict_sales > 0 else 0
	try:
		if predict_profit > 0:
			profit_per = 100*prog_profit/predict_profit
		else:
			profit_per = 0
	except TypeError:
		# 営利発表していない場合
		profit_per = 0
		print "営業利益予想発表なしのため0"
	sales_per_pre = profit_per_pre = 0
	if prog_sales_pre_total > 0:
		sales_per_pre = 100*prog_sales_pre/prog_sales_pre_total
	if prog_profit_pre_total > 0: # 赤字なら計算しない
		profit_per_pre = 100*prog_profit_pre/prog_profit_pre_total
	print "進捗率: 第%d四半期 売上%d%%(前年%d%%) 利益%d%%(前年%d%%)"%(latest_quarter, sales_per, sales_per_pre, profit_per, profit_per_pre)
	ret["quarter"] = latest_quarter
	ret["sales"] = sales_per
	ret["profit"] = profit_per
	ret["sales_pre"] = sales_per_pre
	ret["profit_pre"] = profit_per_pre
	return ret

def calc_gyoseki_score(tables):
	"""業績スコアの計算
	=> int
	"""
	# quarter = tables.has_key("gyoseki_quarter")
	# term = tables.has_key("gyoseki_current")
	table_quarter = tables.get("gyoseki_quarter", [])
	table_current = tables.get("gyoseki_current", [])
	#---- 期数不備のチェック
	#code = tables.get("code",0)
	code_s = "" # code不要だった
	if not check_table(code_s,table_current, table_quarter):
		return 20

	#---- 四半期用の事前計算
	print "----"
	# 0:決算期	1:売上高	2:営業益	3:経常益	4:最終益 を3四半期期分
	quarter_growth = [[0 for j in range(5)] for i in range(3)] # 四半期毎の成長率
	quarter_score = [[0 for j in range(5)] for i in range(3)] # 計算用
	#print table_quarter
	#print quarter_growth
	try:
		for i in (r+1 for r in range(3)):
			for col in (c+1 for c in range(4)):
				try:
					#print "col", col, table_quarter[i+4][col], table_quarter[i][col]
					if table_quarter[i][col] >= 0:
						prev_quarter = table_quarter[i][col] if table_quarter[i][col]>0 else 1 # 0割は無理やり1にする
						if prev_quarter == "－":
							quarter_growth[i-1][col] = 100
							print "  業績の値がないため成長率100に", prev_quarter
						else:
							quarter_growth[i-1][col] = (float(table_quarter[i+4][col])/prev_quarter - 1)*100
					else:
						# 林さんの赤字のとき用計算
						val1 = float(table_quarter[i+4][col]-table_quarter[i][col])
						val2 = (abs(table_quarter[i][col])+abs(table_quarter[i+4][col]))/2.0
						quarter_growth[i-1][col] = (val1/val2)*100
				except IndexError:
					print "!!! 四半期成長率計算できず"
					break
	except ValueError as e:
		print "!!! 業績の値が不正です", quarter_growth
		return 20
	print "四半期業績　過去3四半期の成長率"
	# 得点化: 売上10%以上 利益20%以上
	# (オニール:直近四半期売上25%以上)
	for i, row in enumerate(quarter_growth):
		print [round(r,1) for r in row] # 四半期成長率
		# 売上は10%以上なら1, 利益は20%以上なら1
		for col in (c+1 for c in range(4)):
			if col == 1:	# 売上
				quarter_score[i][col] = 1 if quarter_growth[i][col] >= 10 else 0
			else:	# 利益
				quarter_score[i][col] = 1 if quarter_growth[i][col] >= 20 else 0
	print "-- (得点化) -->"
	for i,row in enumerate(quarter_score):
		print row 

	#---- スコアの計算
	#TODO: 全体的に無駄に複雑、シンプルに
	# 1,過去5年通期売上利益、2,直近通期利益、3,直近四半期利益、4,3四半期売上、5,3四半期利益
	# 利益は3:経常利益でなく2:営業利益を使うように変更
	SCORES = [20,10,15,30,25]
	#---- 1,過去利益持続成長
	term_data = table_current[-6:-1]
	term_data = [t for t in term_data if t[2] != '－']
	term_sales_data = [t[1] for t in term_data if t[1] != '－']
	term_set = zip(term_data[:-1], term_data[1:])
	# 営利の各期成長率(0.2->20%)
	term_growth = [calc_growth_rate2(t[0][2], t[1][2])-1 for t in term_set]
	
	if not term_growth:
		print "!!! 通期利益データがないため補充"
		term_growth = [0.0]
	#if not term_sales_growth:
	#	term_sales_growth = [1.0]
	#print "各期利益成長率:", [round(p,2) for p in term_growth]
	average_past_profit_rate = average_compound2(term_growth)*100
	average_past_sales_rate = calc_cagr(term_sales_data)*100
	#print "営利平均成長率(%):", average_past_profit_rate	
	#print "売上CAGR(%):", average_past_profit_rate
	term_count = len(term_data)	
	print "%d年平均利益成長率: %d%%"%(term_count, average_past_profit_rate)
	print "%d年平均売上CAGR: %d%%"%(len(term_sales_data), average_past_sales_rate)
	# 平均7%以上>4%以上　赤字年は減点
	score_past_profit = step_func(average_past_profit_rate, [0, 4, 7], [0, SCORES[0]/2, SCORES[0]])/2
	score_past_sales = step_func(average_past_sales_rate, [0, 5, 10], [0, SCORES[0]/2, SCORES[0]])/2
	def count_if(condition, seq):
		return sum(1 for item in seq if condition(item))
	# マイナス成長を減点
	#print "期毎経常利益成長率:", [round(p,1) for p in past_profit_rate]
	#red_count = count_if(lambda x:x<0, past_profit_rate)
	red_count = len([v for v in term_growth if v < 0])
	score_past_profit -= 3*red_count
	if score_past_profit<0: score_past_profit=0
	# 予測データでなければ減点
	isLatestTerm = True
	if len(table_current) >=2:
		latest_term = table_current[-2][0]
		year, month = latest_term.split(".")
		# import datetime
		# latest_term_dt =  datetime.date(int(year), int(month), 1)
		# print "予判定:", latest_term, latest_term_dt, datetime.datetime.today()
		# isLatestTerm = datetime.date.today() < latest_term_dt
		isLatestTerm = latest_term.find("予") >= 0
		latest_term_profit = table_current[-2][2]
		latest_term_sales = table_current[-2][1]
		if not isLatestTerm or latest_term_profit == '－': # 予想を出していない
			print "来季利益データでないので減点", latest_term, latest_term_profit
			score_past_profit *= 0.7
		if not isLatestTerm or latest_term_sales == "－":
			print "来季売上データでないので減点", latest_term, latest_term_sales
			score_past_sales *= 0.7
	
	score_past_profitsales = (score_past_profit+score_past_sales)
	print "score_past_profitsales:%d/%d"%(score_past_profitsales, SCORES[0]), "<- 過去平均各期利益-売上成長:%d%% %d%%"%(average_past_profit_rate, average_past_sales_rate),\
	 "(%d年)"%term_count, "マイナス%d回"%red_count
	
	#TODO: 単純に1年ずつ評価していったほうがいいかも　売上も見たい
	#---- 2,直近期利益成長
	#TODO: 3年を見たほうが良いかも
	# TODO: オニールは利益3年25％
	# 20%以上>10%以上
	#future_profit_rate = (past_profit_rate[-2]+past_profit_rate[-1])/2
	future_profit_rate = term_growth[-1]*100
	score_future_profit = 0
	if future_profit_rate>=20:
		score_future_profit = SCORES[1]
	elif future_profit_rate>=10:
		score_future_profit = SCORES[1]/2
	if not isLatestTerm:
		score_future_profit *= 0.7
	# Saas40%ルール適用での補正
	if score_future_profit <= 0:
		#print "Saas40%ルールのチェック"
		term_data2 = table_current[-6:-1] # 改めて生データ取得
		term_set2 = zip(term_data2[:-1], term_data2[1:])
		term_sales_growth = [calc_growth_rate2(t[0][1], t[1][1])-1 for t in term_set2]
		try:
			sales0 = term_data2[-1][1]
			past = False
			if sales0 == "－":
				sales0 = term_data2[-2][1]
				past = True
			profit0 = term_data2[-1][2]
			if profit0 == "－":
				profit0 = term_data[-2][2]
				past = True
		
			profit_rate = float(profit0)/float(sales0) if sales0 != 0 else 0
			sales_rate = term_sales_growth[-1]
			ratio = step_func(profit_rate+sales_rate, [0, 0.3, 0.4, 0.5], [0, 0.6, 0.8, 0.9])
			if ratio > 0 and term_sales_growth[-1] > term_sales_growth[-2]:
				ratio += 0.1
			pt = (SCORES[1])*ratio
			if past:
				pt *= 0.8
			if pt > 0:
				print "  直近期40%ルール補正:", pt, "売上成長: %.2f 利益率:%.2f"%(sales_rate, profit_rate)
			score_future_profit += pt
		except (ValueError, IndexError) as e:
			print "  直近期40%ルール計算できず"
	
	print "score_future_profit: %d/%d"%(score_future_profit, SCORES[1]), \
	"<- 直近期利益成長:%d%%"%(future_profit_rate)

	#TODO: 売上も考えたい
	#---- 3, 直近四半期利益成長
	# 40%以上->20%以上 オニール売上、利益とも25%
	latest_profit_rate = quarter_growth[-1][3]
	score_latest_profit = 0
	if latest_profit_rate >= 40:
		score_latest_profit = SCORES[2]
	elif latest_profit_rate >= 20:
		score_latest_profit = SCORES[2]/2
	# Saas40%ルール適用での補正
	if score_latest_profit <= 0:
		#print "Saas40%ルールのチェック"
		# 売上と利益のデータを取得、なければ過去から
		sales0 = table_quarter[-2][1]
		past = False
		if sales0 == "－":
			sales0 = table_quarter[-3][1]
			past = True
		profit0 = table_quarter[-2][2]
		if profit0 == "－":
			profit0 = table_quarter[-3][2]
			past = True
		try:
			profit_rate = float(profit0)/float(sales0) if sales0 != 0 else 0
			sales_rate = quarter_growth[-1][1]/100.0
			ratio = step_func(profit_rate+sales_rate, [0, 0.3, 0.4, 0.5], [0, 0.6, 0.8, 0.9])
			# 売上加速チェック
			if ratio > 0 and quarter_growth[-1][1] > quarter_growth[-2][1]:
				ratio += 0.1
			pt = (SCORES[2])*ratio
			if past:
				pt *= 0.8
			if pt > 0:
				print "  直近四半期40%ルール補正:", pt, "売上成長: %.2f 利益率:%.2f"%(sales_rate, profit_rate)
			score_latest_profit += pt
		except ValueError as e:
			print "  直近四半期40%ルール計算できず"
	print "score_latest_profit: %d/%d"%(score_latest_profit, SCORES[2]), \
	"<- 最直近四半期利益成長:%d%%"%round(latest_profit_rate,1)

	#---- 4, 直近2,3四半期売上成長
	sales_rate = (quarter_growth[-1][1]+quarter_growth[-2][1]+quarter_growth[-3][1])/3
	quarter_score_avg = 100*(quarter_score[-1][1]+quarter_score[-2][1]+quarter_score[-3][1])/3	
	score_sales_rate = ((SCORES[3]*2/3)*quarter_score_avg)/100
	if quarter_growth[-1][1] > quarter_growth[-2][1]:
		print "　四半期売上加速%.1f%%->%.1f%%"%(quarter_growth[-2][1], quarter_growth[-1][1])
		score_sales_rate += SCORES[3]/3
	print "score_sales_rate:%d/%d"%(score_sales_rate, SCORES[3]), \
	"<- 3四半期売上成長:%d%%"%round(sales_rate,1)
	
	#---- 5, 各四半期利益成長
	score_quarter_profit_rate_avg = 100*((quarter_score[-1][2]+quarter_score[-2][2]+quarter_score[-3][2])\
	+(quarter_score[-1][3]+quarter_score[-2][3]+quarter_score[-3][3])\
	+(quarter_score[-1][4]+quarter_score[-2][4]+quarter_score[-3][4]))/9
	score_quarter_profit_rate = ((SCORES[4]*2/3)*score_quarter_profit_rate_avg)/100
	if quarter_growth[-1][2] > quarter_growth[-2][2]:
		print "　四半期経常益加速%.1f%%->%.1f%%"%(quarter_growth[-2][2], quarter_growth[-1][2])
		score_quarter_profit_rate += SCORES[4]/3	
	profit_rate = (quarter_growth[-1][2]+quarter_growth[-2][2]+quarter_growth[-3][2])/3
	print "score_quarter_profit_rate: %d/%d"%(score_quarter_profit_rate, SCORES[4]), \
	"<- 3四半期利益成長:%d%%"%round(profit_rate,1)
	# 総括して計算
	gyoseki_score = score_past_profitsales+score_future_profit+score_latest_profit\
	+score_sales_rate+score_quarter_profit_rate
	print "----------"
	print "業績スコア:", gyoseki_score
	print "----------"
	return gyoseki_score
	
def get_gyoseki_data(code_s, upd=UPD_INTERVAL):
	"""codeの業績情報をkabutan(又はキャッシュ)から
	パース、計算して取得する
	type: str, bool -> dict
	"""
	# if not os.path.exists("stock_data/kabutan/"):
	# 	os.mkdir("stock_data/kabutan/")
	if upd == UPD_CACHE:
		use_cache = True
	elif upd == UPD_FORCE:
		use_cache = False
	else:
		INTERVAL_DAY = 15
		import rironkabuka
		use_cache = rironkabuka.is_cache_latest(URL_CODE%(str(code_s)), INTERVAL_DAY)

	html = http_get_html(URL_CODE%(str(code_s)), cache_dir=CACHE_DIR, use_cache=use_cache)
	for c in range(3):
		if "Service Temporarily Unavailable" in html:
			if c >= 2:
				print "!!! やっぱりだめみたいなので中止"
				return {}
			print "取得エラーのため再度取得", c
			import time
			time.sleep(c+1)
			html = http_get_html(URL_CODE%(str(code_s)), use_cache=False, cache_dir=CACHE_DIR)
		else:
			break

	print ">>>>> %sの業績を解析 "%code_s
	tables = parse_kabutan_account2(html)
	print "<<<<< 解析完了 "

	# 業績得点の追加
	print "="*5, "業績スコアの計算"
	tables["score_gyoseki"] = calc_gyoseki_score(tables)
	print "="*5, "業績スコアの計算完了"	
	path = CACHE_DIR+get_http_cachname(URL_CODE%(str(code_s)))
	tables["access_date_gyoseki"] = get_file_datetime(path)
	print "date:", tables["access_date_gyoseki"]
	#tables["code"] = code
	set_db_code(tables, code_s)
	return tables

def calc_growth_rate(cur, nxt):
	"""	成長率を返す
	-100~+100％数値で返す
	"""
	if cur > 0:
		try:
			return int(round(100*float(nxt)/cur -100, 0))
		except ValueError:
			return 0
	else:		
		if nxt == 0 and cur == 0:
			return 0 # 計算できない
		else:
			# この計算式は林さんより
			val = float((nxt-cur)/((abs(nxt)+abs(cur))*0.5))
			return int(100*val - 100)

def get_latest_ind(tbl, ind):
	# 決算期、売上高、営業益、経常益、純利益、一株純利益、分割調整後一株利益or売上営業利益
	try:
		y_ind = -2
		if tbl[y_ind][ind] == "－":
			y_ind = -3
		return y_ind
	except IndexError:
		#print "!!! 四半期テーブルがおかしい？", stock.get("code",""), stock.get("stock_name","")
		# for row in tbl:
		# 	for r in row:
		# 		print r,
		return None

def calc_mean(lst):
	mean = sum(lst)/len(lst)
	return mean
def calc_stddev(lst):
	mean = sum(lst)/len(lst)
	variance = sum((x - mean) ** 2 for x in lst) / len(lst)
	stddev = int(variance**0.5)
	return stddev

def calc_annual_growth(stock):
	"""年次成長率を返す
	"""
	tbl = stock.get("gyoseki_current", "")
	if not tbl:
		return ()
	# 最新の年度(四半期)インデックスを返す
	y_ind = get_latest_ind(tbl, 1)
	y_ind2 = get_latest_ind(tbl, 2)
	if not y_ind or not y_ind2:
		return ()
	
	try:
		rate_uriage = calc_growth_rate(tbl[y_ind-1][1], tbl[y_ind][1]) #1: 売上
		rate_eiri = calc_growth_rate(tbl[y_ind2-1][2], tbl[y_ind2][2]) #2:営利
	except IndexError:
		#print "通期データがありません", len(tbl), stock.get("stock_name","")
		return ()
	return tbl[y_ind][0], rate_uriage, rate_eiri # 年度、売上%、営利%

def calc_quarter_growth(stock):
	tbl = stock.get("gyoseki_quarter", "")
	if not tbl:
		return ()
	# 決算期、売上高、営業益、経常益、純利益、一株純利益、分割調整後一株利益or売上営業利益
	y_ind = get_latest_ind(tbl, 1)
	y_ind2 = get_latest_ind(tbl, 2)
	if not y_ind or not y_ind2:
		return ()
	try:
		rate_uriage = calc_growth_rate(tbl[y_ind-4][1], tbl[y_ind][1]) #1: 売上
		rate_eiri = calc_growth_rate(tbl[y_ind2-4][2], tbl[y_ind2][2]) #2:営利
	except (IndexError, ValueError):
		#print "四半期データがありません", len(tbl), stock.get("stock_name","")
		return ()
	return tbl[y_ind][0], rate_uriage, rate_eiri # 年度、売上%、営利%

def calc_annual_quarity_expr(stock):
	"""年度ごとの増収増益分を5年分返す
	"""
	#print "業績クォリティ算出", stock["code"]
	tbl = stock.get("gyoseki_current", "")
	if not tbl:
		return ()
	
	results = [] #0:平均 1:分散
	ref_indexes = [1,2] # 売上、利益参照インデックス	
	for ref_ind in ref_indexes:
		annual_rate_list = []
		y_ind = get_latest_ind(tbl, ref_ind)
		if y_ind is None:
			print "業績履歴データがない", y_ind, ref_ind
			break
		while True:
			try:
				rate = calc_growth_rate(tbl[y_ind-1][ref_ind], tbl[y_ind][ref_ind])
				annual_rate_list.append(rate)
				y_ind-=1
			except IndexError:
				break
		res = [0,0]
		if len(annual_rate_list) > 0:
			res[0] = calc_mean(annual_rate_list)
			res[1] = calc_stddev(annual_rate_list)
			results.append(res)
		else:
			print "!!! %sは過去業績取得できず"%stock.get("code_s","")
	return results

def calc_quarter_quaraity_expr(stock):
	"""四半期ごとの増収・増益率を4四半期分(=2年分)返す
	"""
	tbl = stock.get("gyoseki_quarter", "")
	if not tbl:
		return (),""
	
	results = [] #0:平均 1:分散
	ref_indexes = [1,2] # 売上、利益参照インデックス
	rates = []
	for ref_ind in ref_indexes:
		quarter_rate_list = []
		y_ind = get_latest_ind(tbl, ref_ind)
		if y_ind is None:
			print "四半期業績履歴データがない", y_ind, ref_ind
			break
		while True:
			try:
				rate = calc_growth_rate(tbl[y_ind-4][ref_ind], tbl[y_ind][ref_ind])
				quarter_rate_list.append(rate)
				y_ind-=1
			except IndexError:
				break
		res = [0,0]
		res[0] = calc_mean(quarter_rate_list)
		res[1] = calc_stddev(quarter_rate_list)
		results.append(res)
		rates.append(quarter_rate_list)
	# コード33の判定
	code33 = ""
	try:
		sales_ratios = rates[0]
		# 営利増加率-売上営利率
		prof_ratios = [rates[1][q]-rates[0][q] for q in range(len(rates[0]))]
		if sales_ratios[0] > sales_ratios[1] and sales_ratios[1] > sales_ratios[2]:
			if prof_ratios[0] > prof_ratios[1] and prof_ratios[0] > prof_ratios[-1]:
				code33 = "C3"
	except IndexError:
		print "!!! Code33判定できず"
		pass
	return results, code33

def get_gyoseki_expr(stock_data):
	"""業績表現を返す
	Returns: 業績表現 [A]20%,10%[Q]-10%,-20%[P]1Q28%(18%),44%(10%)
	"""
	annual = calc_annual_growth(stock_data)
	quarter = calc_quarter_growth(stock_data)
	progress = calc_progress_rate(stock_data)
	annual = "[A]%d%%,%d%%"%(annual[1], annual[2]) if annual else "" #annual[0]で年度表示
	quarter = "[Q]%d%%,%d%%"%(quarter[1], quarter[2]) if quarter else ""
	current_quarter = progress.get("quarter",0)
	if current_quarter <= 0:
		progress_expr = '[P]%dQ'%(current_quarter)
	else:
		progress_expr = '[P]%dQ%d%%(%d%%),%d%%(%d%%)'%(progress.get("quarter",0), \
			progress.get("sales",0), progress.get("sales_pre",0),\
				progress.get("profit",0),progress.get("profit_pre",0))
	# 進捗率, 売上・利益成長率
	return progress_expr, annual+quarter

def get_gyoseki_quarity_expr(stock):
	"""業績クォリティ(過去業績)表現を返す
	"""
	res_annual = calc_annual_quarity_expr(stock)
	res_quarter, code33 = calc_quarter_quaraity_expr(stock)
	expr = ""
	if len(res_annual)>0 and len(res_quarter)>0:
		#[A]5±8%[Q]-5±12%
		expr = "[A]%d±%d%%,%d±%d%%[Q]%d±%d%%,%d±%d%%"%(res_annual[0][0],res_annual[0][1], res_annual[1][0],res_annual[1][1], res_quarter[0][0], res_quarter[0][1],res_quarter[1][0], res_quarter[1][1])
		if code33:
			expr += "<%s>"%code33
	return expr

def main():
	#TODO: 複利成長率をちゃんとだしたい
	#TODO: 売上成長率・利益成長率の当期と当四半期、5年成長率を表示させたい
	#TODO: コード33
	#TODO: ROEとか売上営業利益率も見る
	#  ROE: 標準8%<オニール15％以上 <25%<40%
	#  ROE標準偏差:2%以内（大川さん）
	#TODO: ボラティリティをスコアに含める(4%未満 6%以上はだめ) クォリティファクター
	#TODO: 配当2%まで出してるところが10倍株の意外な要素らしい(井村さん)
	#TODO: 通期進捗率を評価に含める？
	#TODO: 利益率伸びPTを表示に入れたい
	#code_list = [6789, 6121, 3038,5401, 2301,6095,7697,8001,8035,3668,4483,3994,3923,4478]
	code_list_s = ["4112"]
	for code_s in code_list_s:
		#gyoseki_data = get_gyoseki_data(c, UPD_INTERVAL) #UPD_FORCE/UPD_INTERVAL
		#calc_progress_rate(gyoseki_data)

		import make_stock_db as db
		stock = db.load_cacehd_stock_db(code_s)
		#print get_gyoseki_expr(stock)
		print get_gyoseki_quarity_expr(stock)

if __name__ == '__main__':
	main()
