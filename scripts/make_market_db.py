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
from db_shelve import get_market_db as _get_market_shelve_db


def parse_theme_html(html):
    # print ux_cmd_head(html, 10)
    # Example: <td class="acrank_url"><a href="/themes/?theme=...">テーマ名</a></td>
    if not html:
        log_warning("parse_theme_html: empty HTML")
        return []
    themes = []
    # Allow for attribute order/spacing and multi-line HTML
    pattern = re.compile(
        r'<td[^>]*class=["\']acrank_url["\'][^>]*>\s*<a[^>]*>(.*?)</a>\s*</td>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        themes.append(m.group(1).strip())
    return themes


def get_timedelta_today(fname):
    """
    fnameファイルの日付と今日の日付の日数を返す
    """
    # TODO: utilに移動
    if not os.path.exists(fname):
        log_print("%sはありません" % fname)
        return None, None
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
            name
            + "_%02d%02d%02d" % (cur_day.year - 2000, cur_day.month, cur_day.day)
            + ext
        )
        count += 1
        if os.path.exists(fname):
            break
    if count >= CountMax:
        log_warning("直前のファイルが見つかりません", fname)
        return "", cur_day
    log_print("直前のファイル:", Path(fname).relative_to(DATA_DIR))
    return fname, cur_day


def get_theme_rank_list():
    """
    テーマランクデータをDBから取得する
    Returns:
        現在のランクデータ、数日前のランクデータ、日付、数日前の日付
    """
    cach_path = os.path.join(DATA_DIR, "market_data", "theme_rank.html")

    delta, cach_date = get_timedelta_today(cach_path)
    if delta is None or cach_date is None:
        log_print("キャッシュファイルがないため取得します:", cach_path)
        use_cache = False
    else:
        use_cache = delta.days < THEME_RANK_INTERVAL

    html = http_get_html(
        URL_THEME_RANK_KABUTAN,
        cache_dir=os.path.join(DATA_DIR, "market_data"),
        cache_fname="theme_rank.html",
        use_cache=use_cache,
    )

    if not html:
        log_warning("テーマランクのHTML取得に失敗しました。cacheを確認します")
        # Try to read cache directly if available
        if os.path.exists(cach_path):
            html = file_read(cach_path)
        else:
            html = ""

    # HTTP取得でキャッシュが更新された場合、mtimeを再取得
    if not use_cache:
        _, cach_date = get_timedelta_today(cach_path)

    theme_rank_list = parse_theme_html(html)

    if cach_date:
        prev_cache, prev_day = get_prev_fname(cach_path, cach_date - timedelta(2))
    else:
        prev_cache, prev_day = "", datetime.today() - timedelta(2)

    # バックアップ処理を復元（コミット2d731f6で削除されていた処理）
    if cach_date and prev_cache:
        if (cach_date - prev_day).days >= INTERVAL_BACKUP:
            backup_file(cach_path, 0)

    prev_theme_rank_list = []
    if prev_cache and os.path.exists(prev_cache):
        prev_html = file_read(prev_cache)
        prev_theme_rank_list = parse_theme_html(prev_html)

    return theme_rank_list, prev_theme_rank_list, cach_date, prev_day


THEME_RANK_INTERVAL = 1  # 再取得までの日数
INTERVAL_BACKUP = 3  # バックアップ日数


def make_theme_data(prev_momentum_rank=None):
    """テーマランクデータを作成

    Args:
        prev_momentum_rank: 前日のモメンタム順位リスト（差分ラベル計算用）
    """
    log_print("テーマランクデータを作成します")
    theme_rank_list, prev_theme_rank_list, cach_date, _ = get_theme_rank_list()

    theme_rank_dict = {v: i + 1 for (i, v) in enumerate(theme_rank_list)}
    prev_theme_rank_dict = {v: i + 1 for (i, v) in enumerate(prev_theme_rank_list)}
    theme_rank2 = {}
    for theme, rank in list(theme_rank_dict.items()):
        moment = 0
        if theme in prev_theme_rank_dict:
            prev_rank = prev_theme_rank_dict[theme]
        else:
            prev_rank = 31
        moment = -(rank - prev_rank)
        log_print("  %s %d->%d" % (theme, prev_rank, rank))
        rank_pt = 31 - rank + moment
        theme_rank2[theme] = rank_pt
    theme_rank2_sorted = sorted(
        list(theme_rank2.items()), key=lambda x: x[1], reverse=True
    )
    theme_rank2_list = [theme for theme, pt in theme_rank2_sorted]
    log_print("モメンタム順位:", ",".join(theme_rank2_list))

    # 当日モメンタム順位 vs 前日モメンタム順位の差分を計算
    theme_rank_diff = {}
    if prev_momentum_rank:
        prev_momentum_dict = {v: i + 1 for (i, v) in enumerate(prev_momentum_rank)}
        cur_momentum_dict = {v: i + 1 for (i, v) in enumerate(theme_rank2_list)}
        for theme in theme_rank2_list:
            if theme not in prev_momentum_dict:
                theme_rank_diff[theme] = None  # 新規テーマ
            else:
                # 前日順位 - 当日順位: 正=上昇、負=下降
                theme_rank_diff[theme] = (
                    prev_momentum_dict[theme] - cur_momentum_dict[theme]
                )
    else:
        # 前日データがない場合は差分なし
        for theme in theme_rank2_list:
            theme_rank_diff[theme] = 0

    market_db = {}
    market_db["theme_rank"] = theme_rank2_list
    market_db["theme_rank_diff"] = theme_rank_diff
    market_db["access_date_theme_rank"] = cach_date
    return market_db


def get_major_theme(themes):
    """
    銘柄テーマから、主要3テーマを取得する
    @themes 銘柄のテーマ: stock_dbの'themes'キー
    """
    market_db = get_market_db()
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


def calc_theme_price_momentum(stocks):
    """テーマ別株価騰落率を計算する。
    DBの全銘柄から、直近取引日のprice_logがある銘柄の
    テーマごとの平均日次騰落率(%)と銘柄数を返す。

    Args:
        stocks (dict): 銘柄DB (code_s -> stock_data)
    Returns:
        dict: テーマ名 -> (平均騰落率(%), 銘柄数)
    """
    from collections import Counter, defaultdict

    # 直近取引日を特定（price_log[0]の日付の最頻値）
    dates = []
    for stock in stocks.values():
        price_log = stock.get("price_log", [])
        if len(price_log) >= 2:
            dates.append(price_log[0][0])
    if not dates:
        return {}
    latest_trade_date = Counter(dates).most_common(1)[0][0]
    log_print("テーマ騰落率: 直近取引日=%s, 対象候補=%d銘柄" % (latest_trade_date, len(dates)))

    # テーマ別に騰落率を集約
    theme_changes = defaultdict(list)
    for code_s, stock in stocks.items():
        price_log = stock.get("price_log", [])
        if len(price_log) < 2:
            continue
        # 直近取引日と一致する銘柄のみ対象
        if price_log[0][0] != latest_trade_date:
            continue
        today_price = price_log[0][1]
        prev_price = price_log[1][1]
        if prev_price <= 0:
            continue
        change_rate = (today_price - prev_price) / prev_price * 100

        themes_str = stock.get("themes", "")
        if not themes_str:
            continue
        for theme in themes_str.split(","):
            theme = theme.strip()
            if theme:
                theme_changes[theme].append(change_rate)

    # テーマごとの平均と銘柄数を計算
    theme_momentum = {}
    for theme, changes in theme_changes.items():
        if changes:
            avg = sum(changes) / len(changes)
            theme_momentum[theme] = (avg, len(changes))

    log_print("テーマ騰落率: %d テーマを集計" % len(theme_momentum))
    return theme_momentum


def make_db_common(code_s):
    """DBデータ更新共通処理
    type: str -> dict<db>
    """
    db = {}
    priced_dict = price.get_daily_price_kabutan(code_s)
    db.update(priced_dict)
    pr = priced_dict.get("price", 0)
    pricew_dict = price.get_weekly_price_data(code_s, UPD_INTERVAL, [pr, pr, pr])
    log_print("RS_RAW=", pricew_dict.get("rs_raw", 0))
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


_market_db_cache = None


def get_market_db():
    """マーケットDBを取得（dictとして返す、キャッシュあり）"""
    global _market_db_cache
    if _market_db_cache is not None:
        return _market_db_cache
    with _get_market_shelve_db() as db:
        _market_db_cache = db.export_to_dict()
    return _market_db_cache


def _save_market_db(market_db):
    """マーケットDBを保存"""
    global _market_db_cache
    _market_db_cache = None  # キャッシュ無効化
    with _get_market_shelve_db() as db:
        db.import_from_dict(market_db)


def update_market_db():
    """マーケットDBを読み込んで最新に更新"""
    market_db = get_market_db()

    # 前日のモメンタム順位をDBに保存（日付が変わった場合のみ退避）
    prev_date = market_db.get("access_date_theme_rank")
    cur_theme_rank = market_db.get("theme_rank", [])
    if prev_date and cur_theme_rank:
        # prev_dateがdatetime.date型の場合はdatetimeに変換（get_price_dayはdatetime必須）
        if not isinstance(prev_date, datetime):
            prev_date = datetime.combine(prev_date, datetime.min.time())
        prev_day = get_price_day(prev_date)
        today = get_price_day(datetime.today())
        if prev_day != today:
            # 日付が変わった → 現在のtheme_rankを前日データとして退避
            market_db["prev_theme_rank"] = list(cur_theme_rank)
            log_print("前日モメンタム順位を退避: %s" % prev_day)
    prev_momentum_rank = market_db.get("prev_theme_rank", [])
    theme_db = make_theme_data(prev_momentum_rank)
    market_db.update(theme_db)

    topix_db = make_topix_db()
    market_db.update(topix_db)

    mothers_db = make_mothers_db()
    market_db.update(mothers_db)
    nikkei_db = make_nikkei_db()
    market_db.update(nikkei_db)
    nasdaq_db = make_nasdaq_db()
    market_db.update(nasdaq_db)

    _save_market_db(market_db)
    log_print("MarketDB保存:", list(market_db.keys()))
    return market_db


def _theme_rank_label(theme, diff):
    """テーマ名にKabutan生ランキングの順位変動インジケーターを付加する

    Args:
        theme: テーマ名
        diff: 順位差分（正=上昇、負=下降、None=新規）
    """
    if diff is None:
        return "%s(NEW)" % theme
    if diff > 0:
        return "%s(↑%d)" % (theme, diff)
    elif diff < 0:
        return "%s(↓%d)" % (theme, -diff)
    else:
        return "%s(←)" % theme


def create_market_csv(market_db=None, shintakane_theme_csv=None):
    """市場DBから表示用CSVデータにする"""
    if shintakane_theme_csv is None:
        shintakane_theme_csv = []
    if not market_db:
        market_db = get_market_db()
    csv_path = os.path.join(DATA_DIR, "code_rank_data", "market_data.csv")

    theme_rank_list, prev_theme_rank_list, _, prev_day = get_theme_rank_list()
    rows = []
    rows.append(["■ テーマランク"])
    row = ["ランク"]
    theme_rank_diff = market_db.get("theme_rank_diff", {})
    for theme in market_db["theme_rank"]:
        diff = theme_rank_diff.get(theme)
        row.append(_theme_rank_label(theme, diff))
    rows.append(row)
    # テーマ別騰落率行
    theme_momentum = market_db.get("theme_momentum", {})
    if theme_momentum:
        row = ["騰落率"]
        for theme in market_db["theme_rank"]:
            if theme in theme_momentum:
                avg_rate, count = theme_momentum[theme]
                row.append("%+.1f%%[%d]" % (avg_rate, count))
            else:
                row.append("-")
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
            distribution_days = ",".join([s[3:] for s in db["distribution_days"]])
            followthrough_days = ",".join([s[3:] for s in db["followthrough_days"]])
            diff = db["spr_buygagher"] - db["spr_20"]
            eval = step_func(diff, [-10, -5, 0, 5, 10], ["E", "D", "C", "B", "A"])
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
            log_warning("市場のDBデータ取得できず", db_name, market_name)
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
        target=googledrive.upload_csv, args=(csv_path, "market_data"), daemon=False
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
        log_print(theme, count)
    return themes_count_sorted


def update_shintakane_theme_csv(stocks, today_list, past_list):
    log_print("新高値テーマの取得")
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



def main():
    # ロガーの初期化
    logger = setup_logger("make_stock_db")

    market_db = update_market_db()  # noqa: F841
    create_market_csv()



if __name__ == "__main__":
    setup_logger("make_market_db")
    main()
