"""市場ブレッドス・信用評価データの取得モジュール (issue #209 / #211 / #292)。

nikkei225jp.com が公開している JS データを HTTP 取得し、JSON 配列としてパースする。

- 信用評価損益率・信用倍率 (週次): fetch_credit_balance_weekly (issue #211)
- 新高値・新安値 銘柄数 (日次): fetch_new_high_low (issue #292)
- 日経VI 恐怖指数 (日次): fetch_nikkei_vi (issue #292)
- 10年国債金利 (日次、財務省): fetch_jgb10 (issue #524、日本版 Fear & Greed の安全資産成分)
"""

import json
import re
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))

CREDIT_BALANCE_URL = "https://nikkei225jp.com/_data/_nfsDATA/data_DAY/dailyweek2.json"
# Referer が無いと 404 を返すサーバー設定 (2026-05 時点で確認)
CREDIT_BALANCE_REFERER = "https://nikkei225jp.com/data/sinyou.php"

NEW_HIGH_LOW_URL = "https://nikkei225jp.com/_data/_nfsDATA/data_DAY/daily2year.json"
NEW_HIGH_LOW_REFERER = "https://nikkei225jp.com/data/new.php"

NIKKEI_VI_URL = "https://nikkei225jp.com/_data/_nfsDATA/json_DAY/SL161_1990.json"
NIKKEI_VI_REFERER = "https://nikkei225jp.com/data/vix.php"

# 財務省「国債金利情報」。all は 1974年〜前月末、current は当月分 (翌営業日11時頃に更新)
JGB_ALL_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
JGB_CURRENT_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
# 和暦の元年の前年 (S1=1926, H1=1989, R1=2019)
_ERA_BASE = {"S": 1925, "H": 1988, "R": 2018}


def fetch_credit_balance_weekly():
    """dailyweek2.json を取得し、信用評価損益率の週次時系列を返す。

    出典: nikkei225jp.com 経由、2市場（東証＋名証）信用評価損益率。
          日経新聞が JPX 公表の信用残高から平均建値を推計して算出した値。

    Returns:
        list of dict: [{date: 'YYYY-MM-DD', credit_eval_rate: float,
                        credit_bairitsu: float|None}, ...]
                      日付昇順、信用評価率が空文字の行はスキップする。
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": CREDIT_BALANCE_REFERER,
    }
    res = requests.get(CREDIT_BALANCE_URL, headers=headers, timeout=15)
    res.raise_for_status()
    return parse_credit_balance(res.text)


def parse_credit_balance(text):
    """dailyweek2.json のレスポンス本文 (var DAILY = [...];) をパース。

    Args:
        text: HTTP レスポンス本文 (JS 配列リテラル)

    Returns:
        list of dict: 日付昇順、信用評価率が空文字の行はスキップ。
    """
    m = re.search(r"var DAILY\s*=\s*(\[.*\])\s*;", text, re.S)
    if not m:
        raise ValueError("DAILY 配列が見つからない")
    rows = json.loads(m.group(1))
    # 列構成 (2026-05 時点で実データ突き合わせ確認済):
    #   [0] unix timestamp (ms)
    #   [7] 信用評価損益率 (%)   ← 負値あり、最新行は公表前で "" のことがある
    #   [8] 信用倍率 (買い残/売り残)
    out = []
    for r in rows:
        eval_rate = r[7]
        if eval_rate == "":
            continue
        ts_ms = r[0]
        d = datetime.fromtimestamp(ts_ms / 1000, tz=JST).date().isoformat()
        bairitsu = r[8] if len(r) > 8 and r[8] != "" else None
        out.append({
            "date": d,
            "credit_eval_rate": float(eval_rate),
            "credit_bairitsu": float(bairitsu) if bairitsu is not None else None,
        })
    return out


def _fetch_nikkei_json(url, referer):
    """nikkei225jp.com の JS データを取得し本文を返す (共通ヘッダ/Referer)。"""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": referer}
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    return res.text


def _parse_js_rows(text, var_name):
    """`var NAME = [[...],[...]];` 形式の各行を個別に dict 化せず list で返す。

    daily2year.json は最終行 (当日未確定) に連続カンマ ([..,,,..]) を含み
    配列全体の json.loads が失敗するため、行単位で抽出し連続カンマを null に
    補正してからパースする。パース不能な行はスキップする。
    """
    # 同一レスポンスに複数 var が並ぶ場合に別配列まで巻き込まないよう非貪欲
    m = re.search(r"var\s+%s\s*=\s*\[(.*?)\]\s*;" % re.escape(var_name), text, re.S)
    if not m:
        raise ValueError("%s 配列が見つからない" % var_name)
    body = m.group(1)
    rows = []
    for row_text in re.findall(r"\[[^\[\]]*\]", body):
        # 連続カンマ (空要素) と先頭/末尾カンマを null に補正
        fixed = re.sub(r",(?=\s*[,\]])", ",null", row_text)
        fixed = re.sub(r"\[\s*,", "[null,", fixed)
        try:
            rows.append(json.loads(fixed))
        except ValueError:
            continue
    return rows


def fetch_new_high_low():
    """daily2year.json から年初来 新高値/新安値 銘柄数の時系列を返す。

    出典: nikkei225jp.com 経由 (new.php と同データ)。
          列 [8]=新高値数, [9]=新安値数 (2026-05 時点で実データ突き合わせ確認済)。

    Returns:
        list of dict: [{date, new_high, new_low}, ...] 日付昇順。
                      新高値/新安値が数値でない行 (最新の未確定行など) はスキップ。
    """
    text = _fetch_nikkei_json(NEW_HIGH_LOW_URL, NEW_HIGH_LOW_REFERER)
    rows = _parse_js_rows(text, "DAILY")
    out = []
    for r in rows:
        if len(r) <= 9 or not isinstance(r[0], (int, float)):
            continue
        high, low = r[8], r[9]
        if not isinstance(high, (int, float)) or not isinstance(low, (int, float)):
            continue
        d = datetime.fromtimestamp(r[0] / 1000, tz=JST).date().isoformat()
        out.append({"date": d, "new_high": int(high), "new_low": int(low)})
    return out


def fetch_market_breadth_daily():
    """daily2year.json から日本市場版 Fear & Greed 用の日次時系列をまとめて返す (issue #212)。

    fetch_new_high_low と同じファイル・同じ取得経路を使い、合成スコアに必要な列を
    1 回の HTTP で抽出する。列構成 (2026-06 時点で実データ突き合わせ確認済):
      [0] unix timestamp(ms), [1] 日経225終値, [5] 値上がり銘柄数,
      [6] 値下がり銘柄数, [8] 新高値数, [9] 新安値数 (いずれも東証プライム)。

    Returns:
        list of dict: [{date, nikkei_close, advances, declines, new_high, new_low}, ...]
                      日付昇順。必要列が数値でない行 (最新の未確定行など) はスキップ。
    """
    text = _fetch_nikkei_json(NEW_HIGH_LOW_URL, NEW_HIGH_LOW_REFERER)
    rows = _parse_js_rows(text, "DAILY")
    out = []
    for r in rows:
        if len(r) <= 9 or not isinstance(r[0], (int, float)):
            continue
        close, adv, dec, high, low = r[1], r[5], r[6], r[8], r[9]
        if not all(isinstance(v, (int, float)) for v in (close, adv, dec, high, low)):
            continue
        d = datetime.fromtimestamp(r[0] / 1000, tz=JST).date().isoformat()
        out.append({
            "date": d,
            "nikkei_close": float(close),
            "advances": int(adv),
            "declines": int(dec),
            "new_high": int(high),
            "new_low": int(low),
        })
    return out


def fetch_nikkei_vi():
    """SL161_1990.json から 日経VI (恐怖指数) の時系列を返す。

    出典: nikkei225jp.com 経由 (vix.php の var S161)。
          [0]=unix timestamp(ms), [1]=日経VI (2026-05 時点で確認)。

    Returns:
        list of dict: [{date, nikkei_vi}, ...] 日付昇順。値が数値でない行はスキップ。
    """
    text = _fetch_nikkei_json(NIKKEI_VI_URL, NIKKEI_VI_REFERER)
    rows = _parse_js_rows(text, "S161")
    out = []
    for r in rows:
        if len(r) < 2 or not isinstance(r[0], (int, float)) \
                or not isinstance(r[1], (int, float)):
            continue
        d = datetime.fromtimestamp(r[0] / 1000, tz=JST).date().isoformat()
        out.append({"date": d, "nikkei_vi": float(r[1])})
    return out


def _parse_jgb_date(text):
    """財務省 CSV の日付 ("R6.10.1" の和暦、または "2024/10/1") を ISO 形式にする。"""
    m = re.fullmatch(r"([SHR])(\d+)\.(\d+)\.(\d+)", text)
    if m:
        y, mo, d = _ERA_BASE[m.group(1)] + int(m.group(2)), int(m.group(3)), int(m.group(4))
    else:
        m = re.fullmatch(r"(\d{4})/(\d+)/(\d+)", text)
        if not m:
            return None
        y, mo, d = (int(g) for g in m.groups())
    return "%04d-%02d-%02d" % (y, mo, d)


def parse_jgb_csv(text):
    """財務省「国債金利情報」CSV から 10年金利の時系列を返す (issue #524)。

    1行目はタイトル、2行目がヘッダ (基準日,1年,...,10年,...)。欠損は "-"。
    ヘッダは日本語版 ("10年") と英語版 ("10Y") のどちらでも読む。
    Returns: [{date, jgb10}, ...] 日付昇順。10年が欠けている行はスキップ。
    """
    lines = [ln.split(",") for ln in text.splitlines() if ln.strip()]
    header_idx = next(
        (i for i, cols in enumerate(lines) if "10年" in cols or "10Y" in cols), None)
    if header_idx is None:
        raise ValueError("10年金利の列が見つからない")
    header = lines[header_idx]
    col = header.index("10年") if "10年" in header else header.index("10Y")
    out = []
    for cols in lines[header_idx + 1:]:
        if len(cols) <= col:
            continue
        date = _parse_jgb_date(cols[0].strip())
        try:
            value = float(cols[col])
        except ValueError:
            continue
        if date:
            out.append({"date": date, "jgb10": value})
    out.sort(key=lambda r: r["date"])
    return out


def fetch_jgb10():
    """財務省の過去分と当月分の CSV を取り、10年金利の日次時系列を返す (issue #524)。

    同じ日付は当月分を優先する。当月分の取得に失敗した場合は例外にする
    (過去分だけでは最新日が1ヶ月近く古くなり、成分の日付が大きくずれるため)。
    """
    by_date = {}
    for url in (JGB_ALL_URL, JGB_CURRENT_URL):
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        res.raise_for_status()
        for r in parse_jgb_csv(res.content.decode("cp932")):
            by_date[r["date"]] = r
    return [by_date[d] for d in sorted(by_date)]
