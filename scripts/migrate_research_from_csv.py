#!/usr/bin/env python3
"""
銘柄調査スプレッドシート CSV → research_shelve 移行スクリプト (issue #92)

入力: Google スプレッドシートからエクスポートした CSV (853 行 × 17 列)
出力: research_shelve (data/stock_data/research_shelve)

4 層構成:
    1. CSV 読込層 (read_csv_rows)
    2. 列パース層 (parse_ir_column / parse_quality_column /
       parse_institutional_column / parse_shikiho_columns)
    3. 統合層 (build_record_from_row)
    4. 実行層 (migrate_csv_to_research_shelve + main)

各パーサは純粋関数で、warnings を戻り値で返す(ログには出さない)。
実行層だけが IO を持つ。
"""

import argparse
import csv
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を sys.path に追加(直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import research_shelve as rs  # noqa: E402

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

EXPECTED_COL_COUNT = 17

# IR 分析列のブロック境界: YY.M の直後が [ / 空白 / タブ / 行末 のいずれか
# `.` 続きは自動的に除外される(22.9.1Q決算説明資料 パターン)
IR_BLOCK_HEADER = re.compile(r"^(\d{2})\.(\d{1,2})(?=\[|\s|$)")

# クォリティ指標列のブロック境界: YY.M 単独行
QUALITY_BLOCK_HEADER = re.compile(r"^(\d{2})\.(\d{1,2})$")

# 機関投資家列の日付行(同一行パターン): YY.M<空白>+内容
INST_DATE_SAME_LINE = re.compile(r"^(\d{2})\.(\d{1,2})\s+(.+)$")

# 機関投資家列の日付行(別行パターン): YY.M 単独
INST_DATE_ALONE = re.compile(r"^(\d{2})\.(\d{1,2})\s*$")


# ===========================================
# 1. CSV 読込層
# ===========================================

def read_csv_rows(csv_path: str) -> List[List[str]]:
    """CSV を読み込み、ヘッダ行と全列空行を除外した 17 列リストを返す。

    - 1 行目はヘッダ(code 列に `6360` が混入しているが他は列名)としてスキップ
    - 全列空の行はスキップ
    - 列数が 17 未満の行は末尾を空文字でパディング
    - 列数が 17 を超える行は警告ログを出しつつ末尾を切り詰める
    """
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        raw_rows = list(reader)

    if not raw_rows:
        return []

    # 1 行目はヘッダとしてスキップ
    data_rows = raw_rows[1:]

    result: List[List[str]] = []
    for row in data_rows:
        # 全列空ならスキップ
        if not any((cell or "").strip() for cell in row):
            continue
        # 列数を 17 に揃える
        if len(row) < EXPECTED_COL_COUNT:
            row = row + [""] * (EXPECTED_COL_COUNT - len(row))
        elif len(row) > EXPECTED_COL_COUNT:
            log_warning(
                f"migrate: 列数が {EXPECTED_COL_COUNT} を超える行を切り詰めます: "
                f"code={row[0]!r}, cols={len(row)}"
            )
            row = row[:EXPECTED_COL_COUNT]
        result.append(row)
    return result


# ===========================================
# 2. 列パース層
# ===========================================

def _split_lines(text: str) -> List[str]:
    """セル内テキストを行単位に分割し、各行の末尾 \\r\\n を削除する。

    - `splitlines()` は \\u2028 等も行区切りとみなすため使わない
    - `split("\\n") + rstrip("\\r\\n")` は LF/CRLF の両方に耐性がある
    """
    if not text:
        return []
    return [line.rstrip("\r\n") for line in text.split("\n")]


def parse_ir_column(text: str) -> Tuple[str, Dict[str, Dict[str, str]], List[Dict[str, str]]]:
    """IR 分析列をパース。

    Returns:
        (overview, blocks, warnings)
        overview: 先頭の日付なしヘッダ行(企業概要)
        blocks: { "YY.M": {"ir_quant": str, "ir_comment": str}, ... }
        warnings: list of {"column": "ir", "message": "..."}
    """
    lines = _split_lines(text)
    overview_lines: List[str] = []
    blocks: Dict[str, Dict[str, str]] = {}
    warnings: List[Dict[str, str]] = []
    current_date: Optional[str] = None  # 現在のブロック日付
    current_comments: List[str] = []    # 現在のブロックのコメント行(ir_comment 用)

    def flush_current():
        """current_date/current_comments を blocks に書き戻す"""
        nonlocal current_comments
        if current_date is not None and current_comments:
            # 既存のコメントに追記(同日が重複した場合を想定)
            existing = blocks[current_date].get("ir_comment", "")
            joined = "\n".join(current_comments)
            if existing:
                blocks[current_date]["ir_comment"] = existing + "\n" + joined
            else:
                blocks[current_date]["ir_comment"] = joined
        current_comments = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # 空行はスキップ(ir_comment に空行を含めない)
            continue

        # ブロック境界検出
        m = IR_BLOCK_HEADER.match(stripped)
        if m:
            yy_str = m.group(1)
            mm_str = m.group(2)
            mm = int(mm_str)
            if 1 <= mm <= 12:
                # 正常なブロック境界
                flush_current()
                # YY は leading zero を保持 (09.7 → "09.7" のまま)
                date_key = f"{yy_str}.{mm}"
                # 日付の直後(ir_quant 部分)
                rest = stripped[len(m.group(0)):]
                # 先頭の空白/タブを取り除く前に、[ で始まるかチェック
                # rest は既に lookahead で [/\\s/$ のいずれか
                ir_quant = rest  # 原文保持(前方空白込み)
                blocks[date_key] = {
                    "ir_quant": ir_quant,
                    "ir_comment": "",
                }
                current_date = date_key
                continue
            else:
                # 月範囲外 → warning 追加、ブロック境界にしない
                warnings.append({
                    "column": "ir",
                    "message": f"不正日付行を破棄: {stripped!r}",
                })
                # 現在のブロックに属していればコメント扱い、なければ overview 扱い
                if current_date is not None:
                    current_comments.append(stripped)
                else:
                    overview_lines.append(stripped)
                continue

        # ブロック境界ではない普通の行
        if current_date is None:
            # 企業概要(先頭)
            overview_lines.append(stripped)
        else:
            # 現在のブロックのコメント
            current_comments.append(stripped)

    flush_current()
    overview = "\n".join(overview_lines)
    return overview, blocks, warnings


def parse_quality_column(
    text: str,
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """クォリティ指標列をパース。

    Returns:
        blocks: { "YY.M": "改行込み原文", ... }
        warnings: list of {"column": "quality", "message": "..."}

    ブロック境界検出ルール:
        - 行の strip 後、`^\\d{2}\\.\\d{1,2}$` (単独行) にマッチ
        - 月が 1-12 のみ有効
    """
    lines = _split_lines(text)
    blocks: Dict[str, str] = {}
    warnings: List[Dict[str, str]] = []
    current_date: Optional[str] = None
    current_content: List[str] = []
    # 月範囲外の日付行に続く本文を捨てるためのフラグ
    # (前ブロックへの混入を防ぐ)
    in_invalid_block = False
    # ブロック境界に入る前の冒頭行 (破棄される)
    preamble_lines: List[str] = []

    def flush_current():
        nonlocal current_content
        if current_date is not None:
            # 末尾の空行を除去
            while current_content and not current_content[-1].strip():
                current_content.pop()
            blocks[current_date] = "\n".join(current_content)
        current_content = []

    for line in lines:
        stripped = line.strip()
        m = QUALITY_BLOCK_HEADER.match(stripped)
        if m:
            yy_str = m.group(1)
            mm_str = m.group(2)
            mm = int(mm_str)
            if 1 <= mm <= 12:
                # 正常なブロック境界 → 前のブロックを flush
                flush_current()
                # YY は leading zero を保持 (09.7 → "09.7" のまま)
                current_date = f"{yy_str}.{mm}"
                in_invalid_block = False
                continue
            else:
                # 月範囲外 → warning + 破棄
                # 前ブロックを flush して隔離し、以降の非境界行は捨てる
                flush_current()
                current_date = None
                in_invalid_block = True
                warnings.append({
                    "column": "quality",
                    "message": f"不正日付行を破棄: {stripped!r}",
                })
                continue

        # ブロック境界ではない行
        if in_invalid_block:
            # 月範囲外ブロックの本文 → 捨てる(前ブロック汚染防止)
            continue
        if current_date is None:
            # 冒頭の非境界行(warning + 破棄対象)
            if stripped:
                preamble_lines.append(line)
        else:
            # 現在のブロックに追加(原文保持)
            current_content.append(line)

    flush_current()

    # 冒頭の非境界行があれば warning
    if preamble_lines:
        warnings.append({
            "column": "quality",
            "message": f"冒頭の非境界行を破棄: {preamble_lines!r}",
        })

    return blocks, warnings


def parse_institutional_column(
    text: str,
) -> Tuple[str, Dict[str, str], List[Dict[str, str]]]:
    """機関投資家列をパース。

    Returns:
        (institutional_comment, kairi_by_date, warnings)
        institutional_comment: 日付行以外の全行を改行連結(出現順保持)
        kairi_by_date: { "YY.M": "理論株価乖離文字列(日付を除いた部分)", ... }
        warnings: list of {"column": "institutional", "message": "..."}

    日付行検出ルール:
        - 同一行パターン: `^(\\d{2}\\.\\d{1,2})\\s+(.+)$`
        - 別行パターン: `^(\\d{2}\\.\\d{1,2})\\s*$` + 次行に値
        - 月が 1-12 のみ有効
    """
    lines = _split_lines(text)
    comment_lines: List[str] = []
    kairi_by_date: Dict[str, str] = {}
    warnings: List[Dict[str, str]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # 同一行パターン: "YY.M 内容"
        m_same = INST_DATE_SAME_LINE.match(stripped)
        if m_same:
            yy_str = m_same.group(1)
            mm = int(m_same.group(2))
            rest = m_same.group(3).strip()
            if 1 <= mm <= 12:
                # YY は leading zero を保持
                date_key = f"{yy_str}.{mm}"
                # 同日が既にあれば上書き(後勝ち)
                kairi_by_date[date_key] = rest
                i += 1
                continue
            # 月範囲外 → コメントに降格 + warning
            warnings.append({
                "column": "institutional",
                "message": f"不正日付行を降格: {stripped!r}",
            })
            comment_lines.append(stripped)
            i += 1
            continue

        # 別行パターン: "YY.M" 単独
        m_alone = INST_DATE_ALONE.match(stripped)
        if m_alone:
            yy_str = m_alone.group(1)
            mm = int(m_alone.group(2))
            if 1 <= mm <= 12:
                # YY は leading zero を保持
                date_key = f"{yy_str}.{mm}"
                # 次行を確認(空行をスキップしつつ)
                j = i + 1
                next_non_empty: Optional[str] = None
                while j < len(lines):
                    nxt = lines[j].strip()
                    if nxt:
                        next_non_empty = nxt
                        break
                    j += 1

                if next_non_empty is None:
                    # 末尾 → 値なし + warning
                    warnings.append({
                        "column": "institutional",
                        "message": f"日付単独行の次行が不正(末尾): {stripped!r}",
                    })
                    i += 1
                    continue

                # 次行が別の日付行(同一/別)なら値なしで破棄
                if INST_DATE_SAME_LINE.match(next_non_empty) or INST_DATE_ALONE.match(next_non_empty):
                    warnings.append({
                        "column": "institutional",
                        "message": f"日付単独行の次行が不正(日付): {stripped!r}",
                    })
                    i += 1  # 次行は別の日付行なので、そこから処理継続
                    continue

                # 次行を kairi の値として紐付け(既存があれば上書き)
                kairi_by_date[date_key] = next_non_empty
                i = j + 1
                continue
            # 月範囲外 → コメントに降格 + warning
            warnings.append({
                "column": "institutional",
                "message": f"不正日付行を降格: {stripped!r}",
            })
            comment_lines.append(stripped)
            i += 1
            continue

        # 日付行ではない → コメント行
        comment_lines.append(stripped)
        i += 1

    institutional_comment = "\n".join(comment_lines)
    return institutional_comment, kairi_by_date, warnings


def parse_shikiho_columns(row: List[str]) -> List[str]:
    """四季報コメント列(col 11-16)を 0-6 件のリストにまとめる。

    - col 11(最新想定)→ col 16 の順で走査
    - 空文字/空白のみのセルはスキップ
    - col 16 に稀にあふれているデータも拾う
    - warning は発生しない(単純な連結のみ)
    """
    result: List[str] = []
    for col_idx in range(11, 17):
        if col_idx >= len(row):
            break
        cell = (row[col_idx] or "").strip()
        if cell:
            result.append(cell)
    return result


# ===========================================
# 3. 統合層
# ===========================================

def build_record_from_row(row: List[str]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """1 行 (17 列) を research_shelve レコードに変換。

    Returns:
        record: create_research_record() が返す dict
        parse_warnings: パーサ層のソフトエラー一覧(各要素に code_s を付与)

    例外 (ハードエラー、呼び出し側が failed_rows にキャッチ):
        - validate_code_s / validate_rating が ValueError
        - stock_name が文字列でない TypeError
    """
    code_s = row[0]
    stock_name = row[1]
    analysis_date_raw = row[2]
    kessan_date_raw = row[3]
    overall_rating = row[4]
    ir_text = row[5]
    quality_text = row[6]
    inst_text = row[7]
    openwork = row[8]
    memo = row[9]
    cramer = row[10]

    # パーサ層を呼ぶ
    overview, ir_blocks, ir_warnings = parse_ir_column(ir_text)
    quality_blocks, quality_warnings = parse_quality_column(quality_text)
    institutional_comment, kairi_by_date, inst_warnings = parse_institutional_column(
        inst_text
    )
    shikiho_comments = parse_shikiho_columns(row)

    # warnings に code_s を付与
    normalized_code = rs.normalize_code_s(code_s)
    parse_warnings: List[Dict[str, str]] = []
    for w in ir_warnings + quality_warnings + inst_warnings:
        parse_warnings.append({"code_s": normalized_code, **w})

    # 3 列の日付セットの和集合
    all_dates = set(ir_blocks.keys()) | set(quality_blocks.keys()) | set(kairi_by_date.keys())
    sorted_dates = sorted(all_dates, key=rs.date_yy_m_sort_key, reverse=True)

    # 各日付についてスナップショット生成
    snapshots: List[Dict[str, Any]] = []
    for date_key in sorted_dates:
        ir_block = ir_blocks.get(date_key, {})
        snap = rs.create_snapshot(
            date_key,
            ir_quant=ir_block.get("ir_quant", ""),
            ir_comment=ir_block.get("ir_comment", ""),
            quality_indicators=quality_blocks.get(date_key, ""),
            rironkabuka_kairi=kairi_by_date.get(date_key, ""),
            data_source="migration",
        )
        snapshots.append(snap)

    # レコード組み立て(ハードエラーは ValueError/TypeError で飛ぶ)
    record = rs.create_research_record(
        code_s,
        stock_name,
        overview=overview,
        overall_rating=overall_rating,
        institutional_comment=institutional_comment,
        memo=memo,
        openwork=openwork,
        cramer=cramer,
        shikiho_comments=shikiho_comments,
        snapshots=snapshots,
        analysis_date_raw=analysis_date_raw,
        kessan_date_raw=kessan_date_raw,
    )
    return record, parse_warnings


# ===========================================
# 4. 実行層
# ===========================================

def migrate_csv_to_research_shelve(
    csv_path: str,
    *,
    db_path: Optional[str] = None,
    dry_run: bool = False,
    progress_every: int = 100,
    show_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """CSV の全行を research_shelve に移行する。

    手順:
        1. read_csv_rows(csv_path) で 17 列のリストを取得
        2. 移行前バックアップ: dry_run=False の場合のみ backup_research_db(db_path=...)
        3. 各行について build_record_from_row(row) → upsert_research_record(...)
           - dry_run=True の場合は upsert をスキップ(build は呼んで検証のみ)
           - 失敗時は log_warning + failed_rows に追加(行は継続)
        4. progress_every 件ごとに進捗ログ
        5. show_codes が指定されていたら get_research_record + format_record_full
           を呼んで stdout に出力(None 安全)
        6. サマリ dict を返す
    """
    log_print(f"[migrate] 読み込み: {csv_path}")

    rows = read_csv_rows(csv_path)
    total = len(rows)
    log_print(f"[migrate] 有効行: {total}")

    # 移行前バックアップ(dry_run=False のみ)
    backup_paths: List[str] = []
    if not dry_run:
        try:
            backup_paths = rs.backup_research_db(db_path=db_path)
            if backup_paths:
                log_print(f"[migrate] バックアップ(実行前): {backup_paths}")
            else:
                log_print("[migrate] バックアップ(実行前): 既存 DB なし、スキップ")
        except Exception as e:
            log_warning(f"[migrate] バックアップ失敗(継続): {e}")

    # 移行本体
    failed_rows: List[Dict[str, str]] = []
    parse_warnings: List[Dict[str, str]] = []
    succeeded = 0

    if dry_run:
        log_print("[migrate] dry_run=True: DB に書き込まず検証のみ実行")
    log_print("[migrate] 移行開始...")

    for idx, row in enumerate(rows, start=1):
        try:
            record, row_warnings = build_record_from_row(row)
            parse_warnings.extend(row_warnings)
            if not dry_run:
                rs.upsert_research_record(record, db_path=db_path)
            succeeded += 1
        except (ValueError, TypeError) as e:
            log_warning(
                f"[migrate] 行失敗 (code={row[0]!r}, name={row[1]!r}): {e}"
            )
            failed_rows.append({
                "code_s": row[0],
                "stock_name": row[1],
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        if progress_every and idx % progress_every == 0:
            log_print(f"[migrate] {idx}/{total} ...")

    log_print(
        f"[migrate] 完了: 成功 {succeeded} 件、失敗 {len(failed_rows)} 件"
        f" (パース warning {len(parse_warnings)} 件)"
    )

    if failed_rows:
        log_print("[migrate] 失敗行一覧:")
        for fr in failed_rows:
            log_print(f"  - {fr['code_s']} ({fr['stock_name']}): {fr['error']}")

    if parse_warnings and len(parse_warnings) <= 30:
        log_print("[migrate] パース warning 一覧:")
        for w in parse_warnings:
            log_print(
                f"  - {w.get('code_s', '')} [{w['column']}]: {w['message']}"
            )
    elif parse_warnings:
        log_print(
            f"[migrate] パース warning が {len(parse_warnings)} 件あります"
            "(30 件超のため個別表示は省略)"
        )

    # --show 対応
    if show_codes:
        for code in show_codes:
            code = code.strip()
            if not code:
                continue
            log_print(f"[migrate] --show {code} ----------------------------")
            record = rs.get_research_record(code, db_path=db_path)
            if record is None:
                log_warning(f"[migrate] --show: レコード未登録: {code}")
                continue
            print(rs.format_record_full(record))

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": len(failed_rows),
        "failed_rows": failed_rows,
        "parse_warnings": parse_warnings,
        "dry_run": dry_run,
        "backup_paths": backup_paths,
    }


# ===========================================
# CLI エントリポイント
# ===========================================

def main() -> int:
    """CLI エントリポイント。

    usage: python migrate_research_from_csv.py <csv_path>
                     [--dry-run] [--db-path PATH] [--show CODE[,CODE...]]
    """
    parser = argparse.ArgumentParser(
        description="銘柄調査スプシ CSV を research_shelve に移行する"
    )
    parser.add_argument(
        "csv_path",
        help="入力 CSV ファイルのパス",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB に書き込まずパース結果のみ検証(バックアップも取らない)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="書き込み先 DB パス (デフォルト: 本番 RESEARCH_SHELVE)",
    )
    parser.add_argument(
        "--show",
        default=None,
        help=(
            "検証用に代表銘柄の format_record_full 出力を stdout に流す。"
            "カンマ区切りで複数指定可 (例: --show 3496,247A,6920)"
        ),
    )
    args = parser.parse_args()

    show_codes: Optional[List[str]] = None
    if args.show:
        show_codes = [c.strip() for c in args.show.split(",") if c.strip()]

    summary = migrate_csv_to_research_shelve(
        args.csv_path,
        db_path=args.db_path,
        dry_run=args.dry_run,
        show_codes=show_codes,
    )

    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
