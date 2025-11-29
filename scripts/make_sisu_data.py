#!/usr/bin/env python3

import re
from datetime import date
import pickle

from ks_util import *

JP_STOCK = "jp_stock"
JP_REIT = "jp_reit"
GL_STOCK = "gl_stock"
GL_REIT = "gl_reit"
EM_STOCK = "em_stock"
GL_BOND = "gl_bond"
GOLD = "gold"
MY_FRONTIER = "my_frontier"
ASSET_CLASSES = [
    JP_STOCK,
    JP_REIT,
    GL_STOCK,
    GL_REIT,
    EM_STOCK,
    GL_BOND,
    GOLD,
    MY_FRONTIER,
]
# RS_DB作成の元データとなる取得先URL
ASSET_URLS = [
    "http://info.finance.yahoo.co.jp/history/?code=1306.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    "http://info.finance.yahoo.co.jp/history/?code=1343.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # "http://finance.yahoo.com/q/hp?s=TOK&a=%02d&b=%d&c=%d&d=%02d&e=%d&f=%d&g=w",
    "http://info.finance.yahoo.co.jp/history/?code=1550.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # "http://finance.yahoo.com/q/hp?s=VNQ&a=%02d&b=%d&c=%d&d=%02d&e=%d&f=%d&g=w",
    # グローバルREIT
    "http://info.finance.yahoo.co.jp/history/?code=2515.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # "http://info.finance.yahoo.co.jp/history/?code=64318081&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # iShareエマージングETF(米)
    # "http://finance.yahoo.com/q/hp?s=EEM&a=%02d&b=%d&c=%d&d=%02d&e=%d&f=%d&g=w",
    # 上場インデックス海外新興国
    "http://info.finance.yahoo.co.jp/history/?code=1681.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # iShareエマージングETF(2018.1上場廃止)
    # "http://info.finance.yahoo.co.jp/history/?code=1582.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # SMTグローバル債券
    # "http://info.finance.yahoo.co.jp/history/?code=64316081&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    "http://info.finance.yahoo.co.jp/history/?code=2511.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # 金ETF
    "http://info.finance.yahoo.co.jp/history/?code=1540.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # インド
    "http://info.finance.yahoo.co.jp/history/?code=1678.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # トルコ
    # "http://info.finance.yahoo.co.jp/history/?code=45311065&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # インドネシア
    # "http://info.finance.yahoo.co.jp/history/?code=7231109B&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
    # フロンティア(2018.1上場廃止)
    # "http://info.finance.yahoo.co.jp/history/?code=1583.T&sy=%d&sm=%d&sd=%d&ey=%d&em=%d&ed=%d&tm=w",
]
ASSET_URL2 = [
    "https://finance.yahoo.co.jp/quote/1306.T/history?from=%s&to=%s&timeFrame=w",
    "https://finance.yahoo.co.jp/quote/1343.T/history?from=%s&to=%s&timeFrame=w",
    "https://finance.yahoo.co.jp/quote/1550.T/history?from=%s&to=%s&timeFrame=w",
    # グローバルREIT
    "https://finance.yahoo.co.jp/quote/2515.T/history?from=%s&to=%s&timeFrame=w",
    # 上場インデックス海外新興国
    "https://finance.yahoo.co.jp/quote/1681.T/history?from=%s&to=%s&timeFrame=w",
    # SMTグローバル債券
    "https://finance.yahoo.co.jp/quote/2511.T/history?from=%s&to=%s&timeFrame=w",
    # 金ETF
    "https://finance.yahoo.co.jp/quote/1540.T/history?from=%s&to=%s&timeFrame=w",
    # インド
    "https://finance.yahoo.co.jp/quote/1678.T/history?from=%s&to=%s&timeFrame=w",
]

# 分配金データ
DISTRIBUTE_DATA = {
    "45311065": {"2013-01-25": 1400},
    "7231109": {"2013-06-07": 2000, "2014-06-09": 1100},
}

RS_DB_NAME = os.path.join(DATA_DIR, "sisu_data", "rs_db.pickle")


def parse_yahoo_jp(text):
    price_list = []
    # 週足、新しい順
    # 日付	始値	高値	安値	終値
    # 2014年8月18日	1,272.14	1,296.02	1,268.37	1,286.07
    for m in re.finditer(r"(\d{4}年.*日)\t(.*)\t(.*)\t(.*)", text):
        # print m.group(1), "-", m.group(4)
        m2 = re.search(r"(\d*)年(\d*)月(\d*)日", m.group(1))
        day = (
            m2.group(1).zfill(4)
            + "/"
            + m2.group(2).zfill(2)
            + "/"
            + m2.group(3).zfill(2)
        )
        price = int(float(m.group(4).replace(",", "")))
        # print day, price
        price_list.append([day, price])
    return price_list


MON_DICT = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def parse_yahoo_us(text):
    """
    yahoous版のコピペtxtから株価リストを取得する
    str => list<str, str>
    """
    # Date	Open	High	Low	Close	Avg Vol	Adj Close*
    # Aug 18, 2014	196.80	199.76	196.69	199.50	68,648,200	199.50
    yen_rate = 100
    price_list = []
    for m in re.finditer(r"(\w{3} .*?)\t(.*)\t(.*)\t(.*)\t(.*)\t(.*)\t(.*)", text):
        # print m.group(1), "-", m.group(7)
        m2 = re.search(r"(\w*) (\d*), (\d*)", m.group(1))
        mon = MON_DICT[m2.group(1)]
        day = (
            m2.group(3).zfill(4) + "/" + str(mon).zfill(2) + "/" + m2.group(2).zfill(2)
        )
        price = int(float(m.group(7).replace(",", "")) * yen_rate)
        # print day, price
        price_list.append([day, price])
    return price_list  # [日付、価格] のリスト


def make_price_list(price_list):
    log_print("価格リストを作成します", len(price_list))
    price_list2 = []
    itr = iter(reversed(price_list))
    # for price in reversed(price_list):
    # 	d = date(int(price[0][0:4]), int(price[0][5:7]), int(price[0][8:10]))
    # 	print d, d.isocalendar()

    start_y = 2004
    start_w = 32
    end_w = 34
    flg = False

    for y in range(start_y, 2015):
        first_w = start_w if y == start_y else 1
        # flg = False
        index = 0
        for w in range(first_w, 53):
            while index < 53 * 5:
                if not flg:
                    try:
                        # print "next"
                        price = next(itr)
                    except StopIteration:
                        if w <= 34:
                            log_print("データ終了補充", price)
                            dt = datetime.strptime("%04d%02d1" % (y, w), "%Y%W%w")
                            date_m = "%04d/%02d/%02d" % (dt.year, dt.month, dt.day)
                            log_print(date_m)
                            price_ = price[:]  # コピー
                            price_[0] = date_m
                            price_list2.append(price_)
                            break
                        else:
                            break
                # print price
                d = date(int(price[0][0:4]), int(price[0][5:7]), int(price[0][8:10]))
                y2 = d.isocalendar()[0]
                w2 = d.isocalendar()[1]
                log_print(y, w, "-", y2, w2)
                if y == y2 and w == w2:
                    price_list2.append(price)
                    flg = False
                    break
                if y * 100 + w < y2 * 100 + w2:  # データがないので追加
                    log_print("データを補充：", price)
                    # 週番号から日付を取得
                    dt = datetime.strptime("%04d%02d1" % (y, w), "%Y%W%w")
                    date_m = "%04d/%02d/%02d" % (dt.year, dt.month, dt.day)
                    # print price_m
                    price_ = price[:]  # コピー
                    price_[0] = date_m
                    price_list2.append(price_)
                    if w2 < 53:
                        flg = True
                    break
                index += 1
    return price_list2


def print_price_list(price_list2):
    log_print("-" * 30)
    for p in price_list2:
        log_print(p)
    log_print(str(len(price_list2)) + "個のデータ")
    log_print("-" * 30)


def parse_topix():
    text = file_read("sisu_data/topix.txt")
    parse_list = parse_yahoo_jp(text)
    price_list2 = make_price_list(parse_list)
    print_price_list(price_list2)
    return price_list2


def parse_tosho_reit():
    text = file_read("sisu_data/tosho_reit.txt")
    price_list = parse_yahoo_jp(text)
    price_list2 = make_price_list(price_list)
    print_price_list(price_list2)
    return price_list2


def parse_spdr_sp500():
    text = file_read("sisu_data/SPDR_S&P500.txt")
    price_list = parse_yahoo_us(text)
    price_list2 = make_price_list(price_list)
    print_price_list(price_list2)
    return price_list2


def parse_vanguard_reit():
    text = file_read("sisu_data/Vanguard_REIT.txt")
    price_list = parse_yahoo_us(text)
    price_list2 = make_price_list(price_list)
    print_price_list(price_list2)
    return price_list2


def parse_ishares_msci_emerging():
    text = file_read("sisu_data/iShares_MSCI_Emerging.txt")
    price_list = parse_yahoo_us(text)
    price_list2 = make_price_list(price_list)
    print_price_list(price_list2)
    return price_list2


def parse_ssga_worldbond():
    text = file_read("sisu_data/SSGA_WorldBond.txt")
    price_list = parse_yahoo_us(text)
    price_list2 = make_price_list(price_list)
    print_price_list(price_list2)
    return price_list2


def parse_spdr_goldshares():
    text = file_read("sisu_data/SPDR_GoldShares.txt")
    price_list = parse_yahoo_us(text)
    price_list2 = make_price_list(price_list)
    print_price_list(price_list2)
    return price_list2


def make_sisu_db():
    # import filecmp
    # print filecmp.cmp("sisu_data/sisu_db.pickle", "sisu_data/sisu_db.pickle のコピー")
    # return
    db_dict = {}
    if os.path.exists("sisu_data/sisu_db.pickle"):
        db_dict = pickle.load(open("sisu_data/sisu_db.pickle", "rb"))

    parser_list = [
        parse_topix,
        parse_tosho_reit,
        parse_spdr_sp500,
        parse_vanguard_reit,
        parse_ishares_msci_emerging,
        parse_ssga_worldbond,
        parse_spdr_goldshares,
    ]
    for i, asset in enumerate(ASSET_CLASSES[:7]):
        log_print("DB作成:", asset)
        db_dict[asset] = parser_list[i]()

    pickle.dump(db_dict, open("sisu_data/sisu_db.pickle", "wb"))


def parse_html_yahoo_jp(html, title=""):
    """
    Yahoo!ファイナンス(日本)の株価時系列テーブルから [日付(YYYY-MM-DD), 調整後終値] を抽出する
    """

    def strip_html(text):
        return re.sub(r"<[^>]+>", "", text).strip()

    rows = []
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
    for table_html in tables:
        # ヘッダの抽出と必要列の特定
        header_cells = re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.S | re.I)
        headers = [strip_html(h) for h in header_cells]
        if not headers or not any("日付" in h for h in headers):
            continue

        date_idx = next((i for i, h in enumerate(headers) if "日付" in h), None)
        # 調整後終値を優先、なければ終値
        price_idx = next(
            (i for i, h in enumerate(headers) if "調整" in h and "終値" in h), None
        )
        if price_idx is None:
            price_idx = next((i for i, h in enumerate(headers) if "終値" in h), None)
        if date_idx is None or price_idx is None:
            continue

        # データ行を走査
        for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)[1:]:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr_html, re.S | re.I)
            if len(cells) <= max(date_idx, price_idx):
                continue

            date_text = strip_html(cells[date_idx])
            # 日付正規化
            if "年" in date_text:
                m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", date_text)
                if not m:
                    continue
                date_s = (
                    f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                )
            else:
                date_text = date_text.replace("/", "-").replace(".", "-")
                m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_text)
                if not m:
                    continue
                date_s = (
                    f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                )

            price_text = strip_html(cells[price_idx]).replace(",", "")
            try:
                price = int(float(price_text))
            except Exception:
                continue

            rows.append([date_s, price])

    log_print("直近価格:", rows[:3])
    return rows


def parse_html_yahoo_us(html, is_all_price=False):
    """
    yahoo_us版のhtmlから株価リストを取得する
    str => list<str, float>
    """
    # TODO: HTMLの形式が変わったようだもう使えない
    rows = []
    for m2 in re.finditer(r'<tr><td class="yfnc_tabledata1".+?</tr>', html):
        # print m2
        m = re.search(
            r"<td.*?>((\w{3}) (\d{1,2}), (\d{4}))</td><td.*?>(.+?)</td><td.*?>(.+?)</td><td.*?>(.+?)</td><td.*?>(.+?)</td><td.*?>(.+?)</td>",
            m2.group(0),
        )
        if m:
            mon = MON_DICT[m.group(2)]
            day = "%04d-%02d-%02d" % (int(m.group(4)), mon, int(m.group(3)))
            if is_all_price:
                prices = [
                    float(m.group(5).replace(",", "")),
                    float(m.group(6).replace(",", "")),
                    float(m.group(7).replace(",", "")),
                    float(m.group(8).replace(",", "")),
                    float(m.group(9).replace(",", "")),
                ]
                rows.append([day] + prices)
            else:
                price = float(m.group(8).replace(",", ""))
                log_print(day, price)
                rows.append([day, price])
        else:
            log_print("  DivideEnd ")  # , day

    return rows


def parse_html(html):
    m = re.search(r"<title>(.*?)</title>", html)
    title = m.group(1)
    log_print("title:", title)
    if "Yahoo!ファイナンス" in title:
        return parse_html_yahoo_jp(html, title)
    elif "Yahoo! Finance" in title:
        return parse_html_yahoo_us(html)


CLM_DATE = 0
CLM_PRICE = 1


def date_from_isoformat(isoformat):
    """
    isoformat文字列をdateオブジェクトに
    (str(yyyy-mm-dd) => date)
    """
    return date(int(isoformat[0:4]), int(isoformat[5:7]), int(isoformat[8:10]))


def update_market_tbl(market_db, tbl_name, rows):
    current_rows = market_db.get(tbl_name, [])
    # rows_new = current_rows[:]
    for row in iter(reversed(rows)):  # 古い順に追加
        update = False
        for j, crow in enumerate(current_rows):
            if row[CLM_DATE] == crow[CLM_DATE]:
                if not row[CLM_PRICE] == crow[CLM_PRICE]:
                    log_print("更新: ", row)
                crow[CLM_PRICE] = row[CLM_PRICE]
                update = True
                break
        if not update:
            insert = False
            for j, crow in enumerate(current_rows):
                row_date = date_from_isoformat(row[CLM_DATE])
                crow_date = date_from_isoformat(crow[CLM_DATE])
                if row_date < crow_date:
                    insert = True
                    current_rows.insert(j, row)
                    log_print("データ挿入[%d]：" % j, row)
                    break
            if not insert:
                current_rows.append(row)
                log_print("データ追加：", row)
    return current_rows


def modify_distribute(code_name, rows):
    if code_name in DISTRIBUTE_DATA:
        log_print("modify_distribute:", code_name)
        distribute = DISTRIBUTE_DATA[code_name]
        for row in rows:
            for k, v in list(distribute.items()):
                if date_from_isoformat(row[CLM_DATE]) > date_from_isoformat(k):
                    # print "  ", row[0], row[1], "->",
                    row[1] = row[1] + v
                    # print row[1]
    return rows


def calc_stdev(lst):
    import math

    m = average(lst)
    square_sum = sum([(l - m) ** 2 for l in lst])
    stdev = square_sum / len(lst)
    stdev = math.sqrt(stdev)
    return stdev


def make_rs_db():
    """
    RS投資用DBを作成
    """
    market_db = {}
    if os.path.exists(RS_DB_NAME):
        market_db = pickle.load(open(RS_DB_NAME, "rb"))

    for i, url in enumerate(ASSET_URL2):
        log_print("-" * 15)
        # 一年前の日付でリクエスト
        today = date.today()
        url = url % (
            "%04d%02d%02d" % (today.year - 1, today.month, today.day),
            "%04d%02d%02d" % (today.year, today.month, today.day),
        )
        # tbl更新
        for p in range(2):
            url_p = url
            url_p = url + "&page=%d" % (p + 1)

            log_print("Request.. %s" % url_p)
            html = http_get_html(
                url_p, cache_dir=os.path.join(DATA_DIR, "sisu_data"), use_cache=True
            )
            rows = parse_html(html)
            log_print("parse完了:", url_p)

            # テーブル名はtbl_コード名
            # "https://finance.yahoo.co.jp/quote/1306.T/history?from=%s&to=%s&timeFrame=w"
            m = re.search(r"\d{4}", url_p)
            if m:
                code_name = m.group(0)
            else:
                log_warning("取得したいDBのコードが不明です", code_name)
                continue

            tbl_name = "tbl_" + code_name  # tbl_プレフィックス
            log_print("tbl:", tbl_name)
            rows = modify_distribute(code_name, rows)
            rows_new = update_market_tbl(market_db, tbl_name, rows)
            market_db[tbl_name] = rows_new
    # マイフロンティア指数
    # print "マイフロンティア指数の作成"
    # tbl1 = market_db["tbl_45311065"]
    # tbl2 = market_db["tbl_7231109"]
    # # tbl3 = market_db["tbl_1583"]
    # rows = []
    # tbl1_price = [t[1] for t in tbl1]
    # stdev = calc_stdev(tbl1_price)
    # tbl1_price = [int(100*t/stdev) for t in tbl1_price]
    # # print tbl1_price
    # tbl2_price = [t[1] for t in tbl2]
    # stdev = calc_stdev(tbl2_price)
    # tbl2_price = [int(100*t/stdev) for t in tbl2_price]
    # for row1, row2, p1, p2 in zip(tbl1, tbl2, tbl1_price, tbl2_price):
    # 	row = [0]*2
    # 	if not row1[CLM_DATE] == row2[CLM_DATE]:
    # 		print " 日付が同じでない", row1[CLM_DATE]
    # 	row[CLM_DATE] = row1[CLM_DATE]
    # 	row[CLM_PRICE] = (p1+p2)/2
    # 	# print row
    # 	rows.append(row)
    # market_db["tbl_myfrontier"] = rows

    # 表示
    log_print("=" * 20)
    log_print("テーブル一覧")
    for key in list(market_db.keys()):
        table = market_db[key]
        log_print("-" * 3, end=" ")
        log_print(key, len(table), "個の列")
        log_print(table[0:3], "...")
        log_print(table[-3:])
    # DB保存
    pickle.dump(market_db, open(RS_DB_NAME, "wb"))


def convert_python2_to3():
    """Python2のpickleをPython3のpickleに変換
    一時的実行用
    """
    import make_stock_db

    RS_DB_NAME_2 = os.path.join(DATA_DIR, "sisu_data", "rs_db_py2.pickle")
    make_stock_db.convert_pickle_latin1_to_utf8(RS_DB_NAME_2, RS_DB_NAME)


def main():
    # ロガーの初期化
    logger = setup_logger("make_stock_db")

    # make_sisu_db()
    make_rs_db()


def test():
    """
    テスト用
    """
    convert_python2_to3()


if __name__ == "__main__":
    main()
    # test()
