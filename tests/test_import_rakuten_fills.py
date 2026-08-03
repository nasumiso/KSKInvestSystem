"""import_rakuten_fills.py のテスト (issue #360 Phase2)。

楽天 取引履歴CSV の fill 取込・冪等 dedup を検証する (issue #387 で自動マッチは廃止)。
"""

import csv

import pytest

import import_rakuten_fills as ir
import portfolio_shelve as ps

# 楽天 取引履歴CSV の実ヘッダ (28列)
_HEADER = [
    "約定日", "受渡日", "銘柄コード", "銘柄名", "市場名称", "口座区分", "取引区分",
    "売買区分", "信用区分", "弁済期限", "数量［株］", "単価［円］", "手数料［円］",
    "税金等［円］", "諸費用［円］", "税区分", "受渡金額［円］", "建約定日", "建単価［円］",
    "建手数料［円］", "建手数料消費税［円］", "金利（支払）〔円〕", "金利（受取）〔円〕",
    "逆日歩／特別空売り料（支払）〔円〕", "逆日歩（受取）〔円〕", "貸株料",
    "事務管理費〔円〕（税抜）", "名義書換料〔円〕（税抜）",
]


def _row(code_s, trade_kind, baibai, qty, price, trade_date="2026/6/22", amount="0",
         tate_date="", tate_price=""):
    """28列の取引行を組み立てる (未使用列は '0' / '-' で埋める)。"""
    row = ["0"] * 28
    row[ir.COL_TRADE_DATE] = trade_date
    row[ir.COL_CODE_S] = code_s
    row[ir.COL_TRADE_KIND] = trade_kind
    row[ir.COL_BAIBAI] = baibai
    row[ir.COL_QTY] = qty
    row[ir.COL_PRICE] = price
    row[ir.COL_AMOUNT] = amount
    row[ir.COL_TATE_DATE] = tate_date
    row[ir.COL_TATE_PRICE] = tate_price
    return row


def _write_csv(path, data_rows):
    """Shift-JIS で ヘッダ + データ行の CSV を書く。"""
    with open(path, "w", encoding="shift_jis", newline="") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        for r in data_rows:
            w.writerow(r)
    return str(path)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_fills_shelve")


@pytest.mark.parametrize(
    "trade_kind,baibai,qty,price,exp_side,exp_qty,exp_price",
    [
        ("現物", "買付", "100", "1,925.0", "buy", 100, 1925.0),
        ("信用新規", "買建", "200", "1,440.0", "buy", 200, 1440.0),
        ("信用返済", "売埋", "100", "7,590.0", "sell", 100, 7590.0),
    ],
)
def test_parse_row(trade_kind, baibai, qty, price, exp_side, exp_qty, exp_price):
    """現物買付 / 信用新規買建 / 信用返済売埋 の side/qty/price を検証。"""
    parsed = ir.parse_fill_row(_row("6315", trade_kind, baibai, qty, price))
    assert parsed["side"] == exp_side
    assert parsed["qty"] == exp_qty
    assert parsed["price"] == exp_price


def test_parse_genbiki_row(tmp_path, db_path):
    """現引 (売買区分が空) は buy 扱いで取込み、単価を取得原価とする (Phase4b)。"""
    parsed = ir.parse_fill_row(
        _row("4369", "現引", "", "100", "3,129.12", trade_date="2026/2/5")
    )
    assert parsed["side"] == "buy"
    assert parsed["trade_kind"] == "現引"
    assert parsed["qty"] == 100
    assert parsed["price"] == 3129.12


def test_parse_tate_price_on_credit_settle():
    """信用返済行の建約定日・建単価をパースする (Phase4b)。"""
    parsed = ir.parse_fill_row(
        _row("9509", "信用返済", "売埋", "100", "1,065.0",
             tate_date="2025/7/7", tate_price="795.0")
    )
    assert parsed["tate_date"] == "2025-07-07"
    assert parsed["tate_price"] == 795.0


def test_parse_no_tate_price_on_genbutsu():
    """現物行は建約定日・建単価を持たない (None)。"""
    parsed = ir.parse_fill_row(_row("6315", "現物", "買付", "100", "1,925.0"))
    assert parsed["tate_date"] is None
    assert parsed["tate_price"] is None


def test_genbiki_imported_to_fill(tmp_path, db_path):
    """現引行が fill として取込まれる (buy, trade_kind=現引)。"""
    path = _write_csv(
        tmp_path / "genbiki.csv",
        [_row("4369", "現引", "", "100", "3,129.12", trade_date="2026/2/5")],
    )
    stats = ir.import_csv_to_fills(path, db_path=db_path)
    assert stats["imported"] == 1
    assert stats["skipped_invalid"] == 0
    fills = ps.list_fills("4369", db_path=db_path)
    assert len(fills) == 1
    assert fills[0]["side"] == "buy"
    assert fills[0]["trade_kind"] == "現引"


def test_backfill_tate_price_on_reimport(tmp_path, db_path):
    """建単価無しで取込済みの信用返済 fill に、再取込で建単価が後付けされる (Phase4b 移行)。"""
    # 1回目: 建単価無し (旧取込を模す)
    path1 = _write_csv(
        tmp_path / "old.csv",
        [_row("9509", "信用返済", "売埋", "100", "1,065.0")],
    )
    ir.import_csv_to_fills(path1, db_path=db_path)
    assert ps.list_fills("9509", db_path=db_path)[0]["tate_price"] is None
    # 2回目: 同一約定に建単価付き → dedup スキップだが tate_price が後付け
    path2 = _write_csv(
        tmp_path / "new.csv",
        [_row("9509", "信用返済", "売埋", "100", "1,065.0",
              tate_date="2025/7/7", tate_price="795.0")],
    )
    stats = ir.import_csv_to_fills(path2, db_path=db_path)
    assert stats["skipped_dup"] == 1
    assert ps.list_fills("9509", db_path=db_path)[0]["tate_price"] == 795.0


def test_etf_rows_are_excluded(tmp_path, db_path, monkeypatch):
    """ETF (ETF_code.txt 掲載) の行は取込対象外 (issue #387)。個別株は通す。"""
    monkeypatch.setattr(ps, "_etf_codes_cache", frozenset({"1357"}))
    rows = [
        _row("1357", "現物", "買付", "100", "4,862.0"),   # ETF → 除外
        _row("6315", "現物", "買付", "100", "3,390.0"),   # 個別株 → 取込
    ]
    csv_path = _write_csv(tmp_path / "etf.csv", rows)

    stats = ir.import_csv_to_fills(csv_path, db_path=db_path)
    assert stats["imported"] == 1
    assert stats["skipped_invalid"] == 1
    assert len(ps.list_fills("1357", db_path=db_path)) == 0
    assert len(ps.list_fills("6315", db_path=db_path)) == 1


def test_dedup_idempotent(tmp_path, db_path):
    """同一CSVを2回取込→1回目 imported、2回目 skipped_dup。occurrence違いは別件。"""
    # 同日同単価の別注文 (occurrence 違い) を 2 行 + 別内容 1 行
    rows = [
        _row("6315", "信用新規", "買建", "100", "3,390.0"),
        _row("6315", "信用新規", "買建", "100", "3,390.0"),  # occurrence=1 → 別 fill
        _row("3496", "信用新規", "買建", "100", "4,140.0"),
    ]
    csv_path = _write_csv(tmp_path / "t.csv", rows)

    s1 = ir.import_csv_to_fills(csv_path, db_path=db_path)
    assert s1["imported"] == 3 and s1["skipped_dup"] == 0
    # occurrence 違いで同日同単価が別 fill として 2 件残る
    assert len(ps.list_fills("6315", db_path=db_path)) == 2

    s2 = ir.import_csv_to_fills(csv_path, db_path=db_path)
    assert s2["imported"] == 0 and s2["skipped_dup"] == 3
    assert len(ps.list_fills(db_path=db_path)) == 3  # 総数不変 (冪等)

