"""issue #53: 月足位置評価の較正スクリプト。

手入力の需給チャート評価 (jukyu_chart) を正解ラベルとして、月足位置タグの
自動判定 (make_stock_db.judge_monthly_position) との一致率を確認する。
閾値 (make_stock_db.MONTHLY_* 定数) を調整して再実行する使い捨て運用。

正解ラベルの期待マッピング:
    月足低位 × 形成/状態なし/ブレイク失敗 → 月低
    月足低位 × ブレイク/再ブレイク     → 月破
    月足低位/月足高値 × ブレイク済み   → 月破または月高 (別枠集計)
    月足高値 × 上記以外               → 月高
    月足CWH/VCP・週足系ラベル          → 対象外 (位置評価の正解にならない)

特徴量は price.get_monthly_data_yfinance → price._calc_monthly_position で
直接計算するため、stock DB の更新前に較正できる (2回目以降はキャッシュ)。
判定は本番と同一の judge_monthly_position を共有する。

Usage:
    python scripts/oneshots/calibrate_monthly_position.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import make_stock_db
import portfolio_shelve as ps
import price
from ks_util import log_print


def expected_tag(jukyu_chart):
    """手入力ラベルから期待タグを返す。対象外は None、別枠 (月破/月高どちらも正解) は "月破|月高"。"""
    label = (jukyu_chart or "").strip()
    if label.startswith("月足低位"):
        state = label[len("月足低位"):]
        if "ブレイク済み" in state:
            return "月破|月高"
        if "ブレイク失敗" in state:  # 失敗して低位に戻った状態
            return "月低"
        if "ブレイク" in state:  # ブレイク/再ブレイク
            return "月破"
        return "月低"  # 形成 or 状態なし
    if label.startswith("月足高値"):
        if "ブレイク済み" in label:  # 低位滞留からブレイクして高値圏に到達した直後は月破もありうる
            return "月破|月高"
        return "月高"
    return None  # CWH/VCP・週足系は位置評価の正解にならない


def main():
    records = ps.list_records(include_excluded=True)
    targets = []
    for r in records:
        jukyu = (r.get("memo") or {}).get("jukyu_chart") or ""
        if "月足" in jukyu:
            targets.append((r["code_s"], r.get("stock_name") or "", jukyu))
    log_print(f"月足ラベル付き銘柄: {len(targets)} 件")

    match = 0
    total = 0
    extra = 0  # 別枠 (ブレイク済み) の一致数
    extra_total = 0
    mismatches = []
    skipped = []
    for code_s, name, jukyu in targets:
        exp = expected_tag(jukyu)
        if exp is None:
            skipped.append((code_s, name, jukyu, "パターン系ラベル"))
            continue
        stock = make_stock_db.get_stock_db(code_s)
        monthly_price_list = price.get_monthly_data_yfinance(code_s, stock)
        if monthly_price_list is None:
            skipped.append((code_s, name, jukyu, "月足取得失敗"))
            continue
        mp = price._calc_monthly_position(monthly_price_list, stock.get("price", 0))
        # 本番と同一ロジックで判定 (price は stock DB の現値)
        pseudo = {"price": stock.get("price"), "monthly_position": mp}
        got = make_stock_db.judge_monthly_position(pseudo)

        if "|" in exp:
            extra_total += 1
            if got in exp.split("|"):
                extra += 1
            else:
                mismatches.append((code_s, name, jukyu, exp, got, mp))
            continue
        total += 1
        if got == exp:
            match += 1
        else:
            mismatches.append((code_s, name, jukyu, exp, got, mp))

    log_print("")
    log_print("==== 較正結果 ====")
    log_print(f"閾値: MIN_MONTHS={make_stock_db.MONTHLY_MIN_MONTHS} "
              f"LOW_POS={make_stock_db.MONTHLY_LOW_POS_PCT}% "
              f"HIGH_POS={make_stock_db.MONTHLY_HIGH_POS_PCT}% "
              f"BREAK_RECENT={make_stock_db.MONTHLY_BREAK_RECENT_MONTHS}ヶ月")
    if total:
        log_print(f"一致率: {match}/{total} ({match * 100 // total}%)")
    if extra_total:
        log_print(f"別枠 (ブレイク済み→月破or月高): {extra}/{extra_total}")
    if mismatches:
        log_print("")
        log_print("---- 不一致銘柄 ----")
        for code_s, name, jukyu, exp, got, mp in mismatches:
            feat = ""
            if mp:
                feat = (f"months={mp['months']} pos={mp['pos_10y_pct']}% "
                        f"median3y={mp['pos_3y_median_pct']}% break={mp['break_month']}")
            log_print(f"{code_s} {name}: 手入力[{jukyu}] 期待[{exp}] 自動[{got or 'なし'}] {feat}")
    if skipped:
        log_print("")
        log_print("---- 対象外/取得失敗 ----")
        for code_s, name, jukyu, reason in skipped:
            log_print(f"{code_s} {name}: [{jukyu}] {reason}")


if __name__ == "__main__":
    main()
