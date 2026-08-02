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


def _row(code_s, trade_kind, baibai, qty, price, trade_date="2026/6/22", amount="0"):
    """28列の取引行を組み立てる (未使用列は '0' / '-' で埋める)。"""
    row = ["0"] * 28
    row[ir.COL_TRADE_DATE] = trade_date
    row[ir.COL_CODE_S] = code_s
    row[ir.COL_TRADE_KIND] = trade_kind
    row[ir.COL_BAIBAI] = baibai
    row[ir.COL_QTY] = qty
    row[ir.COL_PRICE] = price
    row[ir.COL_AMOUNT] = amount
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

