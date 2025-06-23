#!/usr/bin/env python3

# ================================================
# RS投資用指数データを作成
# ================================================
import pickle
import operator

from make_sisu_data import *

db_dict = pickle.load(open(os.path.join(DATA_DIR, "sisu_data/sisu_db.pickle"), "r"))

# ==================================================
# calc func
# ==================================================


def rank_simple(vector):
    return sorted(list(range(len(vector))), key=vector.__getitem__)


def rankdata(a):
    n = len(a)
    ivec = rank_simple(a)
    svec = [a[rank] for rank in ivec]
    sumranks = 0
    dupcount = 0
    newarray = [0] * n
    for i in range(n):
        sumranks += i
        dupcount += 1
        if i == n - 1 or svec[i] != svec[i + 1]:
            averank = sumranks / float(dupcount) + 1
            for j in range(i - dupcount + 1, i + 1):
                newarray[ivec[j]] = averank
            sumranks = 0
            dupcount = 0
    return newarray


# ==================================================
# simulation func
# ==================================================


def buy_and_hold():
    rebaranlce = False
    tax = False
    if not rebaranlce:
        gain_jp_stock = (float)(db_dict["jp_stock"][-1][1]) / db_dict["jp_stock"][0][1]
        gain_jp_reit = (float)(db_dict["jp_reit"][-1][1]) / db_dict["jp_reit"][0][1]
        gain_gl_stock = (float)(db_dict["gl_stock"][-1][1]) / db_dict["gl_stock"][0][1]
        gain_gl_reit = (float)(db_dict["gl_reit"][-1][1]) / db_dict["gl_reit"][0][1]
        gain_em_stock = (float)(db_dict["em_stock"][-1][1]) / db_dict["em_stock"][0][1]
        gain_gl_bond = (float)(db_dict["gl_bond"][-1][1]) / db_dict["gl_bond"][0][1]
        gain_gold = (float)(db_dict["gold"][-1][1]) / db_dict["gold"][0][1]

        print(
            gain_jp_stock,
            gain_jp_reit,
            gain_gl_stock,
            gain_gl_reit,
            gain_em_stock,
            gain_gl_bond,
            gain_gold,
        )
        asset_rate = [20, 5, 20, 10, 10, 25, 10]
        gain_list = [
            gain_jp_stock,
            gain_jp_reit,
            gain_gl_stock,
            gain_gl_reit,
            gain_em_stock,
            gain_gl_bond,
            gain_gold,
        ]
        gain = sumproduct(asset_rate, gain_list)
        if tax:
            gain -= (gain - 100) * 0.2
        # 195 税金あり：176
        print("バイアンドホールド：%d " % int(gain))
        return
    # リバンランスあり
    asset_rate = [20, 5, 20, 10, 10, 25, 10, 0]
    asset_total = asset_rate[:]
    total_return = sum(asset_rate)

    for i, _row in enumerate(db_dict[JP_STOCK]):
        if not i % 4 == 0:
            continue
        print("-" * 15, _row[0])
        # 1ヶ月リターンを取得
        asset_ret1 = [0] * 8
        asset_ret1[7] = 1.0
        for j, asset in enumerate(ASSET_CLASSES):
            row = db_dict[asset][i]
            cur_price = row[1]
            if i - 53 / 12 >= 0:
                start_i = i - 53 / 12
            else:
                start_i = 0
            p1_price = db_dict[asset][start_i][1]
            asset_ret1[j] = (float)(cur_price) / p1_price
        print("asset_ret1:", [round(a, 3) for a in asset_ret1])

        asset_total = [t * r for t, r in zip(asset_total, asset_ret1)]
        print("asset_total_before:", [round(a, 1) for a in asset_total])
        total_return = sum(asset_total)
        asset_total = [total_return * r / 100 for t, r in zip(asset_total, asset_rate)]
        print("asset_total_after:", [round(a, 1) for a in asset_total])
        print("total_return:", round(total_return, 1), round(sum(asset_total), 1))
    # 208
    print("バイアンドホールド(リバランス)	: ", int(total_return))


def rs_3612ma():
    # 前計算
    for asset in ASSET_CLASSES:
        print(asset + "の計算")
        prices = db_dict[asset]
        # if not asset == JP_REIT:
        # 	continue
        for i, price in enumerate(prices):
            start_i = i - 53 if i - 53 >= 0 else 0
            price_term = prices[start_i : i + 1]
            priceonly_term = [p[1] for p in price_term]
            # print price_term, price_term2
            avg = int(average(priceonly_term))
            price.append(avg)  # [2] に52週平均

            start_i = i - 53 / 4 if i - 53 / 4 >= 0 else 0
            return3 = float(price[1]) / prices[start_i][1]
            start_i = i - 53 / 2 if i - 53 / 2 >= 0 else 0
            return6 = float(price[1]) / prices[start_i][1]
            start_i = i - 53 if i - 53 >= 0 else 0
            return12 = float(price[1]) / prices[start_i][1]
            ret_avg = round((return3 + return6 + return12) / 3, 3)
            start_i = i - 53 / 12 if i - 53 / 12 >= 0 else 0
            return1 = float(price[1]) / prices[start_i][1]
            # print return3, return6, return12, return1
            price.append(ret_avg)  # [3]に3-6-12リターン平均
            price.append(return1)  # [4]に1ヶ月リターン
            # print "[%d] %d %d"%(i, price[1], avg)
        # break
    # 本処理
    isAccum = False
    asset_rate = [20, 5, 20, 10, 10, 25, 10, 0]
    asset_rate_initial = asset_rate[:]
    asset_total = asset_rate[:]
    if isAccum:
        asset_total = [0] * 7
        pass
    input_money = sum(asset_total)
    input_money_prev = input_money
    total_return = sum(asset_total)
    index = 0
    buy_count = [0] * 7
    sell_count = [0] * 7
    have_count = [0] * 7
    asset_rate_all = [asset_rate]
    total_return_all = [total_return]
    for i, _row in enumerate(db_dict[JP_STOCK]):
        if not i % 4 == 0:  # 月単位
            continue
        asser_rate_prev = asset_rate[:]
        asset_signal = ["NR"] * 7
        asset_return = [0] * 7
        asset_return_1 = [0] * 8
        asset_return_1[7] = 1.0
        for j, asset in enumerate(ASSET_CLASSES):
            row = db_dict[asset][i]
            if row[1] >= row[2]:  # 価格が52週平均を上回る
                asset_signal[j] = "BUY"
            else:
                asset_signal[j] = "SELL"
            asset_return[j] = row[3]
            asset_return_1[j] = row[4]
        print("-" * 15, _row[0])
        print("signal:", asset_signal)
        print("return:", asset_return)
        asset_return_rank = rankdata(asset_return)
        asset_return_rank = [8 - r for r in asset_return_rank]
        print("rank:", asset_return_rank)
        # アセットレートを更新
        for j, rank in enumerate(asset_return_rank):
            if rank <= 2 and asset_signal[j] == "BUY":
                # if asset_signal[j] == "BUY":
                # if rank <= 2:
                # if rank >= 6:
                asset_rate[j] = 100
            elif asset_signal[j] == "SELL":
                asset_rate[j] = 0
        print("rate:", asset_rate)
        val = 100 / asset_rate[:-1].count(100) if asset_rate[:-1].count(100) > 0 else 0
        # if val == 100: val = 50
        if val > 0:
            for j, rate in enumerate(asset_rate):
                asset_rate[j] = val if rate == 100 else 0
            asset_rate[7] = 0  # キャッシュは0に
        print("rate_mod:", asset_rate)
        if not sum(asset_rate) == 100:
            asset_rate[7] = 100 - sum(asset_rate)
            print("おかしいかも？ cash=", asset_rate[7])
            # break
        # 結果
        buy = []
        sell = []
        have = []
        for j, (cur, prev, asset) in enumerate(
            zip(asset_rate, asser_rate_prev, ASSET_CLASSES)
        ):
            if cur > prev:
                buy.append(asset + "(" + str(cur - prev) + ")")
                buy_count[j] += 1
            elif cur < prev:
                sell.append(asset + "(" + str(prev - cur) + ")")
                sell_count[j] += 1
            if cur > 0:
                have.append(asset + "(" + str(cur) + ")")
                have_count[j] += 1
        if buy:
            print("買い：", buy)
        if sell:
            print("売り：", sell)
        if have:
            print("保有：", have)
        # リターン計算
        tax = False  # 税金考慮ありか
        if i % 52 == 0:  # 年単位
            input_money_current = input_money - input_money_prev
            input_money_prev = input_money
            # input_money_current = input_money
            profit = (
                total_return_all[index]
                - total_return_all[max(index - 52 / 4, 0)]
                - input_money_current
            )
            try:
                total_return_current = total_return_all[index] - input_money_current
                total_return_prev = total_return_all[max(index - 52 / 4, 0)]
                print(
                    "年利益：%.2f(%.1f<-%.1f) %d%% input:%d"
                    % (
                        profit,
                        total_return_current,
                        total_return_prev,
                        100 * total_return_current / total_return_prev - 100,
                        input_money_current,
                    )
                )
            except ZeroDivisionError as e:
                print("年利益：0")
        print("return1:", [round(a, 3) for a in asset_return_1])
        print("asset_total_before:", [round(a, 1) for a in asset_total])
        asset_total = [
            t * r1 for t, r1 in zip(asset_total, asset_return_1)
        ]  # 1ヶ月リターン反映
        total_return = sum(asset_total)
        if tax and i % 52 == 0:
            if profit > 0:
                total_return -= profit * 0.2
        if not isAccum:
            asset_total = [
                total_return * r / 100 for r in asset_rate
            ]  # レートに基づき再配分
        print("asset_total_after:", [round(a, 1) for a in asset_total])
        if isAccum:
            # print "asset_total_before_accum:", [round(a, 1) for a in asset_total]
            # 積立分を追加
            asset_total = [
                a + 1.0 * float(r) / 100 for a, r in zip(asset_total, asset_rate)
            ]
            # asset_total = [a+1.0*float(r)/100 for a, r in zip(asset_total, asset_rate_initial)]
            input_money += sum(asset_total) - total_return
            print("  accum:+%.1f(%d)" % (sum(asset_total) - total_return, input_money))
            print("asset_total_accum:", [round(a, 1) for a in asset_total])
            total_return = sum(asset_total)
        print("total_return:", round(total_return, 1), round(sum(asset_total), 1))

        # asset_rate_all.append(asset_rate)
        total_return_all.append(total_return)

        index += 1
        # if(index>=100):
        # 	break
    print("buy_count:", buy_count)
    print("sell_count:", sell_count)
    print("have_count:", have_count)
    # 459 20%税金あり：344 積立：138 積立ランク低:146
    input_money = input_money
    print(
        "RS_MA3-6-12：%d (%d/%d)"
        % (int(total_return / input_money * 100), total_return, input_money)
    )


def rs_macd():
    def list_get(list, index, default=None):
        try:
            v = list[index]
        except IndexError as e:
            v = default
        return v

    # 前計算
    log = False
    for asset in ASSET_CLASSES:
        # if asset != JP_STOCK:
        # 	continue
        # print "="*15, asset
        price_db = db_dict[asset]
        price_only = [r[1] for r in price_db]  # 価格のみテーブル
        ema12 = [0] * len(price_db)
        ema26 = [0] * len(price_db)
        macd_val = [0] * len(price_db)
        macd_sig9 = [0] * len(price_db)
        buy_sell = [0] * len(price_db)
        buy_signal = sel_signal = 0
        for i, row in enumerate(price_db):
            # print "-"*15, "[%d]"%i, row[0]
            n = 12
            if i < n:
                ema12[i] = average(price_only[0 : i + 1])
            else:
                ema12[i] = ema12[i - 1] + (2.0 / (n + 1)) * (
                    price_only[i] - ema12[i - 1]
                )
            # first = average(price_only[max(i-2*n+1,0):max(i-n+1,1)])
            # for j in range(max(i-n+1,1),i+1):
            # 	first += (2.0/(n+1))*(price_only[j]-first)
            # ema12[i] = first

            n = 26
            if i < n:
                ema26[i] = average(price_only[0 : i + 1])
            else:
                ema26[i] = ema26[i - 1] + (2.0 / (n + 1)) * (
                    price_only[i] - ema26[i - 1]
                )
            # first = average(price_only[max(i-2*n+1,0):max(i-n+1,1)])
            # for j in range(max(i-n+1,1),i+1):
            # 	first += (2.0/(n+1))*(price_only[j]-first)
            # ema26[i] = first
            # print ema12[i], ema26[i]

            n = 9
            macd_val[i] = ema12[i] - ema26[i]
            if i < n:
                macd_sig9[i] = average(macd_val[0 : i + 1])
            else:
                macd_sig9[i] = macd_sig9[i - 1] + (2.0 / (n + 1)) * (
                    macd_val[i] - macd_sig9[i - 1]
                )
            # macd_sig9[i] = average(macd_val[0:i+1])

            macd_hist = macd_val[i] - macd_sig9[i]
            buy_signal = (
                macd_sig9[max(i - 1, 0)] > macd_val[max(i - 1, 0)]
                and macd_val[i] > macd_sig9[i]
            )
            sel_signal = (
                macd_sig9[max(i - 1, 0)] < macd_val[max(i - 1, 0)]
                and macd_val[i] < macd_sig9[i]
            )
            # 1: BUYSIG 2:BUY 3:SELSIG 4:SEL
            if buy_signal:
                buy_sell[i] = 1
            elif sel_signal:
                buy_sell[i] = 3
            else:
                if buy_sell[max(i - 1, 0)] == 1 or buy_sell[max(i - 1, 0)] == 2:
                    buy_sell[i] = 2
                elif buy_sell[max(i - 1, 0)] == 3 or buy_sell[max(i - 1, 0)] == 4:
                    buy_sell[i] = 4

            # print "price:", row[1]
            # print "ema12:%d ema26:%d macd:%d signal:%d hist:%d"%\
            # (ema12[i], ema26[i], macd_val[i], macd_sig9[i], macd_hist)
            # print "BUYSELL: %d buy:%d sel:%d"%(buy_sell[i], buy_signal, sel_signal)
            return1 = float(row[1]) / price_only[max(i - 53 / 12, 0)]
            # データ追加
            row.append(buy_sell[i])  # [2]シグナルコード
            row.append(macd_hist)  # [3]MACDヒストグラム
            row.append(return1)  # [4]1ヶ月リターン
        # break
        # raise
    # 本処理
    asset_rate = [20, 5, 20, 10, 10, 25, 10, 0]
    asset_total = asset_rate[:]
    total_return = sum(asset_rate)
    asset_signal = ["NR"] * 7
    asset_return_1 = [0] * 8
    asset_return_1[7] = 1.0
    asset_hist = [0] * 7
    for i, _row in enumerate(db_dict[JP_STOCK]):
        if not i % 4 == 0:
            continue
        asser_rate_prev = asset_rate[:]
        for j, asset in enumerate(ASSET_CLASSES):
            row = db_dict[asset][i]
            month_signals = [
                row[2],
                db_dict[asset][max(i - 1, 0)][2],
                db_dict[asset][max(i - 2, 0)][2],
                db_dict[asset][max(i - 3, 0)][2],
            ]
            if row[2] == 2:
                if 1 in month_signals:
                    asset_signal[j] = "BUYSIG"
                else:
                    asset_signal[j] = "BUY"
            elif row[2] == 4:
                if 3 in month_signals:
                    asset_signal[j] = "SELLSIG"
                else:
                    asset_signal[j] = "SELL"
            elif row[2] == 1:
                asset_signal[j] = "BUYSIG"
            elif row[2] == 3:
                asset_signal[j] = "SELLSIG"
            asset_return_1[j] = row[4]

            asset_hist[j] = row[3]
        print("-" * 15, _row[0])
        print("signal:", asset_signal)
        print("hist:", [round(a, 3) for a in asset_hist])
        asset_hist_rank = rankdata(asset_hist)
        asset_hist_rank = [8 - r for r in asset_hist_rank]
        print("rank:", asset_hist_rank)
        # 売買ルールにもとづきアセットレートを更新
        for j, rank in enumerate(asset_hist_rank):
            # if (asset_signal[j] == "BUYSIG" or asset_signal[j] == "BUY") and rank<=2:
            if asset_signal[j] == "BUYSIG" or asset_signal[j] == "BUY":
                # if asset_signal[j] == "BUYSIG" or (asset_signal[j] == "BUY" and rank<=2):
                asset_rate[j] = 100
            elif asset_signal[j] == "SELLSIG" or asset_signal[j] == "SELL":
                asset_rate[j] = 0
            # elif asset_signal[j] == "NR":
            # 	pass
            # else:
            # 	asset_rate[j] = 0 #ここ疑問?
        print("rate:", asset_rate)
        count = asset_rate[:-1].count(100)
        val = 100 / count if count > 0 else 0
        if val > 0:
            for j, rate in enumerate(asset_rate):
                asset_rate[j] = val if rate == 100 else 0
            asset_rate[7] = 0  # キャッシュは0に
        print("rate_mod:", asset_rate)
        if not sum(asset_rate) == 100:
            asset_rate[7] += 100 - sum(asset_rate)
            print("おかしいかも？ cash=", asset_rate[7])
        # 売買結果
        buy = []
        sell = []
        have = []
        for i, (cur, prev, asset) in enumerate(
            zip(asset_rate, asser_rate_prev, ASSET_CLASSES)
        ):
            if cur > prev:
                buy.append(asset + "(" + str(cur - prev) + ")")
                # buy_count[i]+=1
            elif cur < prev:
                sell.append(asset + "(" + str(prev - cur) + ")")
                # sell_count[i]+=1
            if cur > 0:
                have.append(asset + "(" + str(cur) + ")")
                # have_count[i]+=1
        if buy:
            print("買い：", buy)
        if sell:
            print("売り：", sell)
        if have:
            print("保有：", have)

        # リターン計算
        print("return1:", [round(a, 3) for a in asset_return_1])
        print("asset_total_before:", [round(a, 1) for a in asset_total])
        asset_total = [t * r1 for t, r1 in zip(asset_total, asset_return_1)]
        total_return = sum(asset_total)
        print("   ", asset_rate)
        asset_total = [total_return * r / 100 for r in asset_rate]
        print("asset_total_after:", [round(a, 1) for a in asset_total])
        # TODO: なんかここが違うことがある
        print("total_return:", round(total_return, 1), round(sum(asset_total), 1))
        if abs(total_return - sum(asset_total) >= 1):
            raise
        # break
    # RSなし：224 RSあり：191 ダメだこりゃ
    print("RS_MACD：%d " % int(total_return))


def latest_3612ma():
    """
    最新のRS投資状態を更新する
    """
    market_db = {}
    if os.path.exists(MARKET_DB_NAME):
        market_db = pickle.load(open(MARKET_DB_NAME, "r"))
    # ASSET_CLASSES = [JP_STOCK, JP_REIT, GL_STOCK, GL_REIT, EM_STOCK, GL_BOND, GOLD]
    ASSET_TO_FUND = {
        JP_STOCK: "tbl_1306",
        JP_REIT: "tbl_1343",
        GL_STOCK: "tbl_1550",  # "tbl_TOK",
        GL_REIT: "tbl_2515",  # "tbl_64318081", "tbl_VNQ", TODO: とれてない URLかわった？
        EM_STOCK: "tbl_1681",  # "tbl_EEM","tbl_1582"
        GL_BOND: "tbl_2511",  # "tbl_64316081" TODO:とれてない
        GOLD: "tbl_1540",
        MY_FRONTIER: "tbl_1678",
        # MY_FRONTIER: "tbl_myfrontier", # TODO: 絶対作れてないわ
    }

    return1 = [0] * len(ASSET_CLASSES)
    return3 = [0] * len(ASSET_CLASSES)
    return6 = [0] * len(ASSET_CLASSES)
    return12 = [0] * len(ASSET_CLASSES)
    return_avg = [0] * len(ASSET_CLASSES)
    signal = [0] * len(ASSET_CLASSES)
    for i, asset in enumerate(ASSET_CLASSES):
        print("-" * 15, asset)
        table_key = ASSET_TO_FUND[asset]
        print(table_key)
        prices = market_db[table_key]
        # prices = prices[:-1]
        current = float(prices[-1][CLM_PRICE])
        prev1 = prices[-1 - 52 * 1 / 12][CLM_PRICE]
        prev3 = prices[-1 - 52 * 3 / 12][CLM_PRICE]
        prev6 = prices[-1 - 52 * 6 / 12][CLM_PRICE]
        try:
            prev12 = prices[-1 - 52 * 12 / 12][CLM_PRICE]
        except IndexError:
            prev12 = prices[0][CLM_PRICE]
        print(current, prev1, prev3, prev6, prev12)
        return1[i] = 100 * (current / prev1 - 1)
        return3[i] = 100 * (current / prev3 - 1)
        return6[i] = 100 * (current / prev6 - 1)
        return12[i] = 100 * (current / prev12 - 1)
        return_avg[i] = (return3[i] + return6[i] + return12[i]) / 3
        # 12m平均の計算
        avg12 = 0
        count = 0
        for j in range(12):
            month_ind = -1 - 52 * j / 12
            try:
                avg12 += prices[month_ind][CLM_PRICE]
                count += 1
            except IndexError:
                print("prices_len:", len(prices), "month_ind:", month_ind)
                print("!!! priceデータがたりません")
        if count < 12:
            print("!!! priceデータがたりなかったようです", count)
        avg12 /= count
        signal[i] = current > avg12

    return_rank = rankdata(return_avg)
    return_rank = [len(ASSET_CLASSES) + 1 - r for r in return_rank]

    # 結果表示
    print("     Asset　 1m    3m    6m    12m  avg  signal 順位")
    for i, asset in enumerate(ASSET_CLASSES):
        print(
            "%10s %+5.1f %+5.1f %+5.1f %+5.1f %+5.1f %5s %2d"
            % (
                asset,
                return1[i],
                return3[i],
                return6[i],
                return12[i],
                return_avg[i],
                "BUY" if signal[i] else "SELL",
                return_rank[i],
            )
        )


def main():
    # buy_and_hold()
    # rs_3612ma()
    # rs_macd()
    # 最新RSデータ更新
    # 実際資金移動する時は積立分を忘れずに（1月5万？）
    # TODO: ビットコイン入れてみようかなあ・・
    # TODO: 税金込みでつみたてでバイアンドホールドとどう差がでるか？
    # TODO: 押し目を上昇トレンドの10ma接近で表示 あと一定機関10maより上
    cmd = "update_latest"
    if cmd == "update_latest":
        make_rs_db()
        latest_3612ma()


if __name__ == "__main__":
    main()
