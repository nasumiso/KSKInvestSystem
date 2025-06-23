#!/usr/bin/env python3

# =================================================
# セクターDBを作成します。
# =================================================

import re
from datetime import datetime, timedelta
import csv
import os

import price
import googledrive
import make_stock_db

from ks_util import *


URL_THEME_RANK_KABUTAN = "https://kabutan.jp/info/accessranking/3_2"
MARKET_DB_PATH = os.path.join(DATA_DIR, "market_data/market_db.pickle")


def parse_theme_html(html):
    # print ux_cmd_head(html, 10)
    # <td class="acrank_url"><a href="/themes/?theme=デジタルトランスフォーメーション">デジタルトランスフォーメーション</a></td> # noqa: E501
    themes = []
    for m in re.finditer(r'<td class="acrank_url"><a href=".*?">(.*?)</a></td>', html):  # noqa: E501
        themes.append(m.group(1))
    return themes


def get_timedelta_today(fname):
    """
    fnameファイルの日付と今日の日付の日数を返す
    """
    # TODO: utilに移動
    if not os.path.exists(fname):
        print("%sはありません" % fname)
        return None
    stat = os.stat(fname)
    fdate = datetime.fromtimestamp(stat.st_mtime)
    today = datetime.today()
    return get_price_day(today) - get_price_day(fdate), fdate


def get_prev_fname(fname, cur_day=datetime.today()):
    """
    fnameの日付より古い日付のファイルを返す
    """
    count = 0
    CountMax = 30
    name, ext = os.path.splitext(fname)
    while count < CountMax:
        cur_day = cur_day - timedelta(1)
        fname = (
            name + "_%02d%02d%02d"
            % (cur_day.year - 2000, cur_day.month, cur_day.day) + ext
        )
        count += 1
        if os.path.exists(fname):
            break
    if count >= CountMax:
        print("!!!直前のファイルが見つかりません", fname)
        return "", cur_day
    print("直前のファイル:", Path(fname).relative_to(DATA_DIR))
    return fname, cur_day


def get_theme_rank_list():
    """
    テーマランクデータをDBから取得する
    Returns:
        現在のランクデータ、数日前のランクデータ、日付、数日前の日付
    """
    cach_path = os.path.join(DATA_DIR, "market_data", "theme_rank.html")

    delta, cach_date = get_timedelta_today(cach_path)
    use_cache = delta.days < THEME_RANK_INTERVAL
    html = http_get_html(
        URL_THEME_RANK_KABUTAN,
        cache_dir=os.path.join(DATA_DIR, "market_data"),
        cache_fname="theme_rank.html",
        use_cache=use_cache,
    )
    theme_rank_list = parse_theme_html(html)
    prev_cache, prev_day = get_prev_fname(cach_path, cach_date - timedelta(2))
    if (cach_date - prev_day).days >= INTERVAL_BACKUP:
        backup_file(cach_path, 0)
    prev_html = file_read(prev_cache)
    prev_theme_rank_list = parse_theme_html(prev_html)

    return theme_rank_list, prev_theme_rank_list, cach_date, prev_day


THEME_RANK_INTERVAL = 1  # 再取得までの日数
INTERVAL_BACKUP = 3  # バックアップ日数


def make_theme_data():  # market_db=None
    """テーマランクデータを作成"""
    print("テーマランクデータを作成します")
    theme_rank_list, prev_theme_rank_list, cach_date, _ = get_theme_rank_list()

    theme_rank_dict = {v: i + 1 for (i, v) in enumerate(theme_rank_list)}
    prev_theme_rank_dict = {
        v: i + 1 for (i, v) in enumerate(prev_theme_rank_list)
    }
    theme_rank2 = {}
    for theme, rank in list(theme_rank_dict.items()):
        moment = 0
        if theme in prev_theme_rank_dict:
            prev_rank = prev_theme_rank_dict[theme]
        else:
            prev_rank = 31
        moment = -(rank - prev_rank)
        print("  %s %d->%d" % (theme, prev_rank, rank))
        rank_pt = 31 - rank + moment
        theme_rank2[theme] = rank_pt
    theme_rank2_sorted = sorted(
        list(theme_rank2.items()), key=lambda x: x[1], reverse=True
    )
    theme_rank2_list = [theme for theme, pt in theme_rank2_sorted]
    print("モメンタム順位:", ",".join(theme_rank2_list))
    market_db = {}
    market_db["theme_rank"] = theme_rank2_list
    market_db["access_date_theme_rank"] = cach_date
    return market_db


def get_major_theme(themes):
    """
    銘柄テーマから、主要3テーマを取得する
    @themes 銘柄のテーマ: stock_dbの'themes'キー
    """
    market_db = memoized_load_pickle(MARKET_DB_PATH)
    theme_rank = market_db["theme_rank"]  # 現在のランキング
    theme_rank_dict = {v: i + 1 for (i, v) in enumerate(theme_rank)}
    if not themes:
        return ""
    themes = themes.split(",")  # リスト化
    themes_dict = {theme: theme_rank_dict.get(theme, 31) for theme in themes}
    themes_sorted = sorted(list(themes_dict.items()), key=lambda x: x[1])
    major_themes_rank = [theme for theme, rank in themes_sorted]
    major_themes_rank = major_themes_rank[:3]
    return ",".join(major_themes_rank)


def make_db_common(code_s):
    """DBデータ更新共通処理
    type: str -> dict<db>
    """
    db = {}
    priced_dict = price.get_daily_price_kabutan(code_s)
    db.update(priced_dict)
    pr = priced_dict.get("price", 0)
    pricew_dict = price.get_weekly_price_data(
        code_s, UPD_INTERVAL, [pr, pr, pr]
    )
    print("RS_RAW=", pricew_dict.get("rs_raw", 0))
    db.update(pricew_dict)
    return db


def make_topix_db():
    code_s = "0010"  # 株探ではTOPIXが0010
    db_dict = make_db_common(code_s)
    db = {}
    db["topix"] = db_dict
    return db


def make_mothers_db():
    code_s = "0012"
    db_dict = make_db_common(code_s)
    db = {}
    db["mothers"] = db_dict
    return db


def make_nikkei_db():
    code_s = "0000"
    db_dict = make_db_common(code_s)
    db = {}
    db["nikkei225"] = db_dict
    return db


def make_dow_db():
    code_s = "0800"
    db_dict = make_db_common(code_s)
    db = {}
    db["dow"] = db_dict
    return db


def make_nasdaq_db():
    code_s = "0802"
    db_dict = make_db_common(code_s)
    db = {}
    db["nasdaq"] = db_dict
    return db


def get_market_db():
    market_db = memoized_load_pickle(MARKET_DB_PATH)
    return market_db


def update_market_db():
    """マーケットDBを読み込んで最新に更新"""
    market_db = load_pickle(MARKET_DB_PATH)

    theme_db = make_theme_data()
    market_db.update(theme_db)

    topix_db = make_topix_db()
    market_db.update(topix_db)

    mothers_db = make_mothers_db()
    market_db.update(mothers_db)
    nikkei_db = make_nikkei_db()
    market_db.update(nikkei_db)
    nasdaq_db = make_nasdaq_db()
    market_db.update(nasdaq_db)

    save_pickle(MARKET_DB_PATH, market_db)
    print("MarketDB保存:", list(market_db.keys()))
    return market_db


def create_market_csv(market_db=None, shintakane_theme_csv=None):
    """市場DBから表示用CSVデータにする"""
    if shintakane_theme_csv is None:
        shintakane_theme_csv = []
    if not market_db:
        market_db = load_pickle(MARKET_DB_PATH)
    csv_path = os.path.join(DATA_DIR, "code_rank_data", "market_data.csv")

    theme_rank_list, prev_theme_rank_list, _, prev_day = get_theme_rank_list()
    rows = []
    rows.append(["■ テーマランク"])
    row = ["ランク"]
    row.extend(market_db["theme_rank"])
    rows.append(row)
    time = market_db["access_date_theme_rank"].date()
    row = [str(time)]
    row.extend(theme_rank_list)
    rows.append(row)
    prev_time = prev_day.date()
    row = [prev_time]
    row.extend(prev_theme_rank_list)
    rows.append(row)
    for row in shintakane_theme_csv:
        rows.append(row)

    def get_db_row(db_name, market_name):
        try:
            db = market_db[db_name]
            trend_expr = make_stock_db.get_trend_template_expr(db)
            distribution_days = ",".join(
                [s[3:] for s in db["distribution_days"]]
            )
            followthrough_days = ",".join(
                [s[3:] for s in db["followthrough_days"]]
            )
            diff = db["spr_buygagher"] - db["spr_20"]
            eval = step_func(
                diff,
                [-10, -5, 0, 5, 10],
                ["E", "D", "C", "B", "A"]
            )
            rows = []
            rows.append(
                [
                    market_name,
                    db["rs_raw"],
                    trend_expr,
                    distribution_days,
                    followthrough_days,
                    db["direction_signal"],
                    "%d,%d,%s" % (db["spr_20"], db["spr_5"], eval),
                    "%.1f,%.1f" % (db["rv_20"], db["rv_5"]),
                ]
            )
            return rows
        except KeyError:
            print("!!! 市場のDBデータ取得できず", db_name, market_name)
            return []

    rows.append([])
    rows.append(["■市場"])
    rows.append(
        [
            "市場名",
            "RS",
            "トレンド",
            "ディストリビューション",
            "フォロースルー",
            "シグナル",
            "売り圧力レシオ(20,5)",
            "ローソク足ボラティリティ(20,5)",
        ]
    )
    rows.extend(get_db_row("topix", "TOPIX"))
    rows.extend(get_db_row("mothers", "マザーズ指数"))
    rows.extend(get_db_row("nikkei225", "日経225"))
    rows.extend(get_db_row("nasdaq", "NASDAQ"))
    rows.append([])
    rows.append(["■決算日"])
    import kessan

    kessan_csv = kessan.make_kessan_csv()
    rows.extend(kessan_csv)
    rows.append([])
    rows.append(["■適宜開示"])
    import disclosure

    disc_csv = disclosure.update_disclosure_all()
    rows.extend(disc_csv)

    with open(csv_path, "w", encoding="utf-8") as f:
        csv_w = csv.writer(f)
        csv_w.writerows(rows)

    # GoogleDriveに非同期アップロード
    import threading

    threading.Thread(
        target=googledrive.upload_csv, args=(csv_path, "market_data"),
        daemon=False
    ).start()
    # googledrive.upload_csv(csv_path, "market_data")


def update_shintakane_theme(stocks, code_list):
    themes_count = {}
    for code_s in code_list:
        if code_s not in stocks:
            continue
        stock = stocks[code_s]
        themes = stock.get("themes", [])
        for theme in themes.split(","):
            if not theme:
                continue
            if theme not in themes_count:
                themes_count[theme] = 0
            themes_count[theme] += 1
    themes_count_sorted = sorted(
        list(themes_count.items()), key=lambda x: x[1], reverse=True
    )
    for theme, count in themes_count_sorted[:30]:
        print(theme, count)
    return themes_count_sorted


def update_shintakane_theme_csv(stocks, today_list, past_list):
    print("新高値テーマの取得")
    today_counts = update_shintakane_theme(stocks, today_list)
    past_counts = update_shintakane_theme(stocks, past_list)
    csv = []
    today = ["当日"]
    today.extend(["%s(%d)" % (t[0], t[1]) for t in today_counts[:30]])
    csv.append(today)
    today = ["過去"]
    today.extend(["%s(%d)" % (t[0], t[1]) for t in past_counts[:30]])
    csv.append(today)
    return csv


def convert_python2():
    """Python2のpickleをPython3のpickleに変換
    一時的実行用
    """
    import make_stock_db

    STOCKS_PICKLE_PY2 = os.path.join(
        DATA_DIR, "market_data", "market_db_py2.pickle"
    )
    make_stock_db.convert_pickle_latin1_to_utf8(
        STOCKS_PICKLE_PY2, MARKET_DB_PATH
    )


def main():
    market_db = update_market_db()  # noqa: F841
    create_market_csv()


def test():
    convert_python2()


if __name__ == "__main__":
    main()
    # test()
