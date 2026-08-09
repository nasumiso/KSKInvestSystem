#!/usr/bin/env python3
"""マネックス証券 取引履歴CSV → portfolio_shelve fill レイヤー 取込 (issue #390)。

楽天・SBI と同じ fill レイヤーへ取り込むが、マネックスの CSV は列構成・語彙が異なる。
マネックスは現在利用しておらず、**過去データの一度きりバックフィル**用途。

マネックス CSV の特徴 (Shift-JIS, 25列):
- 1行目が `データ作成日：YYYY/MM/DD HH:MM:SS` のメタ行。ヘッダは2行目
- `商品` (信用新規/信用返済/現引/株式) と `取引` (半年新規買い/ご売却 等) の
  2列の組で売買区分が決まる
- 銘柄コードは **5桁+パディング空白** (`54710    `)。末尾1桁は付加桁なので落とす
- 信用返済行は 建約定日・建単価 (楽天と同じ) と 受渡金額=決済損益 (SBI と同じ) の両方を持つ
- 税金・入出金・入出庫・配当金の行が混在するので、約定行のみ取り込む
- ETF (成長株ウォッチリスト外) は取込対象外 (issue #387)

楽天との共通処理 (_parse_num / _normalize_trade_date / RowSkip) は
import_rakuten_fills から再利用する (SBI 版と同じ依存の張り方)。
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

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# 定数 (マネックス 取引履歴CSV フォーマット)
# ===========================================

CSV_ENCODING = "shift_jis"
EXPECTED_COL_COUNT = 25

# 列インデックス (0-indexed、実CSVで確認済み)
COL_TRADE_DATE = 0     # 約定日 (YYYY/MM/DD)
COL_PRODUCT = 3        # 商品 (信用新規 / 信用返済 / 現引 / 株式)
COL_ACTION = 4         # 取引 (半年新規買い / 半年返済売り / お買付 / ご売却 ...)
COL_CODE_S = 5         # 銘柄コード (5桁+空白)
COL_QTY = 7            # 数量（株/口）/返済数量
COL_PRICE = 8          # 単価/返済約定単価
COL_AMOUNT = 12        # 受渡金額(円) — 信用返済行では決済損益
COL_TATE_DATE = 13     # 建約定日 (信用返済/現引行のみ)
COL_TATE_PRICE = 14    # 建単価 (信用返済/現引行のみ)

HEADER_FIRST_COL = "約定日"
HEADER_MARKER = "建単価"  # 楽天/SBI に無い列名。ヘッダ行の同定に使う

# (商品, 取引) → (side, 楽天互換の trade_kind)。
# trade_kind を楽天語彙に合わせることで売買履歴タブの集約・表示を一貫させる。
# ここに無い組合せ (税金・入出金・入出庫・配当金) は約定でないので RowSkip される。
_ACTION_MAP = {
    ("信用新規", "半年新規買い"): ("buy", "信用新規"),
    ("信用新規", "半年新規売り"): ("sell", "信用新規"),
    ("信用返済", "半年返済売り"): ("sell", "信用返済"),
    ("信用返済", "半年返済買い"): ("buy", "信用返済"),
    ("現引", "半年現引"): ("buy", "現引"),
    ("株式", "お買付"): ("buy", "現物"),
    ("株式", "ご売却"): ("sell", "現物"),
}

# 商品列の値。_ACTION_MAP を通った後は商品だけで取引の性質が決まる。
# 信用返済 = 受渡金額列が決済損益。現引 = 建玉の現物化 (損益は確定しないが建単価は持つ)。
PRODUCT_SETTLE = "信用返済"
PRODUCT_GENBIKI = "現引"

BROKER = "マネックス"


# ===========================================
# 1. CSV 読込層
# ===========================================

def _find_header_row(rows: List[List[str]]) -> int:
    """ヘッダ行 (`約定日,...,建単価,...`) の index を返す。無ければ -1。"""
    for i, row in enumerate(rows):
        if row and row[0].strip() == HEADER_FIRST_COL and HEADER_MARKER in [c.strip() for c in row]:
            return i
    return -1


def is_monex_csv(csv_path: str) -> bool:
    """CSV がマネックス 取引履歴CSV 形式か判定する。

    楽天 (先頭行ヘッダ・28列) / SBI (メタ行+14列) とは列数と `建単価` 列の有無で区別する。
    """
    try:
        with open(csv_path, "r", encoding=CSV_ENCODING, newline="") as f:
            rows = list(csv.reader(f))
    except (OSError, UnicodeDecodeError):
        return False
    hi = _find_header_row(rows)
    return hi >= 0 and len(rows[hi]) == EXPECTED_COL_COUNT


def read_csv_rows(csv_path: str) -> List[List[str]]:
    """マネックス 取引履歴CSV を Shift-JIS で読み、ヘッダ行以降のデータ行を返す。

    冒頭の `データ作成日：...` メタ行を読み飛ばし、ヘッダ行を探す。
    ヘッダが見つからなければ ValueError。
    """
    with open(csv_path, "r", encoding=CSV_ENCODING, newline="") as f:
        rows = list(csv.reader(f))
    hi = _find_header_row(rows)
    if hi < 0:
        raise ValueError(f"ヘッダ行 (約定日...建単価) が見つかりません: {csv_path}")
    return rows[hi + 1:]


# ===========================================
# 2. 行パース層
# ===========================================

def _normalize_monex_code(raw: str) -> str:
    """マネックスの5桁銘柄コード (`54710    `) を4桁 code_s (`5471`) に変換する。

    末尾1桁は付加桁で、確認済みのマネックス形式では常に `0` (`471A0` → `471A`)。
    末尾が `0` の5桁のときだけ落とす。`54711` のような想定外の値まで切り詰めると
    `5471` として validate_code_s を通り、別銘柄の fill として保存されてしまうため、
    そのまま返して無効コードとしてスキップさせる。
    """
    s = (raw or "").strip().upper()
    return s[:-1] if len(s) == 5 and s.endswith("0") else s


def parse_fill_row(row: List[str]) -> Dict[str, Any]:
    """マネックス CSV 1 行を fill 構築用の中間 dict に変換する。

    約定でない行 (税金・入出金・入出庫・配当金) と ETF は RowSkip。空行・列数不足も RowSkip。
    """
    if not any(c.strip() for c in row):
        raise RowSkip("空行")
    if len(row) < EXPECTED_COL_COUNT:
        raise RowSkip(f"列数不足 ({len(row)})")

    # 先に取引区分を見る (税金・入出金行は銘柄コードも空なので、より的確な理由を出せる)
    product = (row[COL_PRODUCT] or "").strip()
    action = (row[COL_ACTION] or "").strip()
    mapped = _ACTION_MAP.get((product, action))
    if mapped is None:
        raise RowSkip(f"約定行でない/未知の取引区分: {product!r}/{action!r}")
    side, trade_kind = mapped

    code_s = _normalize_monex_code(row[COL_CODE_S])
    try:
        ps.validate_code_s(code_s)
    except (ValueError, TypeError):
        raise RowSkip(f"無効な銘柄コード: {row[COL_CODE_S]!r}")

    # ETF は株式分析の対象外なので取込まない (issue #387)。
    # なお SBI 版の resolve_stock_name によるウォッチリスト外除外は、過去バックフィルで
    # 当時売買した銘柄を落としてしまうため本スクリプトでは採用しない (issue #390)。
    if ps.is_etf_code(code_s):
        raise RowSkip(f"ETF のため対象外: {code_s}")

    trade_date = _normalize_trade_date(row[COL_TRADE_DATE])
    if trade_date is None:
        raise RowSkip(f"約定日パース不可: {row[COL_TRADE_DATE]!r}")

    qty_f = _parse_num(row[COL_QTY])
    price_f = _parse_num(row[COL_PRICE])
    if qty_f is None or qty_f <= 0:
        raise RowSkip(f"数量欠落/不正: {row[COL_QTY]!r}")
    if price_f is None or price_f <= 0:
        raise RowSkip(f"単価欠落/不正: {row[COL_PRICE]!r}")

    amount_f = _parse_num(row[COL_AMOUNT])
    amount = int(amount_f) if amount_f is not None else 0
    # 信用返済行の受渡金額列は諸経費控除後の「決済損益」。P/L の真実源として持たせる。
    # パース不能時は 0 (損益ゼロ) と区別できないので None (不明) のままにする。
    settle_pl = int(amount_f) if (product == PRODUCT_SETTLE and amount_f is not None) else None

    # 建約定日・建単価 (信用返済/現引行のみ)。信用ラウンドの建玉コスト算出に使う。
    # 信用新規行にも同じ列が埋まっているが、そこは自身の約定日・単価のエコーで情報量が無く、
    # 「建玉を持っていた」という誤った意味を持たせないため決済側の行に限って取り込む。
    tate_date = tate_price = None
    if product in (PRODUCT_SETTLE, PRODUCT_GENBIKI):
        tate_date = _normalize_trade_date(row[COL_TATE_DATE])
        tate_price_f = _parse_num(row[COL_TATE_PRICE])
        if tate_price_f is not None and tate_price_f > 0:
            tate_price = tate_price_f

    return {
        "code_s": code_s,
        "trade_date": trade_date,
        "side": side,
        "qty": int(qty_f),
        "price": price_f,
        "amount": amount,
        "trade_kind": trade_kind,
        # dedup 素材。side が dedup キーに入らないため、同日同数量同単価の
        # 新規買 / 新規売 を区別できるよう商品と取引を連結した元区分を持たせる
        "kubun": f"{product}/{action}",
        "settle_pl": settle_pl,
        "tate_date": tate_date,
        "tate_price": tate_price,
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
    """マネックス CSV を読み、各行を fill として冪等取込する。

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
            log_print("import_monex_fills: スキップ", e.reason)
            continue

        # occurrence: 同一CSV内で dedup 素材が同一な行の出現順 (分割約定を別 fill に)
        occ_key = "|".join(
            [
                parsed["trade_date"],
                parsed["code_s"],
                parsed["trade_kind"],
                parsed["kubun"],
                str(parsed["qty"]),
                f"{parsed['price']:.4f}",
                str(parsed["amount"]),
            ]
        )
        occurrence = occurrence_counter.get(occ_key, 0)
        occurrence_counter[occ_key] = occurrence + 1

        # baibai_kubun にはマネックスの元区分 (商品/取引) を渡し、楽天・SBI と dedup 空間を分ける
        dedup_key = ps.make_dedup_key(
            trade_date=parsed["trade_date"],
            code_s=parsed["code_s"],
            trade_kind=parsed["trade_kind"],
            baibai_kubun=parsed["kubun"],
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
            broker=BROKER,
            settle_pl=parsed["settle_pl"],
            tate_date=parsed["tate_date"],
            tate_price=parsed["tate_price"],
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
        log_print("import_monex_fills: dry-run サンプル (先頭 3 件):")
        for s in samples:
            log_print(
                f"  {s['code_s']} {s['trade_date']} {s['side']} "
                f"qty={s['qty']} price={s['price']} kind={s['trade_kind']} "
                f"settle_pl={s['settle_pl']} tate_price={s['tate_price']}"
            )
    return stats


# ===========================================
# 4. 実行層
# ===========================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="マネックス証券 取引履歴CSV を fill レイヤーへ取込 (issue #390)",
    )
    parser.add_argument("csv_path", help="マネックス 取引履歴CSV のパス (Shift-JIS)")
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
        "import_monex_fills: 取込完了",
        f"rows={stats['rows']}",
        f"imported={stats['imported']}",
        f"skipped_dup={stats['skipped_dup']}",
        f"skipped_invalid={stats['skipped_invalid']}",
        "(dry-run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
