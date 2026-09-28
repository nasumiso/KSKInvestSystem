#!/usr/bin/env python3
# flake8: noqa E501

import requests
import time
import re
import pickle

from ks_util import *

URL_YAHOO_TOP = "http://www.yahoo.co.jp/"
URL_YAHOO_LOGIN = (
    "https://login.yahoo.co.jp/config/login?.src=www&.done=http://www.yahoo.co.jp"
)
URL_YAHOO_LOGIN_POST = "https://login.yahoo.co.jp/config/login?"
URL_YAHOO_FINANCE_PORTFOLIO = (
    "http://info.finance.yahoo.co.jp/portfolio/display/?portfolio_id=pf_1"
)
URL_YAHOO_FINANCE_PORTFOLIO2 = (
    "http://info.finance.yahoo.co.jp/portfolio/display/?portfolio_id=pf_2"
)

USER_AGENT_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2272.101 Safari/537.36"  # noqa: E501
ACCOUNT = ("srqys795@yahoo.co.jp", "zidane22")


def http_get_yahoo(url, cookies={}):
    # 実在するユーザエージェントを設定
    headers = {"User-Agent": USER_AGENT_CHROME}
    # キープアライブ設定
    headers["Connection"] = "Keep-Alive"

    r = requests.get(url, headers=headers, cookies=cookies, timeout=5)
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
        log_print("albatross:", params[".albatross"], "->", end=" ")
        params[".albatross"] = m.group(1)
        log_print(params[".albatross"])
    else:
        log_warning(" .albatrossが見つかりません")
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
    log_print("%sへ接続..." % URL_YAHOO_TOP)
    r = http_get_yahoo(URL_YAHOO_TOP)
    log_print("cookies yahoo_top:", r.cookies)
    log_print("B_cookie:", r.cookies["B"])
    time.sleep(float(656 / 1000))

    # --- yahoo ログイン画面を表示
    log_print("%sへ接続..." % URL_YAHOO_LOGIN)
    cookies = dict(B=r.cookies["B"])
    r2 = http_get_yahoo(URL_YAHOO_LOGIN, cookies)
    log_print("cookies yahoo_login:", r2.cookies)
    html = r2.text.encode("utf-8")
    # file_write("tmp.html", html)

    time.sleep(float(724 / 1000))

    # --- yahooへログイン
    log_print("%sへ接続..." % URL_YAHOO_LOGIN_POST)
    data = build_params_for_login(html)  # ログインhtmlからpostパラメータ取得
    cookies = dict(B=r.cookies["B"])
    # print "data:", data #, "cookies", cookies
    r3 = http_post_yahoo(URL_YAHOO_LOGIN_POST, data, cookies)
    if "文字認証" in r3.text.encode("utf-8"):
        log_warning(" 文字認証が求められています")
    time.sleep(float(724 / 1000))

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
        cookies = pickle.load(open(COOKIE_NAME, "rb"))
    log_print(cookies, len(cookies))
    # --- yahooのサイトへアクセス
    if len(cookies) > 0:
        log_print("cookieを%sに保存" % COOKIE_NAME)
        pickle.dump(cookies, open(COOKIE_NAME, "wb"))

        log_print("%sへ接続..." % URL_YAHOO_FINANCE_PORTFOLIO)
        r = http_get_yahoo(URL_YAHOO_FINANCE_PORTFOLIO, cookies)
        html_yahoo = r.text.encode("utf-8")
        file_write("yahoo_portfolio.html", html_yahoo)
        # TODO: ポートフォリオ更新したいけど文字認証いっちゃってログインできない
        # dryscrapeじゃないとだめか？


def test_build_params():
    html = file_read("tmp.html")
    data = build_params_for_login(html)
    log_print(data)


# ==================================================
# ポートフォリオ更新
# ==================================================


def parse_my_portforio():
    """自分のウォッチリストの銘柄コードリストを返す。

    Returns:
        (list<str>,list<str>): (ウォッチ, 保有)
            ウォッチ = ステータスが 2準 / 3監 の銘柄
            保有     = ステータスが 1保 の銘柄
            両リストとも code_s 昇順

    portfolio_shelve を真実源として参照する (Phase 3a)。shelve が空なら
    両リストとも空。参照失敗は呼び出し元へ例外を上げる (issue #192)。
    """
    import portfolio_shelve as ps
    records = ps.list_records()

    possess_list = sorted(
        r["code_s"] for r in records if r.get("status") == "1保"
    )
    code_s_list = sorted(
        r["code_s"] for r in records if r.get("status") in ("2準", "3監")
    )
    return code_s_list, possess_list


def main():
    # ロガーの初期化
    logger = setup_logger("shintakane")

    args = "update"
    # args = "update get"
    args = "get"
    if "get" in args:
        # get_latest_portfolio()
        # test_build_params()
        parse_my_portforio()


if __name__ == "__main__":
    setup_logger("portfolio")
    main()
