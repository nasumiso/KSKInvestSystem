from ks_util import *

# shelveモード切り替え（移行後はTrueに設定）
USE_SHELVE = True

if USE_SHELVE:
    from db_shelve import get_kessan_db as _get_kessan_shelve_db


def is_positive_kessan(summary):
    positive_words = ["上方修正", "増益", "増配", "黒字浮上"]
    for word in positive_words:
        if summary.find(word) >= 0:
            return True
    return False


def get_kessanbi_expr(stock):
    """
    銘柄DBデータから、決算日を返す
    """
    # signal = ""
    # tags = []
    tag = ""
    kessanbi = stock.get("kessanbi", "")
    kessan_announce = stock.get("kessan_announce", "")
    positive_ann = False
    positive_mod = False
    if kessan_announce:
        kessan_type = kessan_announce.split(",")[0]
        summary = kessan_announce.split(",")[2]
        if is_positive_kessan(summary):
            if kessan_type == "修正":
                positive_mod = True
            elif kessan_type == "発表":
                positive_ann = True

    # 決算発表
    if kessanbi:
        # 2週間前のものまで
        # 決算一週間前、発表後一週間内はタグつけ
        dt = datetime.strptime(kessanbi, "%Y/%m/%d")
        today = datetime.today()
        delta_day = (dt - today).days
        tag_ann = ""
        if delta_day > 0 and delta_day <= 7:
            # tags.append("決")
            tag_ann += "決"
        elif delta_day <= 0 and delta_day > -7:
            # tags.append("後")
            tag_ann += "後"
        if delta_day > -14:
            tag_ann += "/".join(kessanbi.split("/")[1:])
        # TODO: 上方タグ 機能してないので一旦封印
        # if tag_ann and positive_ann:
        # 	tag_ann = "上"+tag_ann
        tag += tag_ann
    # 決算修正
    kessan_mod = stock.get("kessan_mod_date", "")
    if kessan_mod:
        # 2週間前までの決算修正を表示
        dt = datetime.strptime(kessan_mod, "%Y/%m/%d")
        today = datetime.today()
        delta_day = (dt - today).days
        tag_mod = ""
        if delta_day > -14:
            tag_mod += "修"
            tag_mod += "/".join(kessan_mod.split("/")[1:])
        # TODO: 上方タグ 機能してないので一旦封印
        # if tag_mod and positive_mod:
        # 	tag_mod = "上"+tag_mod
        if tag:
            tag_mod += "|"
        tag += tag_mod
    tag = tag.strip("|")

    # 記事リンクを貼る
    if tag and kessan_announce:
        # "発表", "修正"
        link = kessan_announce.split(",")[1]
        url = "https://kabutan.jp/" + link
        tag = '=HYPERLINK("%s","%s")' % (url, tag)
    return tag


def get_kessan_quarter(stock):
    """直近決算の四半期Q(1~4)を取得する"""
    import gyoseki

    kessanbi = stock.get("kessanbi")
    res = gyoseki.calc_progress_rate(stock)
    if "quarter" in res:
        # 過去の決算は当Q,現在の決算は次Qを表示する
        dt = datetime.strptime(kessanbi, "%Y/%m/%d").date()
        today = get_price_day(datetime.today())
        if (dt - today).days < 0:
            return int(res["quarter"])
        else:
            return int(res["quarter"]) + 1
    else:
        return 0


PF_KESSAN_PATH = os.path.join(DATA_DIR, "todays_kessan_data", "pf_kessan.pickle")


def _save_kessan_db(pf_kessan_dict):
    """決算DBを保存する内部関数"""
    if USE_SHELVE:
        with _get_kessan_shelve_db() as db:
            db.import_from_dict(pf_kessan_dict)
    else:
        save_pickle(PF_KESSAN_PATH, pf_kessan_dict)


def _load_kessan_db():
    """決算DBをロードする内部関数"""
    if USE_SHELVE:
        with _get_kessan_shelve_db() as db:
            if len(db) == 0:
                return None
            return db.export_to_dict()
    else:
        if not os.path.exists(PF_KESSAN_PATH):
            return None
        return load_pickle(PF_KESSAN_PATH)


def save_pf_kessan_db(stocks):
    """決算DBに銘柄の決算情報を保存する"""
    import portfolio

    code_list_s, possess_list_s = portfolio.parse_my_portforio()
    pf_kessan_dict = {
        k: {
            "kessanbi": v["kessanbi"],
            "stock_name": v["stock_name"],
            "code_s": get_db_code(v),
            "kessan_quarter": get_kessan_quarter(v),
        }
        for k, v in list(stocks.items())
        if (k in code_list_s + possess_list_s and v.get("kessanbi", ""))
    }
    # 決算日でソート
    # pf_kessan_dict = sorted(pf_kessan_dict.items(), key=lambda x: x[1])
    # print pf_kessan_dict
    _save_kessan_db(pf_kessan_dict)


def load_pf_kessan_db():
    pf_kessan_dict = _load_kessan_db()
    if pf_kessan_dict is None:
        # 銘柄DBをロードし、そこから決算情報を抜き出して決算DBに保存
        import make_stock_db as db

        stocks = db.load_stock_db()
        save_pf_kessan_db(stocks)
        pf_kessan_dict = _load_kessan_db()
    return pf_kessan_dict


def make_kessan_csv():
    """make_market_dbで呼ばれる、決算一覧用CSVの作成"""
    pf_kessan_dict = load_pf_kessan_db()
    # 今日〜7日,7日以降、今日以前に振り分ける
    today = get_price_day(datetime.today())  # .date()
    # まず日付ごと
    day_kessan_dict = {}
    for k, v in list(pf_kessan_dict.items()):
        kessanbi = v["kessanbi"]
        if kessanbi not in day_kessan_dict:
            day_kessan_dict[kessanbi] = [v]
        else:
            day_kessan_dict[kessanbi].append(v)
    before_list = []
    current_list = []
    future_list = []
    for k, v in list(day_kessan_dict.items()):
        if not k:  # ''はスルー
            continue
        dt = datetime.strptime(k, "%Y/%m/%d").date()
        if (dt - today).days < 0:
            before_list.append((k, v))  # {k:v}
        elif (dt - today).days < 14:
            current_list.append((k, v))
        else:
            future_list.append((k, v))

    def date_sort(a, b):
        a_date = datetime.strptime(a[0], "%Y/%m/%d")
        b_date = datetime.strptime(b[0], "%Y/%m/%d")
        # return cmp(a_date, b_date)
        return (a_date > b_date) - (a_date < b_date)  # python3対応

    # before_list.sort(cmp=date_sort, reverse=True)
    import functools  # python3対応

    before_list.sort(key=functools.cmp_to_key(date_sort), reverse=True)
    current_list.sort(key=functools.cmp_to_key(date_sort))
    future_list.sort(key=functools.cmp_to_key(date_sort))
    csv_data = []

    def write_to_csv(target_list):
        # 1行目に決算日
        kessanbi_list = [k[0][5:] for k in target_list]  # 5:は2022の年号を除く
        csv_data.append(kessanbi_list)
        # 2行目に銘柄コードと名前
        kessan_code_list = []
        for date, codes in target_list:
            expr = ",".join(
                [
                    "%s%s[%dQ]"
                    % (
                        get_db_code(stock_item),
                        stock_item["stock_name"],
                        stock_item.get("kessan_quarter", 0),
                    )
                    for stock_item in codes
                ]
            )
            kessan_code_list.append(expr)
        csv_data.append(kessan_code_list)

    def write_to_csv_current(target_list):
        for date, codes in target_list:
            csv_rec = []
            csv_rec.append(date[5:])
            for stock in codes:
                csv_rec.append(
                    "%s%s[%dQ]"
                    % (
                        get_db_code(stock),
                        stock["stock_name"],
                        stock.get("kessan_quarter", 0),
                    )
                )
            csv_data.append(csv_rec)

    write_to_csv(before_list)
    write_to_csv_current(current_list)
    write_to_csv(future_list)
    return csv_data


def main():
    # ロガーの初期化
    logger = setup_logger('make_stock_db')

    make_kessan_csv()
    # 決算DB作成テスト
    # import make_stock_db
    # stocks = make_stock_db.load_stock_db()
    # save_pf_kessan_db(stocks)


if __name__ == "__main__":
    setup_logger("kessan")
    main()
