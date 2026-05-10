#!/usr/bin/env python3
"""
保有銘柄管理DB (portfolio_shelve) の基盤モジュール。

保有銘柄のステータス・手動メモ・アクションログを永続化するための
shelve ベースのラッパー。

既存の stocks_shelve / research_shelve とは別DBとして分離運用する:
- stocks_shelve: 揮発性キャッシュ (常に最新値で上書き)
- research_shelve: 不可逆な蓄積資産 (時系列履歴 + 調査メモ)
- portfolio_shelve: 保有状態 + 売買判断メモ + アクションログ

依存は一方向: portfolio_shelve が他の2DBを参照する形。
他のDBのコードは変更しない。

キー名前空間:
- record:<code_s>            -> 保有レコード本体
- action_log:<code_s>:<seq>  -> アクションログ (削除後も残る)
- _seq:<code_s>              -> アクションログの連番カウンタ

ライフサイクル:
- 追加: (新規) -> 3監 (1保/2準への直接登録は禁止)
- ステータス変更: 3監 <-> 2準 <-> 1保 / 3監 <-> 1保
- 売却: 1保 -> 2準 (アクションログ種別「売却」で記録)
- 削除: 3監 のみ (1保/2準 から直接削除は禁止、レコードは物理削除) ※現在 UI 経路なし
- ユニバース除外: 3監 のみ。`excluded=True` フラグで論理削除し、メモ・ログを保持。
  add_to_watch で同コード再投入すると excluded=False に戻して復活する
"""

import fcntl
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from db_shelve import PORTFOLIO_SHELVE, ShelveDB

try:
    from ks_util import DATA_DIR, log_print, log_warning
except ImportError:
    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)


# ===========================================
# スキーマ定数
# ===========================================

# 銘柄コードの正規表現 (CLAUDE.md 規約: "0001"〜"9999" または "215A" 形式)
CODE_S_PATTERN = re.compile(r"^(?:\d{4}|\d{3}[A-Z])$")

# ステータスの許容値
VALID_STATUSES = frozenset({"1保", "2準", "3監"})

# アクションログ種別
VALID_ACTION_TYPES = frozenset(
    {"初回登録", "ステータス変更", "売却", "削除", "メモ更新", "ユニバース除外"}
)

# キー名前空間プレフィックス
KEY_RECORD_PREFIX = "record:"
KEY_ACTION_LOG_PREFIX = "action_log:"
KEY_SEQ_PREFIX = "_seq:"

# レコードの既知フィールド (銘柄名は持たない: 表示時に stocks_shelve / research_shelve から都度取得する)
RECORD_FIELDS = frozenset(
    {
        "code_s",
        "status",
        "registered_at",
        "updated_at",
        "memo",
        "excluded",
    }
)

# 旧スキーマ由来で許容するが扱わないフィールド (新スキーマでは未使用、過去データ互換のため warning しない)
LEGACY_RECORD_FIELDS = frozenset({"stock_name"})

MEMO_FIELDS = frozenset(
    {
        "gyoutai_theme",          # 業態・テーマ
        "watch_in_reason",        # ウォッチ・IN理由
        "trade_idea",             # 投資売買アイデア
        "inago_origin",           # イナゴ元・きっかけ
        "takaichi_sensitivity",   # 高市感応度
        "last_research_update",   # 銘柄調査スプシでの更新日 (M/D 形式、年なし)
        "stage",                  # ステージ評価 (例: "1S", "2S(3T)", "3S")
        "jukyu_chart",            # 需給チャートメモ (例: "月足低位ブレイク CWH")
    }
)

ACTION_LOG_FIELDS = frozenset(
    {
        "code_s",
        "seq",
        "timestamp",
        "action_type",
        "status_from",
        "status_to",
        "reason",
    }
)

# 許可されるステータス遷移 (status_from, status_to)
# (None, "3監") は新規追加。それ以外は (from, to) の組
ALLOWED_TRANSITIONS = frozenset(
    {
        (None, "3監"),     # 新規追加 (1保/2準 への直接登録禁止)
        ("3監", "2準"),
        ("2準", "3監"),
        ("2準", "1保"),
        ("1保", "2準"),    # = 売却
        ("3監", "1保"),
        ("1保", "3監"),
    }
)


# ===========================================
# バリデーション・正規化
# ===========================================

# JST タイムゾーン (timestamp の付与に使う)
JST = timezone(timedelta(hours=9))


def normalize_code_s(code_s: Any) -> str:
    """銘柄コード文字列を正規化する。

    - 文字列型でない場合は TypeError
    - 前後の空白を除去
    - 英字部分を大文字化
    """
    if not isinstance(code_s, str):
        raise TypeError(f"code_s must be str, got {type(code_s).__name__}")
    return code_s.strip().upper()


def validate_code_s(code_s: Any) -> None:
    """銘柄コードを検証する。"""
    normalized = normalize_code_s(code_s)
    if not CODE_S_PATTERN.match(normalized):
        raise ValueError(
            f"invalid code_s: {code_s!r} (正規化後={normalized!r}、"
            "期待形式は4文字の数字または3桁数字+大文字1文字)"
        )


def validate_status(status: Any) -> None:
    """ステータスを検証する。"""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status: {status!r} (許容値: {sorted(VALID_STATUSES)})"
        )


def validate_action_type(action_type: Any) -> None:
    """アクションログ種別を検証する。"""
    if action_type not in VALID_ACTION_TYPES:
        raise ValueError(
            f"invalid action_type: {action_type!r} "
            f"(許容値: {sorted(VALID_ACTION_TYPES)})"
        )


def validate_transition(status_from: Optional[str], status_to: str) -> None:
    """ステータス遷移を検証する。

    status_from=None は新規追加を表す。
    """
    if status_from is not None:
        validate_status(status_from)
    validate_status(status_to)
    if (status_from, status_to) not in ALLOWED_TRANSITIONS:
        raise ValueError(
            f"invalid transition: {status_from!r} -> {status_to!r} "
            f"(許可されていない遷移)"
        )


def now_iso() -> str:
    """現在時刻を ISO 8601 文字列 (JST) で返す。"""
    return datetime.now(JST).isoformat()


# ===========================================
# キー組立
# ===========================================

def _record_key(code_s: str) -> str:
    return f"{KEY_RECORD_PREFIX}{code_s}"


def _action_log_key(code_s: str, seq: int) -> str:
    return f"{KEY_ACTION_LOG_PREFIX}{code_s}:{seq:06d}"


def _seq_key(code_s: str) -> str:
    return f"{KEY_SEQ_PREFIX}{code_s}"


def _action_log_prefix_for(code_s: str) -> str:
    return f"{KEY_ACTION_LOG_PREFIX}{code_s}:"


# ===========================================
# プロセス間排他制御
# ===========================================

_flock_holder = threading.local()


def _lock_path_for(db_path: Optional[str] = None) -> str:
    base = db_path if db_path is not None else PORTFOLIO_SHELVE
    return base + ".lock"


@contextmanager
def _flock(db_path: Optional[str] = None):
    """portfolio_shelve 書き込み用の排他ロック。

    research_shelve と同じパターン。同一スレッドのリエントラントは深さで管理。
    """
    if getattr(_flock_holder, "depth", 0) > 0:
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


def _resolve_db_path(db_path: Optional[str]) -> str:
    return db_path if db_path is not None else PORTFOLIO_SHELVE


# ===========================================
# ファクトリ
# ===========================================

def create_memo(
    *,
    gyoutai_theme: str = "",
    watch_in_reason: str = "",
    trade_idea: str = "",
    inago_origin: str = "",
    takaichi_sensitivity: str = "",
    last_research_update: str = "",
    stage: str = "",
    jukyu_chart: str = "",
) -> Dict[str, str]:
    """手動メモ dict を生成する。"""
    return {
        "gyoutai_theme": gyoutai_theme,
        "watch_in_reason": watch_in_reason,
        "trade_idea": trade_idea,
        "inago_origin": inago_origin,
        "takaichi_sensitivity": takaichi_sensitivity,
        "last_research_update": last_research_update,
        "stage": stage,
        "jukyu_chart": jukyu_chart,
    }


def create_record(
    code_s: str,
    *,
    status: str = "3監",
    memo: Optional[Dict[str, str]] = None,
    registered_at: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """portfolio レコード dict を生成する。

    銘柄名はこのレコードには保存しない (要件 §4: 指標データは保存せず stocks_shelve から
    都度参照する原則を銘柄名にも適用)。

    - code_s は normalize_code_s で大文字化される
    - status はデフォルト "3監" (新規追加用)
    - memo が None なら空メモで埋める
    - registered_at / updated_at が None なら現在時刻を埋める
    """
    validate_code_s(code_s)
    normalized_code = normalize_code_s(code_s)
    validate_status(status)
    if memo is None:
        memo = create_memo()
    elif not isinstance(memo, dict):
        raise TypeError(f"memo must be dict, got {type(memo).__name__}")
    timestamp = registered_at or now_iso()
    return {
        "code_s": normalized_code,
        "status": status,
        "registered_at": timestamp,
        "updated_at": updated_at or timestamp,
        "memo": dict(memo),
        "excluded": False,
    }


# ===========================================
# CRUD: レコード
# ===========================================

def get_record(
    code_s: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """1銘柄の保有レコードを取得する。存在しなければ None。"""
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        return db.get(_record_key(normalized))


def list_records(
    status: Optional[str] = None,
    *,
    include_excluded: bool = False,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """保有レコードを一覧取得する。

    - status: None で全件、"1保"/"2準"/"3監" 指定で絞り込み
    - include_excluded: False (既定) なら excluded=True のレコードを除外。
      True なら除外フラグ無視で全件返す (DB 整合性チェックや fallback 判定用)
    - 結果は code_s 昇順
    """
    if status is not None:
        validate_status(status)
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_RECORD_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            if status is not None and value.get("status") != status:
                continue
            if not include_excluded and value.get("excluded", False):
                continue
            results.append(value)
    results.sort(key=lambda r: r.get("code_s", ""))
    return results


def _next_seq(db: ShelveDB, code_s: str) -> int:
    """指定銘柄の次のアクションログ seq を返し、カウンタを進める。

    db は既にオープン済みの ShelveDB。呼び出し側で flock 保持を前提とする。
    """
    seq_k = _seq_key(code_s)
    current = db.get(seq_k, 0)
    nxt = int(current) + 1
    db[seq_k] = nxt
    return nxt


def append_action_log(
    code_s: str,
    action_type: str,
    *,
    status_from: Optional[str] = None,
    status_to: Optional[str] = None,
    reason: str = "",
    timestamp: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """アクションログを1件追加する。

    内部利用および移行スクリプトからの直接利用を想定。
    transition_status / add_to_watch / delete_record からも呼ばれる。
    レコードを物理削除した後でもログ追記は可能 (ログだけ残す要件のため)。

    Returns: 追記したログエントリ
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    validate_action_type(action_type)
    if status_from is not None:
        validate_status(status_from)
    if status_to is not None:
        validate_status(status_to)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")
    ts = timestamp or now_iso()

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            seq = _next_seq(db, normalized)
            entry = {
                "code_s": normalized,
                "seq": seq,
                "timestamp": ts,
                "action_type": action_type,
                "status_from": status_from,
                "status_to": status_to,
                "reason": reason,
            }
            db[_action_log_key(normalized, seq)] = entry
    log_print(
        "portfolio_shelve: action_log 追記",
        normalized,
        action_type,
        f"seq={seq}",
    )
    return entry


def list_action_logs(
    code_s: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """アクションログを取得する。

    - code_s 指定時はその銘柄のみ、None で全銘柄
    - 結果は (code_s, seq) 昇順
    """
    if code_s is not None:
        validate_code_s(code_s)
        normalized = normalize_code_s(code_s)
        prefix = _action_log_prefix_for(normalized)
    else:
        prefix = KEY_ACTION_LOG_PREFIX
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(prefix):
                continue
            if not isinstance(value, dict):
                continue
            results.append(value)
    results.sort(
        key=lambda r: (r.get("code_s", ""), r.get("seq", 0)),
    )
    return results


# ===========================================
# 高レベル操作
# ===========================================

def add_to_watch(
    code_s: str,
    *,
    memo: Optional[Dict[str, str]] = None,
    reason: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """銘柄を 3監 として登録、または除外済みレコードをユニバース復活させる。

    銘柄名は持たない (表示時に stocks_shelve / research_shelve から都度取得)。

    挙動:
    - 既存レコードなし → 新規追加 (3監)。「初回登録」ログを reason 引数で記録
    - 既存レコードあり & excluded=True → 復活 (excluded=False に戻す)。
      memo / status は既存値を保持。「ユニバース除外」ログを reason="復活" で記録
      (復活時は reason 引数は無視される)
    - 既存レコードあり & excluded=False → ValueError (重複登録防止)

    Returns: 追加または復活したレコード
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            existing = db.get(key)
            if existing is not None and isinstance(existing, dict):
                if existing.get("excluded", False):
                    existing["excluded"] = False
                    existing["updated_at"] = now_iso()
                    db[key] = existing
                    revived_record = existing
                    revived = True
                else:
                    raise ValueError(
                        f"portfolio_shelve: {normalized} は既に登録済みです"
                    )
            else:
                record = create_record(normalized, status="3監", memo=memo)
                db[key] = record
                revived_record = record
                revived = False
        if revived:
            # 復活時は明示的に reason="復活" を記録 (除外ログとの判別用、reason 引数は無視)
            append_action_log(
                normalized,
                "ユニバース除外",
                reason="復活",
                db_path=db_path,
            )
            log_print("portfolio_shelve: ユニバース復活", normalized)
        else:
            append_action_log(
                normalized,
                "初回登録",
                status_from=None,
                status_to="3監",
                reason=reason,
                db_path=db_path,
            )
            log_print("portfolio_shelve: 3監 追加", normalized)
    return revived_record


def upsert_record(
    record: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> None:
    """レコードを追加または上書きする (移行スクリプト用)。

    add_to_watch / transition_status と異なりアクションログは追記しない。
    呼び出し側で必要なら append_action_log を別途呼ぶこと。
    """
    if not isinstance(record, dict):
        raise TypeError(f"record must be dict, got {type(record).__name__}")
    if "code_s" not in record:
        raise ValueError("record に code_s が必須です")
    validate_code_s(record["code_s"])
    if "status" in record:
        validate_status(record["status"])
    normalized = normalize_code_s(record["code_s"])
    stored = dict(record)
    stored["code_s"] = normalized

    unknown = set(stored.keys()) - RECORD_FIELDS - LEGACY_RECORD_FIELDS
    if unknown:
        log_warning(
            "portfolio_shelve: 未知のレコードフィールドを保存します:",
            sorted(unknown),
        )

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            existed = _record_key(normalized) in db
            db[_record_key(normalized)] = stored
    if existed:
        log_print("portfolio_shelve: レコード更新", normalized)
    else:
        log_print("portfolio_shelve: レコード追加", normalized)


def transition_status(
    code_s: str,
    new_status: str,
    *,
    reason: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """既存レコードのステータスを変更する。

    - 1保 -> 2準 は内部的に「売却」として action_type=売却 で記録
    - それ以外の遷移は action_type=ステータス変更
    - 遷移バリデーション (ALLOWED_TRANSITIONS) を満たさなければ ValueError
    - レコードが存在しない場合は KeyError

    Returns: 更新後のレコード
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    validate_status(new_status)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                raise KeyError(
                    f"portfolio_shelve: {normalized} はレコード未登録です"
                )
            record = db[key]
            old_status = record.get("status")
            if old_status == new_status:
                # 同一ステータスへの遷移は no-op (バリデーション前に判定)
                log_print(
                    "portfolio_shelve: 同一ステータスのため遷移スキップ",
                    normalized,
                    new_status,
                )
                return record
            validate_transition(old_status, new_status)
            record["status"] = new_status
            record["updated_at"] = now_iso()
            db[key] = record
        # アクションログ種別: 1保→2準 は売却、それ以外はステータス変更
        action_type = "売却" if old_status == "1保" and new_status == "2準" else "ステータス変更"
        append_action_log(
            normalized,
            action_type,
            status_from=old_status,
            status_to=new_status,
            reason=reason,
            db_path=db_path,
        )
    log_print(
        "portfolio_shelve: ステータス変更",
        normalized,
        f"{old_status} -> {new_status}",
    )
    return record


def delete_record(
    code_s: str,
    *,
    reason: str = "",
    db_path: Optional[str] = None,
) -> bool:
    """レコードを物理削除する (3監 のみ可能)。

    - 1保/2準 を削除しようとすると ValueError
    - レコードが存在しない場合は False を返す
    - 削除に成功した場合は アクションログ「削除」を 1 件記録 (ログは残る)

    Returns: 削除に成功すれば True、未存在なら False
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                return False
            record = db[key]
            current_status = record.get("status")
            if current_status != "3監":
                raise ValueError(
                    f"portfolio_shelve: {normalized} は status={current_status!r} のため "
                    "削除できません (3監 のみ削除可能、先に 3監 へ遷移してください)"
                )
            del db[key]
        append_action_log(
            normalized,
            "削除",
            status_from="3監",
            status_to=None,
            reason=reason,
            db_path=db_path,
        )
    log_print("portfolio_shelve: レコード削除", normalized)
    return True


def exclude_from_universe(
    code_s: str,
    *,
    reason: str = "",
    db_path: Optional[str] = None,
) -> bool:
    """3監レコードをユニバースから除外する (物理削除はしない)。

    - 1保/2準 を除外しようとすると ValueError
    - レコードが存在しない場合は False を返す
    - 既に除外済みなら no-op で False を返す
    - 成功時はアクションログ「ユニバース除外」を 1 件記録

    Returns: 除外を新規に行った場合 True、未存在/既除外なら False
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                return False
            record = db[key]
            if record.get("excluded", False):
                return False
            current_status = record.get("status")
            if current_status != "3監":
                raise ValueError(
                    f"portfolio_shelve: {normalized} は status={current_status!r} のため "
                    "ユニバース除外できません (3監 のみ除外可能、先に 3監 へ遷移してください)"
                )
            record["excluded"] = True
            record["updated_at"] = now_iso()
            db[key] = record
        append_action_log(
            normalized,
            "ユニバース除外",
            reason=reason,
            db_path=db_path,
        )
    log_print("portfolio_shelve: ユニバース除外", normalized)
    return True


def update_memo(
    code_s: str,
    fields: Dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """既存レコードの memo フィールドを部分更新する。

    部分更新セマンティクス:
    - fields に含まれるキーのみ更新する。fields に存在しないキーは現行値を保持
    - 値 "" を明示的に渡した場合は「メモ削除」として "" に上書き
    - 値 None は "" に正規化 (空文字送信と同じ扱い)

    バリデーション:
    - fields のキーは MEMO_FIELDS のサブセットでなければ ValueError
    - 値は str (または None) のみ許容、それ以外は TypeError
    - レコード未登録なら KeyError
    - 排他制御は transition_status と同じ _flock パターン

    差分判定:
    - fields の各 key について現行値と完全一致すれば no-op
      (action_log 追記なし、updated_at 据え置き)
    - 1 つでも変更があれば action_log に "メモ更新" を 1 件追加
      (差分内容は記録しない、reason は空文字)

    Returns: 更新後のレコード dict (no-op 時も現行 record を返す)
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(fields, dict):
        raise TypeError(f"fields must be dict, got {type(fields).__name__}")

    unknown_keys = set(fields.keys()) - MEMO_FIELDS
    if unknown_keys:
        raise ValueError(
            f"portfolio_shelve: 未知の memo フィールド {sorted(unknown_keys)} "
            f"(許容値: {sorted(MEMO_FIELDS)})"
        )

    normalized_fields: Dict[str, str] = {}
    for k, v in fields.items():
        if v is None:
            normalized_fields[k] = ""
        elif isinstance(v, str):
            normalized_fields[k] = v
        else:
            raise TypeError(
                f"portfolio_shelve: memo[{k!r}] must be str or None, "
                f"got {type(v).__name__}"
            )

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                raise KeyError(
                    f"portfolio_shelve: {normalized} はレコード未登録です"
                )
            record = db[key]
            current_memo = record.get("memo", {}) or {}
            changed = any(
                current_memo.get(k, "") != v
                for k, v in normalized_fields.items()
            )
            if not changed:
                log_print(
                    "portfolio_shelve: メモ更新スキップ (差分なし)",
                    normalized,
                )
                return record
            record["memo"] = {**current_memo, **normalized_fields}
            record["updated_at"] = now_iso()
            db[key] = record
        append_action_log(
            normalized,
            "メモ更新",
            db_path=db_path,
        )
    log_print(
        "portfolio_shelve: メモ更新",
        normalized,
        f"keys={sorted(normalized_fields.keys())}",
    )
    return record


# ===========================================
# my_watch_list.txt 一方向同期
# ===========================================

def _resolve_stock_names(code_list: List[str]) -> Dict[str, str]:
    """code_s ごとの銘柄名を解決する (stocks_shelve → research_shelve → "" の優先順)。

    portfolio_shelve は銘柄名を持たないため、表示や txt 同期で必要なら都度こちらを呼ぶ。
    両 shelve とも未登録なら空文字。
    """
    from db_shelve import STOCKS_SHELVE, RESEARCH_SHELVE  # 遅延 import (循環回避)

    result: Dict[str, str] = {c: "" for c in code_list}
    if not code_list:
        return result

    try:
        with ShelveDB(STOCKS_SHELVE) as db:
            for c in code_list:
                rec = db.get(c)
                if rec and rec.get("stock_name"):
                    result[c] = rec["stock_name"]
    except Exception:
        # stocks_shelve が無い等は無視 (research_shelve fallback に進む)
        pass

    missing = [c for c, n in result.items() if not n]
    if missing:
        try:
            with ShelveDB(RESEARCH_SHELVE) as db:
                for c in missing:
                    rec = db.get(c)
                    if rec and rec.get("stock_name"):
                        result[c] = rec["stock_name"]
        except Exception:
            pass

    return result


def sync_to_my_watch_list_txt(
    *,
    txt_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """portfolio_shelve の現在状態を my_watch_list.txt に書き出す。

    一方向同期 (shelve → txt)。txt 廃止 issue (将来) で sync 自体を停止する想定。
    旧コードと既存運用との互換のため、Phase 3 完了後も同期は有効のまま残す。

    excluded=True のレコードは出力しない (`list_records` のデフォルトで除外される)。

    銘柄名は portfolio_shelve には保存されていないため stocks_shelve / research_shelve から
    都度引く (どちらにも無ければ code のみ書き出す)。

    フォーマット (現行 my_watch_list.txt 互換):
    - 1保 → "H<code_s><stock_name>" (H 接頭辞)
    - 2準 → "<code_s><stock_name>" (txt は 2 値しかないので 3監 扱い)
    - 3監 → "<code_s><stock_name>"

    出力順:
    - 1保 を先頭にまとめる (H 付きが先頭にある現行 txt の見た目を維持)
    - 各グループ内は code_s 昇順
    - 1保 と 3監/2準 の間に空行を 1 行入れる (現行 txt の見た目互換)

    Args:
        txt_path: 出力 txt パス。None なら ${DATA_DIR}/my_watch_list.txt
        db_path: portfolio_shelve のパス上書き (テスト用)

    Returns: 書き出した txt のパス
    """
    if txt_path is None:
        txt_path = os.path.join(DATA_DIR, "my_watch_list.txt")
    records = list_records(db_path=db_path)

    holds = sorted(
        (r for r in records if r.get("status") == "1保"),
        key=lambda r: r.get("code_s", ""),
    )
    others = sorted(
        (r for r in records if r.get("status") in ("2準", "3監")),
        key=lambda r: r.get("code_s", ""),
    )

    name_map = _resolve_stock_names([r.get("code_s", "") for r in holds + others])

    lines: List[str] = []
    for r in holds:
        code = r.get("code_s", "")
        lines.append(f"H{code}{name_map.get(code, '')}")
    if holds and others:
        lines.append("")
    for r in others:
        code = r.get("code_s", "")
        lines.append(f"{code}{name_map.get(code, '')}")

    txt_dir = os.path.dirname(txt_path)
    if txt_dir and not os.path.exists(txt_dir):
        os.makedirs(txt_dir, exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")

    log_print(
        f"portfolio_shelve: my_watch_list.txt 同期完了 holds={len(holds)} "
        f"others={len(others)} path={txt_path}"
    )
    return txt_path
