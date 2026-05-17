#!/usr/bin/env python3
"""
my_watch_list.txt → portfolio_shelve 取り込みスクリプト (Phase 3a / issue #170)

入力: ${KS_DATA_DIR}/my_watch_list.txt (H 接頭辞 = 1保、なし = 3監)
出力: portfolio_shelve

マージルール (計画書 §3-3):
- txt のみ存在: ステータスを txt から決定 (H 付き→1保、なし→3監)、メモは空
- スプシのみ存在: 既存の status="3監" のまま、メモはスプシ採用
- 両方存在: ステータスは txt 優先で上書き、メモはスプシ採用 (上書きしない)

ステータスは my_watch_list.txt のみを真実源とする (スプシのステータス列は無視)。
スプシ移行 (migrate_portfolio_from_csv.py) を先に実行し、本スクリプトを後に実行する想定。
"""

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を sys.path に追加 (直接実行時)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402

try:
    from ks_util import DATA_DIR, log_print, log_warning, file_read
except ImportError:
    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)

    def file_read(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()


# my_watch_list.txt の行パターン: 先頭オプションの H + 銘柄コード + 銘柄名
# 銘柄コードは "0001"〜"9999" または "215A" 形式
LINE_PATTERN = re.compile(r"^(H?)(\d[0-9a-zA-Z]\d[0-9A-Z])(.*)$")


def parse_my_watch_list(text: str) -> List[Tuple[str, str, str]]:
    """テキスト内容をパースして [(code_s, stock_name, status), ...] を返す。

    - H 接頭辞付き → status="1保"
    - 接頭辞なし → status="3監"
    - 重複は最初の出現を優先 (txt 内の重複を排除)
    """
    seen: Dict[str, Tuple[str, str, str]] = {}
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = LINE_PATTERN.match(line)
        if not m:
            continue
        prefix, code_raw, rest = m.group(1), m.group(2), m.group(3)
        try:
            ps.validate_code_s(code_raw)
        except (ValueError, TypeError):
            continue
        code_s = ps.normalize_code_s(code_raw)
        stock_name = rest.strip()
        status = "1保" if prefix == "H" else "3監"
        if code_s not in seen:
            seen[code_s] = (code_s, stock_name, status)
    return list(seen.values())


def merge_into_shelve(
    entries: List[Tuple[str, str, str]],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """txt 由来エントリを portfolio_shelve にマージする。

    既存レコードがある場合 (スプシ移行済み):
    - ステータスは txt 優先で上書き
    - メモは既存の値を保持 (スプシ採用)
    既存レコードがない場合 (txt のみ):
    - 新規レコード作成 (ステータスは txt 由来、メモは空)

    銘柄名は portfolio_shelve には保存しない (txt から読んでもレコードには埋めない)。
    表示時は stocks_shelve / research_shelve から都度取得する。

    Returns: {"total": int, "created": int, "updated": int, "unchanged": int}
    """
    created = 0
    updated = 0
    unchanged = 0
    for code_s, _stock_name, status in entries:
        existing = ps.get_record(code_s, db_path=db_path)
        if existing is None:
            # 新規 (txt のみ存在パターン)
            new_record = ps.create_record(code_s, status=status)
            ps.upsert_record(new_record, db_path=db_path)
            ps.append_action_log(
                code_s,
                "初回登録",
                status_from=None,
                status_to="3監",  # ライフサイクル上は新規=3監で記録
                reason="txt 取り込み",
                db_path=db_path,
            )
            # 実際の status が 1保 なら 3監→1保 の遷移ログも追加
            if status != "3監":
                ps.append_action_log(
                    code_s,
                    "ステータス変更",
                    status_from="3監",
                    status_to=status,
                    reason="txt 取り込み (H 接頭辞)",
                    db_path=db_path,
                )
            created += 1
        else:
            # 既存 (スプシ移行済み)
            old_status = existing.get("status")
            if old_status == status:
                unchanged += 1
                continue
            # ステータスのみ更新、メモは保持
            existing["status"] = status
            existing["updated_at"] = ps.now_iso()
            ps.upsert_record(existing, db_path=db_path)
            ps.append_action_log(
                code_s,
                "ステータス変更",
                status_from=old_status,
                status_to=status,
                reason="txt 取り込みでステータス上書き",
                db_path=db_path,
            )
            updated += 1
    log_print(
        f"migrate_my_watch_list: 新規 {created} 件、更新 {updated} 件、"
        f"変化なし {unchanged} 件"
    )
    return {
        "total": len(entries),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
    }


def import_my_watch_list(
    txt_path: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """my_watch_list.txt を読み込んで portfolio_shelve に取り込む。

    Args:
        txt_path: 入力 txt パス。None なら ${DATA_DIR}/my_watch_list.txt

    Returns: merge_into_shelve の戻り値 (totals)
    """
    if txt_path is None:
        txt_path = os.path.join(DATA_DIR, "my_watch_list.txt")
    if not os.path.isfile(txt_path):
        raise FileNotFoundError(f"my_watch_list.txt が見つかりません: {txt_path}")
    text = file_read(txt_path)
    entries = parse_my_watch_list(text)
    log_print(
        f"migrate_my_watch_list: txt から {len(entries)} 件のエントリ抽出"
    )
    return merge_into_shelve(entries, db_path=db_path)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="my_watch_list.txt を portfolio_shelve に取り込む",
    )
    parser.add_argument(
        "--txt-path",
        type=str,
        default=None,
        help=f"入力 txt パス (デフォルト: {os.path.join(DATA_DIR, 'my_watch_list.txt')})",
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
    try:
        result = import_my_watch_list(args.txt_path, db_path=args.db_path)
    except FileNotFoundError as exc:
        log_warning(str(exc))
        return 2
    print(
        f"total={result['total']} created={result['created']} "
        f"updated={result['updated']} unchanged={result['unchanged']}"
    )
    # 同期は呼ばない: txt 取り込み時は元の txt を上書きすべきでないため
    # (取り込み入力 = txt 自体なので、上書きすると先頭順序などが壊れる)
    return 0


if __name__ == "__main__":
    sys.exit(main())
