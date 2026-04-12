#!/usr/bin/env python3
"""
銘柄調査DB (research_shelve) の基盤モジュール。

銘柄ごとの総合評価・手動メモ・決算スナップショット時系列を
永続化するための shelve ベースのラッパー。

既存の stocks_shelve とは別DBとして分離運用する:
- stocks_shelve: 揮発性キャッシュ (常に最新値で上書き)
- research_shelve: 不可逆な蓄積資産 (時系列履歴 + 手動メモ)

依存は一方向: stocks_shelve (読み取り) -> research_shelve (書き込み)
stocks_shelve 側のコードは変更しない。

本モジュールの公開関数 (upsert_research_record, upsert_snapshot,
delete_research_record 等) を経由して更新を行い、他のモジュールが
直接 ShelveDB(RESEARCH_SHELVE) を開いて書き換えてはならない。
"""

import fcntl
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from db_shelve import RESEARCH_SHELVE, ShelveDB

try:
    from ks_util import backup_file, log_print, log_warning
except ImportError:
    # テスト時の依存解決フォールバック
    import shutil as _shutil

    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)

    def backup_file(fname, day=0):  # type: ignore[override]
        # フォールバック版: ks_util がない環境での簡易実装
        if not os.path.exists(fname):
            return None
        backup_fname = fname + ".bak_fallback"
        _shutil.copy2(fname, backup_fname)
        return backup_fname


# ===========================================
# スキーマ定数
# ===========================================

# 銘柄コードの正規表現 (CLAUDE.md 規約: "0001"〜"9999" または "215A" 形式)
# 厳密に4文字。"123" / "1234A" / "A215" は reject する。
CODE_S_PATTERN = re.compile(r"^(?:\d{4}|\d{3}[A-Z])$")

# 日付形式 (YY.M = 月精度, YY.M.D = 日精度)
DATE_YY_M_PATTERN = re.compile(r"^(\d{2})\.(\d{1,2})(?:\.(\d{1,2}))?$")

# 総合評価の許容値
VALID_RATINGS = frozenset({"", "S", "A", "B", "C", "D", "E"})

# スナップショットのデータソース区分
VALID_DATA_SOURCES = frozenset({"manual", "migration", "auto"})

# レコードの既知フィールド (未知フィールドは警告ログに出す)
RECORD_FIELDS = frozenset(
    {
        "code_s",
        "stock_name",
        "overview",
        "overall_rating",
        "institutional_comment",
        "memo",
        "openwork",
        "cramer",
        "shikiho_comments",
        "snapshots",
        "analysis_date_raw",
        "kessan_date_raw",
    }
)

# スナップショットの既知フィールド
SNAPSHOT_FIELDS = frozenset(
    {
        "date_yy_m",
        "ir_quant",
        "ir_comment",
        "quality_indicators",
        "rironkabuka_kairi",
        "data_source",
    }
)


# ===========================================
# バリデーション・正規化
# ===========================================

def normalize_code_s(code_s: Any) -> str:
    """銘柄コード文字列を正規化する。

    - 文字列型でない場合は TypeError
    - 前後の空白を除去
    - 英字部分を大文字化 (実データに "135a" のような小文字混入例が存在)
    """
    if not isinstance(code_s, str):
        raise TypeError(f"code_s must be str, got {type(code_s).__name__}")
    return code_s.strip().upper()


def validate_code_s(code_s: Any) -> None:
    """銘柄コードを検証する。正規化後にパターンに合致しなければ ValueError。"""
    normalized = normalize_code_s(code_s)
    if not CODE_S_PATTERN.match(normalized):
        raise ValueError(
            f"invalid code_s: {code_s!r} (正規化後={normalized!r}、"
            "期待形式は4文字の数字または3桁数字+大文字1文字)"
        )


def validate_date_yy_m(date_yy_m: Any) -> None:
    """スナップショットの日付形式を検証する。

    形式: "YY.M" / "YY.MM"（月精度）または "YY.M.D" / "YY.MM.DD"（日精度）
    例: "26.1", "25.11", "26.4.15"
    """
    if not isinstance(date_yy_m, str):
        raise TypeError(f"date_yy_m must be str, got {type(date_yy_m).__name__}")
    m = DATE_YY_M_PATTERN.match(date_yy_m)
    if not m:
        raise ValueError(
            f"invalid date_yy_m: {date_yy_m!r} (期待形式は 'YY.M' or 'YY.M.D')"
        )
    month = int(m.group(2))
    if not 1 <= month <= 12:
        raise ValueError(
            f"invalid date_yy_m month: {date_yy_m!r} (月は1-12の範囲)"
        )
    if m.group(3) is not None:
        day = int(m.group(3))
        if not 1 <= day <= 31:
            raise ValueError(
                f"invalid date_yy_m day: {date_yy_m!r} (日は1-31の範囲)"
            )


def validate_rating(rating: Any) -> None:
    """総合評価を検証する。"""
    if rating not in VALID_RATINGS:
        raise ValueError(
            f"invalid overall_rating: {rating!r} (許容値: {sorted(VALID_RATINGS)})"
        )


def validate_data_source(data_source: Any) -> None:
    """スナップショットのデータソース区分を検証する。"""
    if data_source not in VALID_DATA_SOURCES:
        raise ValueError(
            f"invalid data_source: {data_source!r} "
            f"(許容値: {sorted(VALID_DATA_SOURCES)})"
        )


def date_yy_m_sort_key(date_yy_m: str) -> tuple:
    """スナップショット並び替え用のソートキーを返す。

    "26.1" -> (26, 1, 0), "25.11" -> (25, 11, 0), "26.4.15" -> (26, 4, 15)
    日なしは day=0 として同月内で先に来る。
    降順ソート時は sort(..., reverse=True) で最新が先頭になる。
    """
    m = DATE_YY_M_PATTERN.match(date_yy_m)
    if not m:
        raise ValueError(f"invalid date_yy_m for sort: {date_yy_m!r}")
    day = int(m.group(3)) if m.group(3) else 0
    return (int(m.group(1)), int(m.group(2)), day)


# ===========================================
# レコード・スナップショットのファクトリ
# ===========================================

def create_research_record(
    code_s: str,
    stock_name: str,
    *,
    overview: str = "",
    overall_rating: str = "",
    institutional_comment: str = "",
    memo: str = "",
    openwork: str = "",
    cramer: str = "",
    shikiho_comments: Optional[List[str]] = None,
    snapshots: Optional[List[Dict[str, Any]]] = None,
    analysis_date_raw: str = "",
    kessan_date_raw: str = "",
) -> Dict[str, Any]:
    """銘柄調査レコードのひな型 dict を生成する。

    - code_s は normalize_code_s で大文字化される
    - overall_rating は空/S〜E のみ許容
    - shikiho_comments と snapshots はリストでない場合に空リストで補完
    - analysis_date_raw / kessan_date_raw はスプシ原文保持用(例: "11/13",
      "22四季報春" などの異形も許容。形式バリデーションはしない。型チェックのみ)
    - 返却した dict は upsert_research_record にそのまま渡せる
    """
    validate_code_s(code_s)
    normalized_code = normalize_code_s(code_s)
    if not isinstance(stock_name, str):
        raise TypeError(f"stock_name must be str, got {type(stock_name).__name__}")
    validate_rating(overall_rating)
    if not isinstance(analysis_date_raw, str):
        raise TypeError(
            f"analysis_date_raw must be str, got {type(analysis_date_raw).__name__}"
        )
    if not isinstance(kessan_date_raw, str):
        raise TypeError(
            f"kessan_date_raw must be str, got {type(kessan_date_raw).__name__}"
        )

    shikiho = list(shikiho_comments) if shikiho_comments else []
    snaps = list(snapshots) if snapshots else []

    return {
        "code_s": normalized_code,
        "stock_name": stock_name,
        "overview": overview,
        "overall_rating": overall_rating,
        "institutional_comment": institutional_comment,
        "memo": memo,
        "openwork": openwork,
        "cramer": cramer,
        "shikiho_comments": shikiho,
        "snapshots": snaps,
        "analysis_date_raw": analysis_date_raw,
        "kessan_date_raw": kessan_date_raw,
    }


def create_snapshot(
    date_yy_m: str,
    *,
    ir_quant: str = "",
    ir_comment: str = "",
    quality_indicators: str = "",
    rironkabuka_kairi: str = "",
    data_source: str = "manual",
) -> Dict[str, Any]:
    """決算スナップショット1件の dict を生成する。

    - date_yy_m は validate_date_yy_m で検証
    - data_source は manual/migration/auto のみ許容 (デフォルト manual)
    - 他フィールドは空文字をデフォルト (原文保持用)
    """
    validate_date_yy_m(date_yy_m)
    validate_data_source(data_source)

    return {
        "date_yy_m": date_yy_m,
        "ir_quant": ir_quant,
        "ir_comment": ir_comment,
        "quality_indicators": quality_indicators,
        "rironkabuka_kairi": rironkabuka_kairi,
        "data_source": data_source,
    }


# ===========================================
# プロセス間排他制御
# ===========================================

def _lock_path_for(db_path: Optional[str] = None) -> str:
    """ロックファイルパスを返す。"""
    base = db_path if db_path is not None else RESEARCH_SHELVE
    return base + ".lock"


# リエントラント検出用: 同一スレッドが既にロックを保持しているか
_flock_holder = threading.local()


@contextmanager
def _flock(db_path: Optional[str] = None):
    """research_shelve 書き込み用の排他ロック (fcntl.flock)。

    Web側 (helpers.py) とバッチ側 (upsert_snapshot 等) の両方が
    同じロックファイルを取ることで、プロセス間の read-modify-write
    競合を防止する。

    リエントラント対応: helpers が外側で _flock() を取得した状態で
    upsert_research_record() を呼ぶと内部でも _flock() に入るが、
    同一スレッドの場合はロック取得をスキップしてデッドロックを防ぐ。
    """
    if getattr(_flock_holder, "depth", 0) > 0:
        # 同一スレッドで既にロック保持中 → そのまま通過
        _flock_holder.depth += 1
        try:
            yield
        finally:
            _flock_holder.depth -= 1
        return

    lock_file = _lock_path_for(db_path)
    lock_dir = os.path.dirname(lock_file)
    if lock_dir and not os.path.exists(lock_dir):
        os.makedirs(lock_dir, exist_ok=True)
    fd = open(lock_file, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _flock_holder.depth = 1
        try:
            yield
        finally:
            _flock_holder.depth = 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ===========================================
# CRUD層
# ===========================================

def _resolve_db_path(db_path: Optional[str]) -> str:
    """テスト時の差し替え用に db_path を解決する。"""
    return db_path if db_path is not None else RESEARCH_SHELVE


def _validate_record_for_upsert(record: Dict[str, Any]) -> None:
    """upsert 対象のレコードを検証する。

    - dict であること
    - code_s が必須 (欠落は ValueError)
    - code_s / overall_rating の値を検証
    - 未知フィールドがあれば警告ログを出す (保存は許容)
    """
    if not isinstance(record, dict):
        raise TypeError(f"record must be dict, got {type(record).__name__}")
    if "code_s" not in record:
        raise ValueError("record に code_s が必須です")
    validate_code_s(record["code_s"])
    if "overall_rating" in record:
        validate_rating(record["overall_rating"])

    unknown = set(record.keys()) - RECORD_FIELDS
    if unknown:
        log_warning(
            "research_shelve: 未知のレコードフィールドを保存します:",
            sorted(unknown),
        )


def get_research_record(
    code_s: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """1銘柄の調査レコードを取得する。

    存在しなければ None。code_s は内部で正規化される。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        return db.get(normalized)


def upsert_research_record(
    record: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> None:
    """銘柄調査レコードを追加または上書きする。

    - record["code_s"] は必須
    - 既存レコードがあれば完全上書き (部分マージは行わない)
    - 未知フィールドは警告ログ付きで保存 (後方互換のため)
    - fcntl.flock でプロセス間排他を保証
    """
    _validate_record_for_upsert(record)
    normalized = normalize_code_s(record["code_s"])
    stored = dict(record)
    stored["code_s"] = normalized
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            existed = normalized in db
            db[normalized] = stored
    if existed:
        log_print("research_shelve: レコード更新", normalized)
    else:
        log_print("research_shelve: レコード追加", normalized)


def delete_research_record(
    code_s: str,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """銘柄調査レコードを削除する。

    成功時は True、非存在時は False (KeyError ではなく bool を返す)。
    fcntl.flock でプロセス間排他を保証。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            if normalized not in db:
                return False
            del db[normalized]
    log_print("research_shelve: レコード削除", normalized)
    return True


# ===========================================
# スナップショット層
# ===========================================

def _validate_snapshot_for_upsert(snapshot: Dict[str, Any]) -> None:
    """upsert 対象のスナップショットを検証する。"""
    if not isinstance(snapshot, dict):
        raise TypeError(f"snapshot must be dict, got {type(snapshot).__name__}")
    if "date_yy_m" not in snapshot:
        raise ValueError("snapshot に date_yy_m が必須です")
    validate_date_yy_m(snapshot["date_yy_m"])
    if "data_source" in snapshot:
        validate_data_source(snapshot["data_source"])

    unknown = set(snapshot.keys()) - SNAPSHOT_FIELDS
    if unknown:
        log_warning(
            "research_shelve: 未知のスナップショットフィールドを保存します:",
            sorted(unknown),
        )


def upsert_snapshot(
    code_s: str,
    snapshot: Dict[str, Any],
    *,
    overwrite_same_date: bool = True,
    db_path: Optional[str] = None,
) -> None:
    """指定銘柄のスナップショット配列に1件追加または上書きする。

    - overwrite_same_date=True (デフォルト): 同一 date_yy_m のエントリが既に
      存在する場合は冪等上書き
    - overwrite_same_date=False: 同一 date_yy_m でも2件目として許容する
      (移行時の複数スナップショット同日発生への安全弁)
    - レコード自体が存在しなければ KeyError (自動作成はしない)
    - 追記後は date_yy_m 降順にソート (最新が先頭)

    ShelveDB は writeback=False なので、record["snapshots"] を直接
    append しても永続化されない。ここでは再代入パターンで書き戻す。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    _validate_snapshot_for_upsert(snapshot)
    new_date = snapshot["date_yy_m"]
    path = _resolve_db_path(db_path)

    with _flock(db_path):
        with ShelveDB(path) as db:
            if normalized not in db:
                raise KeyError(
                    f"research_shelve: レコード未登録の銘柄にスナップショットを"
                    f"追加できません: {normalized} "
                    f"(先に upsert_research_record を呼んでください)"
                )
            record = db[normalized]
            snapshots = list(record.get("snapshots") or [])

            if overwrite_same_date:
                snapshots = [s for s in snapshots if s.get("date_yy_m") != new_date]

            snapshots.append(dict(snapshot))
            snapshots.sort(
                key=lambda s: date_yy_m_sort_key(s["date_yy_m"]),
                reverse=True,
            )

            record["snapshots"] = snapshots
            db[normalized] = record  # 再代入で永続化

    log_print(
        "research_shelve: スナップショット追記",
        normalized,
        new_date,
        f"total={len(snapshots)}",
    )


# ===========================================
# フィルタ層
# ===========================================

# keyword 部分一致の対象フィールド
KEYWORD_SEARCH_FIELDS = (
    "stock_name",
    "overview",
    "memo",
    "openwork",
    "cramer",
    "institutional_comment",
)


def _parse_rating_filter(rating: Optional[str]) -> Optional[set]:
    """rating フィルタ文字列を set に変換する。

    - None なら None (フィルタなし)
    - "S" なら {"S"}
    - "S,A" なら {"S", "A"} (カンマ区切り)
    - 空白や空要素は除外
    - VALID_RATINGS にない値があれば ValueError
    """
    if rating is None:
        return None
    tokens = [t.strip() for t in rating.split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    invalid = [t for t in tokens if t not in VALID_RATINGS]
    if invalid:
        raise ValueError(
            f"invalid rating filter: {invalid} "
            f"(許容値: {sorted(VALID_RATINGS)})"
        )
    return set(tokens)


def _matches_keyword(record: Dict[str, Any], keyword_lower: str) -> bool:
    """レコードが keyword (小文字) を含むかを判定する。"""
    for field in KEYWORD_SEARCH_FIELDS:
        value = record.get(field, "")
        if not isinstance(value, str):
            continue
        if keyword_lower in value.lower():
            return True
    return False


def list_research_records(
    *,
    rating: Optional[str] = None,
    keyword: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """銘柄調査レコードを一覧取得する。

    - rating: None でフィルタなし。"S" / "S,A" のようにカンマ区切り複数指定可
    - keyword: None でフィルタなし。部分一致 (大文字小文字無視)。対象は
      stock_name / overview / memo / openwork / cramer / institutional_comment
    - 結果は code_s 昇順
    """
    rating_set = _parse_rating_filter(rating)
    keyword_lower = keyword.lower() if keyword else None
    path = _resolve_db_path(db_path)

    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for _, record in db.items():
            if not isinstance(record, dict):
                continue
            if rating_set is not None:
                if record.get("overall_rating", "") not in rating_set:
                    continue
            if keyword_lower is not None:
                if not _matches_keyword(record, keyword_lower):
                    continue
            results.append(record)

    results.sort(key=lambda r: r.get("code_s", ""))
    return results


# ===========================================
# バックアップ層
# ===========================================

# shelve は .dat/.dir/.bak の3点セットで構成される (dbm.dumb)
_SHELVE_EXTENSIONS = (".dat", ".dir", ".bak")


def backup_research_db(*, db_path: Optional[str] = None) -> List[str]:
    """research_shelve の実体ファイルを3点セットでバックアップする。

    - .dat / .dir / .bak の各ファイルを ks_util.backup_file() に渡す
    - 存在しないファイルはスキップ (例外にしない)
    - 戻り値: backup_file() が生成/特定した backup_fname のリスト
      (既存のバックアップ名と同じ場合も含む)
    """
    path = _resolve_db_path(db_path)
    created: List[str] = []
    log_print("research_shelve: バックアップ開始", path)
    for ext in _SHELVE_EXTENSIONS:
        target = path + ext
        if not os.path.exists(target):
            continue
        backup_fname = backup_file(target, 0)
        if backup_fname:
            created.append(backup_fname)
    log_print("research_shelve: バックアップ完了", f"files={len(created)}")
    return created


# ===========================================
# 表示整形層 (CLI用)
# ===========================================

_EMPTY_MARK = "-"
_SEPARATOR_LEN = 64


def _value_or_dash(value: Any) -> str:
    """空値を '-' に置換して表示する。"""
    if value is None:
        return _EMPTY_MARK
    text = str(value)
    return text if text else _EMPTY_MARK


def _indent_multiline(text: str, indent: str) -> str:
    """複数行文字列を1行目以降インデントして返す。空文字は '-'。"""
    if not text:
        return _EMPTY_MARK
    lines = text.split("\n")
    if len(lines) == 1:
        return lines[0]
    head = lines[0]
    tail = "\n".join(indent + ln for ln in lines[1:])
    return head + "\n" + tail


def format_record_full(record: Dict[str, Any]) -> str:
    """1銘柄の全フィールドとスナップショット時系列をASCII整形する。"""
    if not isinstance(record, dict):
        raise TypeError(f"record must be dict, got {type(record).__name__}")

    code_s = record.get("code_s", "????")
    stock_name = record.get("stock_name", "")
    rating = record.get("overall_rating", "") or "未評価"
    overview = record.get("overview", "")
    inst = record.get("institutional_comment", "")
    memo = record.get("memo", "")
    openwork = record.get("openwork", "")
    cramer = record.get("cramer", "")
    shikiho = record.get("shikiho_comments") or []
    snapshots = record.get("snapshots") or []
    analysis_date_raw = record.get("analysis_date_raw", "")
    kessan_date_raw = record.get("kessan_date_raw", "")

    sep = "=" * _SEPARATOR_LEN
    lines: List[str] = []
    lines.append(sep)
    lines.append(f"[{code_s}] {stock_name} (レーティング: {rating})")
    lines.append(sep)

    # --- 基本情報ブロック ---
    lines.append(f"概要         : {_indent_multiline(overview, ' ' * 15)}")
    lines.append(f"機関投資家   : {_indent_multiline(inst, ' ' * 15)}")
    lines.append(f"分析日       : {_value_or_dash(analysis_date_raw)}")
    lines.append(f"決算日       : {_value_or_dash(kessan_date_raw)}")
    lines.append("")

    # --- 手動メモ群ブロック ---
    lines.append("-- 手動メモ --")
    lines.append(f"メモ・総括     : {_indent_multiline(memo, ' ' * 17)}")
    lines.append(f"OpenWork      : {_indent_multiline(openwork, ' ' * 16)}")
    lines.append(f"ジムクレイマー : {_indent_multiline(cramer, ' ' * 17)}")
    if shikiho:
        lines.append(f"四季報コメント ({len(shikiho)}件):")
        for i, comment in enumerate(shikiho, start=1):
            # 1 セル内に 【タイトル】... が複数ブロック連結されている場合があるので、
            # 全文を改行インデント付きで表示する
            lines.append(f"  [{i}] {_indent_multiline(comment, ' ' * 6)}")
    else:
        lines.append(f"四季報コメント : {_EMPTY_MARK}")
    lines.append("")

    # --- スナップショットブロック ---
    if snapshots:
        lines.append(
            f"-- スナップショット (最新順、{len(snapshots)}件) --"
        )
        for snap in snapshots:
            date = snap.get("date_yy_m", "??.?")
            data_source = snap.get("data_source", "")
            lines.append(f"[{date}] ({data_source})")
            lines.append(
                f"  IR分析定量    : {_value_or_dash(snap.get('ir_quant'))}"
            )
            lines.append(
                f"  IR分析コメント : "
                f"{_indent_multiline(snap.get('ir_comment', ''), ' ' * 17)}"
            )
            lines.append(
                f"  クォリティ    : "
                f"{_indent_multiline(snap.get('quality_indicators', ''), ' ' * 16)}"
            )
            lines.append(
                f"  理論株価乖離  : "
                f"{_value_or_dash(snap.get('rironkabuka_kairi'))}"
            )
    else:
        lines.append("-- スナップショット (0件) --")

    lines.append(sep)
    return "\n".join(lines)


def format_record_summary(record: Dict[str, Any]) -> str:
    """一覧表示用の1行サマリを返す。

    タブ区切りで code_s / rating / stock_name / スナップショット数 / overview先頭。
    """
    if not isinstance(record, dict):
        raise TypeError(f"record must be dict, got {type(record).__name__}")
    code_s = record.get("code_s", "")
    rating = record.get("overall_rating", "") or "-"
    stock_name = record.get("stock_name", "") or "-"
    snap_count = len(record.get("snapshots") or [])
    overview = (record.get("overview", "") or "").replace("\n", " ")
    if len(overview) > 30:
        overview = overview[:30] + "..."
    if not overview:
        overview = _EMPTY_MARK
    return f"{code_s}\t{rating}\t{stock_name}\t{snap_count}\t{overview}"


# ===========================================
# CLI サブコマンド実装
# ===========================================

def _cmd_show(code_s: str, *, db_path: Optional[str] = None) -> int:
    """show サブコマンド本体。戻り値はプロセス終了コード。"""
    try:
        validate_code_s(code_s)
    except (ValueError, TypeError) as exc:
        log_warning("research_shelve show: 不正な code_s:", exc)
        return 2
    record = get_research_record(code_s, db_path=db_path)
    if record is None:
        log_print(
            f"research_shelve: {normalize_code_s(code_s)} "
            "に該当するレコードはありません"
        )
        return 1
    # 出力本体は標準出力へ直接書く (CLI表示の主目的)
    print(format_record_full(record))
    return 0


def _cmd_list(
    *,
    rating: Optional[str] = None,
    keyword: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """list サブコマンド本体。"""
    try:
        records = list_research_records(
            rating=rating, keyword=keyword, db_path=db_path
        )
    except ValueError as exc:
        log_warning("research_shelve list: 不正なフィルタ:", exc)
        return 2

    header = "code_s\trating\tstock_name\tsnapshots\toverview"
    print(header)
    print("-" * len(header))
    for rec in records:
        print(format_record_summary(rec))
    print("-" * len(header))

    conditions: List[str] = []
    if rating:
        conditions.append(f"rating={rating}")
    if keyword:
        conditions.append(f"keyword={keyword}")
    cond_str = " ".join(conditions) if conditions else "no filter"
    print(f"{len(records)} records ({cond_str})")
    return 0


def _cmd_backup(*, db_path: Optional[str] = None) -> int:
    """backup サブコマンド本体。"""
    created = backup_research_db(db_path=db_path)
    if not created:
        log_print(
            "research_shelve backup: 対象ファイルがありませんでした"
        )
    return 0


def _build_arg_parser():
    """CLI の argparse パーサーを構築する。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="銘柄調査DB (research_shelve) の表示・バックアップCLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # show
    show_p = subparsers.add_parser(
        "show", help="特定銘柄の調査データを表示する"
    )
    show_p.add_argument(
        "code_s",
        type=str,
        help="証券コード (例: 3496, 215A)",
    )

    # list
    list_p = subparsers.add_parser(
        "list", help="レコードをフィルタ表示する"
    )
    list_p.add_argument(
        "--rating",
        type=str,
        default=None,
        help="総合評価フィルタ (例: S / S,A)",
    )
    list_p.add_argument(
        "--keyword",
        type=str,
        default=None,
        help="銘柄名・概要・メモへの部分一致 (大文字小文字無視)",
    )

    # backup
    subparsers.add_parser("backup", help="DB本体のバックアップを作成する")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI エントリポイント。"""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "show":
        return _cmd_show(args.code_s)
    if args.command == "list":
        return _cmd_list(rating=args.rating, keyword=args.keyword)
    if args.command == "backup":
        return _cmd_backup()
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    import sys

    sys.exit(main())
