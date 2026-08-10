#!/usr/bin/env python3
"""証券会社ポートフォリオCSV → portfolio_shelve position レイヤー 取込 (issue #397 Phase1)。

保有ステータス・保有株数の手入力をやめ、証券会社の残高CSVを真実源として
自動同期するための取込コマンド。4ソース (楽天現物/楽天信用/SBI現物/SBI信用) を
まとめて渡し、position / position_source レイヤーへ反映する。

Phase 1 はこのコマンドの --apply でも record (qty/status) には一切触れず、
position の保存と差分プレビューの表示のみ行う (可視化フェーズ)。
qty/status への自動反映は Phase 2 で別途解禁する。

ファイル判別はファイル名に依存しない (SBI は現物・信用が同名 SaveFile*.csv で
降ってくるため)。中身の構造から4ソースいずれかを機械的に特定する。

4 層構成 (import_rakuten_fills.py のパターン踏襲):
    1. CSV 読込・ソース判別層 (detect_source, read_*)
    2. 行パース層 (parse_*_rows)
    3. 統合層 (import_csvs)
    4. 実行層 (main + argparse)
"""

import argparse
import csv
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import portfolio_shelve as ps  # noqa: E402
from webapp.helpers import resolve_stock_name  # noqa: E402

try:
    from ks_util import log_print, log_warning
except ImportError:
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


CSV_ENCODING = "shift_jis"

# 楽天現物 CSV のセクション見出し
RAKUTEN_SPOT_SECTION_MARKER = "■ 保有商品詳細"
RAKUTEN_SPOT_HEADER_FIRST_COL = "種別"
RAKUTEN_SPOT_HEADER_MARKER = "銘柄コード・ティッカー"

# 楽天信用 CSV の1行目マーカーとヘッダ列
RAKUTEN_MARGIN_FIRST_ROW_MARKER = "■表示形式"
RAKUTEN_MARGIN_HEADER_MARKER = "建玉数量［株］"

# SBI CSV の2行目見出し
SBI_SPOT_MARKER = "保有証券一覧"
SBI_MARGIN_MARKER = "信用建玉一覧"
SBI_HEADER_FIRST_COL = "銘柄コード"

# 楽天現物の口座区分マーカー (セクション見出しから account を取る)
SBI_SPOT_SECTION_PREFIX = "株式（"  # 例: "株式（特定預り）"


class RowSkip(Exception):
    """パース対象外の行 (無効コード・ETF・サマリー行など)。理由を保持する。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ===========================================
# 1. CSV 読込・ソース判別層
# ===========================================

def read_csv_rows(csv_path: str) -> List[List[str]]:
    """CSV を Shift-JIS で読み、全行を返す (ヘッダ探索前の生データ)。"""
    with open(csv_path, "r", encoding=CSV_ENCODING, newline="") as f:
        return list(csv.reader(f))


def detect_source(rows: List[List[str]]) -> Optional[Tuple[str, str]]:
    """CSV の中身から (broker, kind) を判別する。判別できなければ None。

    ファイル名に依存しない (SBI は現物・信用が同名 SaveFile*.csv のため)。
    判定順は仕様ドキュメント §5-2 の判別表に従う。
    """
    # 1: SBI/現物・信用 判別 (2行目の見出し)
    if len(rows) > 1:
        second_row_first_col = (rows[1][0] if rows[1] else "").strip()
        if second_row_first_col == SBI_SPOT_MARKER:
            return ("SBI", "現物")
        if second_row_first_col == SBI_MARGIN_MARKER:
            return ("SBI", "信用")

    # 3: 楽天/現物 (■ 保有商品詳細 セクションを持つ)
    for row in rows:
        if row and row[0].strip().startswith(RAKUTEN_SPOT_SECTION_MARKER):
            return ("楽天", "現物")

    # 4: 楽天/信用 (1行目が ■表示形式 かつ 建玉数量［株］ 列を持つ)
    if rows and (rows[0][0] if rows[0] else "").strip() == RAKUTEN_MARGIN_FIRST_ROW_MARKER:
        for row in rows:
            if RAKUTEN_MARGIN_HEADER_MARKER in [c.strip() for c in row]:
                return ("楽天", "信用")

    return None


# ===========================================
# 2. 行パース層
# ===========================================

def _find_header_row(rows: List[List[str]], first_col: str, marker: str) -> int:
    """ヘッダ行 (first_col から始まり marker を含む) の index を返す。無ければ -1。"""
    for i, row in enumerate(rows):
        if row and row[0].strip() == first_col and marker in [c.strip() for c in row]:
            return i
    return -1


def parse_rakuten_spot(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """楽天現物CSV (assetbalance(all)_*.csv) をパースする。

    国内株式のみ採用 (米国株式・外貨預り金は除外)。冒頭の資産サマリー部にも
    「国内株式」で始まる行があるため、ヘッダ行以降のみを対象にする
    (issue #397 §2-2b: サマリー行の誤読対策)。
    """
    hi = _find_header_row(rows, RAKUTEN_SPOT_HEADER_FIRST_COL, RAKUTEN_SPOT_HEADER_MARKER)
    if hi < 0:
        raise ValueError("楽天現物CSV: ヘッダ行 (種別...銘柄コード・ティッカー) が見つかりません")

    parsed = []
    for row in rows[hi + 1:]:
        if len(row) < 5 or row[0].strip() != "国内株式":
            continue
        code_raw = (row[1] or "").strip()
        try:
            ps.validate_code_s(code_raw)
        except (ValueError, TypeError):
            continue
        code_s = ps.normalize_code_s(code_raw)
        if ps.is_etf_code(code_s):
            continue
        account = (row[3] or "").strip() or "特定"
        qty_raw = (row[4] or "").strip().replace(",", "")
        try:
            qty = int(float(qty_raw))
        except ValueError:
            continue
        avg_price_raw = (row[6] or "").strip().replace(",", "") if len(row) > 6 else ""
        try:
            avg_price = float(avg_price_raw)
        except ValueError:
            avg_price = None
        parsed.append({
            "code_s": code_s, "account": account, "kind": "現物",
            "qty": qty, "avg_price": avg_price,
        })
    return parsed


def parse_rakuten_margin(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """楽天信用建玉CSV (marginbalance(JP)_*.csv) をパースする。

    建玉単位の行なので、統合層で銘柄ごとに合算する。売建は "信用売建" kind で
    別集計にする (issue #397 §2-0: merged_qty には含めず covered を偽にする)。
    """
    hi = _find_header_row(rows, "口座区分", "建玉数量［株］")
    if hi < 0:
        raise ValueError("楽天信用CSV: ヘッダ行 (口座区分...建玉数量［株］) が見つかりません")

    parsed = []
    for row in rows[hi + 1:]:
        if len(row) < 8 or not (row[1] or "").strip():
            continue
        code_raw = (row[1] or "").strip()
        try:
            ps.validate_code_s(code_raw)
        except (ValueError, TypeError):
            continue
        code_s = ps.normalize_code_s(code_raw)
        account = (row[0] or "").strip() or "特定"
        side = (row[4] or "").strip()  # 買建 / 売建
        qty_raw = (row[7] or "").strip().replace(",", "")
        try:
            qty = int(float(qty_raw))
        except ValueError:
            continue
        tate_price_raw = (row[9] or "").strip().replace(",", "") if len(row) > 9 else ""
        try:
            tate_price = float(tate_price_raw)
        except ValueError:
            tate_price = None
        kind = "信用" if side == "買建" else "信用売建"
        parsed.append({
            "code_s": code_s, "account": account, "kind": kind,
            "qty": qty, "avg_price": tate_price,
        })
    return parsed


def _iter_sbi_spot_section_headers(rows: List[List[str]]) -> List[Tuple[int, str]]:
    """SBI保有証券CSV 内の全ヘッダ行 index と、直前のセクション見出しから
    導出した account を列挙する (複数口座区分セクションへの対応、issue #397)。

    `_find_header_row` は最初の1件しか返さないため、特定/NISA/一般など
    セクションが複数あるCSVでは後続セクションのデータ行を取りこぼす
    (または直前のヘッダ行の続きとして誤って同一セクション扱いされる)。
    実データの各セクションが空行で区切られる構造を前提に、ヘッダ行の
    出現ごとに直前の "株式（...）" 見出し (合計行を除く) から account を取る。
    """
    results = []
    current_account = "特定"
    for i, row in enumerate(rows):
        cell = (row[0].strip() if row else "")
        if cell.startswith(SBI_SPOT_SECTION_PREFIX) and cell.endswith("）"):
            # "株式（特定預り）" -> "特定" のように「特定」「一般」等を抜き出す
            inner = cell[len(SBI_SPOT_SECTION_PREFIX):-1]
            current_account = inner.replace("預り", "") or "特定"
            continue
        if row and cell == SBI_HEADER_FIRST_COL and "保有株数" in [c.strip() for c in row]:
            results.append((i, current_account))
    return results


def parse_sbi_spot(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """SBI保有証券CSV をパースする。ETF が混在するため除外必須 (issue #397 §2-1b)。

    口座区分の列が無く、セクション見出し (例: "株式（特定預り）") で表現される。
    特定/NISA/一般など**複数のセクションが存在し得る**ため、全セクションを
    走査してそれぞれの account でデータ行を紐付ける。1セクションしか無い
    (実データで確認済みの通常ケース) 場合も同じロジックで動く。
    """
    headers = _iter_sbi_spot_section_headers(rows)
    if not headers:
        raise ValueError("SBI現物CSV: ヘッダ行 (銘柄コード...保有株数) が見つかりません")

    accounts_found = {account for _, account in headers}
    if accounts_found != {"特定"}:
        log_warning(
            f"SBI現物CSV: 想定外の口座区分を検出 ({sorted(accounts_found)})。"
            "account 網羅性の前提 (issue #397 §5-2) を再確認してください"
        )

    parsed = []
    for idx, (hi, account) in enumerate(headers):
        # このセクションのデータ行は、次のヘッダ行 (次セクション) の手前まで
        section_end = headers[idx + 1][0] if idx + 1 < len(headers) else len(rows)
        for row in rows[hi + 1:section_end]:
            if len(row) < 3 or not (row[0] or "").strip():
                continue
            code_raw = (row[0] or "").strip()
            try:
                ps.validate_code_s(code_raw)
            except (ValueError, TypeError):
                continue
            code_s = ps.normalize_code_s(code_raw)
            if ps.is_etf_code(code_s):
                continue
            if not resolve_stock_name(code_s):
                continue
            qty_raw = (row[2] or "").strip().replace(",", "")
            try:
                qty = int(float(qty_raw))
            except ValueError:
                continue
            avg_price_raw = (row[4] or "").strip().replace(",", "") if len(row) > 4 else ""
            try:
                avg_price = float(avg_price_raw)
            except ValueError:
                avg_price = None
            parsed.append({
                "code_s": code_s, "account": account, "kind": "現物",
                "qty": qty, "avg_price": avg_price,
            })
    return parsed


def parse_sbi_margin(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """SBI信用建玉CSV をパースする。建玉単位の行なので統合層で合算する。"""
    hi = _find_header_row(rows, SBI_HEADER_FIRST_COL, "建株数")
    if hi < 0:
        raise ValueError("SBI信用CSV: ヘッダ行 (銘柄コード...建株数) が見つかりません")

    parsed = []
    for row in rows[hi + 1:]:
        if len(row) < 9 or not (row[0] or "").strip():
            continue
        code_raw = (row[0] or "").strip()
        try:
            ps.validate_code_s(code_raw)
        except (ValueError, TypeError):
            continue
        code_s = ps.normalize_code_s(code_raw)
        side = (row[2] or "").strip()  # 買建 / 売建
        account = (row[7] or "").strip() or "特定"
        qty_raw = (row[8] or "").strip().replace(",", "")
        try:
            qty = int(float(qty_raw))
        except ValueError:
            continue
        tate_price_raw = (row[10] or "").strip().replace(",", "") if len(row) > 10 else ""
        try:
            tate_price = float(tate_price_raw)
        except ValueError:
            tate_price = None
        kind = "信用" if side == "買建" else "信用売建"
        parsed.append({
            "code_s": code_s, "account": account, "kind": kind,
            "qty": qty, "avg_price": tate_price,
        })
    return parsed


PARSERS = {
    ("楽天", "現物"): parse_rakuten_spot,
    ("楽天", "信用"): parse_rakuten_margin,
    ("SBI", "現物"): parse_sbi_spot,
    ("SBI", "信用"): parse_sbi_margin,
}


def _aggregate_by_account_kind_code(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """パース結果を (account, kind, code_s) 単位で合算する。

    建玉単位の行 (信用) は同一銘柄が複数行に分かれるため必須。現物は基本1行だが
    同じ関数で扱って問題ない。avg_price は合算せず最後に見つかった値を使う
    (position レイヤーでは表示に使わない前提なので厳密でなくてよい、issue #397 §7)。
    """
    agg: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in rows:
        key = (r["account"], r["kind"], r["code_s"])
        if key not in agg:
            agg[key] = {"code_s": r["code_s"], "account": r["account"], "kind": r["kind"],
                       "qty": 0, "avg_price": None}
        agg[key]["qty"] += r["qty"]
        if r.get("avg_price") is not None:
            agg[key]["avg_price"] = r["avg_price"]
    return agg


# ===========================================
# 3. 統合層
# ===========================================

def import_csvs(
    csv_paths: List[str],
    as_of: str,
    *,
    dry_run: bool = False,
    allow_partial: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """複数CSVをまとめて読み、position/position_source を差分プレビュー・反映する。

    Phase 1: --apply でも position/position_source の保存のみ行い、
    record (qty/status) には一切触れない (可視化フェーズ)。

    Returns: {"sources": {...}, "diffs": [...], "missing_sources": [...]}
    """
    detected: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for path in csv_paths:
        rows = read_csv_rows(path)
        source = detect_source(rows)
        if source is None:
            raise ValueError(f"CSV種別を判別できません: {path}")
        if source in detected:
            raise ValueError(
                f"同一ソース ({source[0]}/{source[1]}) のファイルが複数渡されました: "
                f"{detected[source]['path']} / {path}"
            )
        parser = PARSERS[source]
        parsed_rows = parser(rows)
        detected[source] = {"path": path, "rows": parsed_rows}
        log_print(
            "import_portfolio_csv: ソース判別",
            f"{source[0]}/{source[1]}", os.path.basename(path),
            f"rows={len(parsed_rows)}",
        )

    missing_sources = [
        f"{broker}/{kind}" for broker, kind in ps.EXPECTED_POSITION_SOURCES
        if (broker, kind) not in detected
    ]
    if missing_sources and not allow_partial:
        raise ValueError(
            f"必要なソースが不足しています: {missing_sources}。"
            f"--allow-partial を指定すると不足のまま続行できます"
        )

    # 差分プレビュー用に、既存 DB の状態と比較する
    existing_records = {r["code_s"]: r for r in ps.list_records(db_path=db_path)}
    all_codes = set()
    aggregated: Dict[Tuple[str, str], Dict[Tuple[str, str, str], Dict[str, Any]]] = {}
    for source, data in detected.items():
        agg = _aggregate_by_account_kind_code(data["rows"])
        aggregated[source] = agg
        for (account, kind, code_s) in agg:
            all_codes.add(code_s)
    all_codes |= set(existing_records.keys())

    if not dry_run:
        for source, agg in aggregated.items():
            broker, kind = source
            for (account, k, code_s), entry in agg.items():
                ps.upsert_position(
                    broker, account, k, code_s, entry["qty"],
                    avg_price=entry["avg_price"], as_of=as_of, db_path=db_path,
                )
            accounts = {account for (account, _, _) in agg} or {"特定"}
            for account in accounts:
                row_count = sum(1 for (a, _, _) in agg if a == account)
                ps.upsert_position_source(
                    broker, account, kind, as_of=as_of, row_count=row_count, db_path=db_path,
                )
        log_print("import_portfolio_csv: position 保存完了", f"銘柄数={len(all_codes)}")

    diffs = _build_diff_preview(
        all_codes, existing_records, db_path=db_path if not dry_run else None,
        dry_run_aggregated=aggregated if dry_run else None,
        all_sources_present=not missing_sources,
    )

    return {
        "sources": {f"{b}/{k}": len(v) for (b, k), v in aggregated.items()},
        "missing_sources": missing_sources,
        "diffs": diffs,
    }


def _build_diff_preview(
    all_codes: set,
    existing_records: Dict[str, Dict[str, Any]],
    *,
    db_path: Optional[str],
    dry_run_aggregated: Optional[Dict] = None,
    all_sources_present: bool = True,
) -> List[Dict[str, Any]]:
    """差分プレビュー行を組み立てる (issue #397 §5-3 の判定表)。

    covered は「その銘柄が全ソースに登場するか」ではなく「4ソース全てが
    今回の取込対象として揃っているか」で決まる (issue #397 §5-2: ソース側に
    銘柄が無い=保有ゼロも正常なので、銘柄単位の登場有無では判定しない)。
    dry_run 時は DB に position を書いていないので、aggregated から
    その場で merged_qty を計算する (covered は all_sources_present を使う。
    apply 後の正式な判定は is_covered() を使う)。
    """
    diffs = []
    for code_s in sorted(all_codes):
        record = existing_records.get(code_s)
        status = record.get("status") if record else "未登録"
        db_qty = record.get("qty") if record else None

        if dry_run_aggregated is not None:
            merged_qty = 0
            has_short = False
            for (broker, kind), agg in dry_run_aggregated.items():
                for (account, k, c), entry in agg.items():
                    if c != code_s:
                        continue
                    if k == "信用売建":
                        has_short = True
                    else:
                        merged_qty += entry["qty"]
            covered = all_sources_present and not has_short
        else:
            merged_qty = ps.compute_merged_qty(code_s, db_path=db_path)
            covered = ps.is_covered(code_s, db_path=db_path)

        judgement = _judge(status, covered, merged_qty, db_qty)
        diffs.append({
            "code_s": code_s, "status": status, "db_qty": db_qty,
            "merged_qty": merged_qty, "covered": covered, "judgement": judgement,
        })
    return diffs


def _judge(status: str, covered: bool, merged_qty: int, db_qty: Optional[int]) -> str:
    """issue #397 §5-3 の判定表に基づき差分区分の文字列を返す (プレビュー表示専用)。

    covered=false でも、そもそも CSV 上に何も現れていない銘柄 (merged_qty=0
    かつ 1保 でもない) は「判定不能」ではなく「対象外」とする。covered が
    偽なのは他の銘柄のソース欠落が原因であって、この銘柄自体に差分があるとは
    限らないため (issue #397 §5-2)。
    """
    if status == "1保" and merged_qty == db_qty:
        return "一致"
    if status in ("2準", "3監") and merged_qty == 0:
        return "一致"
    if status == "未登録" and merged_qty == 0:
        return "対象外"
    if not covered:
        return "判定不能 (covered=false)"
    if status == "1保":
        if merged_qty == 0:
            return "売却候補 (Phase2でOUT)"
        return f"株数変更候補 {db_qty}→{merged_qty} (Phase2で反映)"
    if status in ("2準", "3監"):
        return "新規IN候補 (Phase2で保留キュー/自動IN)"
    if status == "未登録":
        return "未登録+保有検出 (Phase2で登録)"
    return "-"


# ===========================================
# 4. 実行層
# ===========================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="証券会社ポートフォリオCSV (4ソース) を position レイヤーへ取込 "
                    "(issue #397 Phase1: 可視化のみ、record は変更しない)",
    )
    parser.add_argument("csv_paths", nargs="+", help="取込むCSVファイル (複数指定可、順不同)")
    parser.add_argument(
        "--as-of", required=True,
        help="CSVの基準日 (YYYY-MM-DD)。全ファイル共通の取込基準日として使う",
    )
    parser.add_argument("--dry-run", action="store_true", help="読込・差分算出のみ、DBへは書かない")
    parser.add_argument("--apply", action="store_true", help="position/position_source をDBへ保存する")
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="4ソースが揃っていなくても実行を続行する (既定はエラー停止)",
    )
    parser.add_argument("--db-path", default=None, help="portfolio_shelve のパス上書き (検証用)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    for path in args.csv_paths:
        if not os.path.exists(path):
            log_warning(f"CSV が見つかりません: {path}")
            return 1

    if not args.dry_run and not args.apply:
        log_warning("--dry-run か --apply のどちらかを指定してください")
        return 1

    try:
        result = import_csvs(
            args.csv_paths, args.as_of,
            dry_run=args.dry_run or not args.apply,
            allow_partial=args.allow_partial,
            db_path=args.db_path,
        )
    except ValueError as e:
        log_warning(f"import_portfolio_csv: {e}")
        return 1

    log_print("import_portfolio_csv: ソース内訳", result["sources"])
    if result["missing_sources"]:
        log_print("import_portfolio_csv: 不足ソース (--allow-partial で続行)", result["missing_sources"])

    log_print("import_portfolio_csv: 差分プレビュー (Phase1は可視化のみ、DBのqty/statusは変更しません)")
    for d in result["diffs"]:
        if d["judgement"] in ("一致", "対象外"):
            continue
        log_print(
            f"  {d['code_s']} status={d['status']} db_qty={d['db_qty']} "
            f"merged_qty={d['merged_qty']} covered={d['covered']} -> {d['judgement']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
