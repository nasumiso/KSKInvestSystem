#!/usr/bin/env python3

# =================================================
# セクターDBを作成します。
# =================================================

import re
import html as html_mod
from datetime import datetime, timedelta
import csv
import os

import price
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


def _migrate_theme_rank_files():
    """旧パス(market_data/)のtheme_rankファイルを新パス(market_data/theme_rank/)に移動

    初回のみ実行: theme_rank/ディレクトリが未作成で旧パスにファイルがある場合。
    """
    old_dir = os.path.join(DATA_DIR, "market_data")
    new_dir = os.path.join(DATA_DIR, "market_data", "theme_rank")
    old_cache = os.path.join(old_dir, "theme_rank.html")
    if not os.path.exists(old_cache) or os.path.isdir(new_dir):
        return
    os.makedirs(new_dir, exist_ok=True)
    log_print("theme_rankファイルを新ディレクトリに移動:", new_dir)
    shutil.move(old_cache, os.path.join(new_dir, "theme_rank.html"))
    for fname in os.listdir(old_dir):
        if re.match(r"theme_rank_\d{6}\.html$", fname):
            shutil.move(os.path.join(old_dir, fname), os.path.join(new_dir, fname))


def _archive_old_theme_rank(theme_rank_dir, days=30):
    """30日以前のtheme_rank_YYMMDD.htmlをhistory/に移動"""
    history_dir = os.path.join(theme_rank_dir, "history")
    today = datetime.today()
    for fname in os.listdir(theme_rank_dir):
        if not re.match(r"theme_rank_\d{6}\.html$", fname):
            continue
        fpath = os.path.join(theme_rank_dir, fname)
        mtime = datetime.fromtimestamp(os.stat(fpath).st_mtime)
        if (today - mtime).days > days:
            os.makedirs(history_dir, exist_ok=True)
            shutil.move(fpath, os.path.join(history_dir, fname))


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
    # 旧パスからの移行（初回のみ）
    _migrate_theme_rank_files()

    theme_rank_dir = os.path.join(DATA_DIR, "market_data", "theme_rank")
    os.makedirs(theme_rank_dir, exist_ok=True)
    cach_path = os.path.join(theme_rank_dir, "theme_rank.html")

    delta, cach_date = get_timedelta_today(cach_path)
    if delta is None or cach_date is None:
        log_print("キャッシュファイルがないため取得します:", cach_path)
        use_cache = False
    else:
        use_cache = delta.days < THEME_RANK_INTERVAL

    html = http_get_html(
        URL_THEME_RANK_KABUTAN,
        cache_dir=theme_rank_dir,
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

    # 30日以前のバックアップをhistory/に移動
    _archive_old_theme_rank(theme_rank_dir)

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
    pricew_dict = price.get_weekly_price_data(code_s, upd=UPD_INTERVAL, prices=[pr, pr, pr])
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


def _make_us_index_db(code_s, ticker_symbol, key):
    """米国指数 (NASDAQ / S&P500 等) を yfinance 経由で取得する共通処理"""
    db_dict = {}
    daily_dict = price.get_us_index_daily(code_s, ticker_symbol)
    db_dict.update(daily_dict)
    pr = daily_dict.get("price", 0)
    weekly_dict = price.get_us_index_weekly(
        code_s, ticker_symbol, prices=[pr, pr, pr]
    )
    log_print("RS_RAW=", weekly_dict.get("rs_raw", 0))
    db_dict.update(weekly_dict)
    return {key: db_dict}


def make_nasdaq_db():
    """NASDAQ総合指数を yfinance (^IXIC) 経由で取得する。
    旧 Kabutan 0802 経由はフォーマット変更で動作しなくなったため切替済み。"""
    return _make_us_index_db("_IXIC", "^IXIC", "nasdaq")


def make_sp500_db():
    """S&P500を yfinance (^GSPC) 経由で取得する"""
    return _make_us_index_db("_GSPC", "^GSPC", "sp500")


_market_db_cache = None
_market_db_lock = threading.Lock()


def get_market_db():
    """マーケットDBを取得（dictとして返す、キャッシュあり・スレッドセーフ）"""
    global _market_db_cache
    if _market_db_cache is not None:
        return _market_db_cache
    with _market_db_lock:
        # ダブルチェックロッキング: ロック取得中に他スレッドがキャッシュ済みの場合
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

    # 前日のモメンタム順位をDBに保存（カレンダー日が変わった場合のみ退避）
    # ※ access_date_theme_rankはキャッシュファイルのmtimeであり実行日ではないため、
    #   休日（キャッシュ未更新）に複数回実行すると毎回退避が走り
    #   prev_theme_rankが当日データで上書きされる問題があった。
    #   退避実行日を別途記録し、同日再実行時は退避をスキップする。
    last_save_date = market_db.get("prev_theme_rank_save_date")
    cur_theme_rank = market_db.get("theme_rank", [])
    today_cal = datetime.today().date()
    if cur_theme_rank and last_save_date != today_cal:
        # 本日まだ退避していない → 現在のtheme_rankを前日データとして退避
        market_db["prev_theme_rank"] = list(cur_theme_rank)
        market_db["prev_theme_rank_save_date"] = today_cal
        log_print("前日モメンタム順位を退避: %s" % today_cal)
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
    sp500_db = make_sp500_db()
    market_db.update(sp500_db)

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

    # --- CSV版（コメントアウト: HTML版に置き換え） ---
    # rows = []
    # rows.append(["■ テーマランク"])
    # row = ["ランク"]
    # theme_rank_diff = market_db.get("theme_rank_diff", {})
    # for theme in market_db["theme_rank"]:
    #     diff = theme_rank_diff.get(theme)
    #     row.append(_theme_rank_label(theme, diff))
    # rows.append(row)
    # theme_momentum = market_db.get("theme_momentum", {})
    # if theme_momentum:
    #     row = ["騰落率"]
    #     for theme in market_db["theme_rank"]:
    #         if theme in theme_momentum:
    #             avg_rate, count = theme_momentum[theme]
    #             row.append("%+.1f%%[%d]" % (avg_rate, count))
    #         else:
    #             row.append("-")
    #     rows.append(row)
    # time = market_db["access_date_theme_rank"].date()
    # row = [str(time)]
    # row.extend(theme_rank_list)
    # rows.append(row)
    # prev_time = prev_day.date()
    # row = [prev_time]
    # row.extend(prev_theme_rank_list)
    # rows.append(row)
    # for row in shintakane_theme_csv:
    #     rows.append(row)

    # def get_db_row(db_name, market_name):
    #     ...（省略）

    # rows.append([])
    # rows.append(["■市場"])
    # ...（省略）
    # --- CSV版ここまで ---

    # 決算・開示データの取得（HTML版でも同じデータを使う）
    import kessan
    kessan_csv = kessan.make_kessan_csv()

    import disclosure
    disc_csv = disclosure.update_disclosure_all()

    # HTML版を生成（市場データ + 適宜開示）。
    # webapp が DATA_DIR からローカル参照するため、GoogleDrive アップロードは行わない。
    theme_rank_data = (theme_rank_list, prev_theme_rank_list, None, prev_day)
    create_market_html(market_db,
                       kessan_csv=kessan_csv,
                       theme_rank_data=theme_rank_data)
    create_disclosure_html(disc_csv)


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
    # HTML版移行に伴い廃止（新高値テーマ分布は不要になった）
    # log_print("新高値テーマの取得")
    # today_counts = update_shintakane_theme(stocks, today_list)
    # past_counts = update_shintakane_theme(stocks, past_list)
    # csv = []
    # today = ["当日"]
    # today.extend(["%s(%d)" % (t[0], t[1]) for t in today_counts[:30]])
    # csv.append(today)
    # today = ["過去"]
    # today.extend(["%s(%d)" % (t[0], t[1]) for t in past_counts[:30]])
    # csv.append(today)
    # return csv
    return []



# ==================================================
# HTML出力用の定数とヘルパー関数
# ==================================================

_HTML_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: "Hiragino Sans", "Yu Gothic", "Meiryo", sans-serif;
  max-width: 1400px; margin: 0 auto; padding: 20px;
  background: #fafafa; color: #333; line-height: 1.5;
}
h1 { font-size: 1.4em; border-bottom: 2px solid #333; padding-bottom: 8px; margin-bottom: 20px; }
h1 .date { color: #666; font-weight: normal; }
h2 {
  font-size: 1.1em; background: #34495e; color: #fff;
  padding: 8px 14px; margin: 28px 0 12px 0; border-radius: 4px;
}
h3 { font-size: 0.95em; color: #555; margin: 16px 0 8px 0; border-bottom: 1px solid #ddd; padding-bottom: 4px; }

/* テーブル共通 */
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px 0; font-size: 0.88em; }
th, td { border: 1px solid #ddd; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #ecf0f1; font-weight: bold; white-space: nowrap; }
tr:hover { background: #f5f5f5; }

/* テーマランク */
.theme-grid { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 16px 0; }
.theme-badge {
  display: inline-flex; flex-direction: column; align-items: center;
  border: 1px solid #ddd; border-radius: 6px; padding: 6px 10px;
  background: #fff; min-width: 110px; font-size: 0.85em;
}
.theme-badge .rank { font-size: 0.75em; color: #333; }
.theme-badge .name { font-weight: bold; text-align: center; color: #333; }
.theme-badge .change { font-size: 0.8em; margin-top: 2px; color: #333; }
.theme-badge .rate { font-size: 0.8em; margin-top: 2px; }
.theme-badge .change-up { color: #c0392b; }
.theme-badge .change-down { color: #2980b9; }
.rate-pos { color: #c0392b; }
.rate-neg { color: #2980b9; }
.rate-bold { font-weight: bold; }
.theme-heat-2 { background: #ef5350; border-color: #e53935; }
.theme-heat-1 { background: #ef9a9a; border-color: #e57373; }
.theme-heat0  { background: #fff; }
.theme-heat1  { background: #a5d6a7; border-color: #81c784; }
.theme-heat2  { background: #66bb6a; border-color: #4caf50; }

/* 市場テーブル */
.market-table th { background: #2c3e50; color: #fff; }
.signal-sell { background: #fdedec; color: #c0392b; font-weight: bold; }
.signal-buy { background: #eafaf1; color: #27ae60; font-weight: bold; }
.trend-good { color: #27ae60; font-weight: bold; }

/* 決算 */
.kessan-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.kessan-card {
  border: 1px solid #ddd; border-radius: 6px; padding: 10px;
  background: #fff; font-size: 0.85em;
}
.kessan-card.future { border-left: 3px solid #3498db; }
.kessan-card.past { opacity: 0.6; }
.kessan-card .card-date { font-weight: bold; color: #2c3e50; margin-bottom: 6px; font-size: 0.95em; }
.kessan-4q { font-weight: bold; }
.kessan-card .stock-list { list-style: none; padding: 0; }
.kessan-card .stock-list li { padding: 2px 0; border-bottom: 1px dotted #eee; }
.kessan-card .stock-list li:last-child { border-bottom: none; }

/* 適宜開示 */
details { margin: 8px 0; }
details summary {
  cursor: pointer; font-weight: bold; padding: 8px 12px;
  background: #ecf0f1; border-radius: 4px; font-size: 0.95em;
}
details summary:hover { background: #d5dbdb; }
.disc-table { font-size: 0.83em; }
.disc-table td:first-child { white-space: nowrap; color: #666; }
.disc-table a { color: #2980b9; text-decoration: none; }
.disc-table a:hover { text-decoration: underline; }
.disc-row-gyoseki { background: #e8f5e9; }

/* ランキング履歴 */
.rank-history { font-size: 0.83em; overflow-x: auto; }
.rank-history table { min-width: 800px; }
.rank-history td { white-space: nowrap; }
.rank-history td:first-child { font-weight: bold; background: #f8f9fa; }
"""


def _html_theme_rank(market_db, theme_rank_data=None):
    """テーマランクセクションのHTMLを生成する

    Args:
        market_db: 市場DB
        theme_rank_data: (theme_rank_list, prev_theme_rank_list, cach_date, prev_day)
    Returns:
        str: テーマランクセクションのHTML文字列
    """
    theme_rank = market_db.get("theme_rank", [])
    theme_rank_diff = market_db.get("theme_rank_diff", {})
    theme_momentum = market_db.get("theme_momentum", {})

    if not theme_rank:
        return ""

    parts = []
    parts.append('<h2>テーマランク</h2>\n<div class="theme-grid">')

    for i, theme in enumerate(theme_rank):
        diff = theme_rank_diff.get(theme)
        # 順位変動テキストとCSSクラスの決定
        if diff is None:
            change_class = ""
            change_text = "NEW"
        elif diff > 0:
            change_class = "change-up"
            change_text = "↑%d" % diff
        elif diff < 0:
            change_class = "change-down"
            change_text = "↓%d" % (-diff)
        else:
            change_class = ""
            change_text = "→"

        # 順位変動に応じたヒートマップクラス
        if diff is None:
            heat_class = "theme-heat0"
        elif diff >= 8:
            heat_class = "theme-heat2"
        elif diff >= 4:
            heat_class = "theme-heat1"
        elif diff <= -8:
            heat_class = "theme-heat-2"
        elif diff <= -4:
            heat_class = "theme-heat-1"
        else:
            heat_class = "theme-heat0"

        # 騰落率
        rate_html = ""
        if theme in theme_momentum:
            avg_rate, count = theme_momentum[theme]
            rate_class = "rate-pos" if avg_rate >= 0 else "rate-neg"
            if abs(avg_rate) >= 3:
                rate_class += " rate-bold"
            rate_html = '<span class="rate %s">%+.1f%% <small>[%d]</small></span>' % (
                rate_class, avg_rate, count
            )

        theme_escaped = html_mod.escape(theme)
        parts.append(
            '  <div class="theme-badge %s">\n'
            '    <span class="rank">#%d</span>\n'
            '    <span class="name">%s</span>\n'
            '    <span class="change %s">%s</span>\n'
            '    %s\n'
            '  </div>' % (heat_class, i + 1, theme_escaped, change_class, change_text, rate_html)
        )

    parts.append('</div>')

    # Kabutanランキング履歴
    if theme_rank_data:
        theme_rank_list, prev_theme_rank_list, _, prev_day = theme_rank_data
        if theme_rank_list or prev_theme_rank_list:
            parts.append('<h3>Kabutanランキング履歴</h3>')
            parts.append('<div class="rank-history"><table>')
            access_date = market_db.get("access_date_theme_rank")
            def _rank_row(date_str, rank_list):
                """ランキング行を生成（10位まで表示 + 残りは折りたたみ）"""
                cells = '<td>%s</td>' % date_str
                for t in rank_list[:10]:
                    cells += '<td>%s</td>' % html_mod.escape(t)
                if len(rank_list) > 10:
                    cells += '<td style="color:#999; cursor:pointer" '
                    cells += 'onclick="this.parentElement.nextElementSibling.hidden=!this.parentElement.nextElementSibling.hidden"'
                    cells += '>▶ ...</td>'
                row = '<tr>%s</tr>' % cells
                if len(rank_list) > 10:
                    extra_cells = '<td></td>'
                    for t in rank_list[10:30]:
                        extra_cells += '<td>%s</td>' % html_mod.escape(t)
                    row += '<tr hidden>%s</tr>' % extra_cells
                return row

            if access_date and theme_rank_list:
                date_str = access_date.strftime("%Y-%m-%d")
                parts.append(_rank_row(date_str, theme_rank_list))
            if prev_day and prev_theme_rank_list:
                prev_date_str = prev_day.strftime("%Y-%m-%d") if hasattr(prev_day, 'strftime') else str(prev_day)
                parts.append(_rank_row(prev_date_str, prev_theme_rank_list))
            parts.append('</table></div>')

    return '\n'.join(parts)


def _html_market(market_db):
    """市場指標セクションのHTMLを生成する

    Args:
        market_db: 市場DB
    Returns:
        str: 市場セクションのHTML文字列
    """
    markets = [
        ("topix", "TOPIX"),
        ("mothers", "マザーズ指数"),
        ("nikkei225", "日経225"),
        ("nasdaq", "NASDAQ"),
        ("sp500", "S&P 500"),
    ]

    rows_html = []
    for db_name, market_name in markets:
        if db_name not in market_db:
            continue
        try:
            db = market_db[db_name]
            trend_expr = make_stock_db.get_trend_template_expr(db)
            distribution_days = ", ".join([s[3:] for s in db.get("distribution_days", [])])
            followthrough_days = ", ".join([s[3:] for s in db.get("followthrough_days", [])])
            signal = db.get("direction_signal", "")
            diff = db.get("spr_buygagher", 0) - db.get("spr_20", 0)
            spr_eval = step_func(diff, [-10, -5, 0, 5, 10], ["E", "D", "C", "B", "A"])

            # シグナルのCSSクラス
            signal_class = ""
            if "sell" in str(signal).lower():
                signal_class = ' class="signal-sell"'
            elif "buy" in str(signal).lower():
                signal_class = ' class="signal-buy"'

            # トレンドのCSSクラス
            trend_class = ""
            if trend_expr.startswith("◯") or trend_expr.startswith("◎"):
                trend_class = ' class="trend-good"'

            rows_html.append(
                '<tr>\n'
                '  <td><strong>%s</strong></td>\n'
                '  <td>%s</td>\n'
                '  <td%s>%s</td>\n'
                '  <td>%s</td>\n'
                '  <td>%s</td>\n'
                '  <td%s>%s</td>\n'
                '  <td>%d, %d, <strong>%s</strong></td>\n'
                '  <td>%.1f, %.1f</td>\n'
                '</tr>' % (
                    html_mod.escape(market_name),
                    db.get("rs_raw", ""),
                    trend_class, html_mod.escape(str(trend_expr)),
                    html_mod.escape(distribution_days),
                    html_mod.escape(followthrough_days),
                    signal_class, html_mod.escape(str(signal)),
                    db.get("spr_20", 0), db.get("spr_5", 0), spr_eval,
                    db.get("rv_20", 0.0), db.get("rv_5", 0.0),
                )
            )
        except KeyError:
            log_warning("市場のDBデータ取得できず", db_name, market_name)

    if not rows_html:
        return ""

    header = (
        '<h2>市場</h2>\n'
        '<table class="market-table">\n'
        '<thead><tr>\n'
        '  <th>市場名</th><th>RS</th><th>トレンド</th>\n'
        '  <th>ディストリビューション</th><th>フォロースルー</th>\n'
        '  <th>シグナル</th><th>売り圧力レシオ (20,5)</th><th>ボラティリティ (20,5)</th>\n'
        '</tr></thead>\n'
        '<tbody>'
    )
    return header + '\n'.join(rows_html) + '\n</tbody></table>'


# 決算の銘柄表記をパースする正規表現: "1234銘柄名[2Q]" → (code_s, stock_name, quarter)
# 銘柄コードは数字+アルファベット（"215A"等）、銘柄名は残り
_RE_KESSAN_STOCK = re.compile(r'^([0-9]+[A-Z]?)(.+)\[(\d+)Q\]$')


def _html_kessan(kessan_csv):
    """決算セクションのHTMLを生成する

    kessan_csvは3種類の構造が混在する:
    - write_to_csv形式: 2行セット [日付リスト行, 銘柄リスト行]
    - write_to_csv_current形式: 日付ごと1行 [日付, 銘柄1, 銘柄2, ...]

    Args:
        kessan_csv: make_kessan_csv()の返り値
    Returns:
        str: 決算セクションのHTML文字列
    """
    if not kessan_csv:
        return ""

    # MM/DD形式の日付パターン
    date_pattern = re.compile(r'^\d{2}/\d{2}$')

    # パース: (date_str, [銘柄文字列リスト], is_past) のリストに正規化
    entries = []
    today = get_price_day(datetime.today())
    i = 0
    while i < len(kessan_csv):
        row = kessan_csv[i]
        if not row:
            i += 1
            continue

        first_cell = str(row[0]).strip()
        if not date_pattern.match(first_cell):
            i += 1
            continue

        # write_to_csv形式かwrite_to_csv_current形式かを判定
        # write_to_csv形式: 日付のみの行 + 次行が銘柄リスト
        # write_to_csv_current形式: 日付 + 同一行に銘柄
        #
        # 判定基準:
        # 1. row[1:]にも日付がある → write_to_csv形式（複数日付）
        # 2. row[1:]が空 → write_to_csv形式（日付1件のみ）
        # 3. row[1:]が非空かつ日付でない → write_to_csv_current形式
        other_cells = [str(c).strip() for c in row[1:] if str(c).strip()]
        is_write_to_csv = (
            not other_cells  # 日付のみの行（1日分）
            or all(date_pattern.match(c) for c in other_cells)  # 全て日付（複数日分）
        )

        if is_write_to_csv:
            # write_to_csv形式: 次の行が銘柄リスト行
            dates_row = row
            i += 1
            if i < len(kessan_csv):
                stocks_row = kessan_csv[i]
                for j, date_str in enumerate(dates_row):
                    date_str = str(date_str).strip()
                    if not date_pattern.match(date_str):
                        continue
                    stock_strs = []
                    if j < len(stocks_row) and stocks_row[j]:
                        # カンマ区切りの銘柄リスト
                        stock_strs = [s.strip() for s in str(stocks_row[j]).split(",") if s.strip()]
                    if stock_strs:
                        entries.append((date_str, stock_strs))
                i += 1
            else:
                i += 1
        else:
            # write_to_csv_current形式: 同一行に銘柄も含む
            date_str = first_cell
            stock_strs = [str(c).strip() for c in row[1:] if str(c).strip()]
            if stock_strs:
                entries.append((date_str, stock_strs))
            i += 1

    if not entries:
        return ""

    # 日付でソート
    def parse_mmdd(s):
        try:
            m, d = s.split("/")
            return int(m) * 100 + int(d)
        except (ValueError, AttributeError):
            return 0
    entries.sort(key=lambda x: parse_mmdd(x[0]))

    # 過去/未来を分類
    past_entries = []
    future_entries = []
    for date_str, stock_strs in entries:
        try:
            m, d = date_str.split("/")
            year = today.year
            dt = datetime(year, int(m), int(d)).date()
            if dt.month < today.month - 6:
                dt = datetime(year + 1, int(m), int(d)).date()
            is_past = dt < today
        except (ValueError, TypeError):
            is_past = False
        if is_past:
            past_entries.append((date_str, stock_strs))
        else:
            future_entries.append((date_str, stock_strs))

    def _render_cards(entries_list, is_past=False):
        """決算カードのHTML生成"""
        cards = []
        for date_str, stock_strs in entries_list:
            card_class = "kessan-card past" if is_past else "kessan-card future"
            date_label = "%s (済)" % date_str if is_past else date_str
            items = []
            for stock_expr in stock_strs:
                match = _RE_KESSAN_STOCK.match(stock_expr)
                if match:
                    code_s = match.group(1)
                    stock_name = match.group(2)
                    quarter = match.group(3)
                    name_html = '<a href="http://localhost:5001/stock/%s">%s</a>' % (
                        html_mod.escape(code_s), html_mod.escape(stock_name))
                    if quarter == "4":
                        items.append(
                            '<li class="kessan-4q"><a href="https://kabutan.jp/stock/chart?code=%s">%s</a> <b>%s [%sQ]</b></li>' % (
                                html_mod.escape(code_s),
                                html_mod.escape(code_s),
                                name_html,
                                quarter,
                            )
                        )
                    else:
                        items.append(
                            '<li><a href="https://kabutan.jp/stock/chart?code=%s">%s</a> %s [%sQ]</li>' % (
                                html_mod.escape(code_s),
                                html_mod.escape(code_s),
                                name_html,
                                quarter,
                            )
                        )
                else:
                    items.append('<li>%s</li>' % html_mod.escape(stock_expr))
            cards.append(
                '  <div class="%s">\n'
                '    <div class="card-date">%s</div>\n'
                '    <ul class="stock-list">%s</ul>\n'
                '  </div>' % (card_class, html_mod.escape(date_label), '\n      '.join(items))
            )
        return '\n'.join(cards)

    # カードHTML生成
    parts = ['<h2>決算日</h2>']

    # 未来の決算（常に表示）
    if future_entries:
        parts.append('<div class="kessan-grid">')
        parts.append(_render_cards(future_entries, is_past=False))
        parts.append('</div>')

    # 過去の決算（折りたたみ）
    if past_entries:
        parts.append(
            '<details>\n'
            '<summary style="cursor:pointer; color:#666; margin:8px 0;">▶ 済の決算を表示 (%d件)</summary>\n'
            '<div class="kessan-grid">' % len(past_entries)
        )
        parts.append(_render_cards(past_entries, is_past=True))
        parts.append('</div>\n</details>')

    return '\n'.join(parts)


# =HYPERLINK("url","text") パターン
_RE_HYPERLINK = re.compile(r'^=HYPERLINK\("([^"]+)","([^"]+)"\)$')


def _html_disclosure(disc_csv):
    """適宜開示セクションのHTMLを生成する

    disc_csvは expoert_to_csv() の返り値で、各行が
    [date_str, =HYPERLINK("url","code_s"), stock_name, type_label, =HYPERLINK("url","heading")]
    の形式。

    Args:
        disc_csv: disclosure.expoert_to_csv()の返り値
    Returns:
        str: 適宜開示セクションのHTML文字列
    """
    if not disc_csv:
        return ""

    # ヘッダー行をスキップ、空行もスキップ
    data_rows = []
    for row in disc_csv:
        if not row or len(row) < 5:
            continue
        if row[0] == "日付":  # ヘッダー行
            continue
        if row[0] == "":  # 空行
            continue
        data_rows.append(row)

    if not data_rows:
        return ""

    # 日付でグループ化（直近3日 vs それ以前）
    today = get_price_day(datetime.today())
    three_days_ago = today - timedelta(days=3)

    thirty_days_ago = today - timedelta(days=30)

    recent_rows = []
    older_rows = []
    for row in data_rows:
        date_str = str(row[0])
        try:
            # YYYYMMDD形式
            dt = datetime.strptime(date_str, "%Y%m%d").date()
            if dt < thirty_days_ago:
                continue  # 30日以上前のデータは除外
            if dt >= three_days_ago:
                recent_rows.append(row)
            else:
                older_rows.append(row)
        except (ValueError, TypeError):
            older_rows.append(row)

    def make_disc_table(rows):
        """開示データ行からテーブルHTMLを生成"""
        lines = ['<table class="disc-table">']
        lines.append('<tr><th>日付</th><th>銘柄</th><th>種類</th><th>内容</th></tr>')
        for row in rows:
            date_str = str(row[0])
            # 日付を表示用に変換 (YYYYMMDD → MM/DD)
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                date_display = "%02d/%02d" % (dt.month, dt.day)
            except (ValueError, TypeError):
                date_display = html_mod.escape(date_str)

            # 銘柄コード（HYPERLINK解析）
            code_match = _RE_HYPERLINK.match(str(row[1]))
            if code_match:
                code_url = code_match.group(1)
                code_text = code_match.group(2)
                code_html = '<a href="%s">%s</a>' % (code_url, html_mod.escape(code_text))
            else:
                code_html = html_mod.escape(str(row[1]))

            # 銘柄名にWebAppリンクを付与
            raw_name = html_mod.escape(str(row[2]))
            disc_code = code_match.group(2).strip() if code_match else ""
            if disc_code:
                stock_name = '<a href="http://localhost:5001/stock/%s">%s</a>' % (
                    html_mod.escape(disc_code), raw_name)
            else:
                stock_name = raw_name
            type_label = html_mod.escape(str(row[3]))

            # 本文（HYPERLINK解析）
            body_match = _RE_HYPERLINK.match(str(row[4]))
            if body_match:
                body_url = body_match.group(1)
                body_text = body_match.group(2)
                body_html = '<a href="%s">%s</a>' % (body_url, html_mod.escape(body_text))
            else:
                body_html = html_mod.escape(str(row[4]))

            # 決算・修正行は背景色を変える
            row_class = ""
            if type_label in ("決算", "修正"):
                row_class = ' class="disc-row-gyoseki"'

            lines.append(
                '<tr%s><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td></tr>' % (
                    row_class, date_display, code_html, stock_name,
                    type_label, body_html,
                )
            )
        lines.append('</table>')
        return '\n'.join(lines)

    parts = ['<h2>適宜開示</h2>']

    if recent_rows:
        parts.append('<details open>')
        parts.append('<summary>直近3日間 (%d件)</summary>' % len(recent_rows))
        parts.append(make_disc_table(recent_rows))
        parts.append('</details>')

    if older_rows:
        parts.append('<details>')
        parts.append('<summary>それ以前 (%d件)</summary>' % len(older_rows))
        parts.append(make_disc_table(older_rows))
        parts.append('</details>')

    return '\n'.join(parts)


def create_market_html(market_db,
                       kessan_csv=None,
                       theme_rank_data=None):
    """市場DBから表示用HTMLファイルを生成する

    適宜開示セクションは別ページ化されたため本HTMLには含まれない。
    create_disclosure_html() で disclosure_data.html を生成する。

    Args:
        market_db: 市場DB（必須）
        kessan_csv: 決算CSVデータ（Noneの場合はセクション省略）
        theme_rank_data: get_theme_rank_list()の返り値タプル（Noneの場合は履歴テーブル省略）
    Returns:
        str: 生成したHTMLファイルのパス
    """
    html_path = os.path.join(DATA_DIR, "code_rank_data", "market_data.html")

    now = datetime.now()
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = "%s (%s)" % (now.strftime("%Y-%m-%d"), weekday_names[now.weekday()])

    # 各セクションのHTML生成
    theme_html = _html_theme_rank(market_db, theme_rank_data)
    market_html = _html_market(market_db)
    kessan_html = _html_kessan(kessan_csv) if kessan_csv else ""

    # HTMLテンプレートに埋め込み
    html_content = (
        '<!DOCTYPE html>\n'
        '<html lang="ja">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<title>市場データ - %s</title>\n'
        '<style>\n%s</style>\n'
        '</head>\n'
        '<body>\n\n'
        '<h1>市場データ <span class="date">%s</span></h1>\n\n'
        '%s\n\n'
        '%s\n\n'
        '%s\n\n'
        '<footer style="margin-top: 40px; padding-top: 12px; border-top: 1px solid #ddd; '
        'font-size: 0.8em; color: #999;">\n'
        '  生成日時: %s | shintakane分析システム\n'
        '</footer>\n\n'
        '</body>\n'
        '</html>\n'
    ) % (
        now.strftime("%Y-%m-%d"),
        _HTML_CSS,
        date_str,
        theme_html,
        market_html,
        kessan_html,
        now.strftime("%Y-%m-%d %H:%M"),
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    log_print("HTML生成完了: %s" % html_path)
    return html_path


def create_disclosure_html(disc_csv):
    """適宜開示データから表示用HTMLファイルを生成する

    issue #148 関連: 市場データページの導線改善のため、
    適宜開示セクションを market_data.html から分離して独立ページ化。

    Args:
        disc_csv: 適宜開示CSVデータ
    Returns:
        str: 生成したHTMLファイルのパス
    """
    html_path = os.path.join(DATA_DIR, "code_rank_data", "disclosure_data.html")

    now = datetime.now()
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = "%s (%s)" % (now.strftime("%Y-%m-%d"), weekday_names[now.weekday()])

    disc_html = _html_disclosure(disc_csv) if disc_csv else ""

    html_content = (
        '<!DOCTYPE html>\n'
        '<html lang="ja">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<title>適宜開示 - %s</title>\n'
        '<style>\n%s</style>\n'
        '</head>\n'
        '<body>\n\n'
        '<h1>適宜開示 <span class="date">%s</span></h1>\n\n'
        '%s\n\n'
        '<footer style="margin-top: 40px; padding-top: 12px; border-top: 1px solid #ddd; '
        'font-size: 0.8em; color: #999;">\n'
        '  生成日時: %s | shintakane分析システム\n'
        '</footer>\n\n'
        '</body>\n'
        '</html>\n'
    ) % (
        now.strftime("%Y-%m-%d"),
        _HTML_CSS,
        date_str,
        disc_html,
        now.strftime("%Y-%m-%d %H:%M"),
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    log_print("HTML生成完了: %s" % html_path)
    return html_path


def main():
    # ロガーの初期化
    logger = setup_logger("make_stock_db")

    market_db = update_market_db()  # noqa: F841
    create_market_csv()



if __name__ == "__main__":
    setup_logger("make_market_db")
    main()
