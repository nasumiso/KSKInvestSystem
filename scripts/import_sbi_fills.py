#!/usr/bin/env python3
"""SBI証券 約定履歴CSV → portfolio_shelve fill レイヤー 取込 (issue #387 Phase3)。

楽天と同じ fill レイヤーへ取り込むが、SBI の CSV は列構成・語彙・冒頭メタ行が異なる。

SBI CSV の特徴 (`SaveFile_*.csv`, Shift-JIS):
- 冒頭に「約定履歴照会」等のメタ行があり、ヘッダは `約定日,銘柄,銘柄コード,...` の行
- 14列。`取引` 列に 信用返済売 / 信用新規買 / 株式現物売 / 株式現物買 が入る
- 建約定日・建単価の列は無い。代わりに末尾 `受渡金額/決済損益` に信用返済の損益が入る
- ETF/投信 (成長株ウォッチリスト外) は取込対象外 (issue #387: 個別株のみ)

楽天との共通処理 (_parse_num / _normalize_trade_date / RowSkip / create_fill / dedup) は
import_rakuten_fills から再利用する。
"""

import argparse
import csv
import os
import sys
from typing import Any, Dict, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
from import_rakuten_fills import RowSkip, _normalize_trade_date, _parse_num  # noqa: E402
from webapp.helpers import resolve_stock_name  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# 定数 (SBI 約定履歴CSV フォーマット)
# ===========================================

CSV_ENCODING = "shift_jis"
EXPECTED_COL_COUNT = 14

# 列インデックス (0-indexed、ヘッダ行を基準に確認済み)
COL_TRADE_DATE = 0     # 約定日 (YYYY/MM/DD)
COL_CODE_S = 2         # 銘柄コード
COL_TRADE_ACTION = 4   # 取引 (信用返済売 / 信用新規買 / 株式現物売 / 株式現物買)
COL_QTY = 8            # 約定数量
COL_PRICE = 9          # 約定単価
COL_AMOUNT = 13        # 受渡金額/決済損益 (信用返済は決済損益、それ以外は受渡金額)

HEADER_FIRST_COL = "約定日"
HEADER_MARKER = "銘柄コード"  # メタ行と本ヘッダを区別する目印

# SBI 取引区分 → (side, 楽天互換の trade_kind)。
# trade_kind を楽天語彙に合わせることで売買履歴タブの集約・表示を一貫させる。
_ACTION_MAP = {
    "株式現物買": ("buy", "現物"),
    "株式現物売": ("sell", "現物"),
    "信用新規買": ("buy", "信用新規"),
    "信用新規売": ("sell", "信用新規"),
    "信用返済買": ("buy", "信用返済"),
    "信用返済売": ("sell", "信用返済"),
}

# 決済損益を持つ取引区分 (信用返済のみ)
_SETTLE_ACTIONS = frozenset({"信用返済買", "信用返済売"})


# ===========================================
# 1. CSV 読込層
# ===========================================

def read_csv_rows(csv_path: str) -> List[List[str]]:
    """SBI 約定履歴CSV を Shift-JIS で読み、ヘッダ行以降のデータ行を返す。

    冒頭のメタ情報行を読み飛ばし、`約定日,銘柄,銘柄コード,...` のヘッダ行を探す。
    ヘッダが見つからなければ ValueError。
    """
    with open(csv_path, "r", encoding=CSV_ENCODING, newline="") as f:
        rows = list(csv.reader(f))
    for i, row in enumerate(rows):
        if row and row[0].strip() == HEADER_FIRST_COL and HEADER_MARKER in [c.strip() for c in row]:
            return rows[i + 1:]
    raise ValueError(f"ヘッダ行 (約定日...銘柄コード) が見つかりません: {csv_path}")


# ===========================================
# 2. 行パース層
# ===========================================

def parse_fill_row(row: List[str]) -> Dict[str, Any]:
    """SBI CSV 1 行を fill 構築用の中間 dict に変換する。

    ETF/投信 (銘柄名が解決できない = ウォッチリスト外) は RowSkip。空行・列数不足も RowSkip。
    """
    if not any(c.strip() for c in row):
        raise RowSkip("空行")
    if len(row) < EXPECTED_COL_COUNT:
        raise RowSkip(f"列数不足 ({len(row)})")

    code_raw = (row[COL_CODE_S] or "").strip()
    try:
        ps.validate_code_s(code_raw)
    except (ValueError, TypeError):
        raise RowSkip(f"無効な銘柄コード: {code_raw!r}")
    code_s = ps.normalize_code_s(code_raw)

    # ETF/投信除外: 成長株ウォッチリスト (stocks_shelve) に無い銘柄はスキップ (issue #387)
    if not resolve_stock_name(code_s):
        raise RowSkip(f"ウォッチリスト外 (ETF/投信の可能性): {code_s}")

    trade_date = _normalize_trade_date(row[COL_TRADE_DATE])
    if trade_date is None:
        raise RowSkip(f"約定日パース不可: {row[COL_TRADE_DATE]!r}")

    action = (row[COL_TRADE_ACTION] or "").strip()
    mapped = _ACTION_MAP.get(action)
    if mapped is None:
        raise RowSkip(f"未知の取引区分: {action!r}")
    side, trade_kind = mapped

    qty_f = _parse_num(row[COL_QTY])
    price_f = _parse_num(row[COL_PRICE])
    if qty_f is None or qty_f <= 0:
        raise RowSkip(f"数量欠落/不正: {row[COL_QTY]!r}")
    if price_f is None or price_f <= 0:
        raise RowSkip(f"単価欠落/不正: {row[COL_PRICE]!r}")

    amount_f = _parse_num(row[COL_AMOUNT])
    amount = int(amount_f) if amount_f is not None else 0
    # 信用返済行の受渡金額列は「決済損益」。それを settle_pl に持たせる (P/L の真実源)
    settle_pl = amount if action in _SETTLE_ACTIONS else None

    return {
        "code_s": code_s,
        "trade_date": trade_date,
        "side": side,
        "qty": int(qty_f),
        "price": price_f,
        "amount": amount,
        "trade_kind": trade_kind,
        "action": action,       # dedup 素材 (元の取引区分)
        "settle_pl": settle_pl,
    }


# ===========================================
# 3. 統合層
# ===========================================

def import_csv_to_fills(
    csv_path: str,
    *,
    dry_run: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """SBI CSV を読み、各行を fill として冪等取込する。

    Returns: {"rows", "imported", "skipped_dup", "skipped_invalid"}
    """
    rows = read_csv_rows(csv_path)
    stats = {"rows": len(rows), "imported": 0, "skipped_dup": 0, "skipped_invalid": 0}
    occurrence_counter: Dict[str, int] = {}
    samples: List[Dict[str, Any]] = []

    for row in rows:
        try:
            parsed = parse_fill_row(row)
        except RowSkip as e:
            stats["skipped_invalid"] += 1
            log_print("import_sbi_fills: スキップ", e.reason)
            continue

        # occurrence: 同一CSV内で dedup 素材が同一な行の出現順 (分割約定を別 fill に)
        occ_key = "|".join(
            [
                parsed["trade_date"],
                parsed["code_s"],
                parsed["trade_kind"],
                parsed["action"],
                str(parsed["qty"]),
                f"{parsed['price']:.4f}",
                str(parsed["amount"]),
            ]
        )
        occurrence = occurrence_counter.get(occ_key, 0)
        occurrence_counter[occ_key] = occurrence + 1

        # baibai_kubun には SBI の元区分 (action) を渡し、楽天と dedup 空間を分ける
        dedup_key = ps.make_dedup_key(
            trade_date=parsed["trade_date"],
            code_s=parsed["code_s"],
            trade_kind=parsed["trade_kind"],
            baibai_kubun=parsed["action"],
            qty=parsed["qty"],
            price=parsed["price"],
            amount=parsed["amount"],
            occurrence=occurrence,
        )
        fill = ps.create_fill(
            parsed["code_s"],
            trade_date=parsed["trade_date"],
            side=parsed["side"],
            qty=parsed["qty"],
            price=parsed["price"],
            amount=parsed["amount"],
            trade_kind=parsed["trade_kind"],
            dedup_key=dedup_key,
            broker="SBI",
            settle_pl=parsed["settle_pl"],
        )

        if dry_run:
            if len(samples) < 3:
                samples.append(fill)
            stats["imported"] += 1
            continue

        _, is_new = ps.append_fill(fill, db_path=db_path)
        if is_new:
            stats["imported"] += 1
        else:
            stats["skipped_dup"] += 1

    if dry_run and samples:
        log_print("import_sbi_fills: dry-run サンプル (先頭 3 件):")
        for s in samples:
            log_print(
                f"  {s['code_s']} {s['trade_date']} {s['side']} "
                f"qty={s['qty']} price={s['price']} kind={s['trade_kind']} "
                f"settle_pl={s['settle_pl']}"
            )
    return stats


# ===========================================
# 4. 実行層
# ===========================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SBI証券 約定履歴CSV を fill レイヤーへ取込 (issue #387 Phase3)",
    )
    parser.add_argument("csv_path", help="SBI 約定履歴CSV のパス (Shift-JIS)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="読込・パースまで行うが DB へは書かない",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="portfolio_shelve のパス上書き (検証用)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not os.path.exists(args.csv_path):
        log_warning(f"CSV が見つかりません: {args.csv_path}")
        return 1

    stats = import_csv_to_fills(
        args.csv_path,
        dry_run=args.dry_run,
        db_path=args.db_path,
    )
    log_print(
        "import_sbi_fills: 取込完了",
        f"rows={stats['rows']}",
        f"imported={stats['imported']}",
        f"skipped_dup={stats['skipped_dup']}",
        f"skipped_invalid={stats['skipped_invalid']}",
        "(dry-run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
