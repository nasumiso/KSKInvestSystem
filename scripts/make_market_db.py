#!/usr/bin/env python3

# =================================================
# セクターDBを作成します。
# =================================================

import re
import html as html_mod
import json
import urllib.parse
from datetime import datetime, timedelta
import csv
import os

import price
import make_stock_db
import fng

from ks_util import *


URL_THEME_RANK_KABUTAN = "https://kabutan.jp/info/accessranking/3_2"
from db_shelve import get_market_db as _get_market_shelve_db

THEME_STRENGTH_WINDOW_DAYS = 40


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


def _archive_old_theme_rank(theme_rank_dir, days=THEME_STRENGTH_WINDOW_DAYS):
    """強度ウィンドウ日数より古いtheme_rank_YYMMDD.htmlをhistory/に移動"""
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


def _theme_rank_history_date(cach_date):
    """テーマランク履歴用の日付文字列を返す。"""
    if cach_date:
        return get_price_day(cach_date).isoformat()
    return get_price_day(datetime.today()).isoformat()


def update_theme_rank_history(history, date_str, theme_rank_list,
                              window_days=THEME_STRENGTH_WINDOW_DAYS):
    """Kabutan生順位履歴を同日置換で更新し、強度値を計算する。

    Args:
        history: [(date_str, [(theme_name, rank), ...]), ...]
        date_str: 更新対象日 (YYYY-MM-DD)
        theme_rank_list: Kabutan生順位のテーマ名リスト
        window_days: 保持する営業日数
    Returns:
        tuple: (new_history, theme_strength, theme_strength_days)
    """
    rank_rows = [(theme, i + 1) for i, theme in enumerate(theme_rank_list[:30])]
    new_history = []
    replaced = False
    for old_date, old_rows in history or []:
        if old_date == date_str:
            new_history.append((date_str, rank_rows))
            replaced = True
        else:
            new_history.append((old_date, old_rows))
    if not replaced:
        new_history.append((date_str, rank_rows))
    new_history = new_history[-window_days:]

    theme_strength = {}
    theme_strength_days = {}
    for _, rows in new_history:
        for theme, rank in rows:
            pt = max(0, 31 - int(rank))
            if pt <= 0:
                continue
            theme_strength[theme] = theme_strength.get(theme, 0) + pt
            theme_strength_days[theme] = theme_strength_days.get(theme, 0) + 1

    return new_history, theme_strength, theme_strength_days


def _shelve_path_exists(path):
    """ShelveDB を新規作成せず、既存DBファイルの有無だけ判定する。"""
    suffixes = ("", ".dat", ".dir", ".bak", ".db")
    return any(os.path.exists(path + suffix) for suffix in suffixes)


def _split_theme_names(themes):
    """stock_data['themes'] をテーマ名リストに正規化する。"""
    if isinstance(themes, str):
        return [t.strip() for t in themes.split(",") if t.strip()]
    if isinstance(themes, (list, tuple)):
        return [str(t).strip() for t in themes if str(t).strip()]
    return []


def build_theme_portfolio_links(portfolio_records, stock_map):
    """保有/準保有銘柄をテーマ名から逆引きできる形にする。

    Returns:
        dict: {theme_name: {"hold": [(code_s, stock_name)], "semi": [...]}}
    """
    status_key = {"1保": "hold", "2準": "semi"}
    links = {}
    for rec in portfolio_records or []:
        status = rec.get("status")
        key = status_key.get(status)
        if not key:
            continue
        code_s = rec.get("code_s")
        stock = (stock_map or {}).get(code_s) or {}
        stock_name = stock.get("stock_name") or code_s
        item = (code_s, stock_name)
        for theme in _split_theme_names(stock.get("themes", "")):
            theme_links = links.setdefault(theme, {"hold": [], "semi": []})
            if item not in theme_links[key]:
                theme_links[key].append(item)
    for theme_links in links.values():
        theme_links["hold"].sort(key=lambda item: item[0])
        theme_links["semi"].sort(key=lambda item: item[0])
    return links


def collect_theme_portfolio_links():
    """portfolio_shelve と stocks_shelve からテーマカード強調用データを作る。"""
    import dbm
    from db_shelve import PORTFOLIO_SHELVE, STOCKS_SHELVE, ShelveDB
    import portfolio_shelve

    if not _shelve_path_exists(PORTFOLIO_SHELVE) or not _shelve_path_exists(STOCKS_SHELVE):
        return {}
    try:
        records = (
            portfolio_shelve.list_records(status="1保")
            + portfolio_shelve.list_records(status="2準")
        )
        if not records:
            return {}
        stock_map = {}
        with ShelveDB(STOCKS_SHELVE) as db:
            for rec in records:
                code_s = rec.get("code_s")
                if code_s:
                    stock_map[code_s] = db.get(code_s) or {}
        return build_theme_portfolio_links(records, stock_map)
    except dbm.error as e:
        log_warning("[theme] portfolio links 作成失敗:", e)
        return {}


def make_theme_data(prev_momentum_rank=None, theme_rank_history=None):
    """テーマランクデータを作成

    Args:
        prev_momentum_rank: 前日のモメンタム順位リスト（差分ラベル計算用）
        theme_rank_history: Kabutan生順位履歴（強度計算用）
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
    history_date = _theme_rank_history_date(cach_date)
    history, strength, strength_days = update_theme_rank_history(
        theme_rank_history, history_date, theme_rank_list,
    )
    market_db["theme_rank_history"] = history
    market_db["theme_strength"] = strength
    market_db["theme_strength_days"] = strength_days
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
    # Stalling Day は週足の high52_weekly を使うため、週足計算後に後付け検出
    high52 = db.get("high52_weekly")
    raw = db.pop("_daily_price_list_raw", None)
    if high52 and raw:
        price.add_stalling_days(db, raw, high52)
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
    high52 = db_dict.get("high52_weekly")
    raw = db_dict.pop("_daily_price_list_raw", None)
    if high52 and raw:
        price.add_stalling_days(db_dict, raw, high52)
    return {key: db_dict}


def make_nasdaq_db():
    """NASDAQ総合指数を yfinance (^IXIC) 経由で取得する。
    旧 Kabutan 0802 経由はフォーマット変更で動作しなくなったため切替済み。"""
    return _make_us_index_db("_IXIC", "^IXIC", "nasdaq")


def make_sp500_db():
    """S&P500を yfinance (^GSPC) 経由で取得する"""
    return _make_us_index_db("_GSPC", "^GSPC", "sp500")


def make_fng_db():
    """CNN Fear & Greed Index を取得する。"""
    return {"fear_and_greed": fng.fetch_fear_and_greed()}


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


def _is_index_fetch_valid(new_index_db):
    """指数取得結果が日次データを含む有効な dict かを判定する。

    通信失敗等で日次取得が失敗した場合、price_log が欠落 or 空になる。
    週足だけ部分的に成功した dict (rs_raw=0 等を含む) も "日次が無い"
    として無効扱いにし、前日DBの上書きを防ぐ。
    """
    if not new_index_db:
        return False
    if not new_index_db.get("price_log"):
        return False
    return True


def _update_index_market_state(prev_index_db, new_index_db):
    """指数 dict に market_state / state_meta / state_history / direction_signal を計算して書き込む。

    前日 state_meta を引き継ぎ、当日の DD/FTD 候補から market_state.py の純関数群で
    新state を導出する。

    Args:
        prev_index_db: 前日の market_db[index_name] dict (なければ {})
        new_index_db: 当日の指数 dict (mutate される)
    """
    import market_state

    # 1. 前日 state_meta を取得 (なければ空)
    prev_meta = prev_index_db.get("state_meta", {}) or {}
    prev_state = prev_index_db.get("market_state")
    prev_history = prev_index_db.get("state_history", []) or []

    # 2. 当日のDD候補を前日のDDリストにマージ
    today_dd_with_close = list(prev_meta.get("distribution_days_with_close", []))
    new_dd_with_close = new_index_db.get("distribution_days_with_close", []) or []
    # 重複を排除しつつ新しいDDを追加
    existing_dates = {d for d, _ in today_dd_with_close}
    for dd_date, dd_close in new_dd_with_close:
        if dd_date not in existing_dates:
            today_dd_with_close.append((dd_date, dd_close))
            existing_dates.add(dd_date)

    # 3. 当日終値・履歴で DD を失効処理
    daily_history = new_index_db.get("daily_history", []) or []
    today_close = float(new_index_db.get("price", 0) or 0)
    valid_dd = market_state.expire_distribution_days(
        today_dd_with_close, today_close=today_close, daily_history=daily_history,
    )

    # 4. ラリーアテンプト追跡 (Correction 状態のときのみ)
    rally_meta = {
        "rally_attempt_start_date": prev_meta.get("rally_attempt_start_date"),
        "rally_attempt_start_low": prev_meta.get("rally_attempt_start_low"),
    }
    today_low = float(new_index_db.get("low", 0) or 0)
    today_high = float(new_index_db.get("high", 0) or 0)
    today_volume = int(new_index_db.get("volume", 0) or 0)
    # 当日と前日の close/low/volume を取得 (price_log[0] が当日、[1] が前日のはず)
    price_log = new_index_db.get("price_log", []) or []
    today_date = daily_history[0] if daily_history else (
        price_log[0][0] if price_log else ""
    )
    prev_close = 0.0
    prev_volume = 0
    prev_low = 0.0
    if len(price_log) >= 2:
        prev_close = float(price_log[1][1] or 0)
    # price_log には close のみ。low/volume は当日分しか new_index_db に残らない
    # daily_history と組み合わせて 1日前の low/volume が必要だが、現状取れない
    # ラリー判定は close 基準 (前日 close) で十分動く。low/volume は当日分のみ参照
    today_dict = {
        "date": today_date,
        "close": today_close,
        "low": today_low,
        "high": today_high,
        "volume": today_volume,
    }
    prev_dict = {
        "close": prev_close,
        "volume": int(prev_meta.get("prev_volume", 0) or 0),
        "low": prev_low,
    }

    if prev_state == market_state.MARKET_IN_CORRECTION:
        rally_meta = market_state.update_rally_attempt(rally_meta, today_dict, prev_dict)
    else:
        # Correction 以外ではラリー追跡は不要 (リセット)
        rally_meta = {"rally_attempt_start_date": None, "rally_attempt_start_low": None}

    # 5. FTD 判定 (Correction 状態 + ラリー追跡中のみ)
    ftd_today = False
    if prev_state == market_state.MARKET_IN_CORRECTION:
        # FTD 判定には今日の出来高 vs 前日出来高が必要
        # prev_meta に prev_volume が無くても、price_log から推定できないので簡易判定
        # 当日の出来高は new_index_db['volume']、前日は state_meta に保存しておく
        if prev_dict["volume"] > 0 and today_dict["volume"] > 0:
            ftd_today = market_state.check_follow_through_day(
                today_dict, prev_dict, rally_meta, daily_history,
            )

    # 6. 状態遷移 (DD/FTD + 週足10MA明確割れ補助ルール)
    below_10ma = market_state.is_below_10ma_clearly(
        new_index_db.get("price_kairi_wma10")
    )
    new_state, trigger = market_state.derive_state(
        prev_state=prev_state,
        valid_dd_count=len(valid_dd),
        ftd_today=ftd_today,
        below_10ma=below_10ma,
    )

    # 7. state_history 更新
    new_history = market_state.append_state_history(
        prev_history, today_date or "", new_state, trigger,
    )

    # 8. 結果を new_index_db に書き戻し
    new_index_db["market_state"] = new_state
    new_index_db["state_meta"] = {
        "rally_attempt_start_date": rally_meta.get("rally_attempt_start_date"),
        "rally_attempt_start_low": rally_meta.get("rally_attempt_start_low"),
        "distribution_days_with_close": valid_dd,
        "last_ftd_date": today_date if ftd_today else prev_meta.get("last_ftd_date"),
        "prev_volume": today_volume,  # 次回呼び出し用に保存
    }
    new_index_db["state_history"] = new_history
    new_index_db["direction_signal"] = market_state.to_direction_signal(
        new_state, today_date or "",
    )


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
    theme_db = make_theme_data(
        prev_momentum_rank,
        theme_rank_history=market_db.get("theme_rank_history", []),
    )
    market_db.update(theme_db)
    market_db["theme_portfolio_links"] = collect_theme_portfolio_links()

    market_db.setdefault("fear_and_greed", {})
    try:
        market_db.update(make_fng_db())
    except Exception as e:
        log_warning(
            "[market] Fear & Greed 取得失敗のため DB 更新をスキップ "
            "(前回データを保持): %s" % e
        )

    # 各指数を更新 (まず DD/FTD 候補と当日価格を取得)
    index_makers = [
        ("topix", make_topix_db),
        ("mothers", make_mothers_db),
        ("nikkei225", make_nikkei_db),
        ("nasdaq", make_nasdaq_db),
        ("sp500", make_sp500_db),
    ]
    for index_name, maker in index_makers:
        prev_index_db = market_db.get(index_name, {}) or {}
        new_data = maker()  # {index_name: {...}}
        new_index_db = new_data.get(index_name, {})
        if not _is_index_fetch_valid(new_index_db):
            log_warning(
                "[market] %s の日次取得失敗のため DB 更新をスキップ "
                "(前日データを保持)" % index_name
            )
            # 初回起動など prev_index_db が無いケースで
            # price.py 等の market_db["topix"] 直接参照が KeyError になるのを防ぐ
            if index_name not in market_db:
                market_db[index_name] = prev_index_db
            continue
        try:
            _update_index_market_state(prev_index_db, new_index_db)
        except Exception as e:
            log_warning("[market_state] %s の State 計算失敗: %s" % (index_name, e))
        market_db[index_name] = new_index_db

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
  border: 1px solid #ddd; border-radius: 6px; padding: 6px 6px 0 6px;
  background: #fff; width: 108px; box-sizing: border-box; font-size: 0.8em;
  overflow: hidden;
}
.theme-badge .rank { font-size: 0.75em; color: #333; }
.theme-badge .name { font-weight: bold; text-align: center; color: #333; font-size: 0.95em; line-height: 1.2; word-break: break-all; overflow-wrap: anywhere; }
.theme-badge a.name { text-decoration: none; }
.theme-badge a.name:hover { text-decoration: underline; }
.theme-badge .change { font-size: 0.8em; margin-top: 2px; color: #333; }
.theme-badge .rate { font-size: 0.8em; margin-top: 2px; }
.theme-badge .strength-track { width: 96px; height: 5px; margin-top: 5px; background: rgba(255,255,255,0.55); }
.theme-badge .strength-bar { height: 100%; background: #1f4e79; }
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
.theme-badge.theme-hold { border: 3px solid #d4af37; }
.theme-badge.theme-semi { border: 2px solid #a8a8a8; }
.theme-badge.theme-clickable { cursor: pointer; }
.theme-modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1000;
  display: none; align-items: center; justify-content: center;
}
.theme-modal-content {
  background: #fff; border-radius: 6px; padding: 1em 1.2em;
  width: min(420px, 90vw); max-height: 80vh; overflow-y: auto;
  box-shadow: 0 4px 16px rgba(0,0,0,0.2);
}
.theme-modal-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 0.6em; padding-bottom: 0.4em; border-bottom: 1px solid #eee;
}
.theme-modal-header h3 { margin: 0; font-size: 1.05em; }
.theme-modal-close {
  background: none; border: none; font-size: 1.4em; cursor: pointer;
  padding: 0 0.3em; line-height: 1; color: #888;
}
.theme-modal-section { margin-top: 0.6em; }
.theme-modal-section h4 { margin: 0 0 0.3em 0; font-size: 0.9em; color: #555; }
.theme-modal-section ul { list-style: none; padding: 0; margin: 0; }
.theme-modal-section li { margin-bottom: 0.3em; font-size: 0.9em; list-style: none; }
.theme-modal-section a { color: #1976d2; text-decoration: none; }
.theme-modal-section a:hover { text-decoration: underline; }

/* 市場テーブル */
/* table のデフォルト width:100% を解除し、列幅の合計だけのコンパクトな表にする */
.market-table { width: auto; }
.market-table th { background: #2c3e50; color: #fff; }
/* 列幅: 市場名と市場状態は内容に合わせ最小幅、それ以外は固定 (需給バランスを広げ、FTDを狭める) */
.market-table th, .market-table td { white-space: nowrap; }
.market-table th.col-rs,    .market-table td.col-rs    { width: 60px; text-align: center; }
.market-table th.col-trend, .market-table td.col-trend { width: 50px; text-align: center; padding: 0; }
.market-table th.col-dd,    .market-table td.col-dd    { width: 80px; text-align: center; }
.market-table th.col-ftd,   .market-table td.col-ftd   { width: 80px; text-align: center; }
.market-table th.col-spr,   .market-table td.col-spr   { width: 140px; }
.signal-sell { background: #fdedec; color: #c0392b; font-weight: bold; }
.signal-buy { background: #eafaf1; color: #27ae60; font-weight: bold; }
/* トレンド列の背景色 (portfolio_list と仕様統一: ◎=濃黄 / ◯=薄黄 / —=水色) */
.trend-bg-strong { background: #fbbc04; }  /* ◎ */
.trend-bg-good   { background: #fce8b2; }  /* ◯ */
.trend-bg-none   { background: #6fa8dc; }  /* — / 空 */
/* 需給バランスセル (issue #247): SPR/rv の横バー 2 本縦積み */
.spr-gauge-cell { padding: 4px; vertical-align: middle; white-space: nowrap; }
.spr-gauge-cell svg { display: block; width: 100%; height: auto; }
/* market_state */
.state-confirmed { background: #eafaf1; color: #27ae60; font-weight: bold; }
.state-pressure  { background: #fffbe6; color: #b8860b; font-weight: bold; }
.state-correction { background: #fdedec; color: #c0392b; font-weight: bold; }
/* DD 進行度の警告色 */
.dd-pressure  { background: #fffbe6; color: #b8860b; font-weight: bold; }
.dd-correction { background: #fdedec; color: #c0392b; font-weight: bold; }
/* 市場指標サマリー表 (Fear & Greed / 信用評価損益率 / 信用倍率 統合) */
.market-indicators { border-collapse: collapse; margin: 0 0 10px 0; background: #fff; width: auto; }
.market-indicators th, .market-indicators td { padding: 3px 8px; border: 1px solid #ddd; text-align: left; white-space: nowrap; }
.market-indicators th.mi-label { font-weight: bold; }
.market-indicators td.mi-value { font-weight: bold; text-align: right; }
.market-indicators td.mi-rating { text-align: center; padding: 3px 6px; }
.market-indicators td.mi-delta { text-align: right; }
.market-indicators .mi-unit { color: #888; font-size: 0.85em; margin-right: 2px; }
.fng-rating { padding: 1px 6px; border-radius: 999px; font-size: 0.85em; font-weight: bold; color: #fff; }
.fng-rating-extreme-fear { background: #a93226; }
.fng-rating-fear { background: #e67e22; }
.fng-rating-neutral { background: #7f8c8d; }
.fng-rating-greed { background: #27ae60; }
.fng-rating-extreme-greed { background: #145a32; }
.fng-delta-pos { color: #c0392b; }
.fng-delta-neg { color: #2980b9; }

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
    theme_strength = market_db.get("theme_strength", {})
    theme_strength_days = market_db.get("theme_strength_days", {})
    theme_portfolio_links = market_db.get("theme_portfolio_links", {})
    max_strength = max(theme_strength.values()) if theme_strength else 0

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

        strength = int(theme_strength.get(theme, 0) or 0)
        strength_days = int(theme_strength_days.get(theme, 0) or 0)
        strength_width = (strength * 100.0 / max_strength) if max_strength else 0
        strength_title = html_mod.escape(
            "強度: %dpt / %d日中%d日Top30" % (
                strength, THEME_STRENGTH_WINDOW_DAYS, strength_days,
            ),
            quote=True,
        )
        link_info = theme_portfolio_links.get(theme, {}) or {}
        hold_items = link_info.get("hold", []) or []
        semi_items = link_info.get("semi", []) or []
        has_links = bool(hold_items or semi_items)
        portfolio_class = " theme-hold" if hold_items else (
            " theme-semi" if semi_items else ""
        )
        clickable_class = " theme-clickable" if has_links else ""
        if has_links:
            hold_json = html_mod.escape(
                json.dumps(hold_items, ensure_ascii=False), quote=True,
            )
            semi_json = html_mod.escape(
                json.dumps(semi_items, ensure_ascii=False), quote=True,
            )
            theme_attr = html_mod.escape(theme, quote=True)
            click_attrs = (
                ' data-theme="%s" data-hold="%s" data-semi="%s"'
                ' onclick="openThemeModal(this)"'
            ) % (theme_attr, hold_json, semi_json)
        else:
            click_attrs = ""
        theme_escaped = html_mod.escape(theme)
        kabutan_theme_url = html_mod.escape(
            "https://kabutan.jp/themes/?theme=" + urllib.parse.quote(theme),
            quote=True,
        )
        name_html = (
            '<a class="name" href="%s" target="_blank" rel="noopener"'
            ' onclick="event.stopPropagation()" title="株探テーマページを開く">%s</a>'
        ) % (kabutan_theme_url, theme_escaped)
        parts.append(
            '  <div class="theme-badge %s%s%s"%s>\n'
            '    <span class="rank">#%d</span>\n'
            '    %s\n'
            '    <span class="change %s">%s</span>\n'
            '    %s\n'
            '    <div class="strength-track" title="%s"><div class="strength-bar" style="width:%.1f%%"></div></div>\n'
            '  </div>' % (
                heat_class, portfolio_class, clickable_class, click_attrs,
                i + 1, name_html, change_class,
                change_text, rate_html, strength_title,
                strength_width,
            )
        )

    parts.append('</div>')

    # 保有/準保有銘柄ポップアップダイアログ
    parts.append(
        '<div id="theme-modal" class="theme-modal-overlay"'
        ' onclick="closeThemeModalOnOverlay(event)">\n'
        '  <div class="theme-modal-content">\n'
        '    <div class="theme-modal-header">\n'
        '      <h3 id="theme-modal-title"></h3>\n'
        '      <button type="button" class="theme-modal-close"'
        ' onclick="closeThemeModal()" aria-label="閉じる">×</button>\n'
        '    </div>\n'
        '    <div id="theme-modal-body"></div>\n'
        '  </div>\n'
        '</div>\n'
        '<script>\n'
        'function openThemeModal(card) {\n'
        '  var theme = card.dataset.theme || "";\n'
        '  var hold = [];\n'
        '  var semi = [];\n'
        '  try { hold = JSON.parse(card.dataset.hold || "[]"); } catch (e) {}\n'
        '  try { semi = JSON.parse(card.dataset.semi || "[]"); } catch (e) {}\n'
        '  document.getElementById("theme-modal-title").textContent = theme;\n'
        '  var body = document.getElementById("theme-modal-body");\n'
        '  body.innerHTML = "";\n'
        '  function section(label, items) {\n'
        '    if (!items.length) return;\n'
        '    var div = document.createElement("div");\n'
        '    div.className = "theme-modal-section";\n'
        '    var h4 = document.createElement("h4");\n'
        '    h4.textContent = label;\n'
        '    div.appendChild(h4);\n'
        '    var ul = document.createElement("ul");\n'
        '    items.forEach(function(item) {\n'
        '      var li = document.createElement("li");\n'
        '      var a = document.createElement("a");\n'
        '      a.href = "/detail/" + encodeURIComponent(item[0]);\n'
        '      a.target = "_blank";\n'
        '      a.rel = "noopener";\n'
        '      a.textContent = item[0] + (item[1] ? " " + item[1] : "");\n'
        '      li.appendChild(a);\n'
        '      ul.appendChild(li);\n'
        '    });\n'
        '    div.appendChild(ul);\n'
        '    body.appendChild(div);\n'
        '  }\n'
        '  section("保有", hold);\n'
        '  section("準保有", semi);\n'
        '  document.getElementById("theme-modal").style.display = "flex";\n'
        '}\n'
        'function closeThemeModal() {\n'
        '  document.getElementById("theme-modal").style.display = "none";\n'
        '}\n'
        'function closeThemeModalOnOverlay(e) {\n'
        '  if (e.target.id === "theme-modal") closeThemeModal();\n'
        '}\n'
        'document.addEventListener("keydown", function(e) {\n'
        '  if (e.key === "Escape") closeThemeModal();\n'
        '});\n'
        '</script>'
    )

    # Kabutanランキング履歴
    if theme_rank_data:
        theme_rank_list, prev_theme_rank_list, _, prev_day = theme_rank_data
        if theme_rank_list or prev_theme_rank_list:
            parts.append(
                '<h3><a href="https://kabutan.jp/info/accessranking/3_2"'
                ' target="_blank" rel="noopener">Kabutanランキング履歴</a></h3>'
            )
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


# 指数向けトレンドテンプレート判定で「RS基準達成」とみなす rs_raw 閾値 (issue #148 Part 1)。
# 銘柄向けの rs_rank>=75 は指数 (TOPIX 自身など) では常に未達となるため、
# 直近1年で 5% 以上上昇していれば「RS基準達成」扱いとする緩い基準を採用。
INDEX_RS_TREND_THRESHOLD = 1.05


def _adjust_index_trend_template(db):
    """指数向けに trend_template の RS 基準を補正する。

    銘柄向けの rs_rank>=75 判定は指数では常に未達となるため、
    rs_raw > INDEX_RS_TREND_THRESHOLD なら "RS" を未達リストから外す。

    Args:
        db: 指数DBの1指数分dict (rs_raw, trend_template を持つ)
    Returns:
        dict: trend_template だけ補正したコピー (元dictは破壊しない)
    """
    misses = list(db.get("trend_template", []))
    if "RS" in misses and db.get("rs_raw", 0) > INDEX_RS_TREND_THRESHOLD:
        misses.remove("RS")
    adjusted = dict(db)
    adjusted["trend_template"] = misses
    return adjusted


def _rs_style(rs_raw):
    """rs_raw 値から inline style 属性を返す (portfolio パレット準拠)。

    - >= 1.2: 濃黄 (#fbbc04)
    - 1.1〜1.2: 薄黄 (#fce8b2)
    - 0.9〜1.1: 色なし (中立)
    - 0.8〜0.9: 水色 (#6fa8dc 薄警告)
    - <= 0.8: 青 (#4285f4 警告) + 白文字
    """
    if not rs_raw:
        return ""
    if rs_raw >= 1.2:
        return ' style="background:#fbbc04"'
    if rs_raw >= 1.1:
        return ' style="background:#fce8b2"'
    if rs_raw <= 0.8:
        return ' style="background:#4285f4;color:#fff"'
    if rs_raw <= 0.9:
        return ' style="background:#6fa8dc"'
    return ""


def _format_fng_delta(score, past_score):
    """Fear & Greed の差分表示用テキストを返す。"""
    try:
        delta = float(score) - float(past_score)
    except (TypeError, ValueError):
        return "—", ""
    cls = "fng-delta-pos" if delta >= 0 else "fng-delta-neg"
    return "%+.1f" % delta, cls


def _format_credit_delta(latest, prev):
    """信用評価/倍率の差分表示 (pt) と方向クラス。

    pt 差が + なら "天井圏寄り" として赤系 (fng-delta-pos 流用)、
    - なら "底値圏寄り" として青系 (fng-delta-neg 流用) を返す。
    値が None なら ("—", "") を返す。
    """
    try:
        delta = float(latest) - float(prev)
    except (TypeError, ValueError):
        return "—", ""
    cls = "fng-delta-pos" if delta >= 0 else "fng-delta-neg"
    return "%+.2f" % delta, cls


def _fng_row(market_db):
    """Fear & Greed の表 1 行分 (label_html, value_html, rating_html, d1_html, d4_html, title)。
    取得失敗時は None。"""
    fng_db = market_db.get("fear_and_greed") or {}
    if not fng_db:
        return None
    try:
        score = float(fng_db["score"])
    except (KeyError, TypeError, ValueError):
        return None
    rating = str(fng_db.get("rating", "")).strip().lower()
    rating_label = rating.replace("_", " ").title() if rating else "Unknown"
    rating_class = "fng-rating-" + rating.replace(" ", "-") if rating else "fng-rating-neutral"
    d5_text, d5_cls = _format_fng_delta(score, fng_db.get("score_5d_ago"))
    d20_text, d20_cls = _format_fng_delta(score, fng_db.get("score_20d_ago"))
    access_date = fng_db.get("access_date")
    title = ""
    if hasattr(access_date, "strftime"):
        title = "更新: " + access_date.strftime("%Y-%m-%d %H:%M")
    label_html = (
        '<a href="https://edition.cnn.com/markets/fear-and-greed"'
        ' target="_blank" rel="noopener">Fear &amp; Greed</a>'
    )
    value_html = '%.1f' % score
    rating_html = '<span class="fng-rating %s">%s</span>' % (
        html_mod.escape(rating_class), html_mod.escape(rating_label),
    )
    d1_html = (
        '<span class="mi-unit">5日</span> '
        '<span class="%s">%s</span>'
    ) % (html_mod.escape(d5_cls), html_mod.escape(d5_text))
    d4_html = (
        '<span class="mi-unit">20日</span> '
        '<span class="%s">%s</span>'
    ) % (html_mod.escape(d20_cls), html_mod.escape(d20_text))
    return label_html, value_html, rating_html, d1_html, d4_html, title


def _credit_rows(market_db):
    """信用評価損益率と信用倍率の表行リストを返す ([(label,value,rating,d1,d4,title), ...])。
    取得失敗時は []。

    market_db は使わず、$KS_DATA_DIR/code_rank_data/credit_balance.json を読む (issue #211)。
    """
    path = os.path.join(DATA_DIR, "code_rank_data", "credit_balance.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return []
    history = payload.get("history") or []
    if not history:
        return []
    latest = history[-1]
    eval_rate = latest.get("credit_eval_rate")
    bairitsu = latest.get("credit_bairitsu")
    if eval_rate is None:
        return []
    date_str = latest.get("date", "")
    title = "%s 基準" % date_str if date_str else ""

    def _prev(field, n):
        if len(history) < n + 1:
            return None
        return history[-(n + 1)].get(field)

    rows = []
    e1, e1_cls = _format_credit_delta(eval_rate, _prev("credit_eval_rate", 1))
    e4, e4_cls = _format_credit_delta(eval_rate, _prev("credit_eval_rate", 4))
    eval_label = (
        '<a href="https://nikkei225jp.com/data/sinyou.php"'
        ' target="_blank" rel="noopener">信用評価損益率</a>'
    )
    rows.append((
        eval_label,
        '%.2f%%' % eval_rate,
        '',
        '<span class="mi-unit">1週</span> <span class="%s">%s</span>pt' % (
            html_mod.escape(e1_cls), html_mod.escape(e1)),
        '<span class="mi-unit">4週</span> <span class="%s">%s</span>pt' % (
            html_mod.escape(e4_cls), html_mod.escape(e4)),
        title,
    ))
    if bairitsu is not None:
        b1, b1_cls = _format_credit_delta(bairitsu, _prev("credit_bairitsu", 1))
        b4, b4_cls = _format_credit_delta(bairitsu, _prev("credit_bairitsu", 4))
        rows.append((
            '信用倍率',
            '%.2f' % bairitsu,
            '',
            '<span class="mi-unit">1週</span> <span class="%s">%s</span>' % (
                html_mod.escape(b1_cls), html_mod.escape(b1)),
            '<span class="mi-unit">4週</span> <span class="%s">%s</span>' % (
                html_mod.escape(b4_cls), html_mod.escape(b4)),
            title,
        ))
    return rows


def _html_market_indicators(market_db):
    """Fear & Greed と信用評価損益率 / 信用倍率を 1 つの表に統合した HTML を返す。

    取得できる行が 1 つもなければ空文字。
    """
    rows = []
    fng = _fng_row(market_db)
    if fng is not None:
        rows.append(fng)
    rows.extend(_credit_rows(market_db))
    if not rows:
        return ""

    body = []
    for label, value, rating, d1, d4, title in rows:
        title_attr = (' title="%s"' % html_mod.escape(title)) if title else ''
        body.append(
            '<tr%s>'
            '<th class="mi-label">%s</th>'
            '<td class="mi-value">%s</td>'
            '<td class="mi-rating">%s</td>'
            '<td class="mi-delta">%s</td>'
            '<td class="mi-delta">%s</td>'
            '</tr>' % (title_attr, label, value, rating, d1, d4)
        )
    return (
        '<table class="market-indicators">\n'
        '<tbody>\n' + '\n'.join(body) + '\n</tbody>\n'
        '</table>\n'
    )


_SPR_GAUGE_WIDTH = 100      # バー本体の幅 (= SPR 0〜100 のスケール)
_SPR_GAUGE_BAR_H = 14       # 1 本のバーの高さ
_SPR_GAUGE_BAR_GAP = 3      # 5日バーと20日バーの縦間隔

# 買い集め評価 A〜E による緑バー (買い側) の背景色。C が現状色 (#d4f4d4)。
# 赤バー (売り側) は濃淡を付けず固定色 (#f4d4d4)。
_SPR_GAUGE_GREEN_COLORS = {
    "A": "#5cc85c",
    "B": "#9be29b",
    "C": "#d4f4d4",
    "D": "#ecfbec",
    "E": "#f8fef8",
}
_SPR_GAUGE_GREEN_DEFAULT = _SPR_GAUGE_GREEN_COLORS["C"]
_SPR_GAUGE_RED_FIXED = "#f4c7c3"  # パレットの「薄赤 (株価マイナス)」と統一


def _to_finite_number(v):
    """None / 数値化不能 / NaN を None に正規化する。それ以外は float を返す。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _format_spr_rv_text(spr_f, rv_f, period_label=None):
    """SPR / rv 値を tooltip 表示用の文字列に整形する (バー単体 / セル全体で共通)。

    spr_f / rv_f は _to_finite_number 通過後の float | None を渡す。
    period_label を渡すと末尾に " (20日)" のような期間表記が付く。
    """
    base = "SPR %d" % int(round(spr_f))
    if rv_f is not None:
        base += " ±%.1f" % rv_f
    if period_label:
        base += " (%s)" % period_label
    return base


def _build_spr_gauge_bar_svg(spr, rv, y, period_label, bg_label=None):
    """SPR/rv 1 本分のバー要素 (rect + ティック [+ ヒゲ]) を SVG 文字列で返す。

    spr が欠損ならバー全体をスキップ ("" を返す)。rv 欠損ならヒゲ無しで描画。
    spr は 0〜100 にクリップ、ヒゲ端も 0〜100 にクリップする。
    period_label は "20日" / "5日" などの期間表示で、SVG <title> に埋め込み
    バー単体のホバーで「SPR ±rv (20日) 買い集めX」が出るようにする。
    bg_label は買い集め評価 (A〜E)。指定があれば緑バーの濃淡を 5 段階で変える
    (A=濃 / C=現状 / E=薄)。赤バーは固定色。None や未知ラベルは C 相当にフォールバック。
    """
    spr_f = _to_finite_number(spr)
    if spr_f is None:
        return ""
    spr_x = max(0.0, min(100.0, spr_f))
    rv_f = _to_finite_number(rv)
    bar_title = _format_spr_rv_text(spr_f, rv_f, period_label)
    if bg_label:
        bar_title += " 買い集め%s" % bg_label
    parts = ['<title>%s</title>' % html_mod.escape(bar_title)]
    # 背景塗り分け: 左=緑 (買い余地、濃淡=買い集め評価) / 右=赤 (売り圧、固定色)
    green = _SPR_GAUGE_GREEN_COLORS.get(bg_label, _SPR_GAUGE_GREEN_DEFAULT)
    parts.append(
        '<rect x="0" y="%d" width="%g" height="%d" fill="%s"/>' % (
            y, spr_x, _SPR_GAUGE_BAR_H, green,
        )
    )
    parts.append(
        '<rect x="%g" y="%d" width="%g" height="%d" fill="%s"/>' % (
            spr_x, y, _SPR_GAUGE_WIDTH - spr_x, _SPR_GAUGE_BAR_H, _SPR_GAUGE_RED_FIXED,
        )
    )
    # ヒゲ (± rv) を先に描いてからティックを最後に上載せ → 交差箇所でティックが消えない
    if rv_f is not None:
        whisker_lo = max(0.0, spr_x - rv_f)
        whisker_hi = min(100.0, spr_x + rv_f)
        whisker_y = y + _SPR_GAUGE_BAR_H / 2
        parts.append(
            '<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#555" stroke-width="1"/>' % (
                whisker_lo, whisker_y, whisker_hi, whisker_y,
            )
        )
    # 中央ティック (SPR 値の縦線): 太め
    parts.append(
        '<line x1="%g" y1="%d" x2="%g" y2="%d" stroke="#555" stroke-width="2"/>' % (
            spr_x, y, spr_x, y + _SPR_GAUGE_BAR_H,
        )
    )
    # 1 本のバーを <g> で包んで <title> をそのバー専用にする
    return '<g>%s</g>' % "".join(parts)


def build_spr_gauge_svg(spr_20, rv_20, spr_5, rv_5, sprw_label=None, sprbg_label=None):
    """SPR/rv の横バーゲージ (2 本縦積み、上=5日 / 下=20日) を SVG 文字列で返す。

    20日バー / 5日バーは独立に欠損判定する:
      - spr_X が欠損 → 該当バーをスキップ (もう一方は描画)
      - rv_X が欠損 → 該当バーはティック + 背景のみ (ヒゲ無し)
      - spr_20 / spr_5 両方欠損 → "—" を返す
    片側だけ欠損で両方非表示になる回帰を避けるため、内部で 2 本独立に判定する。
    sprw_label (週評価 A〜E) → 20日バーの色濃度、sprbg_label (日評価) → 5日バーの色濃度。
    None や未知ラベルは C 相当 (現状色) にフォールバック。
    """
    has_20 = _to_finite_number(spr_20) is not None
    has_5 = _to_finite_number(spr_5) is not None
    if not has_20 and not has_5:
        return "—"
    h = _SPR_GAUGE_BAR_H * 2 + _SPR_GAUGE_BAR_GAP
    bars = [
        _build_spr_gauge_bar_svg(spr_5, rv_5, 0, "5日", sprbg_label),
        _build_spr_gauge_bar_svg(spr_20, rv_20, _SPR_GAUGE_BAR_H + _SPR_GAUGE_BAR_GAP, "20日", sprw_label),
    ]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
        '%s</svg>'
    ) % (_SPR_GAUGE_WIDTH, h, _SPR_GAUGE_WIDTH, h, "".join(bars))


def build_spr_gauge_tooltip(spr_20, rv_20, spr_5, rv_5, sprw_label=None, sprbg_label=None):
    """需給バランスセルの title 属性文字列を組み立てる。

    1 行目: SPR ± rv (5日/20日) — 片側欠損なら欠損側を省略
    2 行目: 買い集め 週X 日Y — sprw_label / sprbg_label が None なら該当を省略、両方 None なら 2 行目自体を省略
    sprw_label / sprbg_label は呼び出し側で評価有無を決められる任意フィールド
    (個別銘柄の portfolio 一覧では「買い集め」列が別途あるため省略する)。
    """
    def _fmt(spr, rv, label):
        spr_f = _to_finite_number(spr)
        if spr_f is None:
            return None
        return _format_spr_rv_text(spr_f, _to_finite_number(rv), label)
    spr_parts = [s for s in (_fmt(spr_5, rv_5, "5日"), _fmt(spr_20, rv_20, "20日")) if s]
    lines = []
    if spr_parts:
        lines.append(" / ".join(spr_parts))
    bg_parts = []
    if sprw_label:
        bg_parts.append("週%s" % sprw_label)
    if sprbg_label:
        bg_parts.append("日%s" % sprbg_label)
    if bg_parts:
        lines.append("買い集め " + " ".join(bg_parts))
    return "\n".join(lines)


def _html_market(market_db):
    """市場指標セクションのHTMLを生成する

    Args:
        market_db: 市場DB
    Returns:
        str: 市場セクションのHTML文字列
    """
    import market_state

    # (DBキー, 表示名, チャートURL)
    # 日本指数は Kabutan、米国指数は Yahoo Finance US のチャートにリンク。
    markets = [
        ("topix", "TOPIX", "https://kabutan.jp/stock/chart?code=0010"),
        ("mothers", "グロース250", "https://kabutan.jp/stock/chart?code=0012"),
        ("nikkei225", "日経225", "https://kabutan.jp/stock/chart?code=0000"),
        ("nasdaq", "NASDAQ", "https://finance.yahoo.com/chart/%5EIXIC"),
        ("sp500", "S&P 500", "https://finance.yahoo.com/chart/%5EGSPC"),
    ]

    rows_html = []
    for db_name, market_name, chart_url in markets:
        if db_name not in market_db:
            continue
        try:
            db = market_db[db_name]
            # 指数向けに trend_template の RS 基準を補正してから表示文字列を作る (issue #148 Part 1)
            adjusted_db = _adjust_index_trend_template(db)
            trend_misses_list = adjusted_db.get("trend_template", [])
            trend_expr = trend_symbol_from_misses(trend_misses_list)
            trend_misses = ",".join(trend_misses_list) if isinstance(trend_misses_list, list) else ""

            # State Machine と整合した DD/FTD/状態表示
            state_meta = db.get("state_meta", {}) or {}
            state_history = db.get("state_history", []) or []
            state = db.get("market_state", "")

            # DD列: 有効DD数 / 危険水準 (例: "3 / 6"、6以上は "6+ / 6")
            # 状態でセル色を切替: pressure=黄、correction=赤
            dd_with_close = state_meta.get("distribution_days_with_close", []) or []
            dd_count = len(dd_with_close)
            dd_threshold = market_state.DD_THRESHOLD_TO_CORRECTION
            if dd_count >= dd_threshold:
                dd_display = "%d+ / %d" % (dd_threshold, dd_threshold)
            else:
                dd_display = "%d / %d" % (dd_count, dd_threshold)
            dd_dates = ", ".join([d[0][3:] for d in dd_with_close if d and len(d) >= 1])
            dd_title = ' title="%s"' % html_mod.escape(dd_dates) if dd_dates else ""
            dd_extra_cls = ""
            if dd_count >= market_state.DD_THRESHOLD_TO_CORRECTION:
                dd_extra_cls = " dd-correction"
            elif dd_count >= market_state.DD_THRESHOLD_TO_PRESSURE:
                dd_extra_cls = " dd-pressure"

            # FTD/ラリー列: correction中はラリー Day N、それ以外は直近FTD日
            if state == market_state.MARKET_IN_CORRECTION:
                rally_start = state_meta.get("rally_attempt_start_date")
                daily_history = db.get("daily_history", []) or []
                day_n = market_state.calc_rally_day(rally_start, daily_history)
                ftd_rally_display = "ラリー Day %d" % day_n if day_n else "—"
            else:
                last_ftd = state_meta.get("last_ftd_date")
                ftd_rally_display = last_ftd[3:] if last_ftd else "—"

            # 市場状態: 日本語ラベル + 遷移日
            state_label = market_state.format_state_label(state)
            trans_date = market_state.find_state_transition_date(state_history, state)
            if state_label and trans_date:
                state_display = "%s (%s〜)" % (state_label, trans_date[3:])
            else:
                state_display = state_label
            state_css = market_state.STATE_CSS_CLASS.get(state, "")

            # トレンド列: portfolio と同仕様で 10WMA乖離率の縦バー + 中央記号 + 詳細tooltip
            kairi_raw = db.get("price_kairi_wma10")
            kairi_for_gauge = kairi_raw if isinstance(kairi_raw, (int, float)) else None
            bg_cls = trend_bg_class(trend_expr)
            trend_extra_cls = " %s" % bg_cls if bg_cls else ""
            # tooltip: 不通過 (◯のみ) + 10WMA乖離率
            kairi_str = format_kairi_wma10(kairi_for_gauge) or "—"
            tooltip_lines = []
            if trend_expr == "◯" and trend_misses:
                tooltip_lines.append("不通過: " + trend_misses)
            tooltip_lines.append("10WMA乖離: " + kairi_str)
            trend_title = ' title="%s"' % html_mod.escape("\n".join(tooltip_lines))
            # 市場指数は個別銘柄より日次ボラが小さいので、ゲージのレンジと濃淡閾値を縮める
            trend_cell_inner = kairi_gauge_svg(
                kairi_for_gauge, trend_expr,
                range_pct=15.0, faint_threshold=5.0, strong_threshold=10.0,
            )

            rs_raw = db.get("rs_raw", "")
            rs_style = _rs_style(rs_raw)

            # 売り圧力レシオ + 買い集め評価 (週,日) → 需給バランスセル (issue #247)
            # 個別銘柄は price.get_spr_expr で評価しているが、指数は sell_pressure_ratio が空のため
            # spr_20 / spr_buygagher (日次) と sell_pressure_ratio_w (週次) から直接計算する。
            from price import step_func
            spr_levels = [-10, -5, 0, 5, 10]
            spr_labels = ["E", "D", "C", "B", "A"]
            spr_20 = db.get("spr_20")
            spr_5 = db.get("spr_5")
            rv_20 = db.get("rv_20")
            rv_5 = db.get("rv_5")
            # 買い集め評価 (週次): sell_pressure_ratio_w[2] - [0] のステップ評価
            sprw = db.get("sell_pressure_ratio_w")
            sprw_label = None
            if isinstance(sprw, list) and len(sprw) >= 3:
                sprw_label = step_func(sprw[2] - sprw[0], spr_levels, spr_labels)
            # 買い集め評価 (日次): spr_buygagher - spr_20 のステップ評価
            spr_bg = db.get("spr_buygagher")
            sprbg_label = None
            if spr_20 is not None and spr_bg is not None:
                sprbg_label = step_func(spr_bg - spr_20, spr_levels, spr_labels)
            gauge_svg = build_spr_gauge_svg(
                spr_20, rv_20, spr_5, rv_5, sprw_label, sprbg_label,
            )
            gauge_tooltip = build_spr_gauge_tooltip(
                spr_20, rv_20, spr_5, rv_5, sprw_label, sprbg_label,
            )
            gauge_title_attr = ' title="%s"' % html_mod.escape(gauge_tooltip) if gauge_tooltip else ""

            state_class = "col-state" + (" " + state_css if state_css else "")
            state_attr = ' class="%s"' % state_class
            rows_html.append(
                '<tr>\n'
                '  <td><strong><a href="%s" target="_blank" rel="noopener">%s</a></strong></td>\n'
                '  <td class="col-rs"%s>%s</td>\n'
                '  <td class="col-trend%s"%s>%s</td>\n'
                '  <td class="col-dd%s"%s>%s</td>\n'
                '  <td class="col-ftd">%s</td>\n'
                '  <td%s>%s</td>\n'
                '  <td class="col-spr spr-gauge-cell"%s>%s</td>\n'
                '</tr>' % (
                    html_mod.escape(chart_url), html_mod.escape(market_name),
                    rs_style, rs_raw,
                    trend_extra_cls, trend_title, trend_cell_inner,
                    dd_extra_cls, dd_title, html_mod.escape(dd_display),
                    html_mod.escape(ftd_rally_display),
                    state_attr, html_mod.escape(state_display),
                    gauge_title_attr, gauge_svg,
                )
            )
        except KeyError:
            log_warning("市場のDBデータ取得できず", db_name, market_name)

    if not rows_html:
        return ""

    indicators_html = _html_market_indicators(market_db)
    header_static = (
        '<table class="market-table">\n'
        '<thead><tr>\n'
        '  <th>市場名</th><th class="col-rs">RS</th><th class="col-trend">トレンド</th>\n'
        '  <th class="col-dd">DD</th><th class="col-ftd">FTD/ラリー</th>\n'
        '  <th class="col-state">市場状態</th><th class="col-spr">需給バランス</th>\n'
        '</tr></thead>\n'
        '<tbody>'
    )
    header = '<h2>市場</h2>\n' + indicators_html + header_static
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
        # 見出しが ASCII のみ = 日本語IRの英語版重複なので除外
        body_match = _RE_HYPERLINK.match(str(row[4]))
        heading = body_match.group(2) if body_match else str(row[4])
        if heading.isascii():
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
        market_html,   # 市場を先 (/market 表示順を「市場 → テーマランク → ニュース → 決算」に揃える)
        theme_html,
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
