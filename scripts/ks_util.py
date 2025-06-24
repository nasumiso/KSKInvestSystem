#!/usr/bin/env python3

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

# プロジェクトルートとデータパスの定義
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))  # ks_util.py の場所
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
DATA_DIR = os.path.join(ROOT_DIR, "data")

# ==================================================
# ユーティリティ
# ==================================================
UPD_CACHE = 0  # 現在DB上のデータ: フsァイルキャッシュがあれば使う
UPD_INTERVAL = (
    1  # 適度に最新なデータ: 間隔が開けば更新、開いてなければファイルキャッシュを使う
)
UPD_REEVAL = 2  # INTERVALと同じだがDBキャッシュは使わず再評価はしてほしい
UPD_FORCE = 3  # 本当に最新なデータ: 間隔に関わらず更新、開いてなくてもファイルキャッシュを使わない


def ux_cmd_head(str, line=10):
    return "\n".join(str.splitlines()[:line])


def print_dict(dict, ex_key=[]):
    """
    dictのキー要素を表示
    """
    print("----------")
    for k in list(dict.keys()):
        if k in ex_key:
            continue
        print(k, ": ", dict[k])
    print("----------")


def memoize(func):
    cache = {}

    def mamoized_function(*args):
        try:
            return cache[args]
        except KeyError:
            print("no_memo:", args)
            value = func(*args)
            cache[args] = value
            return value

    return mamoized_function


def eprint(*args, **kwargs):
    """標準エラー出力にメッセージを出力する"""
    print("ERROR:", *args, file=sys.stderr, **kwargs)


# ==================================================
# ファイルユーティリティ
# ==================================================
def backup_file(fname, day=0):
    """
    fnameのバックアップファイルを作成する
    fnameの日付が今日からday日間経過したものを作成
    """
    if not os.path.exists(fname):
        print("backup対象ファイルがありません:", fname)
        return
    stat = os.stat(fname)
    date = datetime.fromtimestamp(stat.st_mtime)
    today = datetime.today()
    delta = today - date
    print("%sは%s日前" % (fname, delta.days))
    backup_fname = "%s_%02d%02d%02d%s" % (
        os.path.splitext(fname)[0],
        date.year - 2000,
        date.month,
        date.day,
        os.path.splitext(fname)[1],
    )
    if not os.path.exists(backup_fname):
        if delta.days >= day:
            print(
                "バックアップ:%s(%d) => %s"
                % (fname, os.path.getsize(fname), backup_fname)
            )
            shutil.copy2(fname, backup_fname)
        else:
            pass
    else:
        print(backup_fname + "にバックアップ済み")
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
        print(f"実行パス設定: {original_dir} -> {path}")
        os.chdir(path)
        yield
    except Exception as e:
        print(f"Error changing directory to {path}: {e}")
        raise
    finally:
        if os.getcwd() != original_dir:
            print(f"元のパスに戻します: {original_dir}")
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
    print(f"[{threading.current_thread().name}] コンテキストセッションを開始: {token}")
    try:
        yield session  # 必要なら明示的に使えるようにもする
    finally:
        session.close()
        _current_session.reset(token)  # セッション終了＋ContextVarを元に戻す
        print(
            f"[{threading.current_thread().name}] コンテキストセッションを終了: {token}"
        )


@contextmanager
def use_requests_global_session():
    global _global_session
    session = requests.Session()
    _global_session = session
    print(f"[{threading.current_thread().name}] グローバルセッションを開始")
    try:
        yield session  # 必要ならwith文内で明示的にも使える
    finally:
        session.close()
        _global_session = None
        print(f"[{threading.current_thread().name}] グローバルセッションを終了")


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
        print(
            "  htmlをファイルキャッシュから取得します",
            Path(cache_name).relative_to(DATA_DIR),
        )
        html = file_read(cache_name)
        if with_status:
            return html, 200  # ステータスコードも成功として返す
        return html

    # ---- キャッシュがない場合は通信で取得
    print("  htmlを通信で取得します..", end=" ")
    headers = {"User-Agent": USER_AGENT_CHROME}
    # headers["Connection"] = "Keep-Alive"
    with sema:  # セマフォを使って同時実行数を制限
        try:
            # 1. グローバルセッションが有効ならそれを使う
            session = _global_session
            if session is not None:
                req_get = session.get
                print("グローバルセッションを使用")
            else:
                # 2. ContextVarセッションが有効ならそれを使う
                session = _current_session.get()
                if session is not None:
                    req_get = session.get
                    print("ContextVarセッションを使用")
                else:
                    # 3. どちらもなければrequests.getを直接使う
                    req_get = requests.get
                    print("単独セッションを使用")
            res = req_get(url, headers=headers, cookies=cookies, timeout=5)
        except requests.exceptions.ConnectionError as e:
            eprint("!!! 接続失敗")
            print(e)
            if with_status:
                return "", res.status_code if "res" in locals() else 500
            else:
                return ""
        if res.encoding != "utf-8":
            print("html_encoding:", res.encoding, "encoding:", encoding)
        # htmlをutf8で取得
        # html = r.text.encode(encoding)
        html = res.text  # python3ではエンコード済みのテキストが取得される
        # メタ指定での文字コードをutf8に
        # html = html.replace("charset=shift_jis", "charset=utf-8")

        print(
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
        if not (200 <= status_code < 300):  # HTTPステータスコードが200番台は成功
            if count >= retry:
                eprint("!!! やっぱりだめみたいなので中止", url)
                return {}
            print(f"取得エラー({status_code})のためリトライ({count+1}回目)", url)
            time.sleep(count + 1)
            # リトライ実行(キャッシュは無効化)
            html, status_code = http_get_html(
                url,
                use_cache=False,
                cache_dir=cache_dir,
                cache_fname=cache_fname,
                with_status=True,
            )
        else:  # 成功した場合はリトライループを抜け返す
            # TODO: 通信ブロック度合いによってはここで待機
            # time.sleep(random.uniform(0.1, 0.4))
            if count > 0:
                print(f"リトライ取得成功({count+1}回目): {url}")
            break
    return html


def http_post_html(url, use_cache=True, data={}, cookies={}, encoding="utf-8"):
    cache_name = "post_" + get_http_cachname(url)
    if use_cache and os.path.exists(cache_name):
        print("html(post)をファイルキャッシュから取得します", cache_name)
        html = file_read(cache_name)
        return html, ""

    headers = {"User-Agent": USER_AGENT_CHROME}
    r = requests.post(url, headers=headers, data=data, cookies=cookies)
    if r.encoding != "utf-8":
        print("encoding:", r.encoding, "encoding:", encoding)
    html = r.text.encode(encoding)
    # html = html.replace("charset=UTF-8", "charset=euc-jp")

    print("htmlをファイルキャッシュに書き込みます:", cache_name)
    file_write(cache_name, html)
    return html, r.cookies


# ==================================================
# pickleデータベースユーティリティ
# ==================================================
def save_pickle(fname, content):
    print("%sにpickleセーブ" % fname)
    with open(fname, "wb") as f:
        # 高速化のためプロトコル指定
        pickle.dump(content, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_pickle(fname):
    print("%sからpickleロード" % fname)
    try:
        f = open(fname, "rb")
        dat = pickle.load(f)
        f.close()
    except IOError as e:
        print(e)
        return {}
    return dat


memoized_load_pickle = memoize(load_pickle)  # noqa: E305


def load_file(fname, tb="r"):
    print("%sのfileロード" % fname)
    try:
        f = open(fname, tb)
        dat = f.read()
        f.close()
    except IOError as e:
        print(e)
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
            print("!!!strコードがないためintから取得:", code)  # いつかなくなるはず
            return format(code, "04d")
        else:
            print("!!!コード取得できない", rec)
            return ""
    return rec["code_s"]
