# def analyze_from_file():
# 	text = file("shihyou.txt","r").readlines()
# 	#pdb.set_trace()
# 	keys = ["株価20日前変化率", 
# 	"出来高20日前増加率", 
# 	"コンセンサス予想PER",
# 	"時価総額",
# 	"株価(予想)売上高倍率(PSR)",
# 	"売上高営業利益率",
# 	"PEGレシオ",
# 	"有利子負債自己資本比率",
# 	"ベータ(対TOPIX)",
# 	 ]
# 	shihyou = {}
# 	for i,t in enumerate(text):
# 		if t.startswith("株価変化率"):
# 			henka20 = text[i+1].split("\t")[1]
# 			henka20 = henka20[:-2]
# 			shihyou["株価20日前変化率"] = henka20
# 		if t.startswith("出来高増加率"):
# 			dekidaka20 = text[i+1].split("\t")[1][:-1]
# 			shihyou["出来高20日前増加率"] = dekidaka20
# 		if t.startswith("コンセンサス情報"):
# 			per = text[i+1].split("\t")[1].replace("倍","").strip()
# 			shihyou["コンセンサス予想PER"] = per
# 		if t.startswith("時価総額"):
# 			jikasogaku = text[i].split("\t")[1].strip()
# 			jikasogaku = jikasogaku.replace("百万円","")
# 			shihyou["時価総額"] = jikasogaku
# 			#print text[i+1].split("\t")
# 		if t.startswith("PSR"):
# 			psr = text[i].split("\t")[1].strip()
# 			psr = unicode(psr,'utf-8')[:-1]
# 			shihyou["株価(予想)売上高倍率(PSR)"] = psr.encode('utf-8')
# 		if t.startswith("売上高営業利益率"):
# 			val = text[i].split("\t")[1].strip()
# 			val = val[:-1]
# 			shihyou["売上高営業利益率"] = val
# 		if t.startswith("PEG"):
# 			val = text[i].split("\t")[1].replace("倍","").strip()
# 			shihyou["PEGレシオ"] = val
# 		if t.startswith("有利子負債自己資本比率"):
# 			val = text[i].split("\t")[1].strip()[:-1] #%を削除
# 			shihyou["有利子負債自己資本比率"] = val
# 		if t.startswith("ベータ(対TOPIX)"):
# 			val = text[i].split("\t")[1].strip()
# 			shihyou["ベータ(対TOPIX)"] = val
# 		if t.startswith("東証33業種"):
# 			val = text[i].split("\t")[1].strip()
# 			shihyou["東証33業種"] = val

# 	#for k,v in shihyou.items():
# 	#	print k,v
# 	print "------------"
# 	for k in keys:
# 		print shihyou[k] #k winではでない
# 	print "------------"
# 	print shihyou["株価20日前変化率"]
# 	print shihyou["出来高20日前増加率"]
# 	#
# 	print shihyou["コンセンサス予想PER"]
# 	print shihyou["時価総額"]
# 	print shihyou["株価(予想)売上高倍率(PSR)"]
# 	print shihyou["売上高営業利益率"]
# 	#
# 	print shihyou["PEGレシオ"]
# 	print shihyou["有利子負債自己資本比率"]
# 	print shihyou["ベータ(対TOPIX)"]

#==================================================
# 楽天から指標データを取得
#================================================== 
# def get_from_rakuten_html(html, code):
# 	# 指標リンク先URLを取得
# 	m = re.search(r'<iframe name="stockFrame".*?src="(.*?)"></iframe>', html)
# 	url_shiyo = m.group(1) if m else ""
# 	print url_shiyo
# 	html = http_get_html(url_shiyo, use_cache=True, 
# 		cache_dir="stock_data/shihyo", cache_fname="rakuten_shihyo_%d.html"%code)
# 	# TODO: ここから本解析したいがJavascriptで生成されるためムリポ
# 	#shihyo = {}
# 	#m = re.search(r'<span title=".*?">主市場</span>.*?<td class=".*?">(.*?)</td>', html)
# 	#shihyo["main_market"] = "東証"m.group(1)[-9]
# 	#m = re.search(r'<span title=".*?">東証33業種</span>.*?<td class=".*?">(.*?)</td>', html)
# 	#print shihyo
# 	return

# URL_RAKUTEN_TOP = "https://www.rakuten-sec.co.jp/"
# URL_RAKUTEN_LOGIN_TOP = "https://member.rakuten-sec.co.jp/bv/app/Login.do"
# #2: 四季報 6: 業績 7: 指標
# URL_RAKUTEN_CONTENTS = "https://member.rakuten-sec.co.jp/app/info_jp_prc_stock.do;\
# BV_SessionID=%s?eventType=init&infoInit=1&contentId=%d&type=&sub_type=&local=&\
# dscrCd=%d0&marketCd=1&gmn=J&smn=01&lmn=01&fmn=01"
# # rakutenからスクレイピングでとるのはセレニウムなどを導入しないと無理。
# def get_shihyo_html(code, use_cache=True):
# 	def build_params_for_rakuten():
# 		params = {}
# 		params["loginid"] = "NUPM9571"
# 		params["passwd"] = "zidane22"
# 		return params
# 	html, cookies = http_post_html(URL_RAKUTEN_LOGIN_TOP, use_cache=use_cache, data=build_params_for_rakuten())
# 	# cookieを保存
# 	COOKIE_NAME = "rakuten_cookie.txt"
# 	if cookies:
# 		print "cookieを%sに保存"%COOKIE_NAME, len(cookies)
# 		pickle.dump(cookies, file(COOKIE_NAME, 'w'))
# 	else:
# 		cookies = pickle.load(file(COOKIE_NAME, 'r'))
# 	#print html
# 	# セッションIDを取得
# 	m = re.search(r'";BV_SessionID=(.*?)"', html)
# 	session_id = m.group(1) if m else ""
# 	print "session:", session_id
# 	#print "cookies:", cookies
# 	# 四季報データを取得
# 	print ">>>>> 指標データを取得します"
# 	url_shihyo = URL_RAKUTEN_CONTENTS%(session_id, 7, code)
# 	#print "url:", url_shihyo
# 	html = http_get_html(url_shihyo, use_cache=use_cache,
# 		cache_dir="stock_data/shihyo", cache_fname="rakuten_shihyo_%d_tmp.html"%code,
# 		cookies=cookies)
# 	print "<<<<< 指標データを取得しました"
# 	return html

# def analyze_from_rakuten(code):
# 	html = get_shihyo_html(code, use_cache=True)
# 	# 駄目だったら新しいセッション
# 	if 'window.top.location.replace("https://www.rakuten-sec.co.jp/session_error.html");\n\n' in html:
# 		print "セッションエラーのため再取得します"
# 		html = get_shihyo_html(code, use_cache=False)
# 	# TODO: Javascriptだからだめかも。	
# 	d = get_from_rakuten_html(html, code)

#==================================================
# ロイターから指標データを取得
#================================================== 
# URL_REUTERS_BASE = "http://jp.reuters.com/"
# URL_REUTERS_SHIYO_REL = "investing/quotes/detail?symbol=%d.T"
# URL_REUTERS_SHIYO = "http://jp.reuters.com/investing/quotes/detail?symbol=%d.T"
# #URL_REUTER_TEST = "http://www.reuters.com/finance/markets/index?symbol=us!spx&sortBy=&sortDir=&pn=1"
# def get_reuter_html(code, use_cache=True):
# 	"""
# 	ロイターのhtmlを解析:dryscrape使用
# 	"""
# 	cache_path="stock_data/shihyo/reuter_shihyo_%d.html"%code
# 	if use_cache and os.path.exists(cache_path):
# 		print "htmlをファイルキャッシュから取得します", cache_path
# 		html = file_read(cache_path)
# 		return html
	
# 	# WebKitを用いてdryscprapeで取得
# 	try_count = 0
# 	while try_count < 2:
# 		print "dryscrapeでhtmlを取得します..", URL_REUTERS_BASE+URL_REUTERS_SHIYO_REL%code, "try", try_count
# 		import dryscrape
# 		sess = dryscrape.Session(base_url=URL_REUTERS_BASE)
# 		sess.set_attribute('auto_load_images', False)
# 		try:
# 			sess.visit(URL_REUTERS_SHIYO_REL%code)
# 			html = sess.body()
# 			print ".. html取得しました"
# 			break
# 		except Exception as e:
# 			html = ""
# 			print "!!! visit例外:dryscapeでhtml取得できません", e
# 			try_count+=1
# 	shintakane_global.DryScrapeSessionCount += 1
# 	file_write(cache_path, html)
# 	return html

# def analyze_from_reuter(code, latest=False):
# 	shiyo_data={}
# 	html = get_reuter_html(code, use_cache=not latest)
# 	#print ux_cmd_head(html)
# 	# ROE
# 	def get_item(v):
# 		try:
# 			val = float(v)
# 		except ValueError:
# 			val = "-"#0
# 		return val
# 	m = re.search(r'<td>ＲOE率</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>', html)
# 	if not m:
# 		print "!!! Reutorsに指標データのない銘柄のようです"
# 		return {}
# 	#print m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
# 	roe_history = [float(m.group(i).replace(",","")) for i in range(1,7)]
# 	print "ROE:", roe_history
# 	# 売上高営業利益率
# 	m = re.search(r'<td>売上高営業利益率</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>\n\t+<td class="data">(.*?)％</td>', html)
# 	if m:
# 		opm_history = [float(m.group(i).replace(",","")) for i in range(1,7)] #operating margin
# 	else:
# 		print "!!! htmlに売上高営業利益率の項目がありません"
# 		opm_history = [0 for i in range(1,7)]
# 	print "売上高営業利益率:", opm_history
# 	# EV/EBITDA
# 	m = re.search(r'<td>EV/EBITDA</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>', html)
# 	if m:
# 		evebtda_history = [get_item(m.group(i)) for i in range(1,7)]
# 	else:
# 		print "!!! htmlにEV/EBITDAの項目がありません"
# 		evebtda_history = [0 for i in range(1,7)]
# 	print "EV/EBITDA:", evebtda_history
# 	# PCFR
# 	m = re.search(r'<td>PCFR倍率</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>', html)
# 	pcfr_history = [get_item(m.group(i)) for i in range(1,7)]
# 	print "PCFR:", pcfr_history
# 	# PER
# 	m = re.search(r'<td>PER倍率</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>', html)
# 	per_history = [get_item(m.group(i)) for i in range(1,7)]
# 	print "PER:", per_history
# 	# PSR
# 	m = re.search(r'<td>PSR倍率</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>', html)
# 	psr_history = [get_item(m.group(i)) for i in range(1,7)]
# 	print "PSR:", psr_history
# 	# PEG
# 	m = re.search(r'<td>PEG倍率</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>', html)
# 	peg_history = [get_item(m.group(i)) for i in range(1,7)]
# 	print "PEG:", peg_history

# 	shiyo_data["ROE"] = roe_history
# 	shiyo_data["OPM"] = opm_history
# 	shiyo_data["EV/EBITDA"] = evebtda_history
# 	shiyo_data["PCFR"] = pcfr_history
# 	shiyo_data["PER"] = pcfr_history
# 	shiyo_data["PSR"] = psr_history

# 	# 時価総額
# 	#m = re.search(r'<td width="129"> 時価総額 </td> <td width="114" nowrap="" class="data">(.*?)</td>', html)
# 	m = re.search(r'<td>時価総額\(億円\)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>\n\t+<td class="data">(.*?)</td>', html)
# 	if m:
# 		jikasogaku_history = [get_item(m.group(i)) for i in range(1,7)]
# 		jikasogaku = jikasogaku_history[4]
# 		#jikasogaku = int(m.group(1).replace("¥","").replace("百万","").replace(",",""))/100
# 		print "時価総額:", jikasogaku
# 		shiyo_data["jikasogaku"] = jikasogaku
# 	else:
# 		print "!!!reuterから時価総額を取得できませんでした"
# 	return shiyo_datao