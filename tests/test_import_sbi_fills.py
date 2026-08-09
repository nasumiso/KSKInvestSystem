"""import_sbi_fills.py のテスト (issue #387 Phase3)。

SBI証券 約定履歴CSV の fill 取込・side/取引区分の正規化・決済損益保存・
ETF除外・冪等 dedup を検証する。
"""

import csv

import pytest

import import_sbi_fills as isf
import portfolio_shelve as ps
import research_shelve as rs

# SBI CSV の冒頭メタ行 + 本ヘッダ (実ファイル構造の縮図)
_META = [
    [],
    ["約定履歴照会 "],
    [],
    ["商品指定", "約定開始年月日", "約定終了年月日", "明細数", "明細指定開始", "明細指定終了"],
    ["すべての商品", "2026年07月03日", "2026年08月02日", "8", "1", "8"],
    [],
    ["（注）明細数はご指定された期間の合計です。"],
    [],
]
_HEADER = ["約定日", "銘柄", "銘柄コード", "市場", "取引", "期限", "預り", "課税",
           "約定数量", "約定単価", "手数料/諸経費等", "税額", "受渡日", "受渡金額/決済損益"]


def _row(code_s, action, qty, price, amount, trade_date="2026/07/16", name="銘柄"):
    return [trade_date, name, code_s, "東証", action, "6ヶ月", " 特定 ", "申告",
            qty, price, "--", "--", "2026/07/21", amount]


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
def db_path(tmp_path, monkeypatch):
    """ウォッチリスト (stocks_shelve) に個別株 2 件を登録し、ETF は登録しない。"""
    stocks_db = str(tmp_path / "stocks")
    monkeypatch.setattr("db_shelve.STOCKS_SHELVE", stocks_db)
    monkeypatch.setattr("webapp.helpers.STOCKS_SHELVE", stocks_db)
    rs_db = str(tmp_path / "research")
    monkeypatch.setattr("db_shelve.RESEARCH_SHELVE", rs_db)
    monkeypatch.setattr("research_shelve.RESEARCH_SHELVE", rs_db)
    for code, nm in [("6674", "ジーエス・ユアサ"), ("9984", "ソフトバンクG")]:
        rec = rs.create_research_record(code, nm, overall_rating="B")
        rs.upsert_research_record(rec, db_path=rs_db)
    return str(tmp_path / "sbi_fills")


@pytest.mark.parametrize(
    "action,qty,price,exp_side,exp_kind",
    [
        ("株式現物買", "100", "1000", "buy", "現物"),
        ("株式現物売", "100", "1000", "sell", "現物"),
        ("信用新規買", "100", "5424", "buy", "信用新規"),
        ("信用返済売", "100", "5846", "sell", "信用返済"),
    ],
)
def test_parse_side_and_kind(action, qty, price, exp_side, exp_kind, db_path):
    """SBI 取引区分から side と楽天互換 trade_kind を正規化する。"""
    parsed = isf.parse_fill_row(_row("6674", action, qty, price, "0"))
    assert parsed["side"] == exp_side
    assert parsed["trade_kind"] == exp_kind


def test_settle_pl_only_on_credit_repay(db_path):
    """決済損益は信用返済行のみ保存し、それ以外は None。"""
    repay = isf.parse_fill_row(_row("6674", "信用返済売", "100", "5846", "-33482"))
    assert repay["settle_pl"] == -33482
    new = isf.parse_fill_row(_row("9984", "信用新規買", "100", "5424", "0"))
    assert new["settle_pl"] is None


def test_etf_excluded(tmp_path, db_path):
    """ウォッチリスト外 (ETF/投信) はスキップされ、個別株のみ取り込まれる。"""
    rows = [
        _row("9984", "信用新規買", "100", "5424", "0"),
        _row("1306", "株式現物売", "1900", "410.2", "779380", name="TOPIX連動ETF"),
    ]
    csv_path = _write_csv(tmp_path / "sbi.csv", rows)
    stats = isf.import_csv_to_fills(csv_path, db_path=db_path)
    assert stats["imported"] == 1        # 9984 のみ
    assert stats["skipped_invalid"] == 1  # 1306 ETF
    assert len(ps.list_fills("1306", db_path=db_path)) == 0


def test_dedup_idempotent_and_broker(tmp_path, db_path):
    """同一CSV 2回取込は冪等。fill には broker=SBI と settle_pl が保存される。"""
    rows = [_row("6674", "信用返済売", "100", "5846", "-33482")]
    csv_path = _write_csv(tmp_path / "sbi.csv", rows)
    s1 = isf.import_csv_to_fills(csv_path, db_path=db_path)
    assert s1["imported"] == 1
    s2 = isf.import_csv_to_fills(csv_path, db_path=db_path)
    assert s2["imported"] == 0 and s2["skipped_dup"] == 1
    f = ps.list_fills("6674", db_path=db_path)[0]
    assert f["broker"] == "SBI"
    assert f["settle_pl"] == -33482
