"""import_rakuten_fills.py のテスト (issue #360 Phase2)。

楽天 取引履歴CSV の fill 取込・冪等 dedup・エピソード自動マッチを検証する。
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


def test_match_single_and_ambiguous(tmp_path, db_path):
    """buy fill が ±5日内の1保ログに付く / 同日同side2行は曖昧で未マッチ。"""
    # 6315: 単一 buy → 1保ログにマッチ確定
    ps.add_to_watch("6315", db_path=db_path)
    ps.transition_status("6315", "1保", action_date="2026-06-24", qty=100, db_path=db_path)
    # 3496: 同日同side2行 → 曖昧で未マッチ
    ps.add_to_watch("3496", db_path=db_path)
    ps.transition_status("3496", "1保", action_date="2026-06-23", qty=100, db_path=db_path)

    rows = [
        _row("6315", "信用新規", "買建", "100", "3,390.0", trade_date="2026/6/23"),
        _row("3496", "信用新規", "買建", "100", "4,140.0", trade_date="2026/6/23"),
        _row("3496", "信用新規", "買建", "100", "4,200.0", trade_date="2026/6/23"),
    ]
    csv_path = _write_csv(tmp_path / "t.csv", rows)
    ir.import_csv_to_fills(csv_path, db_path=db_path)

    stats = ir.match_fills_to_episodes(db_path=db_path)
    assert stats["matched"] == 1      # 6315 のみ
    assert stats["ambiguous"] == 2    # 3496 の 2 行

    matched = [f for f in ps.list_fills("6315", db_path=db_path) if f["matched_seq"] is not None]
    assert len(matched) == 1
    # 3496 は全て未マッチ
    assert all(f["matched_seq"] is None for f in ps.list_fills("3496", db_path=db_path))


def test_match_no_double_consume(tmp_path, db_path):
    """1つの1保スロットに対し近接2 fill があっても消費は1件、増分取込でも再マッチしない。"""
    ps.add_to_watch("6315", db_path=db_path)
    ps.transition_status("6315", "1保", action_date="2026-06-24", qty=100, db_path=db_path)
    # 別々の約定日 (曖昧集約は回避) だが両方が同じ1保スロットの±3日窓に入る buy 2 件
    rows = [
        _row("6315", "信用新規", "買建", "100", "3,390.0", trade_date="2026/6/23"),
        _row("6315", "信用新規", "買建", "100", "3,400.0", trade_date="2026/6/25"),
    ]
    csv_path = _write_csv(tmp_path / "t.csv", rows)
    ir.import_csv_to_fills(csv_path, db_path=db_path)

    stats = ir.match_fills_to_episodes(db_path=db_path)
    # 1保スロットは1つしかないので、マッチ確定は高々1件 (二重消費されない)
    assert stats["matched"] == 1
    matched = [f for f in ps.list_fills("6315", db_path=db_path) if f["matched_seq"] is not None]
    assert len(matched) == 1

    # 増分取込: 後日の別CSVで同スロット近傍の buy を追加し再マッチしても、既マッチ
    # スロットは消費済みとして除外され二重マッチしない (P1)
    rows2 = [_row("6315", "信用新規", "買建", "100", "3,410.0", trade_date="2026/6/26")]
    csv2 = _write_csv(tmp_path / "t2.csv", rows2)
    ir.import_csv_to_fills(csv2, db_path=db_path)
    ir.match_fills_to_episodes(db_path=db_path)
    matched2 = [f for f in ps.list_fills("6315", db_path=db_path) if f["matched_seq"] is not None]
    assert len(matched2) == 1  # 増分後もマッチは1件のまま


def test_match_two_episodes_no_cross(tmp_path, db_path):
    """同一コードで近接2エピソード時、buy/sell が各エピソードの最近接スロットにのみ付き跨がない。

    6227 縮図: 保有(6/20)→売却(6/25) と 保有(7/01)→売却(7/06) の2サイクル。
    各サイクルの実約定 buy/sell が、隣サイクルに吸着せず自分の区間に収まる。
    """
    ps.add_to_watch("6227", db_path=db_path)
    # サイクル1
    ps.transition_status("6227", "1保", action_date="2026-06-20", qty=100, db_path=db_path)
    ps.transition_status("6227", "2準", action_date="2026-06-25", db_path=db_path)
    # サイクル2
    ps.transition_status("6227", "1保", action_date="2026-07-01", qty=100, db_path=db_path)
    ps.transition_status("6227", "2準", action_date="2026-07-06", db_path=db_path)

    rows = [
        _row("6227", "信用新規", "買建", "100", "7,000.0", trade_date="2026/6/21"),  # cyc1 buy
        _row("6227", "信用返済", "売埋", "100", "7,300.0", trade_date="2026/6/24"),  # cyc1 sell
        _row("6227", "信用新規", "買建", "100", "6,800.0", trade_date="2026/7/02"),  # cyc2 buy
        _row("6227", "信用返済", "売埋", "100", "7,100.0", trade_date="2026/7/05"),  # cyc2 sell
    ]
    csv_path = _write_csv(tmp_path / "t.csv", rows)
    ir.import_csv_to_fills(csv_path, db_path=db_path)
    stats = ir.match_fills_to_episodes(db_path=db_path)

    assert stats["matched"] == 4  # 4件すべてが自分のサイクルにマッチ

    logs = ps.list_action_logs("6227", db_path=db_path)
    hold_seqs = sorted(l["seq"] for l in logs if l.get("status_to") == "1保")
    sell_seqs = sorted(l["seq"] for l in logs if l.get("action_type") == "売却")
    fills = {(f["side"], f["trade_date"]): f["matched_seq"]
             for f in ps.list_fills("6227", db_path=db_path)}
    # cyc1 buy(6/21) は早い方の1保、cyc2 buy(7/02) は遅い方の1保 (跨がない)
    assert fills[("buy", "2026-06-21")] == hold_seqs[0]
    assert fills[("buy", "2026-07-02")] == hold_seqs[1]
    assert fills[("sell", "2026-06-24")] == sell_seqs[0]
    assert fills[("sell", "2026-07-05")] == sell_seqs[1]
