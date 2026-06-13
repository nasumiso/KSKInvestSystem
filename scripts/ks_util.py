#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# このリポジトリのコーディング/コメント規約:
# - コードコメント、docstring は日本語で記述してください（簡潔に分かりやすく）。
# - 技術用語・関数/変数名・外部ライブラリ名などは英語のままで問題ありません。
# - 変更理由や注意点は短い箇条書きで記述してください。

import operator
import os
import os.path
from pathlib import Path
import sys
import shutil
import subprocess
from datetime import datetime, timedelta
import pickle
import contextvars
from contextlib import contextmanager
import threading
import time

import requests  # 外部ライブラリ
from functools import reduce
import logging
import logging.handlers

# プロジェクトルートとデータパスの定義
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))  # ks_util.py の場所
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))


def _resolve_data_dir(fallback_root: str) -> str:
    """
    DATA_DIR を解決する。優先順位:
    1. 環境変数 KS_DATA_DIR（data/ を別の場所に移動した場合に使用）
    2. git rev-parse --git-common-dir でメインリポジトリの data/ を検出
       （ワークツリーからメインの data/ を自動参照）
    3. フォールバック: fallback_root/data（従来通り）
    """
    # 1. 環境変数で明示指定
    env = os.environ.get("KS_DATA_DIR")
    if env:
        return os.path.abspath(env)
    # 2. Git commondir でメインリポジトリを検出（ワークツリー対応）
    #    ワークツリーでは --git-common-dir がメインの .git/ の絶対パスを返す
    #    通常リポジトリでは ".git" （相対パス）を返す
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=fallback_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        git_common = result.stdout.strip() if result.returncode == 0 else ""
        if git_common:
            if not os.path.isabs(git_common):
                git_common = os.path.join(fallback_root, git_common)
            git_common = os.path.abspath(git_common)
            # .git/ の親ディレクトリ = メインリポジトリルート
            candidate = os.path.join(os.path.dirname(git_common), "data")
            if os.path.isdir(candidate):
                return candidate
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    # 3. フォールバック
    return os.path.join(fallback_root, "data")


DATA_DIR = _resolve_data_dir(ROOT_DIR)
LOGS_DIR = os.path.join(ROOT_DIR, "logs")

# theme-news 生成データのルート ($KS_DATA_DIR/theme_news/)。
# コードと生成データの責任分離のため、履歴・カレンダー素材は DATA_DIR 配下に置く
# (issue #279)。code_rank_data/ と同じく DATA_DIR 起点で解決する。
THEME_NEWS_DIR = os.path.join(DATA_DIR, "theme_news")
THEME_NEWS_HISTORY_DIR = os.path.join(THEME_NEWS_DIR, "history")
THEME_NEWS_EVENTS_JSON = os.path.join(THEME_NEWS_DIR, "events.json")
THEME_NEWS_CALENDAR_HTML = os.path.join(THEME_NEWS_DIR, "calendar.html")
THEME_NEWS_CALENDAR_ARCHIVE = os.path.join(THEME_NEWS_DIR, "calendar-archive.md")

# ==================================================
# ユーティリティ
# ==================================================
UPD_CACHE = 0  # 現在DB上のデータ: ファイルキャッシュがあれば使う
UPD_INTERVAL = (
    1  # 適度に最新なデータ: 間隔が開けば更新、開いてなければファイルキャッシュを使う
)
UPD_REEVAL = 2  # INTERVALと同じだがDBキャッシュは使わず再評価はしてほしい
UPD_FORCE = 3  # 本当に最新なデータ: 間隔に関わらず更新、開いてなくてもファイルキャッシュを使わない


# ==================================================
# ログ管理システム
# ==================================================
_logger = None


def setup_logger(script_name=None):
    """
    アプリケーション用のロガーを設定する

    Args:
        script_name: スクリプト名（ログファイル名の一部に使用）

    Returns:
        logger: 設定されたloggerオブジェクト
    """
    global _logger

    if _logger is not None:
        return _logger

    # logsディレクトリの作成
    os.makedirs(LOGS_DIR, exist_ok=True)

    # ロガーの作成
    logger_name = script_name or "ks_invest_system"
    _logger = logging.getLogger(logger_name)
    _logger.setLevel(logging.DEBUG)

    # すでにハンドラーが設定されている場合はスキップ
    if _logger.handlers:
        return _logger

    # フォーマッターの作成
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",  # - %(name)s
        datefmt="%m/%d %H:%M:%S",  # %Y-%m-%d
    )

    # 日付ベースのファイル名でローテーティングファイルハンドラーを作成
    # today = datetime.now().strftime("%Y%m%d")
    log_filename = os.path.join(LOGS_DIR, f"{logger_name}.log")

    # 7日分のログを保持するローテーティングハンドラー
    # 追記になることに注意
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_filename,
        when="D",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    # 環境変数 KS_LOG_DEBUG=1 でDEBUGレベルに変更可能
    if os.environ.get("KS_LOG_DEBUG") == "1":
        file_handler.setLevel(logging.DEBUG)
    else:
        file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # ハンドラーをロガーに追加
    _logger.addHandler(file_handler)

    # コンソールハンドラー（常に追加。cron時はstdoutがファイルにリダイレクトされる）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    _logger.addHandler(console_handler)

    return _logger


def get_logger():
    """
    設定済みのロガーを取得する。未設定の場合は自動で設定する。
    """
    if _logger is None:
        return setup_logger()
    return _logger


def log_print(*args, **kwargs):
    """
    print文の代替関数。常にINFOレベルでログ出力する。

    Args:
        *args: print関数と同じ引数
        **kwargs: print関数と同じキーワード引数（ただし、fileは無視される）
    """
    logger = get_logger()

    # 引数を文字列に変換してメッセージを構築
    message_parts = []
    for arg in args:
        message_parts.append(str(arg))

    message = kwargs.get("sep", " ").join(message_parts)
    logger.info(message)


def log_warning(*args, **kwargs):
    """
    WARNING レベルでログ出力する関数。

    Args:
        *args: print関数と同じ引数
        **kwargs: print関数と同じキーワード引数（ただし、fileは無視される）
    """
    logger = get_logger()

    # 引数を文字列に変換してメッセージを構築
    message_parts = []
    for arg in args:
        message_parts.append(str(arg))

    message = kwargs.get("sep", " ").join(message_parts)
    logger.warning(message)


def log_error(*args, **kwargs):
    """
    ERROR レベルでログ出力する関数。

    Args:
        *args: print関数と同じ引数
        **kwargs: print関数と同じキーワード引数（ただし、fileは無視される）
    """
    logger = get_logger()

    # 引数を文字列に変換してメッセージを構築
    message_parts = []
    for arg in args:
        message_parts.append(str(arg))

    message = kwargs.get("sep", " ").join(message_parts)
    logger.error(message)


def log_debug(*args, **kwargs):
    """
    DEBUG レベルでログ出力する関数。

    Args:
        *args: print関数と同じ引数
        **kwargs: print関数と同じキーワード引数（ただし、fileは無視される）
    """
    logger = get_logger()

    # 引数を文字列に変換してメッセージを構築
    message_parts = []
    for arg in args:
        message_parts.append(str(arg))

    message = kwargs.get("sep", " ").join(message_parts)
    logger.debug(message)


# 下位互換性のためのエイリアス
def smart_print(*args, **kwargs):
    """log_printのエイリアス（下位互換性のため）"""
    log_print(*args, **kwargs)


def ux_cmd_head(str, line=10):
    return "\n".join(str.splitlines()[:line])


def print_dict(dict, ex_key=[]):
    """
    dictのキー要素を表示（デバッグレベル）
    """
    log_debug("----------")
    for k in list(dict.keys()):
        if k in ex_key:
            continue
        log_debug(k, ": ", dict[k])
    log_debug("----------")


def memoize(func):
    cache = {}

    def mamoized_function(*args):
        try:
            return cache[args]
        except KeyError:
            log_debug("no_memo:", args)
            value = func(*args)
            cache[args] = value
            return value

    return mamoized_function


def eprint(*args, **kwargs):
    """標準エラー出力にメッセージを出力する"""
    log_warning("ERROR:", *args)


# ==================================================
# ファイルユーティリティ
# ==================================================
def backup_file(fname, day=0):
    """
    fnameのバックアップファイルを作成する
    fnameの日付が今日からday日間経過したものを作成
    """
    if not os.path.exists(fname):
        log_print("backup対象ファイルがありません:", fname)
        return
    stat = os.stat(fname)
    date = datetime.fromtimestamp(stat.st_mtime)
    today = datetime.today()
    delta = today - date
    log_print("%sは%s日前" % (fname, delta.days))
    backup_fname = "%s_%02d%02d%02d%s" % (
        os.path.splitext(fname)[0],
        date.year - 2000,
        date.month,
        date.day,
        os.path.splitext(fname)[1],
    )
    if not os.path.exists(backup_fname):
        if delta.days >= day:
            log_print(
                "バックアップ:%s(%d) => %s"
                % (fname, os.path.getsize(fname), backup_fname)
            )
            shutil.copy2(fname, backup_fname)
        else:
            pass
    else:
        log_print(backup_fname + "にバックアップ済み")
    return backup_fname


def file_write(fname, content):
    # issue #56: アトミック書き込み。
    # ThreadPoolExecutor 配下で同じキャッシュファイルが並行 read される可能性があり、
    # open("w") の即時 truncate と書き込み完了の間に読まれると空文字列が返るため、
    # 一時ファイルに書いてから os.replace でアトミックに差し替える。
    # 同 PID 内の並行スレッドで衝突しないよう thread id も含める。
    tmp_path = "%s.tmp.%d.%d" % (fname, os.getpid(), threading.get_ident())
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, fname)


def file_read(fname):
    f = open(fname, "r")  # python3では'b'をつけバイナリのまま読み込み?
    content = f.read()
    f.close()
    return content


def get_file_datetime(fname):
    """
    ファイルfnameの日付datetimeを返す
    str -> datetime
    """

    stat = os.stat(fname)
    return datetime.fromtimestamp(stat.st_mtime)


@contextmanager
def suppress_stdout():
    """
    標準出力を抑制するコンテキストマネージャ
    """
    original_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        yield
    finally:
        sys.stdout = original_stdout


@contextmanager
def chdir(path):
    """
    コンテキストマネージャで指定したパスに一時的に移動し、処理終了後または例外発生時に元の作業ディレクトリに戻ります。
    Args:
        :param path: 移動先のパス
    """
    original_dir = os.getcwd()
    try:
        log_debug(f"(chdir)実行パス設定: {original_dir} -> {path}")
        os.chdir(path)
        yield
    except Exception as e:
        log_debug(f"Error changing directory to {path}: {e}")
        raise
    finally:
        if os.getcwd() != original_dir:
            log_debug(f"(chdir)元のパスに戻します: {original_dir}")
            os.chdir(original_dir)


PRICE_HOUR = 17  # これ以前は前日、これ以降は当日


def get_price_day(dt):
    """
    営業日ベースの日付を返す
    17時以前の価格は前日を使う
    daettime -> datetime
    """
    need_dt = dt
    if dt.hour < PRICE_HOUR:
        need_dt = dt - timedelta(1)
    return need_dt.date()


# ==================================================
# 計算ユーティリティ
# ==================================================
def sumproduct(*lists):
    return sum(reduce(operator.mul, data) for data in zip(*lists))


def average(lst):
    """平均を求める
    Args:
        平均を求めたいリスト
    Returns:
        float 平均値
    """
    return reduce(lambda x, y: x + y, lst) / float(len(lst))


def cramp(x, low, high):
    return max(low, min(x, high))


def step_func(val, xs, ys, min_val=None):
    """
    # step_func(shiyo["PER"], [0, 30, 60], [PER_MAX, PER_MAX/2, 0])
    """
    if min_val is None:
        val_y = ys[0]
    else:
        val_y = min_val
    for x, y in reversed(list(zip(xs, ys))):
        if val > x:
            val_y = y
            break
    return val_y


# ==================================================
# httpユーティリティ
# ==================================================
USER_AGENT_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_4) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/36.0.1985.125 Safari/537.36"
)
# スレッドごとに独立したSessionを持たせるためのコンテキスト変数
# (issue #43): requests.Session はスレッドセーフではないため、複数スレッドで
# 共有するとレースコンディションが発生する。マルチスレッド呼び出し側は
# 各ワーカー内で use_requests_session() を呼んで個別 Session を持たせること。
_current_session = contextvars.ContextVar("current_requests_session", default=None)

MAX_REQUESTS = 3  # セマフォによる同時実行数の制限
sema = threading.Semaphore(MAX_REQUESTS)


@contextmanager
def use_requests_session():
    """スレッドごとにSessionをセットするコンテキストマネージャ。
    (issue #43): マルチスレッド呼び出しでは各ワーカー内でこの関数を呼ぶこと。
    ThreadPoolExecutor の各ワーカーは別 ContextVar スコープを持つため、
    Session はワーカーごとに独立して生成され、スレッドセーフ問題は起きない。
    """
    session = requests.Session()
    token = _current_session.set(session)  # 現在のセッションを設定
    log_debug(
        f"[{threading.current_thread().name}] コンテキストセッションを開始: {token}"
    )
    try:
        yield session  # 必要なら明示的に使えるようにもする
    finally:
        session.close()
        _current_session.reset(token)  # セッション終了＋ContextVarを元に戻す
        log_debug(
            f"[{threading.current_thread().name}] コンテキストセッションを終了: {token}"
        )


def get_http_cachname(url):
    """
    urlからデフォルトのキャッシュファイルの場所を返す
    """
    return url.replace("http://", "").replace(".", "").replace("/", "_") + ".html"


def http_get_html(
    url,
    use_cache=True,
    cache_dir="",
    cache_fname="",
    cookies={},
    encoding="utf-8",
    with_status=False,
):
    """
    指定urlをリクエストしエンコード済みテキストを返します。
    TODO: use_cacheを有効期限期日指定したほうがスッキリするはず
    Args:
        url (str): 取得するURL
        use_cache (bool): キャッシュファイルを使用するかどうか
        cache_dir (str): キャッシュディレクトリのパス
        cache_fname (str): キャッシュファイル名（指定がなければURLから生成）
        cookies (dict): リクエストに使用するクッキー
        encoding (str): 取得するHTMLのエンコーディング
        with_status (bool): ステータスコードを返すかどうか
    Returns:
        str or tuple: 取得したHTMLのテキスト（with_statusがTrueの場合はタプルでステータスコードも返す）
    """
    # 指定されていなければurlからキャッシュファイル名を取得
    cache_name = get_http_cachname(url) if not cache_fname else cache_fname
    # 指定されていればキャッシュディレクトリからパス取得
    if cache_dir:
        cache_name = os.path.join(cache_dir, cache_name)
    if use_cache and os.path.exists(cache_name):
        log_debug(
            "  htmlをファイルキャッシュから取得します",
            Path(cache_name).relative_to(DATA_DIR),
        )
        html = file_read(cache_name)
        if with_status:
            return html, 200  # ステータスコードも成功として返す
        return html

    # ---- キャッシュがない場合は通信で取得
    log_debug("  htmlを通信で取得します..")
    headers = {"User-Agent": USER_AGENT_CHROME}
    # headers["Connection"] = "Keep-Alive"
    with sema:  # セマフォを使って同時実行数を制限
        try:
            # ContextVarセッションが有効ならそれを使う、なければrequests.getを直接使う
            # (issue #43): _global_session は requests.Session のスレッドセーフ問題を
            # 引き起こすため廃止。マルチスレッド呼び出し側は各ワーカーで
            # use_requests_session() を呼んで ContextVar 経由の独立 Session を使うこと。
            session = _current_session.get()
            if session is not None:
                req_get = session.get
                log_debug("ContextVarセッションを使用")
            else:
                req_get = requests.get
                log_debug("単独セッションを使用")
            res = req_get(url, headers=headers, cookies=cookies, timeout=5)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            log_warning("接続失敗:")
            log_print(e)
            # res は req_get(...) 完了前の例外なので未定義。常に 500 を返す。
            if with_status:
                return "", 500
            else:
                return ""
        # requests.exceptions.ReadTimeout TODO:
        if res.encoding != "utf-8":
            log_debug("html_encoding:", res.encoding, "encoding:", encoding)
        # htmlをutf8で取得(python3ではエンコード済みのテキストが取得される)
        html = res.text
        # メタ指定での文字コードをutf8に
        # html = html.replace("charset=shift_jis", "charset=utf-8")

        # 成功レスポンスのみキャッシュに書き込む（エラーレスポンスのキャッシュ汚染を防止）
        if 200 <= res.status_code < 300:
            log_debug(
                "  取得したhtmlをファイルキャッシュに書き込みます:",
                Path(cache_name).relative_to(DATA_DIR),
            )
            file_write(cache_name, html)
        else:
            log_warning(
                "  HTTPエラー(%d)のためキャッシュに書き込みません: %s"
                % (res.status_code, url)
            )
        if with_status:
            # ステータスコードも返す
            return html, res.status_code
        else:
            # ステータスコードなしでHTMLのみ返す
            return html


def http_get_html_with_retry(url, use_cach, cache_dir="", cache_fname="", retry=3):
    """リトライ付きのHTML取得関数"""
    html, status_code = http_get_html(
        url,
        use_cache=use_cach,
        cache_dir=cache_dir,
        cache_fname=cache_fname,
        with_status=True,
    )
    # 取得に失敗した場合はリトライ
    for count in range(retry + 1):
        # if "Service Temporarily Unavailable" in html:
        success = 200 <= status_code < 300  # HTTPステータスコードが200番台は成功
        if success:
            # 成功した場合はリトライループを抜け返す
            # 通信ブロック度合いによってはここで待機
            # time.sleep(random.uniform(0.1, 0.4))
            if count > 0:
                log_print(f"リトライ取得成功({count+1}回目): {url}")
            break
        else:
            if status_code in (400, 401, 403, 404, 405, 410):
                log_warning(f"リトライ不要なエラー({status_code})のため中止: {url}")
                return {}
            if count >= retry:
                log_warning("リトライしても通信できないので中止", url)
                return {}
            log_print(f"取得エラー({status_code})のためリトライ({count+1}回目)", url)
            time.sleep(count + 1)
            # リトライ実行(キャッシュは無効化)
            html, status_code = http_get_html(
                url,
                use_cache=False,
                cache_dir=cache_dir,
                cache_fname=cache_fname,
                with_status=True,
            )
    return html


def http_post_html(url, use_cache=True, data={}, cookies={}, encoding="utf-8"):
    cache_name = "post_" + get_http_cachname(url)
    if use_cache and os.path.exists(cache_name):
        log_debug("html(post)をファイルキャッシュから取得します", cache_name)
        html = file_read(cache_name)
        return html, ""

    headers = {"User-Agent": USER_AGENT_CHROME}
    r = requests.post(url, headers=headers, data=data, cookies=cookies)
    if r.encoding != "utf-8":
        log_debug("encoding:", r.encoding, "encoding:", encoding)
    html = r.text.encode(encoding)
    # html = html.replace("charset=UTF-8", "charset=euc-jp")

    log_debug("htmlをファイルキャッシュに書き込みます:", cache_name)
    file_write(cache_name, html)
    return html, r.cookies


# ==================================================
# pickleデータベースユーティリティ
# ==================================================
def save_pickle(fname, content):
    log_debug("%sにpickleセーブ" % fname)
    with open(fname, "wb") as f:
        # 高速化のためプロトコル指定
        pickle.dump(content, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_pickle(fname):
    log_debug("%sからpickleロード" % fname)
    try:
        f = open(fname, "rb")
        dat = pickle.load(f)
        f.close()
    except IOError as e:
        log_print(e)
        return {}
    return dat


memoized_load_pickle = memoize(load_pickle)  # noqa: E305


def load_file(fname, tb="r"):
    log_debug("%sのfileロード" % fname)
    try:
        f = open(fname, tb)
        dat = f.read()
        f.close()
    except IOError as e:
        log_print(e)
        return ""
    return dat


memoized_load_file = memoize(load_file)  # noqa: E305


def set_db_code(rec, code):
    # type: (dict, str) -> None
    rec["code_s"] = str(code)


def get_db_code(rec: dict) -> str:
    if "code_s" not in rec:
        if "code" in rec:
            code = rec["code"]
            log_warning("strコードがないためintから取得:", code)  # いつかなくなるはず
            return format(code, "04d")
        else:
            log_warning("コード取得できない", rec)
            return ""
    return rec["code_s"]


# 記号→背景色クラス。webapp 一覧 (compute_cell_styles) の色仕様と統一:
# ◎=濃黄, ◯=薄黄, ×(全条件miss=Stage4崩壊)=青, —(未評価/欠損)=赤。
_TREND_BG_CLASS = {
    "◎": "trend-bg-strong", "◯": "trend-bg-good",
    "×": "trend-bg-collapse", "—": "trend-bg-missing",
}

# trend_template (Minervini) の全条件数。price.calc_trend_template が不通過項目を
# misses として返すため、miss 数がこれと等しい = 1つも条件を満たさない完全崩壊
# (Stage 4)。make_stock_db._TREND_TEMPLATE_ALL (条件名の集合) と件数を揃える。
TREND_TEMPLATE_CONDITION_COUNT = 7


def trend_symbol_from_misses(misses):
    """trend_template (不通過項目 list) から表示用の記号一文字を返す。

    make_stock_db.get_trend_template_expr() と判定基準を揃える (◎/◯/▲/△)。
    全条件 miss (完全 Stage 4 崩壊) は "×"、None や非 list (未評価/データ欠損) は "—"。
    両者は意味が異なるため記号で区別する (背景色も別: × → 青, — → 赤)。
    """
    if not isinstance(misses, list):
        return "—"
    n = len(misses)
    if n == 0:
        return "◎"
    if n <= 2:
        return "◯"
    if n <= 4:
        return "▲"
    if n < TREND_TEMPLATE_CONDITION_COUNT:
        return "△"
    return "×"


def trend_bg_class(symbol):
    """トレンド記号 (◎/◯/×/—) から背景色 CSS クラス名を返す。▲/△/その他は ""。"""
    return _TREND_BG_CLASS.get(symbol, "")


def format_kairi_wma10(kairi):
    """10WMA乖離率 (%) を "+12%" / "-3%" 形式で整形。None や数値化不能なら "" を返す。"""
    if kairi is None:
        return ""
    try:
        return "%+d%%" % int(round(float(kairi)))
    except (TypeError, ValueError):
        return ""


# overheat 閾値と色は kairi_wma10_zone_class() の `kairi-zone-overheat` 境界と一致させる
_KAIRI_GAUGE_RANGE = 25.0
# マーカー縦線色: |kairi| < faint 黒 (中立) / < strong 淡色 / ≥ strong 濃色
# 個別銘柄の既定は (range=25, faint=10, strong=20)。市場用は (15, 5, 10) に縮める。
_KAIRI_GAUGE_FAINT_THRESHOLD = 10.0
_KAIRI_GAUGE_STRONG_THRESHOLD = 20.0
_KAIRI_GAUGE_NEUTRAL_COLOR = "#000"
_KAIRI_GAUGE_POS_FAINT = "#9be29b"
_KAIRI_GAUGE_POS_STRONG = "#2e7d32"
_KAIRI_GAUGE_NEG_FAINT = "#f4c7c3"
_KAIRI_GAUGE_NEG_STRONG = "#c62828"


def _kairi_gauge_marker_color(kairi_pct: float,
                              faint_threshold: float = _KAIRI_GAUGE_FAINT_THRESHOLD,
                              strong_threshold: float = _KAIRI_GAUGE_STRONG_THRESHOLD) -> str:
    """10WMA乖離率の符号と大きさからマーカー縦線色を返す。"""
    abs_k = abs(kairi_pct)
    if abs_k < faint_threshold:
        return _KAIRI_GAUGE_NEUTRAL_COLOR
    if kairi_pct > 0:
        return _KAIRI_GAUGE_POS_FAINT if abs_k < strong_threshold else _KAIRI_GAUGE_POS_STRONG
    return _KAIRI_GAUGE_NEG_FAINT if abs_k < strong_threshold else _KAIRI_GAUGE_NEG_STRONG


def kairi_gauge_svg(kairi, symbol,
                    range_pct: float = _KAIRI_GAUGE_RANGE,
                    faint_threshold: float = _KAIRI_GAUGE_FAINT_THRESHOLD,
                    strong_threshold: float = _KAIRI_GAUGE_STRONG_THRESHOLD,
                    kairi_ma10=None,
                    ma10_streak: bool = False):
    """10WMA(=50日MA)乖離率を -range_pct〜+range_pct の縦線マーカーで描画する SVG を返す。

    kairi >= +range_pct はクランプ + strong 色のマーカー。
    kairi が None で symbol も空なら "" を返す。

    kairi_ma10 を渡すと10日MA乖離を黒の点線で重ねて描く。
    ma10_streak=True (保持データ内に30日連続10ma上回り期間あり = 利確基準有効)
    のときは赤の太点線に切替える。マーカーの横位置が現在の10ma乖離なので、
    中央より左 (株価が10maを割った) かどうかも位置で読み取れる。
    """
    width = 40
    height = 28
    if kairi is None and not symbol:
        return ""

    px_per_pct = width / (range_pct * 2)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" style="vertical-align:middle;">' % (width, height, width, height)]

    if kairi is not None:
        try:
            k = float(kairi)
        except (TypeError, ValueError):
            k = None
        if k is not None:
            k_clamped = max(-range_pct, min(range_pct, k))
            mx = (k_clamped + range_pct) * px_per_pct
            # 端でクリップされてマーカーが半分の太さに見えないよう、両端は 1px 内側に寄せる
            mx = max(1.0, min(width - 1.0, mx))
            marker_color = _kairi_gauge_marker_color(k, faint_threshold, strong_threshold)
            # 緑系 (株価プラス) は淡色で細く見えるため少し太くする
            is_green = marker_color in (_KAIRI_GAUGE_POS_FAINT, _KAIRI_GAUGE_POS_STRONG)
            marker_w = 3 if is_green else 2
            halo_w = marker_w + 1
            parts.append('<line x1="%g" y1="0" x2="%g" y2="%d" stroke="white" stroke-width="%d"/>' % (mx, mx, height, halo_w))
            parts.append('<line x1="%g" y1="0" x2="%g" y2="%d" stroke="%s" stroke-width="%d"/>' % (mx, mx, height, marker_color, marker_w))

    # 10日MA乖離マーカー (通常は黒の点線。30日連続10ma上回りなら赤の太実線)
    if kairi_ma10 is not None:
        try:
            k10 = float(kairi_ma10)
        except (TypeError, ValueError):
            k10 = None
        if k10 is not None:
            k10_clamped = max(-range_pct, min(range_pct, k10))
            mx10 = (k10_clamped + range_pct) * px_per_pct
            mx10 = max(1.0, min(width - 1.0, mx10))
            if ma10_streak:
                # 利確基準有効: 赤の太点線 + white halo (視認性確保)
                parts.append('<line x1="%g" y1="0" x2="%g" y2="%d" stroke="white" stroke-width="4" stroke-dasharray="3,2"/>' % (mx10, mx10, height))
                parts.append('<line x1="%g" y1="0" x2="%g" y2="%d" stroke="%s" stroke-width="3" stroke-dasharray="3,2"/>' % (mx10, mx10, height, _KAIRI_GAUGE_NEG_STRONG))
            else:
                parts.append('<line x1="%g" y1="0" x2="%g" y2="%d" stroke="#000" stroke-width="1" stroke-dasharray="2,2"/>' % (mx10, mx10, height))

    if symbol:
        parts.append('<text x="50%%" y="50%%" text-anchor="middle" dominant-baseline="central" font-size="12" font-weight="bold" fill="#000" stroke="white" stroke-width="3" paint-order="stroke">%s</text>' % symbol)

    parts.append('</svg>')
    return "".join(parts)
