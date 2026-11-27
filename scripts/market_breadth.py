"""市場ブレッドス・信用評価データの取得モジュール (issue #209 / #211 / #292)。

nikkei225jp.com が公開している JS データを HTTP 取得し、JSON 配列としてパースする。

- 信用評価損益率・信用倍率 (週次): fetch_credit_balance_weekly (issue #211)
- 新高値・新安値 銘柄数 (日次): fetch_new_high_low (issue #292)
- 日経VI 恐怖指数 (日次): fetch_nikkei_vi (issue #292)
- 10年国債金利 (日次、財務省): fetch_jgb10 (issue #524、日本版 Fear & Greed の安全資産成分)
- 信用評価損益率の日次推計 (JPX 銘柄別信用取引残高): list_margin_daily_pdf_urls /
  fetch_margin_by_issue / estimate_credit_eval_rate (issue #600)
"""

import io
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

NIKKEI_VI_URL = "https://nikkei225jp.com/_data/_nfsDATA/json_DAY/SL161_ALL.json"
NIKKEI_VI_REFERER = "https://nikkei225jp.com/data/vix.php"

# 財務省「国債金利情報」。all は 1974年〜前月末、current は当月分 (翌営業日11時頃に更新)
JGB_ALL_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
JGB_CURRENT_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
# 和暦の元年の前年 (S1=1926, H1=1989, R1=2019)
_ERA_BASE = {"S": 1925, "H": 1988, "R": 2018}

# JPX「銘柄別信用取引残高」(東証のみ)。直近5営業日分の PDF (YYYYMMDD_mtall.pdf) だけが置かれ、
# それより前は 404 になる (2026-11 時点)。日々16:00 を目安に前営業日分が載る
MARGIN_BY_ISSUE_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
_JPX_BASE = "https://www.jpx.co.jp"
# 推計に使う銘柄: 買残金額の大きい順に、累計でこの割合までの銘柄 (残りは小型で効きが小さい)
CREDIT_EST_AMOUNT_SHARE = 0.95
# これより低いカバー率の日は記録しない (終値の取得が大きく欠けた日。次の実行で取り直す)
CREDIT_EST_MIN_COVERAGE = 0.9
# yfinance の一括取得1回あたりの銘柄数
MARGIN_CLOSE_BATCH = 100


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
    """SL161_ALL.json から 日経VI (恐怖指数) の時系列を返す。

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


# --- 信用評価損益率の日次推計 (issue #600) ---------------------------------
#
# 評価損益率 = Σ(買残株数 × 終値) ÷ Σ買残金額 − 1。JPX の銘柄別 PDF の「金額」は建てた値段の
# 合計 (建値) で時価ではない (アドバンテストの買残 31,421円/株 に対し終値 27,275円 で確認)。
# 公表値 (nikkei225jp、2市場) とは名証の分と、使う銘柄の範囲だけ違う。

def list_margin_daily_pdf_urls():
    """銘柄別信用取引残高のページから、置かれている PDF を [(YYYY-MM-DD, url)] で返す (日付昇順)。

    ファイル名の日付は「申込み現在」の基準日。添付ファイルのディレクトリは決め打ちしない。
    """
    res = requests.get(MARGIN_BY_ISSUE_PAGE, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    res.raise_for_status()
    found = {}
    for href, ymd in re.findall(r'href="([^"]*?(\d{8})_mtall\.pdf)"', res.text):
        day = "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])
        found[day] = href if href.startswith("http") else _JPX_BASE + href
    return sorted(found.items())


def _margin_num(cell):
    """PDF のセル ("1,234" / "▲ 1,234" / "-" / "*") を数値にする。数値でなければ None。"""
    if cell is None:
        return None
    s = cell.replace(",", "").replace("▲", "-").replace(" ", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_margin_by_issue(rows):
    """pdfplumber の表の行から {code5: (買残株数, 買残金額)} を作る。

    1銘柄は「株数」の行と「金額」の行の2行で、ページをまたぐことがある。
    列: [1] 市場 / [3] コード (5桁) / [5] 株数・金額の別 / [9] 買残高。
    市場の列は日本語側 (株数の行) が銘柄名のはみ出しで崩れるため、英語側 (金額の行) で判定し、
    "Investment trusts" (ETF・REIT 等) を外す。合計行 (コードが無い) も外す。
    """
    shares, amounts, funds = {}, {}, set()
    for r in rows:
        if len(r) < 10 or not r[3] or not r[5]:
            continue
        code = r[3].strip()
        if not re.fullmatch(r"\d[0-9A-Z]{3}0", code):
            continue
        value = _margin_num(r[9])
        if r[5].startswith("株数"):
            shares[code] = value
        elif r[5].startswith("金額"):
            amounts[code] = value
            if "Investment trusts" in (r[1] or ""):
                funds.add(code)
    out = {}
    for code, sh in shares.items():
        amt = amounts.get(code)
        if code in funds or not sh or not amt:
            continue
        out[code] = (sh, amt)
    return out


def parse_margin_balances(rows):
    """pdfplumber の表の行から、全銘柄の {code_s: [買残株数, 買残金額, 売残株数, 売残金額]} を作る (issue #612)。

    銘柄ごとの値を残すためのもので、推計用の parse_margin_by_issue と違い、ETF・REIT・買残ゼロも残す。
    列: [3] コード (5桁、先頭4文字が code_s) / [5] 株数・金額の別 / [6] 売残高 / [9] 買残高。
    金額は建てた値段の合計 (時価ではない)。数値でないセルは None。合計行 (コードが無い) は外す。
    銘柄名が長いと、株数の行 (日本語側) の名前がはみ出してコードの列が崩れる
    (2026-11-26 は 1678 など約200銘柄)。そのときは隣の金額の行 (英語側、崩れない) のコードを使う。
    株数・金額の別の列や値の列まで崩れた行 (同じ日に17銘柄、ほとんど ETF) は補わず、その値は None のまま。
    """
    def code_of(r):
        code = (r[3] or "").strip() if len(r) >= 10 else ""
        return code if re.fullmatch(r"\d[0-9A-Z]{3}0", code) else None

    def is_amount(r):
        return len(r) >= 10 and (r[5] or "").startswith("金額")

    out = {}
    for i, r in enumerate(rows):
        if len(r) < 10 or not r[5]:
            continue
        offset = 0 if r[5].startswith("株数") else 1 if r[5].startswith("金額") else None
        if offset is None:
            continue
        code = code_of(r)
        if code is None and offset == 0:
            # 株数の行の直後 (ページをまたぐと直前) の金額の行が同じ銘柄。株数がもう入っている銘柄には
            # 結びつけない (合計行のようにコードの無い行が、直前の銘柄の値を上書きしないように)
            for j in (i + 1, i - 1):
                cand = code_of(rows[j]) if 0 <= j < len(rows) and is_amount(rows[j]) else None
                if cand and (out.get(cand[:4]) or [None])[0] is None:
                    code = cand
                    break
        if code is None:
            continue
        entry = out.setdefault(code[:4], [None, None, None, None])
        for i, cell in ((offset, r[9]), (2 + offset, r[6])):
            value = _margin_num(cell)
            entry[i] = None if value is None else int(value)
    return out


def fetch_margin_by_issue(url):
    """PDF を取得して (基準日 YYYY-MM-DD, {code5: (買残株数, 買残金額)}, 全銘柄の残高) を返す。

    3つ目は parse_margin_balances の結果 (銘柄ごとに残す用、issue #612)。

    基準日は表題の「YYYY/MM/DD申込み現在」から取る。全109ページ前後の表の読み取りに約50秒かかる。
    """
    import pdfplumber  # 日次バッチの他の処理に読み込み時間を払わせないよう遅延 import

    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    res.raise_for_status()
    rows, base_day = [], None
    with pdfplumber.open(io.BytesIO(res.content)) as pdf:
        for page in pdf.pages:
            if base_day is None:
                m = re.search(r"(\d{4})/(\d{2})/(\d{2})\s*申込み現在", page.extract_text() or "")
                if m:
                    base_day = "-".join(m.groups())
            for table in page.extract_tables():
                rows.extend(table)
    if base_day is None:
        raise ValueError("基準日 (申込み現在) が見つからない: %s" % url)
    return base_day, parse_margin_by_issue(rows), parse_margin_balances(rows)


def top_codes_by_amount(holdings, share=CREDIT_EST_AMOUNT_SHARE):
    """買残金額の大きい順に、累計で share に達するまでの銘柄コードを返す。"""
    total = sum(amt for _, amt in holdings.values())
    codes, acc = [], 0.0
    for code, (_, amt) in sorted(holdings.items(), key=lambda kv: -kv[1][1]):
        if acc >= total * share:
            break
        codes.append(code)
        acc += amt
    return codes


def _raise_open_file_limit(target=4096):
    """このプロセスで同時に開けるファイル数の上限 (soft) を target まで上げる。

    運用機 (ssh・launchd とも) は soft が 256 で、yfinance で1,500銘柄を取るとファイルが開けなくなる
    (キャッシュの sqlite・証明書・DNS が失敗し、ほとんどの銘柄が欠ける)。2026-11-26 に運用機で
    256 のままだと 1,553 銘柄中 288〜500、4096 にすると全銘柄が取れた。hard を超えては上げない。
    """
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))


def fetch_closes_for_margin(codes, base_day):
    """基準日の終値を yfinance の一括取得で返す: ({code5: close}, [分割で外した code5])。

    auto_adjust=False で配当による調整を避ける。ただし yfinance の終値は後から起きた分割に
    合わせて直されるため、基準日以後に分割がある銘柄は PDF の株数・建値と合わず、外す。
    分割の当日 (yfinance の分割日) も外す。その日の JPX の株数が分割の前か後か確かめていないため
    (キーエンス 2026-11-19 の1:5。翌営業日の 11/20 分は分割後の株数で載っていた)。
    """
    import pandas as pd
    import yfinance as yf

    _raise_open_file_limit()
    tickers = {c: c[:4] + ".T" for c in codes}
    end = (datetime.now(JST).date() + timedelta(days=1)).isoformat()
    day = pd.Timestamp(base_day)
    closes, split = {}, []
    items = list(tickers.items())
    # 100銘柄ずつ取る (price.py の一括取得と同じ)
    for i in range(0, len(items), MARGIN_CLOSE_BATCH):
        batch = dict(items[i:i + MARGIN_CLOSE_BATCH])
        df = yf.download(
            list(batch.values()), start=base_day, end=end,
            auto_adjust=False, actions=True, threads=True, progress=False,
        )
        if df is None or df.empty or day not in df.index:
            continue
        for code, t in batch.items():
            try:
                later_splits = df["Stock Splits"][t].loc[df.index >= day].fillna(0)
                close = df["Close"][t].loc[day]
            except KeyError:
                continue
            if (later_splits > 0).any():
                split.append(code)
                continue
            if pd.notna(close):
                closes[code] = float(close)
    return closes, split


def estimate_credit_eval_rate(holdings, closes):
    """評価損益率の推計 (%) とカバー率を返す: (rate|None, coverage)。

    終値の無い銘柄は分子からも分母からも外す。カバー率 = 使った買残金額 ÷ 全銘柄の買残金額。
    """
    total = sum(amt for _, amt in holdings.values())
    cost = value = 0.0
    for code, close in closes.items():
        if code not in holdings:
            continue
        sh, amt = holdings[code]
        cost += amt
        value += sh * close
    if not cost or not total:
        return None, 0.0
    return (value / cost - 1) * 100, cost / total
