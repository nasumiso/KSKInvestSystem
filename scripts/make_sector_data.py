#!/usr/bin/env python3
# flake8: noqa
# ================================================
# セクターDBを作成します。
# 削除候補
# ================================================

import re

# 試しに使ってみたがこんなもん使ってられねー。
from html.parser import HTMLParser

from ks_util import *

from db_shelve import get_sector_db as _get_sector_shelve_db

URL_REUTER_SECTOR_TABLE_TOP = "https://commerce.jp.reuters.com/purchase/fdcScreen2.do"
URL_REUTER_SECTOR_TABLE = "https://commerce.jp.reuters.com/screening/Sectortop.asp"
PATH_STOCKS_PICKLE = "stock_data/stocks.pickle"


def print_list(lst):
    def puts(str):
        log_print(str, end=" ")

    [puts(l) for l in lst]
    log_print()


class SectorHtmlParser(HTMLParser):
    table_counter = 0
    tr_counter = 0
    td_atag_attrs = {}
    row_data = []
    table_data = []

    def __init__(self):
        HTMLParser.__init__(self)

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tr_counter = 0
            if self.table_counter == 1:
                log_print("start_table:", tag, attrs, self.getpos())
        elif tag == "tr":
            self.td_counter = 0
            if self.table_counter == 1:  # 1個目のtableタグらしい
                self.row_data = []
                # print "start_tr[%d]:"%self.tr_counter, tag, attrs
        elif tag == "td":
            if self.table_counter == 1:  # 1個目のtableタグらしい
                # print "startend_td[%d]:"%self.tr_counter, tag, attrs
                pass
        elif tag == "a":
            if self.table_counter == 1:
                if self.td_counter == 8:
                    self.td_atag_attrs = dict(attrs)
                    # print self.td_atag_attrs

    def handle_endtag(self, tag):
        if tag == "table":
            self.table_counter += 1
            if self.table_counter == 1:
                log_print("end:", tag, self.getpos(), self.table_counter)
        elif tag == "tr":
            if self.table_counter == 1:
                log_print("--- row_data[%d]:" % self.tr_counter)
                print_list(self.row_data[:-1])
                self.table_data.append(self.row_data)
            self.tr_counter += 1
        elif tag == "td":
            if self.table_counter == 1:
                pass
            self.td_counter += 1

    def handle_data(self, data):
        if self.table_counter == 1:
            if data.strip():
                # print "  tr[%d][%d] data:"%(self.tr_counter,self.td_counter), data
                if self.td_counter == 8:  # URLリンクのところ
                    data = self.td_atag_attrs.get("href", "")
                    # print data
                    # print self.td_atag_attrs
                if self.td_counter == len(self.row_data):
                    self.row_data.append(data)
                else:
                    self.row_data[-1] = self.row_data[-1] + data


def parse_html(html):
    """セクター解析用htmlを解析する"""
    parser = SectorHtmlParser()
    parser.feed(html)
    parser.close()

    sector_table_org = parser.table_data
    sector_table = {}
    for row in sector_table_org[1:]:
        log_print("-" * 5, row[0], row[2], "を解析..")
        sector_count = int(row[2])
        html = http_get_html(row[8], cache_dir="stock_data/sector")
        code_s_list = []
        for m in re.finditer(r"(\d{4}).(?:T|NG|FU|SP) ", html):
            if m.group(0).endswith("T "):
                # code_list.append(int(m.group(1)))
                code_s_list.append(m.group(1))
            else:
                # print "非東証銘柄:", m.group(0)
                sector_count -= 1
        # print code_list, len(code_list)
        if sector_count != len(code_s_list):
            log_warning(" 取得していない? %d個" % (sector_count - len(code_s_list)))
        sector_table[row[0]] = code_s_list
        log_print(sector_table[row[0]])

    return sector_table


def make_sector_data():
    """
    詳細セクター情報を一から作成する
    """
    # TODO: もうアクセスできないようだ
    html = http_get_html(URL_REUTER_SECTOR_TABLE, cache_dir="stock_data/sector")
    sector_table = parse_html(html)
    # 保存
    _save_sector_db(sector_table)
    return sector_table


PATH_SECTOR_DB = "stock_data/sector/sector_db.pickle"


def _load_sector_db():
    """セクターDBをロードする内部関数"""
    with _get_sector_shelve_db() as db:
        if len(db) == 0:
            return {}
        return db.export_to_dict()


def _save_sector_db(sector_table):
    """セクターDBを保存する内部関数"""
    with _get_sector_shelve_db() as db:
        db.import_from_dict(sector_table)


def get_sector_detail(code_s):
    """
    指定銘柄のセクター情報を取得
    int -> str
    """
    sector_tables = _load_sector_db()
    for sector_name, code_list in list(sector_tables.items()):
        if code_s in code_list:
            return sector_name
    return "unknown"


def test_make_secotr_data():
    """
    セクター情報を銘柄DBから読み込み表示
    """
    latest = False
    sector_db_exists = _get_sector_shelve_db().exists()
    if not sector_db_exists or latest:
        sector_table = make_sector_data()
    else:
        sector_table = _load_sector_db()
    # ロード
    import make_stock_db
    stocks_db = make_stock_db.load_stock_db()
    # 表示
    for sector_name, code_list in list(sector_table.items()):
        unknown_code_list = []
        log_print("-" * 5, sector_name, "%d銘柄" % len(code_list))
        for code_s in code_list:
            if code_s in stocks_db:
                code_name = stocks_db[code_s].get("stock_name", "")
            else:
                code_name = ""
            if code_name:
                log_print("  %s %s" % (code_s, code_name))
            else:
                unknown_code_list.append(code_s)
        log_print("  銘柄名不明:", unknown_code_list)


def update_sector_stockdb():
    """
    stock_db内の銘柄に対して詳細セクター更新
    """
    sector_tables = _load_sector_db()
    # 更新
    import make_stock_db

    stocks = make_stock_db.load_stock_db()
    for code_s in stocks.keys():
        stock = stocks[code_s]
        if "sector_detail" in stock:
            log_print("Unknown:", code_s, stock["sector_detail"])
            continue  # unkownとして更新する場合はコメントアウト
        # 詳細セクター名更新
        for sector_name, code_s_list in list(sector_tables.items()):
            if code_s in code_s_list:
                stocks[code_s]["sector_detail"] = sector_name
                log_print(
                    "詳細セクター更新: %s%s <- %s"
                    % (code_s, stock["stock_name"], sector_name)
                )
                break
    make_stock_db.save_stock_db(stocks)


def main():
    # ロガーの初期化
    logger = setup_logger('make_stock_db')

    # test_make_secotr_data()
    # print get_sector_detail(7270)
    update_sector_stockdb()
    pass


if __name__ == "__main__":
    setup_logger("make_sector_data")
    main()
