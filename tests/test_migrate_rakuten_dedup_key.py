"""migrate_rakuten_dedup_key.py のテスト。

9b76d73 以前の旧形式キー (受渡金額入り) の fill に、現行形式で再取込された二重 fill を
統合でき、移行後の再取込が冪等になることを検証する。
"""

import csv

import import_rakuten_fills as ir
import migrate_rakuten_dedup_key as mig
import portfolio_shelve as ps


def _write_csv(path, rows):
    """楽天 取引履歴CSV (Shift-JIS, 28列) を書く。rows は (取引区分, 売買区分, 数量, 単価, 受渡金額)。"""
    with open(path, "w", encoding="shift_jis", newline="") as f:
        w = csv.writer(f)
        w.writerow([ir.HEADER_FIRST_COL] + ["-"] * 27)
        for trade_kind, baibai, qty, price, amount in rows:
            row = ["0"] * 28
            row[ir.COL_TRADE_DATE] = "2026/6/22"
            row[ir.COL_CODE_S] = "285A"
            row[ir.COL_TRADE_KIND] = trade_kind
            row[ir.COL_BAIBAI] = baibai
            row[ir.COL_QTY] = qty
            row[ir.COL_PRICE] = price
            row[ir.COL_AMOUNT] = amount
            row[ir.COL_TATE_DATE] = ""
            row[ir.COL_TATE_PRICE] = ""
            w.writerow(row)
    return str(path)


def _append_old_format(db_path, trade_kind, baibai, side, qty, price, amount):
    """9b76d73 以前の取込と同じ旧形式キーで fill を登録する。"""
    key = ps.make_dedup_key(trade_date="2026-06-22", code_s="285A", trade_kind=trade_kind,
                            baibai_kubun=baibai, qty=qty, price=price, amount=amount,
                            occurrence=0)
    fill = ps.create_fill("285A", trade_date="2026-06-22", side=side, qty=qty, price=price,
                          amount=amount, trade_kind=trade_kind, dedup_key=key, broker="楽天")
    return ps.append_fill(fill, db_path=db_path)[0]


def test_merge_duplicates_and_reimport_is_idempotent(tmp_path):
    db_path = str(tmp_path / "fills")
    old_buy = _append_old_format(db_path, "現物", "買付", "buy", 10, 1000.0, 10000)
    old_sell = _append_old_format(db_path, "現物(単元未満)", "売付", "sell", 3, 1200.0, 3600)
    csv_path = _write_csv(tmp_path / "t.csv", [
        ("現物", "買付", "10", "1,000.0", "10,000"),
    ])
    ir.import_csv_to_fills(csv_path, db_path=db_path)  # 旧キーと一致せず二重になる
    assert len(ps.list_fills(db_path=db_path)) == 3

    plan = mig.plan_migration(ps.list_fills(db_path=db_path))
    assert (len(plan["merge"]), len(plan["rekey"])) == (1, 1)
    mig.apply_migration(plan, db_path=db_path)
    assert mig.plan_migration(ps.list_fills(db_path=db_path)) == {
        "rekey": [], "merge": [], "unknown": []}

    # 二重取込分が消えて旧 seq が残り、移行後は旧 fill も再取込で重複しない
    _write_csv(tmp_path / "t.csv", [
        ("現物", "買付", "10", "1,000.0", "10,000"),
        ("現物(単元未満)", "売付", "3", "1,200.0", "3,600"),
    ])
    stats = ir.import_csv_to_fills(csv_path, db_path=db_path)
    assert stats["imported"] == 0
    assert sorted(f["seq"] for f in ps.list_fills(db_path=db_path)) == sorted(
        [old_buy["seq"], old_sell["seq"]])
