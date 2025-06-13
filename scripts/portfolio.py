#!/usr/bin/env python3

import requests
import time
import re
import pickle

from ks_util import *

URL_YAHOO_TOP = "http://www.yahoo.co.jp/"
URL_YAHOO_LOGIN = "https://login.yahoo.co.jp/config/login?.src=www&.done=http://www.yahoo.co.jp"
URL_YAHOO_LOGIN_POST = "https://login.yahoo.co.jp/config/login?"
URL_YAHOO_FINANCE_PORTFOLIO = "http://info.finance.yahoo.co.jp/portfolio/display/?portfolio_id=pf_1"
URL_YAHOO_FINANCE_PORTFOLIO2 = "http://info.finance.yahoo.co.jp/portfolio/display/?portfolio_id=pf_2"

# USER_AGENT_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/36.0.1985.125 Safari/537.36"
USER_AGENT_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2272.101 Safari/537.36"
ACCOUNT = ("srqys795@yahoo.co.jp", "zidane22")


def http_get_yahoo(url, cookies={}):
    # 実在するユーザエージェントを設定
    headers = {"User-Agent": USER_AGENT_CHROME}
    # キープアライブ設定
    headers["Connection"] = "Keep-Alive"

    r = requests.get(url, headers=headers, cookies=cookies)
    return r


def http_post_yahoo(url, data={}, cookies={}):
    headers = {"User-Agent": USER_AGENT_CHROME}
    # headers["Connection"] = "Keep-Alive"
    r = requests.post(url, headers=headers, data=data, cookies=cookies)
    return r


def build_params_for_login(html):
    """
    postリクエストのためのパラメータ構築
    """
    matches = re.findall(r'input type="hidden" name="(.*)" value="(.*)".*', html)
    params = {}
    for m in matches:
        params[m[0]] = m[1]
    # print params
    # ログイン対策 (.nojsの削除）
    del params[".nojs"]
    # アルバトロスの更新
    m = re.search(r'getElements.*albatross.*value = "(.*)"', html)
    if m:
        print("albatross:", params[".albatross"], "->", end=' ') 
        params[".albatross"] = m.group(1)
        print(params[".albatross"])
    else:
        print("!!! .albatrossが見つかりません")
    # ユーザ、パスワードの追加
    params["login"] = ACCOUNT[0]
    params["passwd"] = ACCOUNT[1]
    # print "Params: ", params
    return params


def login_yahoo():
    """
    yahooへログインしクッキーを取得
    """
    # --- yahooトップページからB Cookieを取得
    print("%sへ接続..."%URL_YAHOO_TOP)
    r = http_get_yahoo(URL_YAHOO_TOP)
    print("cookies yahoo_top:", r.cookies)
    print("B_cookie:", r.cookies['B'])
    time.sleep(float(656/1000))

    # --- yahoo ログイン画面を表示
    print("%sへ接続..."%URL_YAHOO_LOGIN)
    cookies = dict(B=r.cookies['B'])
    r2 = http_get_yahoo(URL_YAHOO_LOGIN, cookies)
    print("cookies yahoo_login:", r2.cookies)
    html = r2.text.encode('utf-8')
    # file_write("tmp.html", html)

    time.sleep(float(724/1000))

    # --- yahooへログイン
    print("%sへ接続..."%URL_YAHOO_LOGIN_POST)
    data = build_params_for_login(html) # ログインhtmlからpostパラメータ取得
    cookies = dict(B=r.cookies['B'])
    # print "data:", data #, "cookies", cookies
    r3 = http_post_yahoo(URL_YAHOO_LOGIN_POST, data, cookies)
    if "文字認証" in r3.text.encode('utf-8'):
        print("!!! 文字認証が求められています")
    # html_loggedin = r3.text.encode('utf-8')
    # file_write("tmp2.html", html_loggedin)
    # print "cookies yahoo_login_post", r3.cookies, "len:", len(r3.cookies)
    time.sleep(float(724/1000))

    return r3.cookies


def get_latest_portfolio():
    """
    yahooファイナンスからスクレイプしたポートフォリオデータを解析
    => dict
    """
    # クッキーを取得する
    COOKIE_NAME = "yahoo_cookie.txt"
    if not os.path.exists(COOKIE_NAME):
        cookies = login_yahoo()
    else:
        cookies = pickle.load(open(COOKIE_NAME, 'rb'))
    print(cookies, len(cookies))
    # --- yahooのサイトへアクセス
    if len(cookies) > 0:
        print("cookieを%sに保存"%COOKIE_NAME)
        pickle.dump(cookies, open(COOKIE_NAME, 'wb'))
        
        print("%sへ接続..."%URL_YAHOO_FINANCE_PORTFOLIO)
        r = http_get_yahoo(URL_YAHOO_FINANCE_PORTFOLIO, cookies)
        html_yahoo = r.text.encode('utf-8')
        file_write("yahoo_portfolio.html", html_yahoo)
        # TODO: ポートフォリオ更新したいけど文字認証いっちゃってログインできない
        # dryscrapeじゃないとだめか？


def test_build_params():
    html = file_read("tmp.html")
    data = build_params_for_login(html)
    print(data)

# ==================================================
# ポートフォリオ更新
# ==================================================


def parse_portfolio_txt():
    """
    yahooファイナンスからコピペしたポートフォリオデータを解析
    => list< dict > 
    """
    text = file_read("portfolio_yahoo.txt")
    stocks = {}
    for m in re.finditer(r"(\d{4})\n(.*?)\t(.*?)\t(.*?)\t(.*?)\t(.*?)\t(.*?)\t(.*?)\t", text):
        # print m.group(1), "-", m.group(2), "-", m.group(3), \
        #"-", m.group(4), "@5", m.group(5), "@6", m.group(6), "@7", \
        # m.group(7), "@8", m.group(8)
        stock = {}
        # code = int(m.group(1))
        code_s = m.group(1)
        # stock["code"] = code
        set_db_code(stock, code_s)
        stock["market"] = m.group(2)
        stock["name"] = m.group(3).replace("(株)", "")
        stock["date"] = m.group(4)
        stock["price"] = int(float(m.group(5).replace(",", "")))
        stock["volume"] = int(float(m.group(8).replace(",", ""))) # 出来高
        # print stock
        stocks[code_s] = stock
    return stocks


def parse_my_portforio():
    """ 自分のウォッチリストの銘柄コードリストを返す
    Returns:
        (list<str>,list<str>): ウォッチリストの銘柄コードリスト、保有リスト
    """
    stocks = {}
    content = file_read(os.path.join(DATA_DIR, "my_watch_list.txt"))
    code_s_list = []
    possess_list = []
    # 英数字コード対応
    # for m in re.finditer(r'\d\d\d\d', content):
    for m in re.finditer(r'H?(\d[0-9a-zA-Z]\d[0-9A-Z])', content):		
        if m.group(0).find("H")>=0:
            if not m.group(1) in possess_list:
                possess_list.append(m.group(1))
        else:
            if not m.group(1) in code_s_list:
                code_s_list.append(m.group(1))
    # print code_list
    return code_s_list, possess_list


def update_my_portfolio():
    stocks = []
    with open("my_watch_list.txt", 'r') as f:
        stocks = f.readlines()
    import re
    stocks2 = []
    for stock in stocks:
        m = re.search(r'(\d\d\d\d)(.*)', stock)
        stocks2.append([m.group(1), m.group(2)])
    stocks2_list = [l[0] for l in stocks2]
    report_fname = "googledrive/投資売買記録 - 銘柄調査.csv"
    import csv
    csv_r = csv.reader(open(report_fname, 'rb'))
    for row in csv_r:
        code_s = row[0]
        stock_name = row[1]
        evaluation = row[4]
        if evaluation == "S" or evaluation == "A" or evaluation == "B":
            if not code_s in stocks2_list:
                print("追加:%s %s"%(code_s, stock_name))
                stocks2.append([code_s, stock_name])
    stocks2.sort(key=lambda s: int(s[0]))
    stocks2_new = ["%s%s\n"%(s[0],s[1]) for s in stocks2]
    with open("my_watch_list.txt", 'w') as f:
        f.writelines(stocks2_new)


def main():
    args = "update"
    # args = "update get"
    args = "get"
    if "get" in args:
        # get_latest_portfolio()
        # test_build_params()
        parse_my_portforio()
    if "update" in args:
        update_my_portfolio()


if __name__ == '__main__':
    main()
