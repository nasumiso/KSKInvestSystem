#!/usr/bin/env python3
"""楽天証券 取引履歴CSV → portfolio_shelve fill レイヤー 取込 (issue #360 Phase2)。

売買履歴とアクションログを独立させる方針 (issue #387) により、fill は action_log と
突合しない。本スクリプトは CSV 由来の約定事実 (価格・株数・約定日) を fill として
取り込むだけで、fill は売買履歴タブで一級の表示要素になる。

- fill レイヤー (本スクリプトが書く): CSV 由来の約定事実。価格・株数・約定日の真実源。
- 判断レイヤー (既存 action_log): ステータス・タグ・メモ。手動記録。両者は独立。

本スクリプトのスコープ:
- fill 取込 + dedup 保存 (冪等)

4 層構成 (migrate_portfolio_from_csv.py のパターン踏襲):
    1. CSV 読込層 (read_csv_rows)
    2. 行パース層 (parse_fill_row)
    3. 統合層 (import_csv_to_fills)
    4. 実行層 (main + argparse)

入力: 楽天 取引履歴CSV `tradehistory(JP)_YYYYMMDD.csv` (Shift-JIS, 28列, ヘッダあり)。
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# scripts/ を sys.path に追加 (直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# 定数 (楽天 取引履歴CSV フォーマット)
# ===========================================

CSV_ENCODING = "shift_jis"
EXPECTED_COL_COUNT = 28

# 列インデックス (0-indexed、確認済みフォーマット)
COL_TRADE_DATE = 0     # 約定日 (YYYY/M/D)
COL_CODE_S = 2         # 銘柄コード
COL_TRADE_KIND = 6     # 取引区分 (現物 / 信用新規 / 信用返済 / 現物(単元未満))
COL_BAIBAI = 7         # 売買区分 (買付 / 売付 / 買建 / 売埋)
COL_QTY = 10           # 数量[株]
COL_PRICE = 11         # 単価[円]
COL_AMOUNT = 16        # 受渡金額[円]

HEADER_FIRST_COL = "約定日"  # ヘッダ検証用


# ===========================================
# 1. CSV 読込層
# ===========================================

def read_csv_rows(csv_path: str) -> List[List[str]]:
    """楽天 取引履歴CSV を Shift-JIS で読み、ヘッダを除いたデータ行を返す。

    先頭行が既知ヘッダ (約定日...) でなければ ValueError。
    """
    with open(csv_path, "r", encoding=CSV_ENCODING, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"CSV が空です: {csv_path}")
    header = rows[0]
    if not header or header[0].strip() != HEADER_FIRST_COL:
        raise ValueError(
            f"想定外のヘッダです (先頭列={header[0]!r}, 期待={HEADER_FIRST_COL!r}): {csv_path}"
        )
    return rows[1:]


# ===========================================
# 2. 行パース層
# ===========================================

def _parse_num(raw: str) -> Optional[float]:
    """カンマ区切り数値文字列を float に。空欄/"-" は None。"""
    s = (raw or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_trade_date(raw: str) -> Optional[str]:
    """約定日 "YYYY/M/D" を "YYYY-MM-DD" に正規化。パース不可なら None。"""
    s = (raw or "").strip()
    try:
        d = datetime.strptime(s, "%Y/%m/%d").date()
    except ValueError:
        return None
    return d.isoformat()


class RowSkip(Exception):
    """パース対象外の行 (無効コード・未知区分・数量欠落など)。理由を保持する。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_fill_row(row: List[str]) -> Dict[str, Any]:
    """CSV 1 行を fill 構築用の中間 dict に変換する。

    現物・信用の両方に対応。対象外行は RowSkip を投げる (呼び出し側でカウント)。
    dedup_key の occurrence は統合層で付与するため、ここでは素材のみ返す。
    """
    if len(row) < EXPECTED_COL_COUNT:
        raise RowSkip(f"列数不足 ({len(row)})")

    code_raw = (row[COL_CODE_S] or "").strip()
    try:
        ps.validate_code_s(code_raw)
    except (ValueError, TypeError):
        raise RowSkip(f"無効な銘柄コード: {code_raw!r}")
    code_s = ps.normalize_code_s(code_raw)

    trade_date = _normalize_trade_date(row[COL_TRADE_DATE])
    if trade_date is None:
        raise RowSkip(f"約定日パース不可: {row[COL_TRADE_DATE]!r}")

    baibai = (row[COL_BAIBAI] or "").strip()
    try:
        side = ps.normalize_side(baibai)
    except ValueError as e:
        raise RowSkip(str(e))

    qty_f = _parse_num(row[COL_QTY])
    price_f = _parse_num(row[COL_PRICE])
    if qty_f is None or qty_f <= 0:
        raise RowSkip(f"数量欠落/不正: {row[COL_QTY]!r}")
    if price_f is None or price_f <= 0:
        raise RowSkip(f"単価欠落/不正: {row[COL_PRICE]!r}")
    amount_f = _parse_num(row[COL_AMOUNT])

    trade_kind = (row[COL_TRADE_KIND] or "").strip()

    return {
        "code_s": code_s,
        "trade_date": trade_date,
        "side": side,
        "qty": int(qty_f),
        "price": price_f,
        "amount": int(amount_f) if amount_f is not None else 0,
        "trade_kind": trade_kind,
        "baibai": baibai,
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
    """CSV を読み、各行を fill として冪等取込する。

    同一CSV内の同一 dedup 素材の出現順 (occurrence) を数えて dedup_key に含める
    (同日同単価の別注文を別 fill として扱いつつ、再取込は冪等)。

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
            log_print("import_rakuten_fills: スキップ", e.reason)
            continue

        # occurrence: 同一CSV内で dedup 素材が同一な行の出現順
        occ_key = "|".join(
            [
                parsed["trade_date"],
                parsed["code_s"],
                parsed["trade_kind"],
                parsed["baibai"],
                str(parsed["qty"]),
                f"{parsed['price']:.4f}",
                str(parsed["amount"]),
            ]
        )
        occurrence = occurrence_counter.get(occ_key, 0)
        occurrence_counter[occ_key] = occurrence + 1

        dedup_key = ps.make_dedup_key(
            trade_date=parsed["trade_date"],
            code_s=parsed["code_s"],
            trade_kind=parsed["trade_kind"],
            baibai_kubun=parsed["baibai"],
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
        log_print("import_rakuten_fills: dry-run サンプル (先頭 3 件):")
        for s in samples:
            log_print(
                f"  {s['code_s']} {s['trade_date']} {s['side']} "
                f"qty={s['qty']} price={s['price']} kind={s['trade_kind']}"
            )
    return stats


# ===========================================
# 4. 実行層
# ===========================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="楽天 取引履歴CSV を fill レイヤーへ取込 (issue #360 Phase2)",
    )
    parser.add_argument("csv_path", help="楽天 取引履歴CSV のパス (Shift-JIS)")
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
        "import_rakuten_fills: 取込完了",
        f"rows={stats['rows']}",
        f"imported={stats['imported']}",
        f"skipped_dup={stats['skipped_dup']}",
        f"skipped_invalid={stats['skipped_invalid']}",
        "(dry-run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
