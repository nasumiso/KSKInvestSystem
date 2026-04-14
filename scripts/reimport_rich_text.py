#!/usr/bin/env python3
"""
銘柄調査スプレッドシートのリッチテキスト再インポートスクリプト (issue #115)

Google Sheets API で元スプシからセル書式情報 (textFormatRuns, hyperlink) を
直接取得し、HTML 片として research_shelve に再インポートする。

対象フィールド:
    - ir_comment (スナップショット内、日付ブロック単位)
    - memo (レコード直属)
    - openwork (レコード直属)

4 層構成:
    1. API 読込層 (fetch_sheet_with_formatting)
    2. HTML 変換層 (textFormatRuns_to_html)
    3. IR 列特殊処理層 (apply_formatting_to_ir_blocks)
    4. 実行層 (reimport_rich_text + main)
"""

import argparse
import html
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を sys.path に追加(直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import research_shelve as rs  # noqa: E402
from html_sanitizer import sanitize_html  # noqa: E402
from migrate_research_from_csv import (  # noqa: E402
    EXPECTED_COL_COUNT,
    IR_BLOCK_HEADER,
    build_record_from_row,
)

try:
    from ks_util import log_debug, log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_debug(*args, **kwargs):
        pass

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# 定数
# ===========================================

DEFAULT_SPREADSHEET_ID = "1dveeGsQUiw1XzIE6k-GUiy16CXw4J6qzFp9-aosde70"

# スプシ列インデックス
COL_CODE = 0
COL_IR = 5
COL_OPENWORK = 8
COL_MEMO = 9


# ===========================================
# 1. API 読込層
# ===========================================

def fetch_sheet_with_formatting(
    sheets_service, spreadsheet_id: str
) -> List[List[dict]]:
    """Sheets API で書式情報付きの全行データを取得する。

    Args:
        sheets_service: Google Sheets API サービスオブジェクト
        spreadsheet_id: スプレッドシート ID

    Returns:
        ヘッダ行を除いた行リスト。各行はセル dict のリスト。
        セル dict は {"formattedValue", "textFormatRuns", "hyperlink",
                      "effectiveFormat"} を含む（存在するフィールドのみ）。
    """
    resp = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        includeGridData=True,
        fields=(
            "sheets.data.rowData.values("
            "formattedValue,textFormatRuns,hyperlink,"
            "effectiveFormat.textFormat"
            ")"
        ),
    ).execute()

    sheets = resp.get("sheets", [])
    if not sheets:
        log_warning("reimport: スプレッドシートにシートがありません")
        return []

    grid_data = sheets[0].get("data", [])
    if not grid_data:
        return []

    row_data = grid_data[0].get("rowData", [])
    if not row_data:
        return []

    # ヘッダ行 (index 0) をスキップ
    result = []
    for row in row_data[1:]:
        cells = row.get("values", [])
        result.append(cells)
    return result


def api_row_to_text_row(api_row: List[dict]) -> List[str]:
    """API 行データを formattedValue の17列テキストリストに変換する。

    build_record_from_row() に渡せる形式にする。
    列数が17未満の場合は空文字でパディングする。
    """
    row = [_get_cell_value(cell) for cell in api_row]
    if len(row) < EXPECTED_COL_COUNT:
        row.extend([""] * (EXPECTED_COL_COUNT - len(row)))
    return row[:EXPECTED_COL_COUNT]


def _get_cell_value(cell: dict) -> str:
    """セルの formattedValue を取得する。"""
    return cell.get("formattedValue", "") or ""


def _get_cell_runs(cell: dict) -> List[dict]:
    """セルの textFormatRuns を取得する。"""
    return cell.get("textFormatRuns") or []


def _get_cell_hyperlink(cell: dict) -> Optional[str]:
    """セルレベルのハイパーリンクを取得する。"""
    return cell.get("hyperlink")


def _get_cell_default_format(cell: dict) -> dict:
    """セルのデフォルトテキストフォーマットを取得する。"""
    ef = cell.get("effectiveFormat") or {}
    return ef.get("textFormat") or {}


# ===========================================
# 2. HTML 変換層
# ===========================================

def _color_to_hex(color_dict: Optional[dict]) -> Optional[str]:
    """Google Sheets のカラー dict を #RRGGBB 形式に変換する。

    色が黒（デフォルト）に近い場合は None を返す。

    Args:
        color_dict: {"red": 0.0-1.0, "green": 0.0-1.0, "blue": 0.0-1.0}

    Returns:
        "#RRGGBB" 文字列 or None
    """
    if not color_dict:
        return None
    r = color_dict.get("red", 0.0) or 0.0
    g = color_dict.get("green", 0.0) or 0.0
    b = color_dict.get("blue", 0.0) or 0.0
    # 黒に近い色はデフォルトとみなす
    if r < 0.1 and g < 0.1 and b < 0.1:
        return None
    ri = min(255, int(r * 255))
    gi = min(255, int(g * 255))
    bi = min(255, int(b * 255))
    return f"#{ri:02x}{gi:02x}{bi:02x}"


def _get_run_color(fmt: dict, default_format: dict) -> Optional[str]:
    """run のフォーマットから色情報を抽出する。

    foregroundColorStyle.rgbColor → foregroundColor の優先順。
    デフォルトフォーマットと同じ色なら None。
    """
    # run 側の色を取得
    fg_style = fmt.get("foregroundColorStyle", {})
    fg_color = fg_style.get("rgbColor") if fg_style else None
    if not fg_color:
        fg_color = fmt.get("foregroundColor")
    if not fg_color:
        return None

    # デフォルト側の色を取得
    def_fg_style = default_format.get("foregroundColorStyle", {})
    def_fg_color = def_fg_style.get("rgbColor") if def_fg_style else None
    if not def_fg_color:
        def_fg_color = default_format.get("foregroundColor")

    # デフォルトと同じ色なら無視
    run_hex = _color_to_hex(fg_color)
    def_hex = _color_to_hex(def_fg_color)
    if run_hex == def_hex:
        return None

    return run_hex


def textFormatRuns_to_html(
    formatted_value: str,
    runs: List[dict],
    cell_hyperlink: Optional[str] = None,
    default_format: Optional[dict] = None,
) -> str:
    """textFormatRuns を HTML 片に変換する。

    Args:
        formatted_value: セルのテキスト内容
        runs: textFormatRuns リスト
        cell_hyperlink: セルレベルのハイパーリンク URL
        default_format: セルのデフォルトテキストフォーマット

    Returns:
        HTML 文字列
    """
    if not formatted_value:
        return ""

    if default_format is None:
        default_format = {}
    default_bold = default_format.get("bold", False)

    # 書式情報なし → プレーンテキスト
    if not runs and not cell_hyperlink:
        return html.escape(formatted_value)

    # セルレベルハイパーリンクのみ（runs なし）
    if not runs and cell_hyperlink:
        escaped = html.escape(formatted_value)
        safe_href = html.escape(cell_hyperlink, quote=True)
        return f'<a href="{safe_href}" target="_blank">{escaped}</a>'

    # runs からセグメントを構築
    segments = _build_segments(formatted_value, runs)

    result_parts = []
    for start, end, fmt in segments:
        text_slice = formatted_value[start:end]
        escaped = html.escape(text_slice)
        if not escaped:
            continue

        # 書式判定
        is_bold = fmt.get("bold", False) and not default_bold
        color_hex = _get_run_color(fmt, default_format)
        link_uri = None
        link_data = fmt.get("link")
        if link_data:
            link_uri = link_data.get("uri")

        # タグ適用（内側から: bold → color → link）
        part = escaped
        if is_bold:
            part = f"<b>{part}</b>"
        if color_hex:
            part = f'<span style="color:{color_hex}">{part}</span>'
        if link_uri:
            safe_href = html.escape(link_uri, quote=True)
            part = f'<a href="{safe_href}" target="_blank">{part}</a>'

        result_parts.append(part)

    return "".join(result_parts)


def _build_segments(
    formatted_value: str, runs: List[dict]
) -> List[Tuple[int, int, dict]]:
    """textFormatRuns からセグメント (start, end, format) のリストを構築する。"""
    text_len = len(formatted_value)
    segments = []

    for i, run in enumerate(runs):
        start = run.get("startIndex", 0)
        if i + 1 < len(runs):
            end = runs[i + 1].get("startIndex", text_len)
        else:
            end = text_len
        fmt = run.get("format", {})
        segments.append((start, end, fmt))

    # 先頭の run が 0 から始まらない場合、ギャップを補完
    if segments and segments[0][0] > 0:
        segments.insert(0, (0, segments[0][0], {}))

    return segments


# ===========================================
# 3. IR 列特殊処理層
# ===========================================

def apply_formatting_to_ir_blocks(
    formatted_value: str,
    runs: List[dict],
    cell_hyperlink: Optional[str] = None,
    default_format: Optional[dict] = None,
) -> Tuple[str, Dict[str, str]]:
    """IR 列のリッチテキストを日付ブロック単位の HTML 片に変換する。

    parse_ir_column と同じロジックで日付ブロックを識別しつつ、
    各 ir_comment 部分のオフセット範囲を追跡して HTML 変換する。

    Args:
        formatted_value: IR セルのテキスト全体
        runs: textFormatRuns
        cell_hyperlink: セルレベルハイパーリンク
        default_format: セルのデフォルトフォーマット

    Returns:
        (overview_html, ir_comment_html_by_date)
        overview_html: 日付ブロック前のテキストの HTML
        ir_comment_html_by_date: {"YY.M": "<html片>", ...}
    """
    if not formatted_value:
        return "", {}

    if default_format is None:
        default_format = {}

    lines = formatted_value.split("\n")
    current_date = None
    # 各日付ブロックの ir_comment 行のオフセット範囲を収集
    overview_ranges = []  # [(start, end), ...]
    block_ranges = {}  # {"YY.M": [(start, end), ...]}

    offset = 0
    for line in lines:
        line_start = offset
        line_end = offset + len(line)
        offset = line_end + 1  # +1 for \n

        stripped = line.strip()
        if not stripped:
            continue

        # ブロック境界検出 (parse_ir_column と同じロジック)
        m = IR_BLOCK_HEADER.match(stripped)
        if m:
            yy_str = m.group(1)
            mm = int(m.group(2))
            if 1 <= mm <= 12:
                date_key = f"{yy_str}.{mm}"
                current_date = date_key
                block_ranges.setdefault(date_key, [])
                continue

        # ブロック境界ではない行
        if current_date is None:
            # overview 行
            overview_ranges.append((line_start, line_end))
        else:
            # ir_comment 行
            block_ranges.setdefault(current_date, [])
            block_ranges[current_date].append((line_start, line_end))

    # overview の HTML 生成
    overview_html = _render_html_for_ranges(
        formatted_value, runs, default_format, overview_ranges
    )

    # 各日付ブロックの ir_comment HTML を生成
    ir_comment_html = {}
    for date_key, ranges in block_ranges.items():
        if not ranges:
            ir_comment_html[date_key] = ""
        else:
            ir_comment_html[date_key] = _render_html_for_ranges(
                formatted_value, runs, default_format, ranges
            )

    return overview_html, ir_comment_html


def _render_html_for_ranges(
    formatted_value: str,
    runs: List[dict],
    default_format: dict,
    ranges: List[Tuple[int, int]],
) -> str:
    """複数のオフセット範囲に対して HTML を生成する。

    各範囲を改行で連結する。
    """
    if not ranges:
        return ""

    parts = []
    for start, end in ranges:
        segment_html = _render_html_for_range(
            formatted_value, runs, default_format, start, end
        )
        parts.append(segment_html)
    return "\n".join(parts)


def _render_html_for_range(
    formatted_value: str,
    runs: List[dict],
    default_format: dict,
    range_start: int,
    range_end: int,
) -> str:
    """指定オフセット範囲の textFormatRuns を HTML に変換する。

    runs をクリップして対象範囲のみ処理する。
    """
    if range_start >= range_end:
        return ""

    text_len = len(formatted_value)
    default_bold = default_format.get("bold", False)

    # runs がない場合はプレーンテキスト
    if not runs:
        return html.escape(formatted_value[range_start:range_end])

    # runs を範囲にクリップ
    all_segments = _build_segments(formatted_value, runs)
    result_parts = []

    for seg_start, seg_end, fmt in all_segments:
        # 範囲外のセグメントをスキップ
        if seg_end <= range_start or seg_start >= range_end:
            continue
        # クリップ
        clipped_start = max(seg_start, range_start)
        clipped_end = min(seg_end, range_end)

        text_slice = formatted_value[clipped_start:clipped_end]
        escaped = html.escape(text_slice)
        if not escaped:
            continue

        # 書式判定
        is_bold = fmt.get("bold", False) and not default_bold
        color_hex = _get_run_color(fmt, default_format)
        link_uri = None
        link_data = fmt.get("link")
        if link_data:
            link_uri = link_data.get("uri")

        part = escaped
        if is_bold:
            part = f"<b>{part}</b>"
        if color_hex:
            part = f'<span style="color:{color_hex}">{part}</span>'
        if link_uri:
            safe_href = html.escape(link_uri, quote=True)
            part = f'<a href="{safe_href}" target="_blank">{part}</a>'

        result_parts.append(part)

    return "".join(result_parts)


# ===========================================
# 4. 実行層
# ===========================================

def reimport_rich_text(
    spreadsheet_id: str,
    *,
    db_path: Optional[str] = None,
    dry_run: bool = False,
    show_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """スプシのリッチテキストを取得し research_shelve に全件再インポートする。

    CSV エクスポートは不要。Sheets API から formattedValue（テキスト）と
    textFormatRuns（書式）の両方を取得し、テキストから構造化レコードを構築
    しつつ書式情報で HTML 化する。

    手順:
        1. Sheets API で全行データ（テキスト + 書式）を取得
        2. research_shelve をクリア
        3. 各行について:
           - formattedValue からテキスト行を構築 → build_record_from_row
           - textFormatRuns で ir_comment / memo / openwork を HTML 化
           - sanitize_html() で XSS 防止
           - upsert_research_record() で保存

    Args:
        spreadsheet_id: Google スプレッドシート ID
        db_path: DB パス (None で本番)
        dry_run: True で DB 書き込みスキップ
        show_codes: 検証用に表示する銘柄コードリスト

    Returns:
        サマリー dict
    """
    from googledrive import get_sheets_service

    log_print("[reimport] Sheets API からデータ取得中...")
    sheets_service = get_sheets_service()
    api_rows = fetch_sheet_with_formatting(sheets_service, spreadsheet_id)
    log_print(f"[reimport] API行数: {len(api_rows)}")

    # 全列空の行を除外
    api_rows = [
        row for row in api_rows
        if any((_get_cell_value(c) or "").strip() for c in row)
    ]
    log_print(f"[reimport] 有効行: {len(api_rows)}")

    # バックアップ + DB クリア
    if not dry_run:
        try:
            backup_paths = rs.backup_research_db(db_path=db_path)
            if backup_paths:
                log_print(f"[reimport] バックアップ(実行前): {backup_paths}")
        except Exception as e:
            log_warning(f"[reimport] バックアップ失敗(継続): {e}")

        # DB クリア
        _clear_research_db(db_path=db_path)
        log_print("[reimport] research_shelve をクリアしました")
    else:
        log_print("[reimport] dry_run=True: DB に書き込まず検証のみ実行")

    # 移行本体
    failed_rows = []
    parse_warnings = []
    succeeded = 0

    log_print("[reimport] 再インポート開始...")
    for idx, api_row in enumerate(api_rows):
        text_row = api_row_to_text_row(api_row)

        try:
            # テキスト行からレコード構築（既存パーサを再利用）
            record, row_warnings = build_record_from_row(text_row)
            parse_warnings.extend(row_warnings)

            # Sheets API 書式情報で HTML 化
            _apply_rich_text_to_record(record, api_row)

            if not dry_run:
                rs.upsert_research_record(record, db_path=db_path)
            succeeded += 1
        except (ValueError, TypeError) as e:
            code_s = text_row[0] if text_row else "?"
            name = text_row[1] if len(text_row) > 1 else "?"
            log_warning(
                f"[reimport] 行失敗 (code={code_s!r}, name={name!r}): {e}"
            )
            failed_rows.append({
                "code_s": code_s,
                "stock_name": name,
                "error": f"{type(e).__name__}: {e}",
            })

        if (idx + 1) % 100 == 0:
            log_print(f"[reimport] {idx + 1}/{len(api_rows)} ...")

    log_print(
        f"[reimport] 完了: 成功 {succeeded} 件、失敗 {len(failed_rows)} 件"
        f" (パース warning {len(parse_warnings)} 件)"
    )

    if failed_rows:
        log_print("[reimport] 失敗行一覧:")
        for fr in failed_rows:
            log_print(f"  - {fr['code_s']} ({fr['stock_name']}): {fr['error']}")

    # --show 対応
    if show_codes:
        for code in show_codes:
            code = code.strip()
            if not code:
                continue
            log_print(f"[reimport] --show {code} ----------------------------")
            record = rs.get_research_record(code, db_path=db_path)
            if record is None:
                log_warning(f"[reimport] --show: レコード未登録: {code}")
                continue
            print(rs.format_record_full(record))

    return {
        "total": len(api_rows),
        "succeeded": succeeded,
        "failed": len(failed_rows),
        "failed_rows": failed_rows,
        "parse_warnings": parse_warnings,
        "dry_run": dry_run,
    }


def _apply_rich_text_to_record(record: dict, api_row: List[dict]) -> None:
    """API の書式情報を使ってレコードの対象フィールドを HTML 化する。"""

    # col 5: IR 列 → スナップショット内の ir_comment + overview
    if len(api_row) > COL_IR:
        ir_cell = api_row[COL_IR]
        ir_value = _get_cell_value(ir_cell)
        ir_runs = _get_cell_runs(ir_cell)
        ir_link = _get_cell_hyperlink(ir_cell)
        ir_default = _get_cell_default_format(ir_cell)

        if ir_value and (ir_runs or ir_link):
            overview_html, ir_html_by_date = apply_formatting_to_ir_blocks(
                ir_value, ir_runs, ir_link, ir_default
            )
            # overview を HTML で上書き
            if overview_html:
                record["overview"] = sanitize_html(overview_html)

            # スナップショットの ir_comment を HTML で上書き
            for snap in record.get("snapshots") or []:
                date_key = snap.get("date_yy_m", "")
                if date_key in ir_html_by_date:
                    snap["ir_comment"] = sanitize_html(
                        ir_html_by_date[date_key]
                    )

    # col 8: openwork
    if len(api_row) > COL_OPENWORK:
        ow_cell = api_row[COL_OPENWORK]
        ow_value = _get_cell_value(ow_cell)
        ow_runs = _get_cell_runs(ow_cell)
        ow_link = _get_cell_hyperlink(ow_cell)
        ow_default = _get_cell_default_format(ow_cell)

        if ow_value:
            ow_html = textFormatRuns_to_html(
                ow_value, ow_runs, ow_link, ow_default
            )
            record["openwork"] = sanitize_html(ow_html)

    # col 9: memo
    if len(api_row) > COL_MEMO:
        memo_cell = api_row[COL_MEMO]
        memo_value = _get_cell_value(memo_cell)
        memo_runs = _get_cell_runs(memo_cell)
        memo_link = _get_cell_hyperlink(memo_cell)
        memo_default = _get_cell_default_format(memo_cell)

        if memo_value:
            memo_html = textFormatRuns_to_html(
                memo_value, memo_runs, memo_link, memo_default
            )
            record["memo"] = sanitize_html(memo_html)


def _clear_research_db(*, db_path: Optional[str] = None) -> None:
    """research_shelve の全レコードを削除する。"""
    from db_shelve import RESEARCH_SHELVE, ShelveDB

    path = db_path if db_path is not None else RESEARCH_SHELVE
    with rs._flock(db_path):
        with ShelveDB(path) as db:
            keys = list(db.keys())
            for key in keys:
                del db[key]
    log_print(f"[reimport] DB クリア完了: {len(keys)} 件削除")


# ===========================================
# CLI エントリポイント
# ===========================================

def main() -> int:
    """CLI エントリポイント。

    usage: python reimport_rich_text.py [--dry-run] [--db-path PATH]
                     [--show CODE,CODE,...] [--spreadsheet-id ID]
    """
    parser = argparse.ArgumentParser(
        description="銘柄調査スプシのリッチテキストを再インポートする (issue #115)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB に書き込まず検証のみ実行",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="書き込み先 DB パス (デフォルト: 本番 RESEARCH_SHELVE)",
    )
    parser.add_argument(
        "--show",
        default=None,
        help="検証用に表示する銘柄コード (カンマ区切り、例: --show 3496,247A)",
    )
    parser.add_argument(
        "--spreadsheet-id",
        default=DEFAULT_SPREADSHEET_ID,
        help=f"Google スプレッドシート ID (デフォルト: {DEFAULT_SPREADSHEET_ID})",
    )
    args = parser.parse_args()

    show_codes = None
    if args.show:
        show_codes = [c.strip() for c in args.show.split(",") if c.strip()]

    # ロガー初期化
    try:
        from ks_util import setup_logger
        setup_logger("shintakane")
    except ImportError:
        pass

    summary = reimport_rich_text(
        args.spreadsheet_id,
        db_path=args.db_path,
        dry_run=args.dry_run,
        show_codes=show_codes,
    )

    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
