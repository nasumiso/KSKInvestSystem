#!/usr/bin/env python3

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
DATA_DIR = os.path.join(ROOT_DIR, "data")
LOGS_DIR = os.path.join(ROOT_DIR, "logs")

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
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # ハンドラーをロガーに追加
    _logger.addHandler(file_handler)

    # コンソールハンドラー（ターミナルからの実行のみ）
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        # console_handler.setFormatter(formatter)  # コンソールはデフォルトフォーマッターで
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
    dictのキー要素を表示
    """
    log_print("----------")
    for k in list(dict.keys()):
        if k in ex_key:
            continue
        log_print(k, ": ", dict[k])
    log_print("----------")


def memoize(func):
    cache = {}

    def mamoized_function(*args):
        try:
            return cache[args]
        except KeyError:
            log_print("no_memo:", args)
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
    f = open(fname, "w")  # python3では'b'をつけバイナリのまま保存?
    f.write(content)
    f.close()


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
        log_print(f"(chdir)実行パス設定: {original_dir} -> {path}")
        os.chdir(path)
        yield
    except Exception as e:
        log_print(f"Error changing directory to {path}: {e}")
        raise
    finally:
        if os.getcwd() != original_dir:
            log_print(f"(chdir)元のパスに戻します: {original_dir}")
            os.chdir(original_dir)


PRICE_HOUR = 18  # これ以前は前日、これ以降は当日


def get_price_day(dt):
    """
    営業日ベースの日付を返す
    18時以前の価格は前日を使う
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
# スレッドローカル(スレッド間で共有されない)なセッションコンテキスト変数
# つまり、マルチスレッドでは使い回せない
_current_session = contextvars.ContextVar("current_requests_session", default=None)
# グローバルなセッション変数
# 本来はhttp_get_htmlの引数にセッションを渡せるようにすべき
_global_session = None

MAX_REQUESTS = 3  # セマフォによる同時実行数の制限
sema = threading.Semaphore(MAX_REQUESTS)


@contextmanager
def use_requests_session():
    """スレッドごとにSessionをセットするコンテキストマネージャ"""
    session = requests.Session()
    token = _current_session.set(session)  # 現在のセッションを設定
    log_print(
        f"[{threading.current_thread().name}] コンテキストセッションを開始: {token}"
    )
    try:
        yield session  # 必要なら明示的に使えるようにもする
    finally:
        session.close()
        _current_session.reset(token)  # セッション終了＋ContextVarを元に戻す
        log_print(
            f"[{threading.current_thread().name}] コンテキストセッションを終了: {token}"
        )


@contextmanager
def use_requests_global_session():
    global _global_session
    session = requests.Session()
    _global_session = session
    log_print(f"[{threading.current_thread().name}] グローバルセッションを開始")
    try:
        yield session  # 必要ならwith文内で明示的にも使える
    finally:
        session.close()
        _global_session = None
        log_print(f"[{threading.current_thread().name}] グローバルセッションを終了")


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
        log_print(
            "  htmlをファイルキャッシュから取得します",
            Path(cache_name).relative_to(DATA_DIR),
        )
        html = file_read(cache_name)
        if with_status:
            return html, 200  # ステータスコードも成功として返す
        return html

    # ---- キャッシュがない場合は通信で取得
    log_print("  htmlを通信で取得します..")
    headers = {"User-Agent": USER_AGENT_CHROME}
    # headers["Connection"] = "Keep-Alive"
    with sema:  # セマフォを使って同時実行数を制限
        try:
            # 1. グローバルセッションが有効ならそれを使う
            session = _global_session
            if session is not None:
                req_get = session.get
                log_print("グローバルセッションを使用")
            else:
                # 2. ContextVarセッションが有効ならそれを使う
                session = _current_session.get()
                if session is not None:
                    req_get = session.get
                    log_print("ContextVarセッションを使用")
                else:
                    # 3. どちらもなければrequests.getを直接使う
                    req_get = requests.get
                    log_print("単独セッションを使用")
            res = req_get(url, headers=headers, cookies=cookies, timeout=5)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            log_warning("接続失敗:")
            log_print(e)
            if with_status:
                return "", res.status_code if "res" in locals() else 500
            else:
                return ""
        # requests.exceptions.ReadTimeout TODO:
        if res.encoding != "utf-8":
            log_print("html_encoding:", res.encoding, "encoding:", encoding)
        # htmlをutf8で取得(python3ではエンコード済みのテキストが取得される)
        html = res.text
        # メタ指定での文字コードをutf8に
        # html = html.replace("charset=shift_jis", "charset=utf-8")

        log_print(
            "  取得したhtmlをファイルキャッシュに書き込みます:",
            Path(cache_name).relative_to(DATA_DIR),
        )
        file_write(cache_name, html)
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
        log_print("html(post)をファイルキャッシュから取得します", cache_name)
        html = file_read(cache_name)
        return html, ""

    headers = {"User-Agent": USER_AGENT_CHROME}
    r = requests.post(url, headers=headers, data=data, cookies=cookies)
    if r.encoding != "utf-8":
        log_print("encoding:", r.encoding, "encoding:", encoding)
    html = r.text.encode(encoding)
    # html = html.replace("charset=UTF-8", "charset=euc-jp")

    log_print("htmlをファイルキャッシュに書き込みます:", cache_name)
    file_write(cache_name, html)
    return html, r.cookies


# ==================================================
# pickleデータベースユーティリティ
# ==================================================
def save_pickle(fname, content):
    log_print("%sにpickleセーブ" % fname)
    with open(fname, "wb") as f:
        # 高速化のためプロトコル指定
        pickle.dump(content, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_pickle(fname):
    log_print("%sからpickleロード" % fname)
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
    log_print("%sのfileロード" % fname)
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
