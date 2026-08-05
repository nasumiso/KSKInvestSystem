#!/usr/bin/env python3

import re

# from scipy import stats
# import numpy as np

from ks_util import *

import rironkabuka


def _clean_html_text(html_text):
    return re.sub(r"<[^>]+>", "", html_text).replace("&nbsp;", "").strip()


def _get_table_row_period(row_html):
    """財務/CFテーブル行の <th> から期表記を抽出して正規化する。

    kabutanの期表記には "連　2023.05*" や "予　2025.06" のように会計区分文字や
    上場前マーカー "*" が混入する。通期 (YYYY.MM) パターンが含まれていれば
    その数値部分だけを返し、含まれなければクリーン後の生テキスト
    (中間期 "25.06-02" 等) を返す。空 <th> や <th> なしは "" を返す。
    """
    th_m = re.search(r"<th[^>]*>(.*?)</th>", row_html, re.S)
    if not th_m:
        return ""
    text = _clean_html_text(th_m.group(1))
    # "YY.MM-MM" のような中間期表記はそのまま（後段で通期判定に落とす）
    if re.search(r"\d{1,2}\.\d{1,2}-\d{1,2}", text):
        return text
    # "YYYY.MM" を抜き出す（"連　2023.05*" → "2023.05"）
    m = re.search(r"\d{4}\.\d{1,2}", text)
    if m:
        return m.group(0)
    return text


def _is_annual_period(period_text):
    return re.fullmatch(r"\d{4}\.\d{1,2}", period_text) is not None


# ==================================================
# 株探から指標データを取得
# ==================================================


def get_from_kabutan(html):
    # 財務テーブルを取得
    # re.S:"."に改行を含める ?:non greedy(最小マッチング)
    log_print("---> 財務テーブル取得")
    if not html:
        log_print("!!! kabutanHTML解析エラー htmlが空")
        return {}
    zaimu_table_html = re.search(r"財務 【実績】.*?<table>(.*?)</table>", html, re.S)
    if not zaimu_table_html:
        log_print(
            "!!! kabutanHTML解析エラー フォーマット変わってる　もしくはインデックスETF"
        )
        return {}
    zaimu_table_html = zaimu_table_html.group(1) if zaimu_table_html else ""

    def get_table_html(table_html):
        try:
            ms_td = re.findall(
                r"<tr >.*?</tr>", table_html, re.DOTALL
            )  # なんかスペースあいてる
        except IndexError as e:
            log_warning(" 財務情報がありません")
        if not ms_td:
            log_warning(" kabutanHTML解析エラー")
            return {}
        return ms_td

    ms_td = get_table_html(zaimu_table_html)
    latest_zaimu_html = ms_td[-1]
    # print latest_zaimu_html
    log_print("<--- 財務テーブル取得完了")

    # 取得した財務htmlから
    # 有利子負債倍率を取得
    def get_table_row(html):
        ReStr_Td = r"<td.*?>(.*?)</td>"
        msitr = re.finditer(ReStr_Td, html, re.DOTALL)
        items = [m.group(1) for m in msitr]
        # print "items:", items
        # return (items[1], items[5]) # 5: 有利子負債倍率の列番号(期の項目はないため)
        return items

    items = get_table_row(latest_zaimu_html)
    jiko_ratio = items[1]
    debut = items[5]

    def get_table_prev(val, item_ind):
        try:
            if val.find("－") >= 0:
                log_debug("有利子負債倍率一つ前を見る")
                items = get_table_row(ms_td[-2])
                val = items[item_ind]
                if val.find("－") >= 0:
                    val = 0
                else:
                    val = float(val)
        except (ValueError, IndexError) as e:
            log_warning(" 有利子負債倍率か自己資本比率の値がありません")
            val = 0
        return val

    debut = get_table_prev(debut.replace(",", ""), 5)
    jiko_ratio = get_table_prev(jiko_ratio, 1)
    log_debug("有利子負債自己資本比率:", debut)
    log_debug("自己資本比率:", jiko_ratio)
    shiyo_data = {}
    shiyo_data["debt_ratio"] = float(debut)
    shiyo_data["capital_ratio"] = float(jiko_ratio)

    # ---- EVR計算用の通期行データを取得
    # CFテーブルは通期のみのため、自己資本・有利子負債倍率・現金を同じ通期に揃える。
    # 既存の debt_ratio/capital_ratio (ms_td[-1]=中間期含む最新行) は他用途で使われ続ける。
    # 列構成: [1株純資産, 自己資本比率, 総資産, 自己資本, 剰余金, 有利子負債倍率, 発表日]
    # 通期判定: <th>内の期表記が "YYYY.MM" 形式（中間期は "YY.MM-MM" 形式）
    annual_rows = [
        (period, row_html)
        for row_html in reversed(ms_td)
        for period in [_get_table_row_period(row_html)]
        if _is_annual_period(period)
    ]
    if annual_rows:
        log_debug("EVR用通期行数:", len(annual_rows))

        def parse_annual_float(items_a, item_ind):
            try:
                val = items_a[item_ind].replace(",", "")
            except IndexError:
                return None
            if "－" in val:
                return None
            try:
                return float(val)
            except ValueError:
                return None

        for period, row in annual_rows:
            items_a = get_table_row(row)
            jiko_val = parse_annual_float(items_a, 3)
            debt_annual = parse_annual_float(items_a, 5)
            if jiko_val is None or debt_annual is None:
                continue
            shiyo_data["evr_period"] = period
            shiyo_data["jikoshihon"] = jiko_val / 100.0  # 百万円→億円
            shiyo_data["debt_ratio_annual"] = debt_annual
            log_debug("EVR用通期:", period)
            log_debug("自己資本(億円,通期):", shiyo_data["jikoshihon"])
            log_debug("有利子負債倍率(通期):", debt_annual)
            break
        if "evr_period" not in shiyo_data:
            log_debug("EVR用通期データ取得できず（EVRは計算しない）")
    else:
        log_debug("通期行が見つからず（EVRは計算しない）")

    # 現金等残高(億円) — CFテーブル(同じfinance HTML内)からEVR用通期に合わせる
    evr_period = shiyo_data.get("evr_period")
    cash_equiv = parse_cash_kabutan(html, target_period=evr_period)
    if evr_period and cash_equiv is not None:
        shiyo_data["cash_equiv"] = cash_equiv
        log_debug("現金等残高(億円,通期):", cash_equiv)

    # ---- ROE, 売上営業利益率取得
    profit_html_m = re.search(
        r'<table>.*?<th scope="col" class="fb_02">(?:<span[^>]*>)?　ＲＯＥ(?:<[^>]*>)*\s*</th>.*?<tbody>(.*?)</tbody>.*?</table>',
        html,
        re.S,
    )
    if not profit_html_m:
        log_warning(" 収益性htmlのフォーマットが変わっている？")
        return shiyo_data
    profit_html = profit_html_m.group(1)
    profit_htmls = get_table_html(profit_html)

    def get_roe_and_profit_margin(prof_html):
        items = get_table_row(prof_html)
        # ROEはインデックス3, 利益率はインデックス2(期はカウントしない)
        try:
            roe = float(items[3])
            profit_margin = float(items[2])
        except ValueError:
            roe = 0
            profit_margin = 0
        return roe, profit_margin

    roe, profit_margin = get_roe_and_profit_margin(profit_htmls[-1])
    if roe == 0 or profit_margin == 0:
        log_debug("ROE一つ前を見る")
        try:
            roe, profit_margin = get_roe_and_profit_margin(profit_htmls[-2])
        except IndexError:
            pass
    log_debug("ROE:", roe)
    log_debug("売上営業利益率:", profit_margin)
    shiyo_data["ROE"] = roe
    shiyo_data["profit_margin"] = profit_margin

    return shiyo_data


def parse_cash_kabutan(html, target_period=None):
    """
    株探 finance htmlのキャッシュフロー推移テーブルから
    現金等残高(億円)を取得する。target_period 指定時は同じ期の行だけを見る。
    取得失敗時は None を返す。
    """
    # cashflow_name アンカー直後の最初のテーブルを取る
    m = re.search(
        r'name="cashflow_name".*?<table>(.*?)</table>',
        html,
        re.S,
    )
    if not m:
        return None
    rows = re.findall(r"<tr >.*?</tr>", m.group(1), re.S)
    if not rows:
        return None
    # <td>[5] = 現金等残高(百万円)
    def get_cash_td(row_html):
        tds = re.findall(r"<td.*?>(.*?)</td>", row_html, re.S)
        if len(tds) < 6:
            return ""
        return tds[5].replace(",", "").strip()

    if target_period:
        rows = [row for row in rows if _get_table_row_period(row) == target_period]
        if not rows:
            return None

    # target_period 指定なしの場合だけ、従来通り最新行から直前行へフォールバックする。
    target_rows = reversed(rows) if target_period is None else rows
    raw = ""
    for row in target_rows:
        raw = get_cash_td(row)
        if raw not in ("－", "-", "", "&nbsp;"):
            break
    try:
        return float(raw) / 100.0  # 百万円→億円
    except ValueError:
        return None


def parse_jikasogaku_kabutan(html):
    """
    株探htmlから時価総額(億円)を取得する
    """
    jikasogaku_m = re.search(
        r'<td colspan="2" class="v_zika2">(.*)<span>億円</span></td>', html
    )
    if not jikasogaku_m:
        log_warning(" 時価総額の値が取得できません(フォーマット変更？または上場廃止)")
        return 0
    else:
        if jikasogaku_m.group(1).find("兆") >= 0:
            # 11<span>兆</span>5899
            # 兆対応
            jikasogaku_m = re.search(r"(.*)<span>兆</span>(.*)", jikasogaku_m.group(1))
            return float(jikasogaku_m.group(1)) * 10000 + float(
                jikasogaku_m.group(2).replace(",", "")
            )
        else:
            return float(jikasogaku_m.group(1).replace(",", ""))


def get_from_kabutan_base(html, shiyo_data):
    # 時価総額
    jikasogaku = parse_jikasogaku_kabutan(html)
    if jikasogaku == 0:
        shiyo_data["jikasogaku"] = 0
        return shiyo_data  # その後PER,PSR計算できないので
    else:
        shiyo_data["jikasogaku"] = jikasogaku  # 億円
        log_debug("時価総額(億円):", shiyo_data["jikasogaku"])

    # ---- PER
    stockinfo_m = re.search(r'<div id="stockinfo_i3">(.*?)</div>', html, re.DOTALL)
    per = 0.0
    need_per = False
    if stockinfo_m:
        # <td>48.8<span>倍</span></td>
        # per_m = re.search(r'<td>(.*)<span>倍</span></td>', stockinfo_m.group(1))
        per_ms = re.finditer(r"<td>(.*)<span.*?倍</span></td>", stockinfo_m.group(1))
        # だいぶ変則的だが、PERが5桁の場合「倍」が省略されるためその場合はとばす
        # iteratorはリセットや長さがわからないため再度finditer
        per_ms_len = sum(1 for _ in per_ms)
        per_ms = re.finditer(r"<td>(.*)<span.*?倍</span></td>", stockinfo_m.group(1))
        # print "per_ms:", per_ms, per_ms_len
        if per_ms and per_ms_len >= 3:
            try:
                per_m = next(per_ms)
                if per_m:
                    per = float(per_m.group(1))
                    log_debug("PER:", per)
                    shiyo_data["PER"] = per
            except ValueError:
                log_debug("PER計算できない", per_m.group(1))
                shiyo_data["PER"] = 0
                need_per = True  # PERを実績から計算する
        else:
            log_warning(" PER取得できず(フォーマット変更？)")
            need_per = True
        # PBRも
        pbr_m = next(per_ms)
        if pbr_m:
            try:
                pbr = float(pbr_m.group(1))
                log_debug("PBR:", pbr)
                shiyo_data["PBR"] = pbr
            except ValueError:
                log_debug("PBR取得できず", pbr_m.group(1))
        # 信用倍率、信用買残、信用売残
        name = "credit_ratio"
        try:
            dividend_nd = next(per_ms)
            if dividend_nd:
                try:
                    val = float(dividend_nd.group(1))
                    log_debug(name + ":", val)
                    shiyo_data[name] = val
                except ValueError:
                    log_debug(name + "取得できず", dividend_nd.group(1))
        except StopIteration:
            log_debug(name + "取得できず")
        margin_m = re.search(
            r'<h2 class="mgt6">信用取引&nbsp;\(単位:千株\)</h2>\r\n<table>(.*?)</table>',
            html,
            re.DOTALL,
        )
        if margin_m:
            margin_html = margin_m.group(1)
            margin_itr = re.finditer("<td>(.*?)</td>", margin_html)
            if margin_itr:
                try:
                    margin_m2 = next(margin_itr)
                    # tmp = margin_m2.group(1) # 売り残
                    td = margin_m2.group(1)
                    shiyo_data["credit_sell"] = (
                        int(float(td.replace(",", ""))) * 1000
                    )  # 買い残
                    margin_m2 = next(margin_itr)
                    td = margin_m2.group(1)
                    shiyo_data["credit_buy"] = (
                        int(float(td.replace(",", ""))) * 1000
                    )  # 買い残
                    margin_m2 = next(margin_itr)
                    # tmp = margin_m2.group(1) # 信用倍率
                except StopIteration:
                    pass

        # 配当利回り"dividend_yield"
        stockinfo_m_text = stockinfo_m.group(1)
        m = re.search(r"<td>(.*)<span.*?％</span></td>", stockinfo_m_text)
        if m:
            try:
                val = float(m.group(1))
                log_debug("配当利回り:", val)
                shiyo_data["dividend_yield"] = val
            except ValueError:
                log_debug("配当利回り取得できず", m.group(1))

    if per == 0.0 and not need_per:
        log_warning(" PER取得できず(フォーマット変更？)")

    # ---- PSR
    psr = 0.0
    # 通期業績テーブル。株探の2ページでレイアウトが異なるため両対応する。
    #   finance ページ: <div class="fin_year_t0_d fin_year_result_d">
    #       列= 決算期/売上高/営業益/経常益/最終益/...、単位=百万円 (2026-08 に旧形式から変更)
    #   base ページ:    <div class="gyouseki_block">
    #       列= 決算期/売上高/経常益/最終益/...、単位=億円
    # kubun1 (単/連/予 等) を持つ行だけが決算期行で、前期比 行は kubun1 が無いので除外される。
    # 時価総額が億円なので、百万円のページだけ 100 で割って単位を揃える。
    gyoseki_m = re.search(
        r'<div class="fin_year_t0_d fin_year_result_d">\s*<table>(.*?)</table>',
        html,
        re.DOTALL,
    )
    if gyoseki_m:
        keijo_idx, profit_idx, unit_divisor = 2, 3, 100.0  # 営業益列あり・百万円
    else:
        gyoseki_m = re.search(
            r'<div class="gyouseki_block">\s*<div class="title">(.*?)</table>',
            html,
            re.DOTALL,
        )
        keijo_idx, profit_idx, unit_divisor = 1, 2, 1.0  # 営業益列なし・億円
    uriage_lst = []
    profit = 0
    if gyoseki_m:
        latest_term = ""
        for row_m in re.finditer(
            # scope='row' (base) と scope="row" (finance) の両方を許容
            r'<th scope=[\'"]row[\'"][^>]*><span class="kubun1">(.*?)</span>(.*?)</th>(.*?)</tr>',
            gyoseki_m.group(1),
            re.DOTALL,
        ):
            # 値セルは class 付き (<td class="high">) もあるため属性を許容して順に拾う。
            # 売上高は常に index 0、経常益/最終益の位置はページごとに異なる。
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row_m.group(3), re.DOTALL)
            if len(tds) <= profit_idx:
                continue
            cur_term = row_m.group(2).replace("&nbsp;", "")
            try:
                uriage = float(tds[0].replace(",", "")) / unit_divisor
                latest_term = cur_term
                uriage_lst.append(uriage)
            except ValueError:
                log_debug("  売上高取得できず:", tds[0], cur_term)

            try:
                profit = float(tds[profit_idx].replace(",", "")) / unit_divisor  # 最終益
            except ValueError:
                log_debug("  最終益取得できず:", tds[profit_idx], cur_term)
            try:
                keijo = float(tds[keijo_idx].replace(",", "")) / unit_divisor  # 経常益
            except ValueError:
                keijo = 0
                log_debug("   経常益取得できず", tds[keijo_idx], cur_term)
        if uriage_lst:
            uriage = uriage_lst[-1]  # 直近
            if uriage > 0:
                psr = round(jikasogaku / uriage, 1)
                log_debug(
                    "PSR: %.1f 直近売上高(億円): %.1f(%s)" % (psr, uriage, latest_term)
                )
            else:
                log_debug("売上が0のためPSR計算できず")
        if need_per and profit != 0:
            per = round(jikasogaku / profit, 1)
            log_debug("PERを実績から計算: ", per, "<- 最終益=%.1f億" % profit)
            if per > 0:
                shiyo_data["PER"] = per
        mper = 0
        if keijo != 0 and profit != 0:
            if profit >= keijo * 0.6 and profit <= keijo * 0.7:
                mper = round(jikasogaku / profit, 1)
                mper = max(mper, 0)
            else:
                log_debug("修正PERを適用")
                mper = round(jikasogaku / (keijo * 0.65), 1)
                mper = max(mper, 0)
            log_debug("MPER:", mper)
        if mper != 0:
            shiyo_data["MPER"] = mper
        else:
            shiyo_data["MPER"] = per
    if psr == 0.0:
        log_warning(" PSR取得できず(フォーマット変更？)")
    else:
        shiyo_data["PSR"] = psr

    # ---- EVR (Enterprise Value / Sales)
    # EV = 時価総額 + 有利子負債 - 現金等残高
    # 有利子負債(億円) = 自己資本(億円) × 有利子負債倍率
    if uriage_lst:
        uriage_for_ev = uriage_lst[-1]
    else:
        uriage_for_ev = 0
    if uriage_for_ev > 0 and jikasogaku > 0:
        # EVR用: 自己資本・有利子負債倍率は通期最新行で揃える(CFが通期のため)
        debt_ratio = shiyo_data.get("debt_ratio_annual")
        jikoshihon = shiyo_data.get("jikoshihon")
        if debt_ratio is not None and jikoshihon is not None:
            interest_debt = jikoshihon * debt_ratio
            cash = shiyo_data.get("cash_equiv")
            cash_approx = cash is None
            if cash_approx:
                cash = 0.0
            ev = jikasogaku + interest_debt - cash
            ev_sales = round(ev / uriage_for_ev, 1)  # 負値も保存する
            log_debug(
                "EVR: %.1f EV: %.1f(時価%.1f+負債%.1f-現金%.1f) 売上: %.1f%s"
                % (
                    ev_sales,
                    ev,
                    jikasogaku,
                    interest_debt,
                    cash,
                    uriage_for_ev,
                    " (現金近似)" if cash_approx else "",
                )
            )
            shiyo_data["EV_Sales"] = ev_sales
            if cash_approx:
                shiyo_data["EV_Sales_approx"] = True
        else:
            log_debug("EVR計算不能（debt_ratio/jikoshihonなし）→ PSR で代用")
    return shiyo_data


def analyze_from_kabutan(code_s, upd=UPD_INTERVAL):
    # code = int(code)
    # 自己資本比率
    import rironkabuka

    html = rironkabuka.get_kabutan_html(code_s, upd)
    shiyo_data = get_from_kabutan(html)
    # 時価総額
    html = rironkabuka.get_kabutan_base_html(code_s, upd)
    if not html:
        log_warning(" kabutan base htmlが取得できていません")
        return shiyo_data
    shiyo_data = get_from_kabutan_base(html, shiyo_data)
    # print shiyo_data
    return shiyo_data


def calc_shihyo_pt(code_s, upd=UPD_INTERVAL, stock={}):
    """指標計算本体"""
    # 理論株価をどう絡めるか？
    # PER, PBRは理論株価に含まれる PSRは外して良い
    # ROEも含まれる 営業利益率は含むべき
    # code = int(code)
    # 自己資本比率、時価総額、株主比率
    shiyo = analyze_from_kabutan(code_s, upd)
    # TODO: 0のときの5年計算してしまうため
    # print shiyo_data
    JIKASOGAKU_MAX = 30
    DEBT_RATIO_MAX = 30  # 減点用
    # PER_MAX = 25
    # PBR_MAX = 10
    # PSR_MAX = 35
    # 時価総額ファクター（成長性×流動性×外国資本流入の山型分布）
    jikasogaku_pt = 0
    if "jikasogaku" in shiyo:
        jikasogaku_pt = step_func(
            shiyo["jikasogaku"],
            [100, 400, 1000, 3000],
            [25, JIKASOGAKU_MAX, 15, 5],
            min_val=0,
        )
    # 発行株式数(小型株ファクター)

    # 有利子負債自己資本比率(クォリティファクター)
    debt_ratio_pt = 0
    if "debt_ratio" in shiyo:
        if shiyo["debt_ratio"] >= 0:
            debt_ratio_pt = step_func(
                (shiyo["debt_ratio"]),
                [0.0, 0.5, 1.0, 3.0],
                [DEBT_RATIO_MAX / 3, 0, -DEBT_RATIO_MAX / 2, -DEBT_RATIO_MAX],
            )
        else:
            debt_ratio_pt = -DEBT_RATIO_MAX
    else:
        log_warning(" 有利子負債自己資本比率データがありません")
        debt_ratio_pt = 0
    # 理論株価
    # 理論価格乖離率[0, 20, 40, 60, 100]
    # 上限[30, 60, 100, 150, 300]
    # 下限[-60, -30, 0, 30]
    thoery_total_pt = rironkabuka.calc_theory_pt(code_s, stock)

    # PER(バリューファクター)
    # per_pt = 0
    # if shiyo.has_key("MPER"): # PER->MPERに
    # 	if shiyo["MPER"] > 0:
    # 		per_pt = step_func(shiyo["MPER"], [0, 30, 60], [PER_MAX, PER_MAX/2, 0])
    # # PBR(バリューファクター)
    # pbr_pt = 0
    # if shiyo.has_key("PBR"):
    # 	pbr_pt = step_func(shiyo["PBR"], [0, 1], [PBR_MAX, 0])
    # # PSR(バリューファクター)
    # psr_pt = 0
    # if shiyo.has_key("PSR"):
    # 	psr_pt = step_func(shiyo["PSR"], [0, 0.75, 2.5, 10], [PSR_MAX, PSR_MAX/2, PSR_MAX/4, 0])

    log_debug("時価総額pt: %d/%d" % (jikasogaku_pt, JIKASOGAKU_MAX))
    log_debug("有利子負債自己資本比率pt: %d/%d" % (debt_ratio_pt, -DEBT_RATIO_MAX))
    # print "PER pt: %d/%d"%(per_pt, PER_MAX)
    # print "PBR pt: %d/%d"%(pbr_pt, PBR_MAX)
    # print "PSR pt: %d/%d"%(psr_pt, PSR_MAX)
    # TODO: ROEと売上営業利益率は株探から取得可能(クォリティファクター)
    # TODO: 配当利回りも？

    # shiyo_pt = jikasogaku_pt + debt_ratio_pt+ per_pt+psr_pt
    shiyo_pt = jikasogaku_pt + debt_ratio_pt + thoery_total_pt
    shiyo_pt = int(cramp(shiyo_pt, 0, 100))
    log_print("----------")
    log_print("指標PT:", shiyo_pt)
    log_print("----------")
    return shiyo_pt, shiyo


CACHE_DIR_KABUTAN = os.path.join(DATA_DIR, "stock_data", "kabutan", "finance")
URL_CODE_KABUTAN = "http://kabutan.jp/stock/finance?code=%s&mode=k"


def get_shihyo_data(stocks, code_s, upd=UPD_INTERVAL):
    """
    指標の更新・取得
    type: (dict<stock_db>, str, bool) -> dict<stock>
    """
    log_print("-" * 5, "指標の取得計算 upd=", upd)
    tables = {}
    stock = stocks[code_s] if code_s in stocks else {}
    # わかりにくいがここで通信している
    shihyo_pt, shihyo_data = calc_shihyo_pt(code_s, upd, stock)
    log_print("-" * 5, "指標の取得計算完了")
    # 指標データの取得元ファイルの日付を格納
    cache_path = os.path.join(
        CACHE_DIR_KABUTAN, get_http_cachname(URL_CODE_KABUTAN % str(code_s))
    )
    if shihyo_data:
        # 指標データが取得できた場合のみaccess_dateを更新（空の場合は次回再取得を促す）
        tables["access_date_shihyo"] = get_file_datetime(cache_path)
        log_debug("date_shihyo:", tables["access_date_shihyo"])
    else:
        # 指標データが空の場合はaccess_dateを削除して次回再取得を促す
        tables["access_date_shihyo"] = None
        log_warning("指標データが空のためaccess_date_shihyoを削除します（次回再取得）")
    # shihyo_ptは下流で無条件参照されるため常に設定する
    tables["shihyo_pt"] = shihyo_pt
    # 指標データ登録
    tables["shihyo"] = shihyo_data
    # tables["PER"] = shihyo_data["PER"]
    # tables["PBR"] = shihyo_data["PBR"]
    # tables["PSR"] = shihyo_data["PSR"]
    # tables["ROE"] = shihyo_data["ROE"]
    # tables["profit_margin"] = shihyo_data["profit_margin"]
    # tables["debt_ratio"] = shihyo_data["debt_ratio"]
    # tables["capital_ratio"] = shihyo_data["capital_ratio"]

    tables["code_s"] = code_s

    return tables


def get_credit_expr(stock_data):
    # 信用倍率
    ind_credit = ""
    if "credit_ratio" in stock_data["shihyo"]:
        ind_credit = str(round(stock_data["shihyo"]["credit_ratio"], 2))
    # 出来高買い残倍率
    volume_creditbuy_expr = ""
    if "credit_buy" in stock_data["shihyo"]:
        credit_buy = stock_data["shihyo"].get("credit_buy")
        # 価格更新時に更新される平均出来高
        avg_volume_dat = stock_data.get("avg_volume_d", [])
        if avg_volume_dat and avg_volume_dat[0] > 0:
            avg_volume = avg_volume_dat[0]  # 0: 20日のもの
            volume_creditbuy = float(credit_buy) / avg_volume
            if volume_creditbuy < 1:
                volume_creditbuy_expr = "%.2f" % volume_creditbuy
            elif volume_creditbuy < 10:
                volume_creditbuy_expr = "%.1f" % volume_creditbuy
            else:
                volume_creditbuy_expr = "%d" % volume_creditbuy
    return "売%s,出%s" % (ind_credit, volume_creditbuy_expr)


def get_shihyo_expr(stock_data):
    def get_indicator_exp(key, keta=0):
        if key in stock_data["shihyo"]:
            try:
                if keta == 0:
                    return str(int(stock_data["shihyo"][key]))
                else:
                    return str(round(stock_data["shihyo"][key], keta))
            except TypeError:
                return "Error:" + str(stock[0])
        return ""

    ind_marketcap = "%d" % int(stock_data.get("market_cap", 0))
    if ind_marketcap == 0:
        ind_marketcap = "-"
    ind_per = "%s" % get_indicator_exp("MPER")
    ind_pbr = "%s" % get_indicator_exp("PBR", 1)
    # EVR優先、欠損時はPSRにフォールバック
    shihyo = stock_data.get("shihyo", {})
    if "EV_Sales" in shihyo:
        ind_value_label = "EVR"
        ind_value = get_indicator_exp("EV_Sales", 1)
        if shihyo.get("EV_Sales_approx"):
            ind_value = "%s~" % ind_value
    else:
        ind_value_label = "PSR"
        ind_value = get_indicator_exp("PSR", 1)
    ind_roe = "%s" % get_indicator_exp("ROE")
    # 利益率は整数部1桁なら小数点1桁、2桁以上なら整数で表示
    margin_val = stock_data.get("shihyo", {}).get("profit_margin")
    if margin_val is None or margin_val == "":
        ind_margin = "%"
    else:
        try:
            margin_f = float(margin_val)
            if abs(margin_f) < 10:
                ind_margin = "%.1f%%" % margin_f
            else:
                ind_margin = "%d%%" % int(margin_f)
        except (TypeError, ValueError):
            ind_margin = "Error:%s%%" % margin_val
    ind_debt = "%s" % get_indicator_exp("debt_ratio", 2)
    ind_capital = "%s%%" % get_indicator_exp("capital_ratio")
    # ind_credit = "%s"%get_indicator_exp("credit_ratio", 2)
    ind_dividend_yield = "%s" % get_indicator_exp("dividend_yield", 1)
    indicator = "%s億 PER%s PBR%s %s%s 配当%s ROE%s 利益率%s 負債%s 自己%s" % (
        ind_marketcap,
        ind_per,
        ind_pbr,
        ind_value_label,
        ind_value,
        ind_dividend_yield,
        ind_roe,
        ind_margin,
        ind_debt,
        ind_capital,
    )
    return indicator


def main():
    # ロガーの初期化
    logger = setup_logger("make_stock_db")

    """
    指標:PERやPSRなど経営上の指標　を取得、分析する
    """
    # code_list = [1768,1959,1820,1793,1352,2152,1801,1812,1332,1764,11827,1782,1899,1905,1301,1810,1824,2109,1946,2162,1720,1788,1934,1870,1376,1911,1515,1885,1939,1941]
    # code_list = [3038,2301,6095,8001,8031,8035,3668]
    code_list = ["9509"]  # 5034,4436,7808, 2780,6083
    # import make_stock_db
    # stocks = make_stock_db.load_stock_db()
    for code_s in code_list:
        log_print("-" * 30)
        log_print("%sの指標を計算します" % code_s)
        import make_stock_db as db

        stock = db.load_cacehd_stock_db(code_s)
        shiyo_pt, shihyo = calc_shihyo_pt(code_s, UPD_REEVAL, stock)  # UPD_FORCE
        log_print(shihyo)
        # print get_shihyo_expr(stock)
        # print get_credit_expr(stock)


if __name__ == "__main__":
    setup_logger("shihyou")
    # TODO: 9272ブティックス 取得できてない？
    #!!! 不正な関連銘柄です
    # もでてる
    main()
