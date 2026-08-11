#!/usr/bin/env python3
"""証券会社ポートフォリオCSV → portfolio_shelve position レイヤー 取込 (issue #397)。

保有ステータス・保有株数の手入力をやめ、証券会社の残高CSVを真実源として
自動同期するための取込コマンド。4ソース (楽天現物/楽天信用/SBI現物/SBI信用) を
まとめて渡し、position / position_source レイヤーへ反映する。

--apply のみ: position/position_source を保存するが record (qty/status) には
一切触れない (Phase1、可視化フェーズ)。
--apply --apply-records: 上記に加え、covered な銘柄 (4ソース全てが取込済みの
銘柄。基準日の一致は要求しない) の qty更新・自動OUT・戦略ありの自動IN を実際に
反映する (Phase2)。戦略未設定の新規保有は pending_in (保留キュー) に積み、
自動反映しない。

4ファイル全てを毎回揃える必要はない。今回渡さなかったソースは、DB に前回の
position_source があればそのまま引き継いで covered 判定に使う (Phase3b、
実運用では楽天のみ更新することが多いための部分更新対応)。

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
    apply_records: bool = False,
    overrides: Optional[Dict[str, Dict[str, str]]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """複数CSVをまとめて読み、position/position_source を差分プレビュー・反映する。

    apply_records=False (既定): --apply でも position/position_source の保存のみ行い、
    record (qty/status) には一切触れない (issue #397 Phase1、可視化フェーズ)。
    apply_records=True かつ dry_run=False のときのみ、covered な銘柄について
    qty更新・自動OUT・戦略ありの自動IN を実際に反映する (issue #397 Phase2)。

    今回アップロードされなかったソースは、DB に既存の position_source が
    あればそれを「引き継ぎ」として扱い、今回アップロード分だけの部分更新を許容する
    (issue #397 Phase3b: 実運用では楽天のみ更新することが多いため)。
    DB にも既存データが無いソースのみ missing_sources として報告する。

    Returns: {"sources": {...}, "carried_over_sources": {...}, "missing_sources": [...],
              "diffs": [...], "applied": [...]}
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

    # 今回アップロードされなかったソースは、DB の既存 position_source で
    # 引き継げるか確認する (issue #397 Phase3b)。引き継げないものだけ missing。
    existing_sources = {(s["broker"], s["kind"]): s for s in ps.list_position_sources(db_path=db_path)}
    not_uploaded = [
        (broker, kind) for broker, kind in ps.EXPECTED_POSITION_SOURCES
        if (broker, kind) not in detected
    ]
    carried_over_sources = {
        f"{broker}/{kind}": existing_sources[(broker, kind)]["as_of"]
        for broker, kind in not_uploaded if (broker, kind) in existing_sources
    }
    missing_sources = [
        f"{broker}/{kind}" for broker, kind in not_uploaded
        if (broker, kind) not in existing_sources
    ]
    if missing_sources and not allow_partial:
        raise ValueError(
            f"必要なソースが不足しています (DBに引き継ぎ可能な前回分もありません): {missing_sources}。"
            f"--allow-partial を指定すると不足のまま続行できます"
        )

    # 差分プレビュー用に、既存 DB の状態と比較する
    existing_records = {r["code_s"]: r for r in ps.list_records(db_path=db_path)}
    # list_positions() は DB を open して全件走査するので1回だけ呼び、
    # all_codes の補完と code_s ごとのグルーピングの両方に使い回す
    # (issue #397 Phase3b: 銘柄ごとに呼ぶと N×M の計算量になり実データ規模で致命的に遅い)
    all_positions = ps.list_positions(db_path=db_path)
    all_codes = set()
    aggregated: Dict[Tuple[str, str], Dict[Tuple[str, str, str], Dict[str, Any]]] = {}
    for source, data in detected.items():
        agg = _aggregate_by_account_kind_code(data["rows"])
        aggregated[source] = agg
        for (account, kind, code_s) in agg:
            all_codes.add(code_s)
    all_codes |= set(existing_records.keys())
    # 部分更新 (issue #397 Phase3b): 引き継ぎソース由来で record が未登録の銘柄
    # (例: SBI現物のみに存在し楽天CSVには登場しない銘柄) も対象に含める
    all_codes |= {p["code_s"] for p in all_positions}

    if not dry_run:
        for source, agg in aggregated.items():
            broker, kind = source
            ps.delete_positions_for_source(broker, kind, db_path=db_path)
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
        # 書き込み後の状態を1回だけ再取得する (compute_merged_qty/is_covered を
        # 銘柄ごとに呼ぶと DB を都度 open して全件走査するため、実データ規模
        # (数百銘柄) では致命的に遅い。issue #397 Phase3b で実測・修正済み)
        all_positions = ps.list_positions(db_path=db_path)
        existing_sources = {(s["broker"], s["kind"]): s for s in ps.list_position_sources(db_path=db_path)}

    carried_over_keys = {
        (broker, kind) for broker, kind in not_uploaded if (broker, kind) in existing_sources
    }  # carried_over_sources と同じ集合 (キーが "broker/kind" 文字列か tuple かの違いのみ)
    positions_by_code: Dict[str, List[Dict[str, Any]]] = {}
    for pos in all_positions:
        positions_by_code.setdefault(pos["code_s"], []).append(pos)
    diffs = _build_diff_preview(
        all_codes, existing_records,
        dry_run_aggregated=aggregated if dry_run else None,
        carried_over_sources=carried_over_keys if dry_run else None,
        positions_by_code=positions_by_code,
        source_map=existing_sources,
        all_sources_present=not missing_sources,
    )

    applied = []
    if apply_records and not dry_run:
        applied = _sync_records(diffs, as_of, db_path=db_path, overrides=overrides)

    return {
        "sources": {f"{b}/{k}": len(v) for (b, k), v in aggregated.items()},
        "carried_over_sources": carried_over_sources,
        "missing_sources": missing_sources,
        "diffs": diffs,
        "applied": applied,
    }


def _build_diff_preview(
    all_codes: set,
    existing_records: Dict[str, Dict[str, Any]],
    *,
    dry_run_aggregated: Optional[Dict] = None,
    carried_over_sources: Optional[set] = None,
    positions_by_code: Dict[str, List[Dict[str, Any]]],
    source_map: Dict[Tuple[str, str], Dict[str, Any]],
    all_sources_present: bool = True,
) -> List[Dict[str, Any]]:
    """差分プレビュー行を組み立てる (issue #397 §5-3 の判定表)。

    covered は「その銘柄が全ソースに登場するか」ではなく「4ソース全てが
    今回の取込対象として揃っているか」で決まる (issue #397 §5-2: ソース側に
    銘柄が無い=保有ゼロも正常なので、銘柄単位の登場有無では判定しない)。
    dry_run 時は DB に position を書いていないので、aggregated から
    その場で merged_qty を計算する (covered は all_sources_present を使う)。
    apply 後は positions_by_code/source_map (呼び出し元で書き込み後に1回だけ
    取得済み) から compute_merged_qty/is_covered 相当を計算する。
    銘柄ごとに DB を呼ぶ (compute_merged_qty/is_covered/list_positions(code_s))
    と都度 open して全件走査するため N×M の計算量になり、実データ規模
    (数百銘柄) では致命的に遅くなる (issue #397 Phase3b で実測・修正済み)。

    carried_over_sources は今回アップロードされず DB の前回分を引き継ぐ
    (broker, kind) の集合 (issue #397 Phase3b の部分更新)。dry_run 時、
    この分の merged_qty は positions_by_code から補って合算する。
    """
    diffs = []
    carried_pos_keys = set(carried_over_sources or set())
    carried_pos_keys |= {
        (broker, "信用売建") for broker, kind in (carried_over_sources or set())
        if kind == "信用"
    }
    for code_s in sorted(all_codes):
        record = existing_records.get(code_s)
        status = record.get("status") if record else "未登録"
        db_qty = record.get("qty") if record else None
        code_positions = positions_by_code.get(code_s, [])

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
            # DB position の kind は "現物"/"信用"/"信用売建" の3種。carried_over_sources
            # (broker, kind) の kind は PARSERS キー相当の "現物"/"信用" のみなので、
            # "信用" ソースを引き継ぐ場合は同一 broker の "信用売建" position も対象に含める。
            for pos in code_positions:
                if (pos.get("broker"), pos.get("kind")) not in carried_pos_keys:
                    continue
                if pos.get("kind") == "信用売建":
                    has_short = True
                else:
                    merged_qty += pos.get("qty", 0)
            covered = all_sources_present and not has_short
        else:
            # compute_merged_qty(code_s) / is_covered(code_s) 相当をインライン計算
            merged_qty = sum(
                p.get("qty", 0) for p in code_positions if p.get("kind") != "信用売建"
            )
            has_short = any(p.get("kind") == "信用売建" for p in code_positions)
            covered = not has_short and all(
                (broker, kind) in source_map for broker, kind in ps.EXPECTED_POSITION_SOURCES
            )

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
        return "判定不能 (ソース不足のため反映されません)"
    if status == "1保":
        if merged_qty == 0:
            return "売却候補 (反映すると準保有へ)"
        return f"株数変更候補 {db_qty}→{merged_qty}"
    if status in ("2準", "3監"):
        return "新規IN候補 (反映すると保留キューまたは自動INへ)"
    if status == "未登録":
        return "未登録+保有検出 (反映すると監視へ登録)"
    return "-"


def _sync_records(
    diffs: List[Dict[str, Any]], as_of: str, *,
    db_path: Optional[str],
    overrides: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """covered な銘柄について実際に record (qty/status) を CSV に同期する
    (issue #397 Phase2)。§5-3 の判定表・§5-4 の売却・§6-2 の新規IN分岐を実装する。

    - covered=false の銘柄は一切触らない (§5-2)
    - 「一致」「対象外」は no-op
    - `source="csv_import"` を全ての反映操作に付与する

    overrides は {code_s: {"trade_idea": ..., "note": ...}} (issue #397 Phase3b:
    確認画面でユーザーが入力した内容)。新規IN では trade_idea が指定されていれば
    既存の戦略より優先し、record にも保存し直す。note は生成した reason の末尾に
    追記する (機械生成分は消さない)。両方省略可 (既定の自動反映のみ行う)。

    Returns: 実際に反映した内容のログ (dry-run では呼ばれない)
    """
    source_detail = f"ポートフォリオCSV/{as_of}"
    overrides = overrides or {}
    applied = []
    for d in diffs:
        code_s, status, covered = d["code_s"], d["status"], d["covered"]
        merged_qty, db_qty = d["merged_qty"], d["db_qty"]
        if not covered:
            continue
        note = (overrides.get(code_s, {}).get("note") or "").strip()

        if status == "1保":
            if merged_qty == db_qty:
                continue
            if merged_qty == 0:
                # 売却: 2準 に落とす (issue #397 §5-4)。3監にはしない
                # (売買履歴の集計から漏れるため。既存フローと同じ action_type=売却 で記録)
                # transition_status は遷移前の record["qty"] を売却ログに残すため、
                # 先に遷移させてから qty=0 に更新する (順序が逆だと「何株売ったか」の
                # 記録が失われる)。record 自体の qty は CSV=真実源の方針 (§3) に沿って
                # 0 に同期し、2準/3監なのに旧qtyが残る矛盾状態を避ける。
                reason = "CSV取込による売却検出"
                if note:
                    reason = f"{reason} / {note}"
                ps.transition_status(
                    code_s, "2準", reason=reason, action_date=as_of,
                    source="csv_import", source_detail=source_detail, db_path=db_path,
                )
                ps.update_qty(
                    code_s, 0, reason=reason, action_date=as_of, log_action=False,
                    source="csv_import", source_detail=source_detail, db_path=db_path,
                )
                applied.append({"code_s": code_s, "action": "売却(OUT)", "detail": f"{db_qty}→0"})
            else:
                # 株数変更のみ (既に1保なので log_action=True で株数変更ログを残す)
                reason = "CSV取込"
                ps.update_qty(
                    code_s, merged_qty, reason=reason, action_date=as_of,
                    source="csv_import", source_detail=source_detail, db_path=db_path,
                )
                applied.append({"code_s": code_s, "action": "株数変更", "detail": f"{db_qty}→{merged_qty}"})
            continue

        if status in ("2準", "3監") and merged_qty == 0:
            if ps.remove_pending_in(code_s, db_path=db_path):
                applied.append({"code_s": code_s, "action": "保留キューから削除", "detail": "qty=0"})
            continue

        if status in ("2準", "3監") and merged_qty > 0:
            record = ps.get_record(code_s, db_path=db_path)
            existing_trade_idea = (record.get("memo") or {}).get("trade_idea") if record else ""
            chosen_trade_idea = (overrides.get(code_s, {}).get("trade_idea") or "").strip() or existing_trade_idea
            if status == "2準" and chosen_trade_idea:
                # 戦略あり: webapp/routes/portfolio.py:520-537 と同じ順序で自動IN (issue #397 §6-2)
                # 確認画面で戦略が変更されていれば先に記録し直す (§ Phase3b)
                if chosen_trade_idea != existing_trade_idea:
                    ps.update_memo(code_s, {"trade_idea": chosen_trade_idea}, db_path=db_path)
                reason = "CSV取込による新規保有検出"
                if note:
                    reason = f"{reason} / {note}"
                ps.transition_status(
                    code_s, "1保", reason=reason, action_date=as_of, qty=merged_qty,
                    source="csv_import", source_detail=source_detail, db_path=db_path,
                )
                ps.update_qty(
                    code_s, merged_qty, reason=reason, action_date=as_of, log_action=False,
                    source="csv_import", source_detail=source_detail, db_path=db_path,
                )
                ps.remove_pending_in(code_s, db_path=db_path)
                applied.append({
                    "code_s": code_s, "action": "新規IN(自動)",
                    "detail": f"株数{merged_qty} / 戦略「{chosen_trade_idea}」",
                })
            else:
                # 3監、または 2準 でも戦略未設定 -> 保留キュー (issue #397 §6-2)
                ps.upsert_pending_in(code_s, merged_qty, as_of, db_path=db_path)
                applied.append({"code_s": code_s, "action": "保留キューへ", "detail": f"qty={merged_qty}"})
            continue

        if status == "未登録" and merged_qty > 0:
            # 登録は必ず3監から (issue #397 §5-3b)。1保への遷移は保留キュー経由で人が確定する
            reason = "CSV取込で保有を検出"
            ps.add_to_watch(
                code_s, reason=reason, action_date=as_of,
                source="csv_import", source_detail=source_detail, db_path=db_path,
            )
            ps.upsert_pending_in(code_s, merged_qty, as_of, db_path=db_path)
            applied.append({"code_s": code_s, "action": "登録+保留キューへ", "detail": f"qty={merged_qty}"})

    log_print("import_portfolio_csv: record 反映完了", f"件数={len(applied)}")
    return applied


# ===========================================
# 4. 実行層
# ===========================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="証券会社ポートフォリオCSV (4ソース) を position レイヤーへ取込 "
                    "(issue #397)。既定 (--apply のみ) は Phase1: 可視化のみで "
                    "record は変更しない。--apply-records を足すと Phase2: covered な "
                    "銘柄の qty/status を実際に同期する",
    )
    parser.add_argument("csv_paths", nargs="+", help="取込むCSVファイル (複数指定可、順不同)")
    parser.add_argument(
        "--as-of", required=True,
        help="CSVの基準日 (YYYY-MM-DD)。全ファイル共通の取込基準日として使う",
    )
    parser.add_argument("--dry-run", action="store_true", help="読込・差分算出のみ、DBへは書かない")
    parser.add_argument("--apply", action="store_true", help="position/position_source をDBへ保存する")
    parser.add_argument(
        "--apply-records", action="store_true",
        help="Phase2: covered な銘柄の qty更新・自動OUT・戦略ありの自動INを実際に反映する "
             "(--apply と併用必須。--dry-run とは併用不可)",
    )
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="未アップロードソースがDBに前回分もなく引き継げない場合でも実行を続行する "
             "(既定はエラー停止。DBに前回分があれば自動で引き継ぐため指定不要)",
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
    if args.apply_records and not args.apply:
        log_warning("--apply-records は --apply と併用してください")
        return 1
    if args.apply_records and args.dry_run:
        log_warning("--apply-records は --dry-run と併用できません")
        return 1

    try:
        result = import_csvs(
            args.csv_paths, args.as_of,
            dry_run=args.dry_run or not args.apply,
            allow_partial=args.allow_partial,
            apply_records=args.apply_records,
            db_path=args.db_path,
        )
    except ValueError as e:
        log_warning(f"import_portfolio_csv: {e}")
        return 1

    log_print("import_portfolio_csv: ソース内訳", result["sources"])
    if result["carried_over_sources"]:
        log_print("import_portfolio_csv: 前回分を引き継ぎ (未アップロード)", result["carried_over_sources"])
    if result["missing_sources"]:
        log_print("import_portfolio_csv: 不足ソース (--allow-partial で続行)", result["missing_sources"])

    if args.apply_records:
        log_print("import_portfolio_csv: record 反映結果 (Phase2)")
        for a in result["applied"]:
            log_print(f"  {a['code_s']} {a['action']} {a['detail']}")
    else:
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
