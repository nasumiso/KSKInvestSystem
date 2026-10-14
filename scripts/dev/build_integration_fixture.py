"""公開市場データだけを抜き出し、#145 の固定 shelve を再生成する。"""

import argparse
import dbm.dumb
from pathlib import Path
import shelve
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_shelve import ShelveDB
from ks_util import log_print

CODES = ("5032", "3496", "135A", "247A", "218A")
# メモ・保有情報・URL・認証情報は取り込まない。
FIELDS = """
code_s stock_name market sector themes price market_cap
access_date access_date_price access_date_gyoseki access_date_shihyo
access_date_rironkabuka score_gyoseki shihyo_pt momentum_pt funda_pt
gyoseki_current gyoseki_quarter shihyo rironkabuka rironkabuka_up
rironkabuka_down rironkabuka_preceding kessanbi kessan_jisseki_date
price_log price_week_log rs_raw rs_rank_log stock_rank_log trend_template
avg_volume_d stddev_volatility sell_pressure_ratio sell_pressure_ratio_w
price_kairi_ma10 price_kairi_wma10
""".split()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="停止中のDBまたはコピーの shelve パス（拡張子なし）")
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[2] / "tests/fixtures/integration_stocks_shelve/stocks_shelve"
    with shelve.Shelf(dbm.dumb.open(args.source, "r")) as source:
        stocks = {code_s: {key: source[code_s][key] for key in FIELDS if key in source[code_s]} for code_s in CODES}
    with ShelveDB(str(destination)) as db:
        db.replace_from_dict(stocks)
    log_print("統合テスト用DB生成:", destination, len(stocks), "銘柄")


if __name__ == "__main__":
    main()
