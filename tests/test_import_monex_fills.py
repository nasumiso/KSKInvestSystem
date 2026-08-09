"""import_monex_fills.py のテスト (issue #390)。

マネックス証券 取引履歴CSV の fill 取込・(商品,取引) の正規化・5桁銘柄コード変換・
決済損益/建単価の保存・非約定行スキップ・冪等 dedup を検証する。
"""

import csv

import pytest

import import_monex_fills as imf
import portfolio_shelve as ps

# マネックス CSV の冒頭メタ行 + 本ヘッダ (実ファイル構造の縮図)
_META = [["データ作成日：2026/08/09 15:15:44"]]
_HEADER = ["約定日", "受渡日", "口座", "商品", "取引", "銘柄コード", "銘柄名",
           "数量（株/口）/返済数量", "単価/返済約定単価", "手数料",
           "税金(手数料消費税及び譲渡益税)", "利金・分配金・償還金", "受渡金額(円)",
           "建約定日", "建単価", "建約定金額", "建手数料", "建手数料消費税",
           "事務管理費", "名義書換料", "順日歩", "逆日歩", "貸株料", "諸経費", "備考"]


def _row(code_s, product, action, qty, price, amount,
         trade_date="2026/01/09", tate_date="", tate_price="", name="銘柄"):
    """マネックス CSV の1行を組み立てる (銘柄コードは実CSV同様に空白パディング)。"""
    return [trade_date, "2026/01/14", "特定", product, action, f"{code_s}    ", name,
            qty, price, "0", "0", "0", amount,
            tate_date, tate_price, "", "0", "0", "", "", "", "", "0", "", ""]


def _write_csv(path, data_rows):
    with open(path, "w", encoding="shift_jis", newline="") as f:
        w = csv.writer(f)
        for r in _META:
            w.writerow(r)
        w.writerow(_HEADER)
        for r in data_rows:
            w.writerow(r)
    return str(path)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "monex_fills")


@pytest.mark.parametrize(
    "product,action,exp_side,exp_kind,exp_tate",
    [
        # 建約定日・建単価は決済側 (信用返済/現引) の行だけ取り込む。信用新規行では
        # 同じ列に自身の約定日・単価がエコーされているだけなので捨てる。
        ("信用新規", "半年新規買い", "buy", "信用新規", False),
        ("信用新規", "半年新規売り", "sell", "信用新規", False),
        ("信用返済", "半年返済売り", "sell", "信用返済", True),
        ("信用返済", "半年返済買い", "buy", "信用返済", True),
        ("現引", "半年現引", "buy", "現引", True),
        ("株式", "お買付", "buy", "現物", False),
        ("株式", "ご売却", "sell", "現物", False),
    ],
)
def test_parse_side_kind_and_tate(product, action, exp_side, exp_kind, exp_tate):
    """(商品,取引) の組から side / trade_kind / 建単価の取込可否を正規化する。"""
    parsed = imf.parse_fill_row(
        _row("54710", product, action, "200", "1,888", "0",
             tate_date="2026/01/09", tate_price="1,888")
    )
    assert parsed["side"] == exp_side
    assert parsed["trade_kind"] == exp_kind
    assert (parsed["tate_price"] is not None) is exp_tate
    assert (parsed["tate_date"] is not None) is exp_tate


@pytest.mark.parametrize(
    "raw_code,exp_code_s",
    [
        ("54710    ", "5471"),  # 数字4桁+付加桁 (実CSVは空白パディング付き)
        ("471A0", "471A"),      # 英字混じり4文字+付加桁
        ("5471", "5471"),       # 5桁でなければそのまま
    ],
)
def test_code_normalization(raw_code, exp_code_s):
    """5桁銘柄コードの末尾付加桁を落として4文字 code_s にする。"""
    assert imf._normalize_monex_code(raw_code) == exp_code_s


def test_etf_excluded_but_non_watchlist_stock_imported(tmp_path, db_path):
    """ETF は除外するが、ウォッチリスト外の個別株は取り込む (issue #390 バックフィル方針)。

    SBI 版のような resolve_stock_name による除外を入れると、過去に売買しただけで
    現在ウォッチリストに無い銘柄の約定が欠落するため、ETF 除外のみとしている。
    """
    rows = [
        # 1540 純金信託 = ETF_code.txt 掲載 → 除外
        _row("15400", "株式", "ご売却", "10", "23,440", "234,125", name="純金信託"),
        # ウォッチリストに登録していない個別株 → 取り込まれること
        _row("98800", "株式", "ご売却", "100", "1,500", "149,725", name="イノテック"),
    ]
    csv_path = _write_csv(tmp_path / "monex.csv", rows)
    stats = imf.import_csv_to_fills(csv_path, db_path=db_path)
    assert stats["imported"] == 1
    assert stats["skipped_invalid"] == 1
    assert len(ps.list_fills("1540", db_path=db_path)) == 0
    assert len(ps.list_fills("9880", db_path=db_path)) == 1


def test_skip_non_trade_rows_and_dedup(tmp_path, db_path):
    """税金・入出庫行はスキップし、信用返済は settle_pl と建単価を保持。再取込は冪等。"""
    rows = [
        _row("", "", "源泉徴収税（所得税）", "0", "0", "7,036"),
        _row("15400", "株式", "入庫", "10", "24,480", "0", name="純金信託"),
        _row("54710", "信用返済", "半年返済売り", "200", "2,165", "54,831",
             trade_date="2026/01/15", tate_date="2026/01/09", tate_price="1,888"),
    ]
    csv_path = _write_csv(tmp_path / "monex.csv", rows)
    s1 = imf.import_csv_to_fills(csv_path, db_path=db_path)
    assert s1["imported"] == 1 and s1["skipped_invalid"] == 2

    s2 = imf.import_csv_to_fills(csv_path, db_path=db_path)
    assert s2["imported"] == 0 and s2["skipped_dup"] == 1

    f = ps.list_fills("5471", db_path=db_path)[0]
    assert f["broker"] == "マネックス"
    assert f["settle_pl"] == 54831       # 受渡金額 = 諸経費控除後の決済損益
    assert f["tate_price"] == 1888
    assert f["tate_date"] == "2026-01-09"
