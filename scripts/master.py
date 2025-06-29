#!/usr/bin/env python3

import os
from ks_util import *

import re
import csv

import rironkabuka
import make_sector_data
import make_market_db
from datetime import datetime


def parse_master_html_kabutan(html):
    """株探基本情報htmlを解析し銘柄情報抽出
    str -> dict
    Returns:
        dict: 銘柄情報
          market_cap: 時価総額
          lowest_purchase_money: 最低購入代金
          stock_name: 銘柄名
          market: 市場
          sector: 業種
          overview: 概要
          themes: テーマ
          relates: 比較される銘柄
          kessanbi: 決算日
          corporate_url: 会社サイト
    """
    # 時価総額、最低購入代金、銘柄名
    # セクター、概要
    detail = {}
    # 時価総額
    import shihyou

    jikasogaku = shihyou.parse_jikasogaku_kabutan(html)
    detail["market_cap"] = jikasogaku
    log_print("時価総額:", jikasogaku)

    # 最低購入代金
    lowest_purchase_money = 0
    m = re.search(r"<th scope='row'>売買最低代金</th>\r\n      <td>(.*?)</td>", html)
    if m:
        try:
            lowest_purchase_money = int(
                m.group(1).replace("&nbsp;円", "").replace(",", "")
            )
        except ValueError:
            pass
    log_print("最低購入代金:", lowest_purchase_money)
    detail["lowest_purchase_money"] = lowest_purchase_money
    # 銘柄名
    m = re.search(r'<h1 id="kobetsu">(.*?)\(\d[0-9a-zA-Z]\d[0-9A-Z]\).*?</h1>', html)
    stock_name = "銘柄名不明"
    if m:
        # $1略名がある場合:<abbr title="ＮＥＸＴ　ＦＵＮＤＳ野村株主還元７０">株主還元７０</abbr>
        m2 = re.search(r"<abbr title=.*?>(.*?)</abbr>", m.group(1))
        if m2:
            stock_name = m2.group(1).strip()
        else:
            stock_name = m.group(1).strip()
        detail["stock_name"] = stock_name
    log_print("銘柄名:", stock_name)
    # 市場
    m = re.search(r'<span class="market">(.*?)</span>', html)
    market = "市場名不明"
    if m:
        market = m.group(1).strip()
        detail["market"] = market
    log_print("市場:", market)
    # 業種
    m = re.search(r'<a href="/themes/\?industry=\d{1,2}&market=\d">(.*?)</a>', html)
    sector_name = "セクター名不明"
    if m:
        sector_name = m.group(1).strip()
        detail["sector"] = sector_name
    log_print("セクター名:", sector_name)
    # 概要
    m = re.search(r"<th scope='row'>概要</th>\r\n      <td>(.*?)</td>", html)
    overview = ""
    if m:
        overview = m.group(1).strip()
        detail["overview"] = overview
    log_print("概要:", overview)
    # テーマ
    themes = []
    for m in re.finditer(r'<li><a href="/themes.*?>(.*?)</a></li>', html):
        themes.append(m.group(1))
    log_print("テーマ:", ",".join(themes))
    detail["themes"] = ",".join(themes)
    # 比較される銘柄
    relates = []
    for m in re.finditer(
        r'<dd><a href="javascript:set_stock_url\(2,\'(\d\d\d\d)\'', html
    ):
        relates.append(m.group(1))
    log_print("比較銘柄:", relates)
    detail["relates"] = ",".join(relates)
    # 決算日
    kessan = ""
    m = re.search(r'<div id="kessan_happyoubi">(.*?)</div>', html, re.DOTALL)
    if m:
        # print "m:", m.group(1)
        m2 = re.search(r'<time datetime=".*?">(.*?)</time>', m.group(1), re.DOTALL)
        if m2:
            kessan = m2.group(1)
    log_print("決算日:", kessan)
    detail["kessanbi"] = kessan
    # 会社サイト
    m = re.search(
        r'<th scope=\'row\'>会社サイト</th>.*?<a href="(.*?)".*?</a>', html, re.DOTALL
    )
    if m:
        corpo = m.group(1)
        detail["corporate_url"] = corpo
    return detail


def get_master_data_kabutan(code_s, upd=UPD_INTERVAL):
    log_print("銘柄詳細ファイルを取得 キャッシュ:")
    html = rironkabuka.get_kabutan_base_html(code_s, upd)
    return html


def memoized_report_evaluation():
    eval_dict = {}
    alreadys = [False]

    def create_report_evaluation():
        if alreadys[0]:
            return eval_dict
        else:
            # ↓一旦封印のため注意
            report_fname = os.path.join(DATA_DIR, "googledrive/銘柄調査 - 銘柄調査.csv")
            csv_r = csv.reader(open(report_fname, "r", encoding="utf-8"))
            for row in csv_r:
                code = row[0]
                # stock_name = row[1]
                evaluation = row[4]
                if evaluation:
                    try:
                        eval_dict[int(code)] = evaluation
                    except ValueError:
                        pass
            alreadys[0] = True
            return eval_dict

    return create_report_evaluation


get_report_evalutation = memoized_report_evaluation()


def calc_fundamental(code_s, themes):
    log_print("テーマポイントの計算")  # , themes
    market_db = make_market_db.get_market_db()
    theme_rank_pt = {v: 30 - i for (i, v) in enumerate(market_db["theme_rank"])}
    total_pt = 0
    for theme in themes.split(","):
        theme_pt = theme_rank_pt.get(theme, 0)
        # if theme_pt > 0:
        # 	print theme, theme_pt
        total_pt += theme_pt
    log_print("テーマポイント:", total_pt)
    total_pt = min(total_pt, 100)  # オレレポート封印のため80から100に

    # オレレポート分のファンダポイントを加算
    # 一旦封印
    # eval_dict = get_report_evalutation()
    # evaluation = eval_dict.get(code_s, "")
    # eval_pt_dict = {"S": 40, "A": 30, "B": 20, "C": 5, "D": 0, "E": -10}
    # eval_pt = eval_pt_dict.get(evaluation, 0)
    # log_print("オレ評価:%s(%d)" % (evaluation, eval_pt))
    # total_pt += eval_pt
    # total_pt = min(total_pt, 100)
    return total_pt


def is_delist(parse_data):
    """上場廃止かどうかをチェックする"""
    return (
        parse_data.get("market_cap", 0) == 0
        and parse_data.get("lowest_purchase_money", 0) == 0
    )


def get_stock_master_data(code_s, upd):
    """銘柄基本情報を株探から取得
    Returns:
        dict<str, Any>:
        strはfunda_pt/code/access_date/sector_detail
    """
    # DBにない場合はWebから取得
    detail_text = get_master_data_kabutan(code_s, upd)
    log_print(">>>>> %sのマスター情報を解析 " % code_s)
    parsed_data = parse_master_html_kabutan(detail_text)
    if parsed_data:
        # print "銘柄名:%s,時価総額:%d百万円\n最低購入代金:%d"%\
        # (parsed_data["stock_name"], parsed_data["market_cap"], parsed_data["lowest_purchase_money"])
        for k, v in list(parsed_data.items()):
            log_print("%s: %s" % (k, v))
        log_print("<<<<< 解析完了 ")
    # テーマからファンダポイントを計算
    funda_pt = calc_fundamental(code_s, parsed_data["themes"])
    parsed_data["funda_pt"] = funda_pt

    # ---- 情報を追加してdictを返す
    set_db_code(parsed_data, code_s)
    # マスター情報更新日(上場廃止でも更新)
    master_fname = rironkabuka.get_kabutan_cachename(code_s)
    if os.path.exists(master_fname):
        stat = os.stat(master_fname)
        parsed_data["access_date"] = datetime.fromtimestamp(stat.st_mtime)

    # セクターを取得
    parsed_data["sector_detail"] = make_sector_data.get_sector_detail(code_s)
    log_print("詳細セクター:", parsed_data["sector_detail"])
    return parsed_data


def main():
    # ロガーの初期化
    logger = setup_logger('make_stock_db')
    
    # TODO: !!! 時価総額の値が取得できません でとる
    code_list = ["176A"]  # 7776
    for code in code_list:
        log_print("-" * 30)
        log_print("%sの基本情報を更新します" % code)
        price_dict = get_stock_master_data(
            code, UPD_REEVAL
        )  # noqa: E501 UPD_INTERVAL UPD_FORCE
        log_print(price_dict)


if __name__ == "__main__":
    main()
