#!/usr/bin/python
# -*- coding: utf-8 -*-
import os, os.path
import shutil
from datetime import datetime, timedelta
import pickle
from contextlib import contextmanager
import sqlite3

import requests

# プロジェクトルートとデータパスの定義
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))  # ks_util.py の場所
#ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
ROOT_DIR = SCRIPT_DIR
DATA_DIR = os.path.join(ROOT_DIR, "data")

#==================================================
# ユーティリティ
#==================================================
UPD_CACHE = 0 # 現在DB上のデータ: ファイルキャッシュがあれば使う
UPD_INTERVAL = 1 # 適度に最新なデータ: 間隔が開けば更新、開いてなければファイルキャッシュを使う
UPD_REEVAL = 2 # INTERVALと同じだがDBキャッシュは使わず再評価はしてほしい
UPD_FORCE = 3 # 本当に最新なデータ: 間隔に関わらず更新、開いてなくてもファイルキャッシュを使わない

def ux_cmd_head(str, line=10):
	return "\n".join(str.splitlines()[:line])

def print_dict(dict, ex_key=[]):
	"""
	dictのキー要素を表示
	"""
	print "----------"
	for k in dict.keys():
		if k in ex_key:
			continue
		print k, ": ", dict[k]
	print "----------"

def memoize(func):
	cache = {}
	def mamoized_function(*args):
		try:
			return cache[args]
		except KeyError:
			print "no_memo:", args
			value = func(*args)
			cache[args] = value
			return value
	return mamoized_function

#==================================================
# ファイルユーティリティ
#==================================================
def backup_file(fname, day=0):
	"""
	fnameのバックアップファイルを作成する
	fnameの日付が今日からday日間経過したものを作成
	"""
	if not os.path.exists(fname):
		print "backup対象ファイルがありません:", fname
		return
	stat = os.stat(fname)
	date = datetime.fromtimestamp(stat.st_mtime)
	today = datetime.today()
	delta = today-date
	print "%sは%s日前"%(fname, delta.days)
	backup_fname = "%s_%02d%02d%02d%s"%(os.path.splitext(fname)[0], \
		date.year-2000, date.month, date.day, os.path.splitext(fname)[1])
	#backup_fname = "stocks_pickle_back/"+backup_fname
	#print backup_fname
	if not os.path.exists(backup_fname):
		if delta.days >= day:
			print "バックアップ:%s(%d) => %s"%(fname, os.path.getsize(fname), \
			backup_fname)
			shutil.copy2(fname, backup_fname)
		else:
			pass
	else:
		print backup_fname+"にバックアップ済み"
	return backup_fname

def file_write(fname, content):
	f = open(fname, 'w')
	f.write(content)
	f.close()

def file_read(fname):
	f = open(fname, 'r')
	content = f.read()
	f.close()
	return content

def get_file_datetime(fname):
	"""
	ファイルfnameの日付datetimeを返す
	str -> datetime
	"""

	stat = os.stat(fname)
	return datetime.fromtimestamp(stat.st_mtime)

PRICE_HOUR = 18 # これ以前は前日、これ以降は当日
def get_price_day(dt):
	"""
	営業日ベースの日付を返す
	16時以前の価格は前日を使う
	daettime -> datetime
	"""
	need_dt = dt
	if dt.hour < PRICE_HOUR:
		need_dt = (dt-timedelta(1))
	return need_dt.date()

#==================================================
# 計算ユーティリティ
#==================================================
def sumproduct(*lists):
    return sum(reduce(operator.mul, data) for data in zip(*lists))

def average(lst):
	"""平均を求める
	Args:
		平均を求めたいリスト
	Returns:
		float 平均値
	"""
	return reduce(lambda x, y: x + y, lst) / float(len(lst))

def cramp(x, low, high):
	return max(low, min(x, high))

def step_func(val, xs, ys, min_val=None):
	"""
	# step_func(shiyo["PER"], [0, 30, 60], [PER_MAX, PER_MAX/2, 0])
	"""	
	if min_val is None:
		val_y = ys[0]
	else:
		val_y = min_val
	for x,y in reversed(zip(xs, ys)):
		if val > x:
			val_y = y
			break
	return val_y		
#==================================================
# httpユーティリティ	
#==================================================
USER_AGENT_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/36.0.1985.125 Safari/537.36"
def get_http_cachname(url):
	"""
	urlからデフォルトのキャッシュファイルの場所を返す
	"""
	return url.replace("http://", "").replace(".", "").replace("/", "_")+".html"

def http_get_html(url, use_cache=True, cache_dir="", cache_fname="", cookies={}, encoding='utf-8'):
	"""
	指定urlをリクエストしエンコード済みテキストを返します。
	TODO: use_cacheを有効期限期日指定したほうがスッキリするはず
	"""
	cache_name = get_http_cachname(url) if not cache_fname else cache_fname
	if cache_dir:
		cache_name = os.path.join(cache_dir, cache_name)
	if use_cache and os.path.exists(cache_name):
		print "  htmlをファイルキャッシュから取得します", cache_name
		html = file_read(cache_name)
		return html
	print "  htmlを通信で取得します.."
	headers = {"User-Agent": USER_AGENT_CHROME}
	#headers["Connection"] = "Keep-Alive"
	try:
		r = requests.get(url, headers=headers, cookies=cookies)
	except requests.exceptions.ConnectionError as e:
		print "!!! 接続失敗"
		print e
		return ""
	if r.encoding != 'utf-8':
		print "html_encoding:", r.encoding, "encoding:", encoding
	# htmlをutf8で取得
	html = r.text.encode(encoding)
	# メタ指定での文字コードをutf8に
	#html = html.replace("charset=shift_jis", "charset=utf-8")

	print "  取得したhtmlをファイルキャッシュに書き込みます:", cache_name
	file_write(cache_name, html)
	return html

def http_post_html(url, use_cache=True, data={}, cookies={}, encoding='utf-8'):
	cache_name = "post_"+get_http_cachname(url)
	if use_cache and os.path.exists(cache_name):
		print "html(post)をファイルキャッシュから取得します", cache_name
		html = file_read(cache_name)
		return html, ""

	headers = {"User-Agent": USER_AGENT_CHROME}
	r = requests.post(url, headers=headers, data=data, cookies=cookies)
	if r.encoding != 'utf-8':
		print "encoding:", r.encoding, "encoding:", encoding
	html = r.text.encode(encoding)
	#html = html.replace("charset=UTF-8", "charset=euc-jp")
	
	print "htmlをファイルキャッシュに書き込みます:", cache_name
	file_write(cache_name, html)
	return html, r.cookies

#==================================================
# pickleデータベースユーティリティ	
#==================================================
def save_pickle(fname, content):
	print "%sにpickleセーブ"%fname
	with open(fname, 'wb') as f:
		pickle.dump(content, f)

def load_pickle(fname):
	print "%sからpickleロード"%fname
	try:
		f = file(fname, 'rb')
		dat = pickle.load(f)
		f.close()
	except IOError, e:
		print e
		return {}
	return dat
memoized_load_pickle = memoize(load_pickle)

def load_file(fname, tb='r'):
	print "%sのfileロード"%fname
	try:
		f = file(fname, tb)
		dat = f.read()
		f.close()
	except IOError, e:
		print e
		return ""
	return dat
memoized_load_file = memoize(load_file)

#==================================================
# SQLデータベースユーティリティ	
#==================================================
@contextmanager
def open_db(dbname):
	print "---> open_db:",dbname
	con = sqlite3.connect(dbname)
	yield con
	print "<--- close_db:",dbname
	con.close()

def exec_sql(cur, sql, param=()):
	try:
		print "exec_sql:", sql, param
		cur.execute(sql, param)
	except sqlite3.Error, e:
		print "--- !!! SQL Error ---"
		print e.message
		print "---------------------"


def exists_table(cur, table_name):
	sql = """select count(*) from sqlite_master where type='table' and name='%s'"""%table_name
	exec_sql(cur, sql)
	count = cur.fetchone()[0]
	return count > 0

def create_table(cur, table_name):
	sql = """create table stocks(code integer primary key, name text, jikasogaku integer, konyukagaku integer)"""
	exec_sql(cur, sql)


	
def list_db():
	with open_db("stock_data/stock.db") as con:
		cur = con.cursor()
		sql = """select * from sqlite_master where type='table' and name='stocks'"""
		exec_sql( cur, sql )
		for row in cur:
			print row

		sql = """select * from stocks"""
		exec_sql( cur, sql )
		for row in cur:
			for r in row:
				if isinstance(r, unicode):
					print r.encode('utf-8'), 
				else:
					print r,
			print 

def update_db(stock_data):
	with open_db("stock_data/stock.db") as con:
		# テーブル
		cur = con.cursor()
		if not exists_table(cur, "stocks"):
			create_table(cur, "stocks")
		# 更新
		sql = """insert into stocks values(?,?,?,?)"""
		exec_sql( cur, sql, (stock_data["コード"],  unicode(stock_data["銘柄名"], 'utf-8'),\
			stock_data["時価総額"], stock_data["最低購入代金"] ))
		# TODO: insert or update 複数コードの更新

		con.commit()

def edit_db():
	with open_db("stock_data/stock.db") as con:
		sql = """alter table stocks add name text after code"""
		exec_sql( con.cursor(), sql )
		con.commit()
	list_db()

def set_db_code(rec, code):
	# type: (dict, str) -> None
	rec["code_s"] = str(code)

def get_db_code(rec):
	# type: dict -> dict
	if not rec.has_key("code_s"):
		if rec.has_key("code"):
			code = rec["code"]
			print "!!!strコードがないためintから取得:", code # いつかなくなるはず
			return format(code, "04d")
		else:
			print "!!!コード取得できない", rec
			return ""
	return rec["code_s"]