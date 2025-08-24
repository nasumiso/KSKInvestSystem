#!/usr/bin/env python3

import argparse
import sys
import os
import re
from datetime import datetime, timedelta
import csv
import shutil
import traceback

import make_stock_db as stock_db
import make_market_db

import kessan
from ks_util import *


def search_fromcsv(fname):
    """新高値csvを読み込んで各銘柄情報をディクショナリのリストにして返す

    Returns: ファイルから得られる銘柄データ(dict)のリスト
    """
    if not os.path.exists(fname):
        return []

    result_list = []
    csv_r = csv.reader(
        open(fname, "r", encoding="utf-8")
    )  # python3ではrbではなくrで開く
    for row in csv_r:
        row_dict = {}
        row_dict["rank"] = row[0]  # 順位
        # dict["code"] = row[1].split()[0]
        row_dict["code_s"] = row[1].split()[0]
        row_dict["name"] = row[1].split()[1] if len(row[1].split()) > 1 else "名前不明"
        row_dict["place"] = row[2]  # 市場
        row_dict["sector"] = row[3]  # セクター
        row_dict["kabuka"] = int(float(row[4].replace(",", "")))  # 株価
        row_dict["zenjitsuhi"] = int(float(row[5].replace(",", "")))
        row_dict["zenjitsuhi_per"] = row[6]
        row_dict["dekidaga"] = float(row[7].replace(",", ""))
        row_dict["origin"] = "shintakane"
        result_list.append(row_dict)
    return result_list


def search_fromcsv_dekidakaup(fname):
    """
    新高値csvからディクショナリにして返す
    """
    if not os.path.exists(fname):
        return []

    result_list = []
    csv_r = csv.reader(
        open(fname, "r", encoding="utf-8")
    )  # python3ではrbではなくrで開く
    for row in csv_r:
        row_dict = {}
        row_dict["rank"] = row[0]
        # dict["code"] = row[1].split()[0]
        row_dict["code_s"] = row[1].split()[0]
        row_dict["name"] = row[1].split()[1] if len(row[1].split()) > 1 else "名前不明"
        row_dict["place"] = row[2]
        row_dict["sector"] = row[3]
        row_dict["kabuka"] = int(float(row[4].replace(",", "")))
        row_dict["zenjitsuhi"] = int(float(row[5].replace(",", "")))
        row_dict["zenjitsuhi_per"] = row[6]
        row_dict["dekidaga"] = float(row[7].replace(",", ""))
        row_dict["average_dekidaga"] = float(row[8].replace(",", ""))  # 平均出来高
        row_dict["dekidaka_upratio"] = row[9]  # 出来高増加率
        row_dict["origin"] = "dekidakaup"
        result_list.append(row_dict)
    return result_list


def get_shintakane_day_txtname(today):
    """
    datetime から日付新高値テキストファイル名を取得
    """
    txt_template = os.path.join(DATA_DIR, "shintakane_data", "shintakane_%02d%02d%02d")
    today_txt = txt_template % (today.year - 2000, today.month, today.day)
    return today_txt


def get_dekidakaup_day_txtname(today):
    """
    datetime から日付テキストファイル名を取得
    """
    txt_template = os.path.join(DATA_DIR, "shintakane_data", "dekidakaup_%02d%02d%02d")
    today_txt = txt_template % (today.year - 2000, today.month, today.day)
    return today_txt


def get_latest_dekidakaup_fname():
    # 今日の日付
    today = datetime.today()
    today_csv = get_dekidakaup_day_txtname(today) + ".csv"
    # 探す
    count = 0
    CountMax = 60
    while not os.path.exists(today_csv) and count < CountMax:
        log_print("注：今日の情報がありません", today_csv, count)
        today = today - timedelta(1)
        today_csv = get_dekidakaup_day_txtname(today) + ".csv"
        count += 1
    if count >= CountMax:
        log_warning("今日の出来高急増ファイルが見つかりません。")
        return "", today
    return today_csv, today


def get_latest_shintakane_fname():
    """
    最新日付の新高値ファイル名を返す
    """
    # 今日の日付
    today = datetime.today()
    today_csv = get_shintakane_day_txtname(today) + ".csv"
    # 探す
    count = 0
    CountMax = 60
    while not os.path.exists(today_csv) and count < CountMax:
        log_print("注：今日の情報がありません", today_csv, count)
        today = today - timedelta(1)
        today_csv = get_shintakane_day_txtname(today) + ".csv"
        count += 1
    if count >= CountMax:
        log_warning("今日の新高値ファイルが見つかりません。")
        return "", today
    return today_csv, today


def todays_shintakane(upd=UPD_INTERVAL):
    """本日の新高値銘柄データを解析して結果を表示する"""
    log_print("=" * 30)
    log_print("新高値銘柄の解析を開始します・・")
    # 設定パラメータ
    BACK_DAY = 14  # 10 すでに調査したとみなす日数
    # DEKIDAGA_VALUE = 1000*50 # 株価*出来高
    DEKIDAGA_VALUE = 10000  # 株価*出来高
    # LOWEST_PURCHASE_MONEY_VALUE = 80*10000 # 最低購入代金
    # MARKET_CAP = 1000000 # 時価総額 百万円

    # today = datetime.today()
    today_data, latest_csv_dt = get_latest_shintakane_fname()
    today_data_d, latest_csv_dt_d = get_latest_dekidakaup_fname()
    log_print("最新新高値ファイル:", today_data, today_data_d)

    # 各種フィルタ関数：テンバガー成長株の条件
    # 出来高による候補フィルタ関数：一定以上の出来高に絞る
    def dekidaga_filter(val):
        # return val['dekidaga']*val['kabuka'] >= DEKIDAGA_VALUE
        return val["dekidaga"] == 0 or val["dekidaga"] >= DEKIDAGA_VALUE

    def compose_list(day_list_s, day_list_d):
        day_list_codes = [c["code_s"] for c in day_list_s]
        for item in day_list_d:
            if item["code_s"] in day_list_codes:
                # 同じ銘柄は、要素を合成する
                for d in day_list_s:
                    if d["code_s"] == item["code_s"]:
                        # print d["code"], "を合成:", d["origin"], item["origin"]
                        d["origin"] = d["origin"] + item["origin"]
                        break
            else:
                # 新高値にない銘柄は追加
                day_list_s.append(item)
        return day_list_s

    def create_already_list():
        """過去のデータを分析し、既に見た銘柄リストを作成"""
        already_list = []
        # stocks = stock_db.load_stock_db()	# 全銘柄情報をロード
        # print stocks.keys()
        BACK_DAY_DEKIDAKA = 5
        for i in range(BACK_DAY, 0, -1):  # 昨日から
            day = latest_csv_dt - timedelta(i)
            day_csv = get_shintakane_day_txtname(day) + ".csv"
            # 出来高急増リストも追加
            day_csv_d = get_dekidakaup_day_txtname(day) + ".csv"
            day_list = []
            if os.path.exists(day_csv):
                day_list = search_fromcsv(day_csv)
                # 出来高急増は一定日数以内
                if i < BACK_DAY_DEKIDAKA:
                    day_list_d = search_fromcsv_dekidakaup(day_csv_d)
                else:
                    day_list_d = []
                # day_listとday_list_dを合成
                log_print(
                    "新高値銘柄%d個と出来高急増銘柄%d個を合成(%s)"
                    % (len(day_list), len(day_list_d), day.date())
                )
                compose_list(day_list, day_list_d)
                # print "---> 計%d個"%len(day_list)
            if len(day_list) == 0:
                continue
            log_print("----- %s(%d)を分析" % (day_csv, len(day_list)))

            # 各種フィルタ判定した上でリスト作成
            day_list_filtered = day_list
            log_print("候補銘柄%d個" % len(day_list_filtered))
            # まだalready_listにないものは追加
            already_code = [c["code_s"] for c in already_list]
            for d in day_list_filtered:
                if not d["code_s"] in already_code:
                    already_list.append(d)

            # day_list_filtered = [d["code_s"] for d in day_list_filtered]
            # print day_code_list
            # already_list += day_list_filtered
        return already_list
        # return list(set(already_list))	#重複削除

    # 過去すでにみた銘柄リスト(コード)
    already_list = create_already_list()
    already_list_code = [a["code_s"] for a in already_list]

    # 今日のを分析
    log_print("本日:", today_data, "を分析")

    def create_today_list():
        today_list = []  # 本日更新銘柄データ(dict)のリスト
        if today_data.endswith(".csv"):
            today_list = search_fromcsv(today_data)
            # 出来高急増も追加
            today_list_d = search_fromcsv_dekidakaup(today_data_d)
            log_print(
                "新高値銘柄%d個と出来高急増銘柄%d個を合成"
                % (len(today_list), len(today_list_d))
            )
            compose_list(today_list, today_list_d)
            # today_listには本日銘柄: 本日の新高値と出来高銘柄リスト
            log_print("本日銘柄%s個" % len(today_list))
        return today_list

    today_list = create_today_list()
    today_list_code = [t["code_s"] for t in today_list]

    # 決算発表/修正の銘柄を追加
    kessan_lst_code = get_todays_kessan_list()
    log_print("決算更新追加:", len(kessan_lst_code), "個")
    log_print(kessan_lst_code)
    # 更新を行う必要のあるすべての銘柄
    updatelist_all_code = already_list_code + today_list_code + kessan_lst_code
    updatelist_all_code = list(set(updatelist_all_code))  # 重複削除
    # TODO: ETFや投資法人を除外する
    # ETFや投資法人のコードリストを取得して除外
    etf_codes = set(stock_db.load_etf_codes())
    updatelist_all_code = [
        code for code in updatelist_all_code if code not in etf_codes
    ]

    # ---- マスターデータ更新
    # 最低購入代金取得のためdbを更新
    log_print("---> masterデータ更新(最低購入代金、銘柄概要取得のため)")
    stocks = stock_db.update_db_rows(
        updatelist_all_code, tables=["master"], upd=upd, sync=False
    )
    log_print("<---- masterデータ更新完了")

    def filter_today_list(lst):
        """候補に対してを出来高上昇率、ETFなどで
        調査不要なものを省く
        """

        # print "候補数:", len(lst)
        def filter_dekidakaup(item):
            if "dekidaka_upratio" not in item:
                return True
            return float(item["dekidaka_upratio"].replace(",", "")) >= 300

        lst = [item for item in lst if filter_dekidakaup(item)]  # 出来高上昇率300%以上

        def filter_themes(item):
            stock = stocks.get(item["code_s"])
            if not stock:
                return False
            themes = stock.get("themes", "")
            return themes != ""

        # print "候補数2:", len(lst)
        lst = [item for item in lst if filter_themes(item)]

        def filter_etf_closure():
            """ETFコードリストをクロージャでキャッシュ"""
            etf_codes = stock_db.load_etf_codes()

            def filter_etf(item):  # noqa: E306
                code_s = item["code_s"]
                return code_s not in etf_codes

            return filter_etf

        filter_etf = filter_etf_closure()
        # log_print("候補数3:", len(lst))
        lst = [item for item in lst if filter_etf(item)]
        # log_print("候補数4:", len(lst))
        return lst

    # 調査不要なものを除く(stocksが必要なのでここで)
    today_list = filter_today_list(today_list)
    already_list = filter_today_list(already_list)
    already_list_code = [a["code_s"] for a in already_list]
    today_list_code = [a["code_s"] for a in today_list]
    # 未調査フィルタ
    today_only_list = [c for c in today_list if not c["code_s"] in already_list_code]
    today_already_list = [t for t in today_list if t not in today_only_list]
    already_only_list = [t for t in already_list if not t["code_s"] in today_list_code]

    log_print(
        "本日銘柄%d [未調査 %d 調査済み%d] | 過去銘柄%d"
        % (
            len(today_list),
            len(today_only_list),
            len(today_already_list),
            len(already_only_list),
        )
    )

    # today_list_candiate = today_list
    def update_price_gyoseki_shihyo(today_list_all):
        """今日と過去も考慮した銘柄today_list_allに対して
        DB更新(価格、業績、指標、理論株価)
        """
        # ---- 価格データ更新
        log_print("")
        log_print("----> 価格データ更新")
        stocks = stock_db.update_db_rows(
            today_list_all, tables=["price"], upd=upd, sync=False
        )
        log_print("<---- 価格データ更新完了")

        # ------------------------------
        # 業績の更新
        # ------------------------------
        # 業績取得のためdbを更新
        log_print("=" * 20)
        log_print("業績を更新します", upd)
        log_print("=" * 20)
        # lates:Trueで業績や指標を更新
        # print "業績更新:", today_list_all
        stocks = stock_db.update_db_rows(
            today_list_all, tables=["gyoseki"], upd=upd, sync=False
        )

        # ---- 指標の更新
        log_print("=" * 20)
        log_print("指標を更新します")
        log_print("=" * 20)
        # dryscrapeでのreuter情報は封印していることに注意
        # print "%　指標更新:", code_list
        stocks = stock_db.update_db_rows(
            today_list_all, tables=["shihyo"], upd=upd, sync=False
        )

        # ---- 理論株価の更新
        log_print("=" * 20)
        log_print("理論株価を更新します")
        log_print("=" * 20)
        stocks = stock_db.update_db_rows(
            today_list_all, tables=["rironkabuka"], upd=upd, sync=False
        )

        return stocks

    # 価格更新: これコメントアウトすれば速くなる
    stocks = update_price_gyoseki_shihyo(updatelist_all_code)

    # ---- マーケットの更新
    def update_market():
        make_market_db.update_market_db()
        past_code = [t["code_s"] for t in already_only_list]
        today_code = [t["code_s"] for t in today_list]
        shintakane_theme_csv = make_market_db.update_shintakane_theme_csv(
            stocks, today_code, past_code
        )
        make_market_db.create_market_csv(None, shintakane_theme_csv)

    update_market()  # マーケットDB更新、アップロード

    # ---- ここから結果表示
    # コードのみリストと　コード、表、出来高詳細、最低購入代金
    def puts(x):
        log_print(x, end=" ")

    COLUMNS = [
        "コード",
        "銘柄名",
        "種類",
        "市場",
        "セクター",
        # "詳細セクター",
        "株価",
        "前日比(%)",
        "出来高",
        "最低購入代金",
        "時価総額",
        "ローソク足ボラティリティ(20, 5)",
        "売り圧力レシオ(20, 5)",
        "シグナル",
        "タグ",
        "決算",
        "業績スコア",
        "指標",
        "RS",
        "ファンダ",
        "テーマ",
        "概要",
    ]
    TO_CSV = True
    rows = []
    if TO_CSV:
        rows.append(COLUMNS)
    else:
        [puts(c) for c in COLUMNS]

    def puts_detail(d):
        # 銘柄の情報表示
        kabuka = d["kabuka"]
        code = stock_db.get_code_exp(d["code_s"])
        stock = stocks[d["code_s"]]
        stock_name = stock_db.get_stock_name_exp(stock)
        sector = stock.get("sector", "セクター名不明")  # d["sector"]
        market = stock.get("market", "市場不明")
        # sector_detail = stock["sector_detail"]
        market_cap = int(stock["market_cap"])
        try:
            vola = ",".join(
                [str(round(v, 0)) for v in stock.get("stddev_volatility", [])]
            )
        except TypeError:
            log_warning("volaが取得できない")
            vola = 0
        sprs = stock.get("sell_pressure_ratio", [])
        sprs_w = stock.get("sell_pressure_ratio_w", [])
        import price

        sell_press = price.get_spr_expr(sprs, sprs_w)
        # pocket_pivot = stock.get("pocket_pivot", "")
        signal, tags = stock_db.make_signal(stock)
        tags = "/".join(tags)
        kessanbi = kessan.get_kessanbi_expr(stock)
        score_gyoseki = stock.get("score_gyoseki", 0)
        shihyo_pt = stock.get("shihyo_pt", 0)
        relates_rank = stock.get("relates_rank", 0)
        mom_pt = stock.get("momentum_pt", 0) + 0.1 * relates_rank
        # momentum = stock[4] + 0.1 * relates_rank
        funda_pt = stock.get("funda_pt", 0)
        overview = stock.get("overview", "")
        purchase_money = int(stock.get("lowest_purchase_money", 0)) / 10000
        origin = ""
        if d["origin"].find("shintakane") >= 0:
            origin += "新"
        if d["origin"].find("dekidakaup") >= 0:
            origin += "出"
        major_theme = make_market_db.get_major_theme(stock.get("themes", ""))
        if TO_CSV:
            row = [
                code,
                stock_name,
                origin,
                market,
                sector,
                # sector_detail,
                kabuka,
                d["zenjitsuhi_per"],
                d["dekidaga"],
                purchase_money,
                market_cap,
                vola,
                sell_press,
                signal,
                tags,
                kessanbi,
                score_gyoseki,
                shihyo_pt,
                mom_pt,
                funda_pt,
                major_theme,
                overview,
            ]
            rows.append(row)
        else:
            try:
                log_print(
                    "%s %s [%s] %s %s(%s) %d(%s) %d | %d万 %d億"
                    % (
                        code,
                        stock_name,
                        origin,
                        market,
                        sector,
                        # sector_detail,
                        kabuka,
                        d["zenjitsuhi_per"],
                        d["dekidaga"],
                        purchase_money,
                        market_cap,
                    )
                )
                log_print(
                    "    %.2f %d | %d %d %d"
                    % (vola, sell_press, score_gyoseki, shihyo_pt, mom_pt)
                )
                log_print("    %s" % overview)
            except TypeError as e:
                log_print("表示エラー", e)

    log_print()

    def score_func(t):
        code_s = t["code_s"]
        stock = stocks[code_s]
        score_gyoseki = stock.get("score_gyoseki", 0)
        score_shihyo = stock.get("shihyo_pt", 0)
        score_mom = stock.get("momentum_pt", 0)
        return score_gyoseki * 0.5 + score_shihyo * 0.2 + score_mom * 0.3

    if TO_CSV:
        rows.append(["【本日銘柄】"])
    else:
        log_print("【本日銘柄】")
    # 今日のものを出力
    today_list_show = sorted(today_only_list, key=score_func, reverse=True)
    [puts_detail(d) for d in today_list_show]

    log_print()
    if TO_CSV:
        rows.append(["【本日既出銘柄】"])
    else:
        log_print("【本日既出銘柄】")
    today_already_list_sort = sorted(today_already_list, key=score_func, reverse=True)
    [puts_detail(d) for d in today_already_list_sort]
    rows.append(["【過去銘柄】"])
    already_only_list_sort = sorted(already_only_list, key=score_func, reverse=True)
    [puts_detail(d) for d in already_only_list_sort]

    # CSV書き込み
    if TO_CSV:
        shintakane_result_csv = os.path.join(
            DATA_DIR,
            "shintakane_result_data/shintakane_result_%02d%02d%02d.csv"
            % (latest_csv_dt.year % 2000, latest_csv_dt.month, latest_csv_dt.day),
        )
        with open(shintakane_result_csv, "w", encoding="utf-8") as f:
            shintakane_result_csv_w = csv.writer(f)
            shintakane_result_csv_w.writerows(rows)

        shutil.copy2(
            shintakane_result_csv,
            os.path.join(DATA_DIR, "shintakane_result_data/shintakane_result.csv"),
        )

    # マーケット情報を表示
    # TODO: yahooUSのhtml形式が変わったようなので対応するまで封印
    # print "マーケット", "-"*10
    # import analyze_market
    # analyze_market.analyze_market()


def convert_kabutan_dekidakaup_html(html):
    rows = []
    body = re.search(
        r'<table class="stock_table st_market">(.*?)</table>', html, re.S
    ).group(0)
    # print body
    rank = 1
    for m in re.finditer(
        r'<td class="tac">(.*?)</td>.*?<th scope="row" class="tal">(.*?)</th>.*?<td class="tac">(.*?)</td>.*?<td>(.*?)</td>.*?<td>(.*?)</td>.*?<td>(.*?)</td>.*?<td>(.*?)</td>.*?<td>(.*?)</td>',
        body,
        re.S,
    ):
        # .*?<td class="w61">(.*?)</td.*?<td class="w50">(.*?)</td>
        # TODO: 英数字コード対応
        # code = re.search(r'\d\d\d\d', m.group(1)).group(0)
        code = re.search(r"\d[0-9a-zA-Z]\d[0-9A-Z]", m.group(1)).group(0)
        stock_name = m.group(2)
        market_name = m.group(3)
        kabuka = m.group(4)
        dekidaka = m.group(7)
        try:
            zenjitsuhi = re.search(r'<span class="up">(.*)</span>', m.group(6)).group(1)
            dekidaka_up = re.search(r'<span class="up">(.*)</span>', m.group(8)).group(
                1
            )
        except AttributeError:
            zenjitsuhi = 0
            dekidaka_up = 0
        # print code, stock_name, market_name, kabuka, zenjitsuhi, dekidaka, dekidaka_up

        row = []
        row.append(str(rank))
        row.append(code + " " + stock_name)
        row.append(market_name)
        row.append("セクター")
        row.append(kabuka)
        row.append(zenjitsuhi)
        row.append("0")  # 前日比
        row.append(dekidaka)  # "出来高"
        row.append("0")  # 平均出来高
        row.append(dekidaka_up)  # 出来高前日比
        rank += 1
        rows.append(row)
    return rows


def convert_dekidakaup_html(html):
    # 0:No、1:銘柄名、2:市場、3:業種、4:価格、5:前日比、6:前日比％、
    # 7:出来高、8:平均出来高、9:出来高倍率
    # print ux_cmd_head(html)
    rows = []
    for m in re.finditer(
        r'<td class="">(.*)</td>\r\n'
        r'<td class="tLeft "><a href=".*?" target="_chart">(.*)</a></td>\r\n'
        r'<td class="tLeft ">(.*)</td>\r\n'
        r'<td class="tRight " >(.*)</td>\r\n'
        r'<td class="tRight " >(.*)</td>\r\n'
        r'<td class="tRight " >(.*)</td>\r\n'
        r"\r\n\r\n\r\n\r\n"
        r'<td class="tRight ">(.*)</td>\r\n'
        r"\r\n\r\n"
        r'<td class="tRight rkgSelected01">(.*)</td>\r\n',
        html,
    ):
        # print "-"*15
        # print m.groups()
        row = []
        row.append(m.group(1))  # No
        # stock_name = re.search(r'>(.*)<', m.group(2)).group(1)
        stock_name = m.group(2)
        row.append(stock_name)  # コード、銘柄名
        # print stock_name
        market = m.group(3).split("<br />")[0]
        sector = m.group(3).split("<br />")[1]
        row.append(market)  # 市場
        row.append(sector)  # セクター
        # print market, sector
        price = re.search(r"((\d|,)+)<", m.group(4)).group(1)
        row.append(price)  # 価格
        # print price
        m2 = re.search(r".*?((\d|,|\+|-)+)<br>(.*)(</span>)?", m.group(5))
        zenjitsuhi = m2.group(1)
        zenjitsuhi_per = m2.group(3).replace("</span>", "")
        row.append(zenjitsuhi)
        row.append(zenjitsuhi_per)
        # print zenjitsuhi, zenjitsuhi_per
        volume = m.group(6)  # .replace('"', '')
        # print volume
        row.append(volume)  # 出来高
        average_volume = m.group(7)
        row.append(average_volume)  # 平均出来高
        # print average_volume
        volume_upratio = m.group(8)  # 出来高増加率
        row.append(volume_upratio)
        # print volume_upratio
        rows.append(row)
        # print row
    return rows


def convert_kabutan_shintakane_html(html):
    """
    株探の新高値htmlを解析してリストとして取得
    """
    rows = []
    body = re.search(
        r'<table class="stock_table st_market">(.*?)</table>', html, re.S
    ).group(0)
    # print body
    rank = 1
    for m in re.finditer(
        r'<td class="tac">(.*?)</td>.*?<th scope="row" class="tal">(.*?)</th>.*?<td class="tac">(.*?)</td>.*?<td>(.*?)</td>.*?<td>(.*?)</td>.*?<td class="w61">(.*?)</td.*?<td class="w50">(.*?)</td>',
        body,
        re.S,
    ):
        # 英数字コード対応
        # code = re.search(r'\d\d\d\d', m.group(1)).group(0)
        code = re.search(r"\d[0-9a-zA-Z]\d[0-9A-Z]", m.group(1)).group(0)
        stock_name = m.group(2)
        market_name = m.group(3)
        kabuka = m.group(4)
        try:
            zenjitsuhi = re.search(r'<span class="up">(.*)</span>', m.group(6)).group(1)
            zenjitsuhi_per = (
                re.search(r'<span class="up">(.*)</span>', m.group(7)).group(1) + "%"
            )
        except AttributeError:
            zenjitsuhi = 0
            zenjitsuhi_per = 0
        # print code, stock_name, market_name, kabuka, zenjitsuhi, zenjitsuhi_per
        row = []
        row.append(str(rank))
        row.append(code + " " + stock_name)
        row.append(market_name)
        row.append("セクター")
        row.append(kabuka)
        row.append(zenjitsuhi)
        row.append(zenjitsuhi_per)
        row.append("0")  # "出来高"
        rank += 1
        rows.append(row)
    return rows


def convert_shintakane_html(html):
    """
    ケンミレの新高値htmlを解析してリストとして取得
    """
    # print ux_cmd_head(html)
    rows = []
    for m in re.finditer(
        r'<td class="">(.*)</td>\r\n'
        r'<td class="tLeft rkgSelected01">(.*)</td>\r\n'
        r'<td class="tLeft ">(.*)</td>\r\n'
        r'<td class="tRight " >(.*)</td>\r\n'
        r'<td class="tRight " >(.*)</td>\r\n'
        r'<td class="tRight " >(.*)</td>\r\n',
        html,
    ):
        # print "-"*15
        # print m.groups()
        row = []
        row.append(m.group(1))
        stock_name = re.search(r">(.*)<", m.group(2)).group(1)
        row.append(stock_name)
        market = m.group(3).split("<br />")[0]
        sector = m.group(3).split("<br />")[1]
        row.append(market)
        row.append(sector)
        price = re.search(r"((\d|, )+)<", m.group(4)).group(1)
        # print stock_name, market, sector, price
        row.append(price)
        m2 = re.search(r".*?((\d|,|\+|-)+)<br>(.*)(</span>)?", m.group(5))
        zenjitsuhi = m2.group(1)
        zenjitsuhi_per = m2.group(3).replace("</span>", "")
        row.append(zenjitsuhi)
        row.append(zenjitsuhi_per)
        volume = m.group(6)  # .replace('"', '')
        # print zenjitsuhi, zenjitsuhi_per, volume
        row.append(volume)
        rows.append(row)
        # print row
    return rows


def get_todays_dekidakaup():
    """本日の出来高増銘柄を株探からスクレイピングして
    csv2保存
    """
    log_print("=" * 30)
    log_print("出来高急増銘柄を更新します・・")
    latest_csv, _ = get_latest_dekidakaup_fname()
    # Force = True
    if latest_csv:
        latest_csv_dt = get_file_datetime(latest_csv)
        tdy = datetime.today()
        tdy = get_price_day(tdy)
        # if tdy.hour < 17:
        # 	tdy = tdy-timedelta(days=1)
        goodissue_dt = datetime(tdy.year, tdy.month, tdy.day, PRICE_HOUR)
        if latest_csv_dt > goodissue_dt:
            log_print(
                "本日分のcsvは取得済みです",
                latest_csv,
                latest_csv_dt,
                "goodissue",
                goodissue_dt,
            )
            return

    log_print("----> 株探から出来高急増情報を取得します・・")
    URL_KABUTAN_DEKIDAKA = "https://kabutan.jp/tansaku/"
    QUERY = "?mode=2_0311&market=0&capitalization=-1&stc=v3&stm=1&page=%d"
    cache_dir = os.path.join(DATA_DIR, "cache_data")
    path_dekidaka = os.path.join(
        cache_dir, get_http_cachname(URL_KABUTAN_DEKIDAKA + QUERY % 1)
    )
    htmls = []
    try:
        cach_dt = get_file_datetime(path_dekidaka)
        latest_html = file_read(path_dekidaka)
        # 更新日付を取得
        latest_date_m = re.search(
            r'<div class="meigara_count">.*(\d\d\d\d)年(\d\d)月(\d\d)日.*?</div>',
            latest_html,
            re.S,
        )  # re.S:改行を含む
        cach_dt = datetime(
            int(latest_date_m.group(1)),
            int(latest_date_m.group(2)),
            int(latest_date_m.group(3)),
        )
        useCache = cach_dt.date() >= datetime.today().date()
        # TODO: ↑土日も取得してしまう
        log_print("株探 出来高急増キャシュ：", cach_dt, useCache)
    except (IOError, OSError) as e:
        log_warning("出来高急増ファイルがない", e)
        useCache = False
    html = http_get_html(
        URL_KABUTAN_DEKIDAKA + QUERY % 1, use_cache=useCache, cache_dir=cache_dir
    )
    htmls.append(html)
    # 更新日付
    date_m = re.search(
        r'<div class="meigara_count">.*(\d\d\d\d)年(\d\d)月(\d\d)日.*?</div>',
        html,
        re.S,
    )
    date = date_m.group(1) + date_m.group(2) + date_m.group(3)
    date = date[2:]
    log_print("株探 出来高急増更新日：", date)
    # ページ分のhtmlを取得
    # ページ数を取得
    page_div = re.search(r'<div class="pagination">(.*?)</div>', html, re.S).group(0)
    pages = [int(m.group(1)) for m in re.finditer(r"page=(\d)", page_div)]
    page_count = max(pages)
    log_print("ページ数：", page_count)
    with use_requests_session():
        for p in range(page_count):
            if p < 1:
                continue  # 1ページ目は取得済み
            html = http_get_html(
                URL_KABUTAN_DEKIDAKA + QUERY % (p + 1),
                use_cache=useCache,
                cache_dir=cache_dir,
            )
            htmls.append(html)

    rows = []
    for html in htmls:
        rows += convert_kabutan_dekidakaup_html(html)

    # 新高値情報リストを.csvファイルに保存
    csv_fname = os.path.join(DATA_DIR, "shintakane_data/dekidakaup_" + date + ".csv")
    csv_w = csv.writer(
        open(csv_fname, "w", encoding="utf-8")
    )  # python3ではwbではなく、テキストモードで読み書き
    csv_w.writerows(rows)
    log_print("今日の出来高急増を%sに保存しました" % csv_fname)
    log_print("<---- 取得完了")


def get_todays_shintakane():
    """本日の新高値情報を株探から取得し、
    csvファイルに保存する
    """
    log_print("=" * 30)
    log_print("新高値銘柄を更新します・・")
    latest_csv, _ = get_latest_shintakane_fname()
    if latest_csv:
        latest_csv_dt = get_file_datetime(latest_csv)
        tdy = datetime.today()
        if tdy.hour < 17:
            tdy = tdy - timedelta(days=1)
        goodissue_dt = datetime(tdy.year, tdy.month, tdy.day, 17)
        if latest_csv_dt > goodissue_dt:
            log_print(
                "本日分のcsvは取得済みです",
                latest_csv,
                latest_csv_dt,
                "goodissue",
                goodissue_dt,
            )
            return

    log_print("----> 株探から新高値情報を取得します・・")
    URL_KABUTAN_SHINTAKANE = "https://kabutan.jp/warning/"
    # QUERY = "?mode=3_3&market=0&capitalization=-1&stc=&stm=0&page=%d"
    QUERY = "record_w52_high_price?market=0&capitalization=-1" "&stc=&stm=0&page=%d"
    # 最新キャッシュ取得日を取得
    cache_dir = os.path.join(DATA_DIR, "cache_data")
    try:
        latest_html = file_read(
            os.path.join(
                cache_dir, get_http_cachname(URL_KABUTAN_SHINTAKANE + QUERY % 1)
            )
        )
        latest_date_m = re.search(
            r'<div class="meigara_count">.*(\d\d\d\d)年(\d\d)月(\d\d)日.*?</div>',
            latest_html,
            re.S,
        )  # re.S:改行を含む
        cach_dt = datetime(
            int(latest_date_m.group(1)),
            int(latest_date_m.group(2)),
            int(latest_date_m.group(3)),
        )
        useCache = cach_dt.date() >= datetime.today().date()
        log_print("株探新高値 キャシュ：", cach_dt, useCache)
    except IOError:
        useCache = False
    # 最初のページ
    htmls = []
    html = http_get_html(
        URL_KABUTAN_SHINTAKANE + QUERY % 1, use_cache=useCache, cache_dir=cache_dir
    )
    htmls.append(html)
    # 更新日付
    date_m = re.search(
        r'<div class="meigara_count">.*(\d\d\d\d)年(\d\d)月(\d\d)日.*?</div>',
        html,
        re.S,
    )  # re.S:改行を含む
    date = date_m.group(1)[-2:] + date_m.group(2) + date_m.group(3)
    log_print("株探新高値更新日：", date)
    # ページ分のhtmlを取得
    # ページ数を取得
    try:
        page_div = re.search(r'<div class="pagination">(.*?)</div>', html, re.S).group(
            0
        )
        pages = [int(m.group(1)) for m in re.finditer(r"page=(\d)", page_div)]
        page_count = max(pages)
    except AttributeError:
        log_print("ページ情報がhtmlにないため1とする")
        page_count = 1
    for p in range(page_count):
        if p < 1:
            continue  # 1ページ目は取得済み
        html = http_get_html(
            URL_KABUTAN_SHINTAKANE + QUERY % (p + 1),
            use_cache=useCache,
            cache_dir=cache_dir,
        )
        htmls.append(html)
    # 新高値htmlからデータを保持
    rows = []
    for html in htmls:
        rows += convert_kabutan_shintakane_html(html)

    # 新高値情報リストを.csvファイルに保存
    csv_fname = os.path.join(DATA_DIR, "shintakane_data", "shintakane_" + date + ".csv")
    csv_w = csv.writer(
        open(csv_fname, "w", encoding="utf-8")
    )  # python3ではwbではなく、テキストモードで読み書き
    csv_w.writerows(rows)
    log_print("今日の新高値を%sに保存しました" % csv_fname)

    log_print("<---- 取得完了")


# def wait_connect():
#     """
#     laucnhdのための接続
#     """
#     import datetime
#     import time

#     first_time = datetime.datetime.now()
#     diff = (datetime.datetime.now() - first_time).seconds
#     while diff <= 30:
#         try:
#             url = "https://www.google.co.jp/"
#             import requests

#             requests.get(url)
#             log_print("接続確立")
#             return True
#         except requests.exceptions.ConnectionError as e:
#             log_warning("接続失敗", diff, "秒")
#             log_print(e)
#             time.sleep(5)
#         diff = (datetime.datetime.now() - first_time).seconds
#     return False


def parse_kessan_html(html):
    """決算ページのhtmlを解析して、決算修正と決算発表の
    リストを返す
    決算修正と決算発表のhtmlは同じフォーマットなので、
    wordを変えることで両方取得できる"""

    def re_search_kessan(word, body_html):
        kessan_list = []
        # 24.3 フォーマット変更対応
        re_expr = (
            r'<td class="news_time"><time datetime="(.*?)T.*?">.*?</time></td>.*?<td><div class=".*?%s.*?" data-code="(.*?)">.*?</div></td>.*?<td><a href="(.*?)">(.*?)</a></td>'
            % word
        )
        for m in re.finditer(
            re_expr, body_html, re.S
        ):  # .decode('utf-8'): python3ではhtmlはbytes型なのでdecodeする?
            date = m.group(1).replace("-", "/")
            # datetime.strptime(date, "%Y/%m/%d")
            code_s = m.group(2)
            link = m.group(3)
            summary = m.group(4)
            kessan_list.append((code_s, date, link, summary))
        if not kessan_list:
            log_warning("決算ページフォーマット変更?")
        return kessan_list

    # htmlから、決算情報が含まれば箇所を取得(正規表現にコストかかるため高速化)
    body_html = re.search(
        r'<table class="s_news_list mgbt0">(.*?)</table>', html, re.S
    ).group(1)
    mod_lst = re_search_kessan("ctg3_ks", body_html)
    log_print("決算修正:", [item[:2] for item in mod_lst])

    announce_lst = re_search_kessan("ctg3_kk", body_html)
    log_print("決算発表:", [item[:2] for item in announce_lst])
    return mod_lst, announce_lst


# ------------------------------
# 決算関係
# ------------------------------
def get_todays_kessan_list(positive=False):
    cache_csv_path = os.path.join(DATA_DIR, "todays_kessan_data", "todays_kessan.csv")
    csv_r = csv.reader(
        open(cache_csv_path, "r", encoding="utf-8")
    )  # python3ではrbではなくrで開く
    code_s_lst = []
    for row in csv_r:
        if kessan.is_positive_kessan(row[3]):
            code_s_lst.append(row[0])
    return code_s_lst


def update_todays_kessan():
    """決算速報URLを解析して銘柄コード更新リストにする"""
    modify_lst = []
    announce_lst = []
    cache_dir = os.path.join(DATA_DIR, "todays_kessan_data")
    cache_csv_path = os.path.join(cache_dir, "todays_kessan.csv")
    log_print("-" * 30)
    log_print("決算発表/修正に対するDB更新")
    log_print("-" * 30)
    # とりあえず、本日分までページを読むことにする
    page_max = 33  # 最大確認ページ数
    before_day = 2  # 本日からさかのぼって確認する日数
    with use_requests_session():
        for p in range(page_max):
            page = p + 1
            TODAYS_KESSAN_URL = "https://kabutan.jp/news/?page=%d" % page
            cache_fname = "todays_kessan_page_%d.html" % page
            cache_path = os.path.join(cache_dir, cache_fname)
            # 1ページの日付でキャッシュを使うか判定
            # if page == 1:
            use_cache = False
            if os.path.exists(cache_path):
                cache_date = get_price_day(get_file_datetime(cache_path))

                tdy_date = get_price_day(datetime.today())
                if os.path.exists(cache_path) and tdy_date <= cache_date:
                    use_cache = True
                log_print(
                    "%dページ決算発表キャッシュ日付:%s キャッシュ:%s"
                    % (page, cache_date, use_cache)
                )

            kessan_html = http_get_html(
                TODAYS_KESSAN_URL,
                use_cache=use_cache,
                cache_dir=cache_dir,
                cache_fname=cache_fname,
            )
            # print ux_cmd_head(kessan_html)
            # 決算htmlの日付を取得
            mod, announce = parse_kessan_html(kessan_html)
            # print mod, announce
            modify_lst += mod
            announce_lst += announce
            if len(modify_lst) > 0:
                current_day = datetime.strptime(modify_lst[-1][1], "%Y/%m/%d").date()
            else:
                current_day = datetime.strptime(announce_lst[-1][1], "%Y/%m/%d").date()
            today = datetime.today().date()
            kessan_ge_day = today - timedelta(before_day)
            log_print(
                "決算ページ:%d 今読んでいる決算日付:%s ここまで取得日:%s"
                % (page, current_day, kessan_ge_day)
            )
            if p > 0 and kessan_ge_day > current_day:
                break

    # CSVキャッシュに保存
    with open(
        cache_csv_path, "w", encoding="utf-8"
    ) as f:  # python3ではwbではなく、テキストモードで読み書き
        csv_w = csv.writer(f)
        kessan_lst = modify_lst + announce_lst
        csv_w.writerows(kessan_lst)
        log_print(cache_csv_path, "に保存しました")

    # ---- DBに反映
    stocks = stock_db.load_stock_db()
    # raise "hoge"
    for item in modify_lst:
        code_s = item[0]
        mod_date = item[1]
        try:
            stocks[code_s]["kessan_mod_date"] = mod_date
            stocks[code_s]["kessan_announce"] = "修正," + item[2] + "," + item[3]
            log_print(
                "決算修正:", code_s, stocks[code_s]["stock_name"], mod_date, item[3]
            )
        except KeyError:
            log_print(code_s, "はDBにありません")
    for item in announce_lst:
        code_s = item[0]
        announce_date = item[1]
        try:
            kessanbi = stocks[code_s].get("kessanbi")
            if kessanbi and kessanbi != announce_date:
                stocks[code_s]["kessanbi"] = announce_date
                log_print("決算発表日更新", code_s, announce_date)
            stocks[code_s]["kessan_announce"] = "発表," + item[2] + "," + item[3]
        except KeyError:
            log_print(code_s, "はDBにありません")
            continue
        log_print(
            "決算発表:",
            code_s,
            stocks[code_s].get("stock_name", "銘柄名不明"),
            announce_date,
            item[3],
        )
    stock_db.save_stock_db(stocks)
    # ついでにmake_market_dbで表示される監視銘柄の決算日付更新
    update_pf_kessan_db(stocks)


def update_pf_kessan_db(stocks):
    log_print("-" * 10, "監視銘柄決算データ更新", "-" * 10)
    import kessan

    kessan.save_pf_kessan_db(stocks)


def main():
    """メイン関数"""
    # raise NotImplementedError("main関数は実装されていません")
    # args = "update analyze"
    # args = "update"
    # args = "analyze"
    # args = "udpate_kessan_db"
    # TODO: 新高値更新タイミングをタグで
    # TODO: 業績発表日、反映のらぐのため一日余裕もたせる
    # TODO: 今Qが通期予想伸びより良いも考慮（進捗率的なもの進捗率を見たほうが正確ではある）
    args = "update analyze"
    args += " " + " ".join(sys.argv[1:])
    log_print("args:", args)
    # if "launchd" in args:
    #     if not wait_connect():  # 接続確立待ち
    #         raise "!!! ネット接続できませんでした"
    #     args = "update analyze"
    # 新高値銘柄一覧の最新情報を取得する
    if "update" in args:
        update_todays_kessan()  # テストコメントアウト
        get_todays_shintakane()
        get_todays_dekidakaup()
    # 新高値銘柄の各種解析
    if "analyze" in args:
        todays_shintakane(UPD_INTERVAL)  # UPD_FORCE/UPD_INTERVAL/UPD_CACHE/UPD_REEVAL
        # GoogleDriveにアップロード
        shintakane_result_csv = os.path.join(
            DATA_DIR, "shintakane_result_data/shintakane_result.csv"
        )
        import threading
        import googledrive

        threading.Thread(
            target=googledrive.upload_csv,
            args=(shintakane_result_csv, "shintakane_result"),
            daemon=False,
        ).start()
        # googledrive.upload_csv(shintakane_result_csv, "shintakane_result")
    # 現在の銘柄DBをもとに決算DBの更新
    if "udpate_kessan_db" in args:
        stocks = stock_db.load_stock_db()
        update_pf_kessan_db(stocks)


# TODO: やりたいが保留リスト
# 直近決算も更新候補にする
# https://kabutan.jp/warning/?mode=4_2&market=0&capitalization=-1&stc=&stm=0&page=1
# https://kabutan.jp/warning/?mode=4_3
# TODO: 週足で過去イチの出来高銘柄は、already_listから選んでタグ付けしたい
if __name__ == "__main__":
    # ロガーの初期化
    logger = setup_logger("shintakane")

    # カレントディレクトリをこの.pyの場所に
    with chdir(os.path.abspath(os.path.dirname(__file__))):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--quiet", action="store_true", help="標準出力(print)を抑制する"
        )
        args = parser.parse_args()
        log_print("=" * 30)
        log_print("shintakane.pyを実行します", args)
        log_print("=" * 30)
        try:
            if args.quiet:
                log_print("標準出力を抑制します")
                with suppress_stdout():
                    main()
                log_print("抑制終了")
            else:
                main()
        except Exception as e:
            log_print("エラー発生", e)
            logger.exception(
                "Unhandled exception occurred:\n%s", traceback.format_exc()
            )
            raise e
            # exit(1)
