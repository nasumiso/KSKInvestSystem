#!/usr/bin/env python3

from datetime import datetime, timedelta
import re

import portfolio
from ks_util import *

DISCLOSURE_DIR = os.path.join(DATA_DIR, "disclosure")
DISCLOSURE_CACHE_DIR = os.path.join(DISCLOSURE_DIR, "cache")
os.makedirs(DISCLOSURE_CACHE_DIR, exist_ok=True)
DISCLOSURE_URL = "https://kabutan.jp/stock/news?code=%s"
DISCLOSURE_CSV = os.path.join(DISCLOSURE_DIR, "disclosure_db.csv")

UPD_INTERVAL = 0
UPD_CACHE = 1  # html取得できていればキャッシュから
UPD_FORCE = 2  # html取得から強制

HEAD_TYPE_DIC = {
    # 新HTML形式（2026年3月〜）: <div class="newslist_ctg newsctgXX_b">
    "newsctg5_b": "special",     # 特集
    "newsctg3_kk_b": "modify",   # 決算・修正
    "newsctg3_ks_b": "modify",   # 決算・修正
    "newsctg12_b": "5per",       # 5%
    "newsctg9_b": "kessan",      # 注目/決算
    "newsctg1_b": "zairyo",      # 市況 → 材料扱い
    "newsctg13_b": "zairyo",     # 業界 → 材料扱い
    "newsctg4_b": "zairyo",      # テク → 材料扱い
    # 旧HTML形式（キャッシュ互換）: <td class="ctgXX">
    "ctg5": "special",
    "ctg3_kk": "modify",
    "ctg3_ks": "modify",
    "ctg12": "5per",
    "ctg9": "kessan",
}

DISCLOSURE_IMPACT_RULES = [
    {
        "kind": "downward",
        "label": "下方",
        "tone": "negative",
        "strength": "strong",
        "keywords": ("下方修正",),
    },
    {
        "kind": "upward",
        "label": "上方",
        "tone": "positive",
        "strength": "strong",
        "keywords": ("上方修正",),
    },
    {
        "kind": "profit_high",
        "label": "最高益",
        "tone": "positive",
        "strength": "weak",
        "keywords": ("最高益",),
    },
    {
        "kind": "dividend_positive",
        "label": "増配",
        "tone": "positive",
        "strength": "weak",
        "keywords": ("増額修正", "増配", "復配"),
    },
    {
        "kind": "dividend_negative",
        "label": "減配",
        "tone": "negative",
        "strength": "weak",
        "keywords": ("減配", "減額修正", "無配"),
    },
]


def classify_disclosure_impact(heading):
    """開示見出しから株価インパクトの大きいキーワードを分類する。

    株探のカテゴリタグではなく見出し本文だけを見る。増益/減益の着地見出しは
    上方/下方修正ではないため、明示キーワードが無い限り分類しない。
    """
    heading = heading or ""
    for rule in DISCLOSURE_IMPACT_RULES:
        if any(keyword in heading for keyword in rule["keywords"]):
            result = {
                "kind": rule["kind"],
                "label": rule["label"],
                "tone": rule["tone"],
                "strength": rule["strength"],
                "surprise": "一転" in heading,
            }
            return result
    return None

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
        # それ以外（材料・修正・5%・特集・決算・市況・テク等）
        # 新HTML: <tr>単位で time, newslist_ctg div, リンクを一括抽出
        _NEWS_PATTERN = re.compile(
            r'<tr>\s*'
            r'<td[^>]*><time datetime="(\d{4})-(\d{2})-(\d{2})T[^"]*">[^<]*</time></td>\s*'
            r'<td><div class="newslist_ctg\s+(newsctg\w+)"[^>]*>[^<]*</div></td>\s*'
            r'<td>(?:<img[^>]*>)?\s*<a href="([^"]*)">(.*?)</a></td>',
            re.S,
        )
        for m in _NEWS_PATTERN.finditer(html):
            year, month, day = m.group(1), m.group(2), m.group(3)
            tag = m.group(4)
            url = m.group(5)
            heading = m.group(6)
            # 開示は既存パターンで取得済みのためスキップ
            if tag == "newsctg_kaiji_b":
                continue
            if "nmode=0" in url:  # 月へのリンクは除外
                continue
            head_type = HEAD_TYPE_DIC.get(tag, "zairyo")
            date = "%s%s%s" % (year, month, day)
            record = {}
            record["type"] = head_type
            set_db_code(record, code_s)
            record["stock_name"] = stock_name
            record["date"] = date
            # 相対URLを絶対URLに変換
            if url.startswith("/"):
                record["url"] = "https://kabutan.jp" + url
            else:
                record["url"] = url
            record["heading"] = heading
            record_list.append(record)
        # 旧HTML形式のフォールバック（キャッシュに残っている旧フォーマット対応）
        # 新パターンでkaiji以外が取れなかった場合のみ実行
        non_kaiji = [r for r in record_list if r["type"] != "kaiji"]
        if not non_kaiji:
            for m in re.finditer(
                r'<td class="(.*?)"></td>\s+?<td><a href="(.*?)">(.*?)</a></td>', html
            ):
                if "nmode=0" not in m.group(2):
                    tag = m.group(1)
                    url = m.group(2)
                    heading = m.group(3)
                    head_type = HEAD_TYPE_DIC.get(tag, "zairyo")
                    m3 = re.search(r"b=[n|k](\d*)", url)
                    if not m3:
                        continue
                    date = m3.group(1)[:8]
                    record = {}
                    record["type"] = head_type
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


def update_disclosure(code_s, disc_db=None, upd=UPD_INTERVAL):
    # issue #56: ミュータブルデフォルト引数 (disc_db=[]) を None ガードに修正。
    # 既存呼び出し元は常に明示的に渡しているため発火していなかったが、将来のリスク回避。
    if disc_db is None:
        disc_db = []
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


def load_todays_news():
    """todays_disclosure.csvを読み込み、銘柄コード別にニュースを返す

    Returns:
        dict<str, list<tuple>>: code_s → [(date_expr, type_expr, heading, url), ...]
        最大3件/銘柄、CSVの順序（日付降順・優先カテゴリ先）を維持
    """
    import csv

    todays_csv = os.path.join(DATA_DIR, "disclosure", "todays_disclosure.csv")
    if not os.path.exists(todays_csv):
        log_debug("todays_disclosure.csvが見つかりません: %s" % todays_csv)
        return {}

    news_by_code = {}
    with open(todays_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 5:
                continue
            # ヘッダー行・空行をスキップ
            if row[0] == "日付" or not row[0].strip():
                continue
            # 日付: YYYYMMDD → YY/MM/DD
            raw_date = row[0].strip()
            if len(raw_date) == 8 and raw_date.isdigit():
                date_expr = "%s/%s/%s" % (raw_date[2:4], raw_date[4:6], raw_date[6:8])
            else:
                date_expr = raw_date
            # 銘柄コード: HYPERLINK式からcode_sを抽出
            m_code = re.search(r'"(\d[0-9a-zA-Z]\d[0-9A-Z])"', row[1])
            if not m_code:
                continue
            code_s = m_code.group(1)
            # 種類
            type_expr = row[3].strip()
            # 本文: HYPERLINK式からURLと見出しを抽出
            m_link = re.search(r'=HYPERLINK\("(.+?)","(.+?)"\)', row[4])
            if not m_link:
                continue
            url = m_link.group(1)
            heading = m_link.group(2)
            if code_s not in news_by_code:
                news_by_code[code_s] = []
            if len(news_by_code[code_s]) < 3:
                news_by_code[code_s].append((date_expr, type_expr, heading, url))

    return news_by_code


def load_disclosure_for_code(code_s, days=30):
    """disclosure_db.csvから指定銘柄の直近N日以内の開示を返す

    Args:
        code_s: 銘柄コード（文字列）
        days: 何日以内の開示を返すか（デフォルト30日）
    Returns:
        list[tuple]: [(date_expr, type_expr, heading, url), ...] 日付降順
    """
    import csv

    # 英字部分を大文字化して正規化（"135a" → "135A"）
    code_s = code_s.strip().upper()

    if not os.path.exists(DISCLOSURE_CSV):
        log_debug("disclosure_db.csvが見つかりません: %s" % DISCLOSURE_CSV)
        return []

    today_date = get_price_day(datetime.today())
    cutoff = today_date - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y%m%d")

    results = []
    with open(DISCLOSURE_CSV, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 5:
                continue
            # ヘッダー行・空行をスキップ
            if row[0] == "日付" or not row[0].strip():
                continue
            # 銘柄コード: HYPERLINK式からcode_sを抽出
            m_code = re.search(r'"(\d[0-9a-zA-Z]\d[0-9A-Z])"', row[1])
            if not m_code:
                continue
            if m_code.group(1) != code_s:
                continue
            # 日付フィルタ
            raw_date = row[0].strip()
            if raw_date < cutoff_str:
                continue
            # 種類（「材料」はノイズが多いため除外）
            type_expr = row[3].strip()
            if type_expr == "材料":
                continue
            # 日付: YYYYMMDD → MM/DD
            if len(raw_date) == 8 and raw_date.isdigit():
                date_expr = "%s/%s" % (raw_date[4:6], raw_date[6:8])
            else:
                date_expr = raw_date
            # 本文: HYPERLINK式からURLと見出しを抽出
            m_link = re.search(r'=HYPERLINK\("(.+?)","(.+?)"\)', row[4])
            if not m_link:
                continue
            url = m_link.group(1)
            heading = m_link.group(2)
            # 見出しが ASCII のみ = 日本語IRの英語版重複なので除外
            if heading.isascii():
                continue
            results.append((date_expr, type_expr, heading, url))

    return results


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
