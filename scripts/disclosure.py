#!/usr/bin/env python3

from datetime import datetime, timedelta
import re

import portfolio
from ks_util import *

DISCLOSURE_DIR = os.path.join(DATA_DIR, "disclosure")
DISCLOSURE_CACHE_DIR = os.path.join(DISCLOSURE_DIR, "cache")
DISCLOSURE_URL = "https://kabutan.jp/stock/news?code=%s"
DISCLOSURE_CSV = os.path.join(DISCLOSURE_DIR, "disclosure_db.csv")

UPD_INTERVAL = 0
UPD_CACHE = 1  # html取得できていればキャッシュから
UPD_FORCE = 2  # html取得から強制

HEAD_TYPE_DIC = {
    "ctg5": "special",  # 特集
    "ctg3_kk": "modify",  # 修正下方
    "ctg3_ks": "modify",  # 修正上方
    "ctg12": "5per",  # 5%
    "ctg9": "kessan",
}  # 決算

HEAD_TYPE_EXPR = {
    "kaiji": "開示",
    "zairyo": "材料",
    "modify": "修正",
    "5per": "5パー",
    "kessan": "決算",
    "special": "特集",
}


def need_update_disclosure(code_s):
    """キャッシュとその日時から更新必要を判断"""
    code_url = DISCLOSURE_URL % (code_s)
    html_path = os.path.join(DISCLOSURE_CACHE_DIR, get_http_cachname(code_url))
    if not os.path.exists(html_path):
        return True
    # キャッシュの日時判断
    stat = os.stat(html_path)
    fdate = datetime.fromtimestamp(stat.st_mtime)
    today = datetime.today()
    if (get_price_day(today) - get_price_day(fdate)).days >= 1:
        return True
    else:
        return False


def parse_disclosure_html(html):
    """適宜開示htmlをパースして専用形式で保存
    Args:
        html(str): htmlテキスト本体
    Returns:
        list<dict>: 適宜開示1レコードのリスト
    """
    # プレミアム以外のものを拾えばみたせるためそれらを披露
    # 適宜開示
    record_list = []
    # 自己完結のためコードを取得
    # <title>ユークス【4334】｜ニュース｜株探（かぶたん）</title>
    m = re.search(r"<title>(.*)【(\d[0-9a-zA-Z]\d[0-9A-Z])】.*</title>", html)
    code_s = ""
    stock_name = ""
    if m:
        code_s = m.group(2)
        stock_name = m.group(1)
    if not code_s:
        log_warning("コードを取得できません（株探フォーマット変更？）")
        return {}
    try:
        for m in re.finditer(
            r'<td class="td_kaiji"><a href="(.*)" target=".*">(.*)<img', html
        ):
            url = m.group(1)
            heading = m.group(2)
            date = url.split("/")[-3][0:8]  # 20220603
            head_type = "kaiji"
            # print head_type, url, heading
            record = {}
            record["type"] = head_type
            # record["code"] = code
            set_db_code(record, code_s)
            record["stock_name"] = stock_name
            record["date"] = date
            record["url"] = url
            record["heading"] = heading
            record_list.append(record)
        # それ以外
        # ctg9:決算 ctg2:材料
        for m in re.finditer(
            r'<td class="(.*?)"></td>\s+?<td><a href="(.*?)">(.*?)</a></td>', html
        ):  # |re.DOTALL: .が複数行にマッチする re.MULTILINE: ^$が行頭行末
            if not "nmode=0" in m.group(2):  # これは月へのリンクのため除く
                tag = m.group(1)
                url = m.group(2)
                heading = m.group(3)
                head_type = HEAD_TYPE_DIC.get(tag, "zairyo")
                # print tag, head_type, url, heading
                m3 = re.search(r"b=[n|k](\d*)", url)
                date = m3.group(1)[:8]  # 20220603
                record = {}
                record["type"] = head_type
                # record["code"] = code
                set_db_code(record, code_s)
                record["stock_name"] = stock_name
                record["date"] = date
                record["url"] = "https://kabutan.jp/" + url
                record["heading"] = heading
                record_list.append(record)
    except AttributeError:
        log_warning(" 適宜開示htmlパース失敗: 株探フォーマット変更？")
    log_print("%sの適宜開示データ%d個追加" % (code_s, len(record_list)))
    return record_list


def update_disclosure(code_s, disc_db=[], upd=UPD_INTERVAL):
    use_cache = True
    if upd == UPD_CACHE:
        use_cache = True
    elif upd == UPD_INTERVAL:
        use_cache = not need_update_disclosure(code_s)
    elif upd == UPD_FORCE:
        use_cache = False
    # html取得
    code_url = DISCLOSURE_URL % (code_s)
    html = http_get_html(code_url, use_cache=use_cache, cache_dir=DISCLOSURE_CACHE_DIR)
    up_recs = parse_disclosure_html(html)
    # 更新
    disc_db += up_recs



def expoert_to_csv(disc_db, csv_path=None):
    # まず日付順にソート
    def disc_cmp(a, b):
        pt_a = int(a["date"])
        prior_type = ["kaiji", "modify", "special", "5per", "kessan"]
        if a["type"] in prior_type:
            pt_a += 100000000
        pt_b = int(b["date"])
        if b["type"] in prior_type:
            pt_b += 100000000
        return (pt_a > pt_b) - (pt_a < pt_b)  # cmpの代替

    import functools  # python3対応

    disc_db = sorted(disc_db, key=functools.cmp_to_key(disc_cmp), reverse=True)

    rows = []
    # rows.append(["■適宜開示"])
    rows.append(["日付", "銘柄コード", "銘柄名", "種類", "本文"])

    def make_link(heading, url):
        # =HYPERLINK("https://kabutan.jp/stock/chart?code=6070","6070")
        return '=HYPERLINK("%s","%s")' % (url, heading)

    def type_expr(type):
        return HEAD_TYPE_EXPR.get(type, "")

    def code_expr(code):
        code_s = str(code)
        KABUTAN_URL = "https://kabutan.jp/stock/chart?code=%s"
        return '=HYPERLINK("%s","%s")' % (KABUTAN_URL % code_s, code_s)

    for rec in disc_db:
        link = make_link(rec["heading"], rec["url"])
        rows.append(
            [
                rec["date"],
                code_expr(get_db_code(rec)),
                rec["stock_name"],
                type_expr(rec["type"]),
                link,
            ]
        )
    # 材料の切れ目に空行を入れジャンプしやすくする
    insert_ind = -1
    for ind, row in enumerate(rows):
        if row[3] == type_expr("zairyo"):
            insert_ind = ind
            break
    if insert_ind >= 0:
        rows.insert(insert_ind, [""])

    import csv

    output_path = csv_path if csv_path else DISCLOSURE_CSV
    with open(output_path, "w", encoding="utf-8") as f:  # python3対応(wbから)
        csv_w = csv.writer(f)
        csv_w.writerows(rows)

    return rows


def update_disclosure_all(upd=UPD_INTERVAL):
    # disc_db = load_pickle(DISCLOSURE_DB)
    # if not disc_db:
    #    disc_db = []
    disc_db = []
    code_list_s, possess_list_s = portfolio.parse_my_portforio()
    with use_requests_session():  # 中でhttp_get_htmlを使うためセッションを指定
        for code_s in code_list_s + possess_list_s:
            update_disclosure(code_s, disc_db, upd)
    # 更新した内容で保存
    # save_pickle(DISCLOSURE_DB, disc_db)
    return expoert_to_csv(disc_db)


def filter_recent_news(record_list, days=3):
    """ニュースレコードリストから直近N日以内のものだけを返す

    Args:
        record_list: parse_disclosure_html()の返り値（list<dict>）
        days: 何日以内のニュースを残すか
    Returns:
        list<dict>: フィルタされたレコードリスト
    """
    if not record_list:
        return []
    today_date = get_price_day(datetime.today())
    cutoff = today_date - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y%m%d")
    return [r for r in record_list if r.get("date", "") >= cutoff_str]


def update_disclosure_for_today(code_s_list, days=3):
    """「今日の銘柄」のニュースを収集し、CSVに出力する

    Args:
        code_s_list: 銘柄コード(文字列)のリスト
        days: 直近何日以内のニュースを対象とするか
    """
    disc_db = []
    with use_requests_session():
        for code_s in code_s_list:
            update_disclosure(code_s, disc_db)
    # 直近N日以内にフィルタ
    disc_db = filter_recent_news(disc_db, days=days)
    # 専用CSVに出力（既存のdisclosure_db.csvは上書きしない）
    todays_csv = os.path.join(DATA_DIR, "disclosure", "todays_disclosure.csv")
    expoert_to_csv(disc_db, csv_path=todays_csv)
    log_print("本日の銘柄ニュース%d件を%sに保存しました" % (len(disc_db), todays_csv))


def main():
    # ロガーの初期化
    logger = setup_logger('shintakane')

    # TODO: 特集(神戸物産)、5%(スノーピーク)、修正(アドベンチャー)、決算(メディアドゥ)は
    # 開示のところにしたい
    upd = UPD_INTERVAL  # UPD_INTERVAL,UPD_CACHE
    update_disclosure_all(upd)
    # 3678,7816,3038
    # update_disclosure(3038, upd=upd)


if __name__ == "__main__":
    setup_logger("disclosure")
    main()
