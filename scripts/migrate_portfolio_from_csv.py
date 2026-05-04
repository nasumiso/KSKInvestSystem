#!/usr/bin/env python3
"""
保有銘柄シート CSV → portfolio_shelve 移行スクリプト (Phase 3a / issue #170)

入力: Google スプレッドシートからエクスポートした CSV (36 列、139 銘柄想定)
出力: portfolio_shelve

4 層構成 (migrate_research_from_csv.py のパターン踏襲):
    1. CSV 読込層 (read_csv_rows)
    2. 列パース層 (parse_memo_columns)
    3. 統合層 (build_record_from_row)
    4. 実行層 (migrate_csv_to_portfolio_shelve + main)

重要な前提:
- スプシ 36 列のうち、指標系列 (順位・PER・配当・RS・トレンドテンプレート 等) と
  保有リスト列は code_rank.csv からの VLOOKUP 表示値 → 移行対象外
- ステータスは my_watch_list.txt のみで決定するため、本スクリプトでは設定しない
  (仮で "3監" を埋めて、後段の migrate_my_watch_list_to_shelve.py で上書き)
- 移行対象は計 7 列: 銘柄コード / 銘柄名 / 業態テーマ / IN理由 / 売買アイデア
  / イナゴ元 / 高市感応度
"""

import argparse
import csv
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

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
# 定数
# ===========================================

EXPECTED_COL_COUNT = 36

# 列インデックス (確定済み、計画書 §3-2-2 参照)
COL_CODE_S = 0           # 銘柄コード
COL_STOCK_NAME = 1       # 銘柄名
COL_GYOUTAI = 3          # 業態・テーマ
COL_WATCH_REASON = 31    # ウォッチ・IN理由
COL_INAGO = 33           # イナゴ元・きっかけ
COL_TRADE_IDEA = 34      # 投資売買アイデア
COL_TAKAICHI = 35        # 高市感応度


# ===========================================
# 1. CSV 読込層
# ===========================================

def read_csv_rows(csv_path: str) -> List[List[str]]:
    """CSV を読み込み、ヘッダ・空行を除外した 36 列リストを返す。

    入力 CSV の構造:
    - 1 行目: 全列空 (区切り文字のみ)
    - 2 行目: ヘッダ
    - 3 行目以降: データ 139 行

    - 全列空の行はスキップ
    - 列数が 36 未満ならパディング、超えていれば警告して切り詰め
    """
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        raw_rows = list(reader)

    if not raw_rows:
        return []

    # 先頭の 2 行 (空行 + ヘッダ) をスキップ
    # 2 行目がヘッダなのは確認済み (col[0]="銘柄コード")
    data_start = 0
    for i, row in enumerate(raw_rows):
        if any((cell or "").strip() for cell in row):
            # 最初の非空行がヘッダ
            data_start = i + 1
            break

    data_rows = raw_rows[data_start:]

    result: List[List[str]] = []
    for row in data_rows:
        if not any((cell or "").strip() for cell in row):
            continue
        if len(row) < EXPECTED_COL_COUNT:
            row = row + [""] * (EXPECTED_COL_COUNT - len(row))
        elif len(row) > EXPECTED_COL_COUNT:
            log_warning(
                f"migrate_portfolio: 列数が {EXPECTED_COL_COUNT} を超える行を切り詰めます: "
                f"code={row[COL_CODE_S]!r}, cols={len(row)}"
            )
            row = row[:EXPECTED_COL_COUNT]
        result.append(row)
    return result


# ===========================================
# 2. 列パース層
# ===========================================

def parse_memo_columns(row: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """5 つのメモ列をパースして memo dict を返す。

    Returns: (memo dict, warnings)
    """
    warnings: List[str] = []
    if len(row) < EXPECTED_COL_COUNT:
        warnings.append(
            f"列数不足: {len(row)} < {EXPECTED_COL_COUNT}"
        )
        return ps.create_memo(), warnings

    memo = ps.create_memo(
        gyoutai_theme=(row[COL_GYOUTAI] or "").strip(),
        watch_in_reason=(row[COL_WATCH_REASON] or "").strip(),
        trade_idea=(row[COL_TRADE_IDEA] or "").strip(),
        inago_origin=(row[COL_INAGO] or "").strip(),
        takaichi_sensitivity=(row[COL_TAKAICHI] or "").strip(),
    )
    return memo, warnings


# ===========================================
# 3. 統合層
# ===========================================

def build_record_from_row(
    row: List[str],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """1行から portfolio レコードを組み立てる。

    Returns: (record dict, warnings)
        - 銘柄コードが不正 / 空の場合は (None, warnings) を返す
    """
    warnings: List[str] = []
    code_raw = (row[COL_CODE_S] or "").strip() if len(row) > COL_CODE_S else ""
    if not code_raw:
        warnings.append("空の銘柄コード行をスキップ")
        return None, warnings

    try:
        ps.validate_code_s(code_raw)
    except (ValueError, TypeError) as exc:
        warnings.append(f"不正な銘柄コード: {code_raw!r} ({exc})")
        return None, warnings

    code_s = ps.normalize_code_s(code_raw)
    # 銘柄名は portfolio_shelve には保存しない (要件 §4: 表示時に他DBから取得)。
    # CSV の銘柄名列は無視するが、空欄は CSV 側の不備として警告のみ出す。
    if len(row) > COL_STOCK_NAME and not (row[COL_STOCK_NAME] or "").strip():
        warnings.append(f"{code_s}: CSV の銘柄名列が空 (portfolio_shelve に保存しないため致命的ではない)")

    memo, memo_warnings = parse_memo_columns(row)
    warnings.extend(memo_warnings)

    # 仕様: ステータスは仮で 3監 (後段の txt 取り込みで上書きされる)
    record = ps.create_record(
        code_s,
        status="3監",
        memo=memo,
    )
    return record, warnings


# ===========================================
# 4. 実行層
# ===========================================

def migrate_csv_to_portfolio_shelve(
    csv_path: str,
    *,
    dry_run: bool = False,
    db_path: Optional[str] = None,
    record_initial_log: bool = True,
) -> Dict[str, Any]:
    """CSV 全体を portfolio_shelve に移行する。

    Args:
        csv_path: 入力 CSV パス
        dry_run: True なら読み込み・組立まで行うが書き込まない
        db_path: portfolio_shelve のパス上書き (テスト用)
        record_initial_log: True なら各レコードに 初回登録 ログを記録 (デフォルト)

    Returns: {"total": int, "saved": int, "skipped": int, "warnings": List[str]}
    """
    rows = read_csv_rows(csv_path)
    log_print(f"migrate_portfolio: {len(rows)} 行を読み込み")

    saved = 0
    skipped = 0
    all_warnings: List[str] = []
    sample_records: List[Dict[str, Any]] = []

    for row in rows:
        record, warnings = build_record_from_row(row)
        if warnings:
            all_warnings.extend(warnings)
            for w in warnings:
                log_warning(f"migrate_portfolio: {w}")
        if record is None:
            skipped += 1
            continue
        if dry_run:
            if len(sample_records) < 3:
                sample_records.append(record)
            saved += 1
            continue
        # 既存があれば上書き、新規なら追加
        existed = ps.get_record(record["code_s"], db_path=db_path) is not None
        ps.upsert_record(record, db_path=db_path)
        if record_initial_log and not existed:
            ps.append_action_log(
                record["code_s"],
                "初回登録",
                status_from=None,
                status_to=record["status"],
                reason="スプシ移行",
                db_path=db_path,
            )
        saved += 1

    log_print(
        f"migrate_portfolio: 保存 {saved} 件、スキップ {skipped} 件、"
        f"警告 {len(all_warnings)} 件"
    )
    if dry_run and sample_records:
        log_print("migrate_portfolio: dry-run サンプル (先頭 3 件):")
        for rec in sample_records:
            log_print(f"  - {rec['code_s']} status={rec['status']}")
            for k, v in rec["memo"].items():
                if v:
                    snippet = v[:40].replace("\n", " ")
                    log_print(f"      {k}: {snippet}{'...' if len(v) > 40 else ''}")
    return {
        "total": len(rows),
        "saved": saved,
        "skipped": skipped,
        "warnings": all_warnings,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="保有銘柄シート CSV を portfolio_shelve に移行する",
    )
    parser.add_argument(
        "csv_path",
        type=str,
        help="入力 CSV パス (例: $KS_DATA_DIR/migration/portfolio_sheet.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB に書き込まず、読み込み結果のサマリだけ表示する",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="portfolio_shelve のパス上書き (テスト用)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if not os.path.isfile(args.csv_path):
        log_warning(f"migrate_portfolio: CSV が存在しません: {args.csv_path}")
        return 2
    result = migrate_csv_to_portfolio_shelve(
        args.csv_path,
        dry_run=args.dry_run,
        db_path=args.db_path,
    )
    print(
        f"total={result['total']} saved={result['saved']} "
        f"skipped={result['skipped']} warnings={len(result['warnings'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
