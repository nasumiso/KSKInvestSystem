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
    {"初回登録", "ステータス変更", "売却", "削除", "ユニバース除外", "株数変更"}
)

# キー名前空間プレフィックス
KEY_RECORD_PREFIX = "record:"
KEY_ACTION_LOG_PREFIX = "action_log:"
KEY_SEQ_PREFIX = "_seq:"
KEY_THEME_PREFIX = "theme:"
KEY_TRADE_IDEA_PREFIX = "trade_idea:"

# テーママスター (issue #282)
THEME_FIELDS = frozenset({"name", "description", "created_at"})
THEME_NAME_MAX_LEN = 30  # UI バッジを崩さない上限
# URL に含めると曖昧になる文字 + HTML/JS リテラルを破壊する文字を name に許可しない
# (`/portfolio/themes/<name>/...` ルートの破綻防止 + テンプレ展開時の注入予防)
_THEME_NAME_FORBIDDEN_RE = re.compile(r"[\/\?\#\&\%\+\<\>\"\'\\\x00-\x1F]")

# PF 全体で 1 つだけ保持するメタキー (どこかの銘柄で qty が変化したら更新)
KEY_QTY_GLOBAL_UPDATED_AT = "_meta:qty_global_updated_at"

# レコードの既知フィールド (銘柄名は持たない: 表示時に stocks_shelve / research_shelve から都度取得する)
RECORD_FIELDS = frozenset(
    {
        "code_s",
        "status",
        "registered_at",
        "updated_at",
        "memo",
        "excluded",
        "qty",
    }
)

# 旧スキーマ由来で許容するが扱わないフィールド (新スキーマでは未使用、過去データ互換のため warning しない)
LEGACY_RECORD_FIELDS = frozenset({"stock_name"})

MEMO_FIELDS = frozenset(
    {
        "gyoutai_theme",          # 旧: 業態・テーマ (str/改行区切り、移行期間中のみ残す。issue #187)
        "gyoutai_themes",         # 新: 業態・テーマ (list[str]、UI では最大2件)
        "watch_in_reason",        # ウォッチ・IN理由
        "trade_idea",             # 売買戦略 (旧: 投資売買アイデア)
        "inago_origin",           # イナゴ元・きっかけ
        "takaichi_sensitivity",   # 売買メモ (旧: 高市感応度)
        "last_research_update",   # 銘柄調査スプシでの更新日 (M/D 形式、年なし)
        "stage",                  # ステージ評価 (例: "1S", "2S(3T)", "3S")
        "jukyu_chart",            # 需給チャートメモ (例: "月足低位ブレイク CWH")
    }
)

# list[str] として扱う memo フィールド (str 系とバリデーションを分ける)
MEMO_LIST_FIELDS = frozenset({"gyoutai_themes"})

# gyoutai_themes の UI スロット上限 (issue #187)
GYOUTAI_THEMES_MAX_SLOTS = 2

# trade_idea (売買戦略) の定型リスト (issue #327: 自由記述から単一選択式へ移行)。
# 空文字 = 未分類。リスト外の既存値 (旧自由記述) は update_memo で保持を許可する (救済)。
# CSV 移行 (create_record 経由) はこのチェックを通らないため自由記述のまま格納される。
# 時間軸は戦略名に内包する (中期テーマ等)。各戦略の定義・決算またぎ可否は編集画面 issue で管理予定。
# 各戦略の説明 (UI の tooltip 用)。値は戦略の時間軸・狙いを 1 行で表す。
TRADE_IDEA_DESCRIPTIONS = {
    "GARP": "中長期: 安定成長株を押し目・一時的売り込まれ局面で拾う",
    "ピーターリンチ": "中長期: 身近な実感で好印象の銘柄を持つ",
    "中期テーマ": "中期: 相場の物色テーマの波に乗る",
    "中期モメンタム": "中期: 新高値ブレイク順張り 2〜3ヶ月保有 (メイン戦略)",
    "短期イベント": "短期: 決算・材料・政策などカタリスト狙い",
    "底値リバ": "短期: 下げすぎリバ。サイズ小・機械的利確損切り",
    "夢枠": "長期: 2〜3年の夢に乗る。現物放置",
    "大型高配当": "恒常: PF安定・信用代用",
}
TRADE_IDEA_OPTIONS = tuple(TRADE_IDEA_DESCRIPTIONS.keys())

# 戦略マスター (issue #335: 定数 shelve 移行・編集画面)
VALID_TIME_HORIZONS = ("短期", "中期", "中長期", "長期", "恒常", "")

_TRADE_IDEA_SEED = [
    {"name": "GARP",           "description": "安定成長株を押し目・一時的売り込まれ局面で拾う。業績成長シナリオが崩れたら売る",    "time_horizon": "中長期", "over_earnings": True},
    {"name": "ピーターリンチ", "description": "身近な実感で好印象の銘柄を持つ。身近な実感・好印象が消えたら売る",                  "time_horizon": "中長期", "over_earnings": False},
    {"name": "中期テーマ",     "description": "相場の物色テーマの波に乗る。物色が他テーマへ移ったら売る",                          "time_horizon": "中期",   "over_earnings": False},
    {"name": "中期モメンタム", "description": "新高値ブレイク順張り 2〜3ヶ月保有。トレンド（チャート）が崩れたら売る",             "time_horizon": "中期",   "over_earnings": False},
    {"name": "短期イベント",   "description": "決算・材料・政策などカタリスト狙い。イベント通過で手仕舞い",                        "time_horizon": "短期",   "over_earnings": True},
    {"name": "底値リバ",       "description": "下げすぎリバ取り。MAタッチ/10%で機械的利確。サイズ小・損切り事前設定",             "time_horizon": "短期",   "over_earnings": False},
    {"name": "夢枠",           "description": "2〜3年の夢に乗る。現物放置。売らない",                                              "time_horizon": "長期",   "over_earnings": True},
    {"name": "大型高配当",     "description": "PF安定・信用代用を兼ねる。恒常保有。売らない",                                      "time_horizon": "恒常",   "over_earnings": True},
]

# issue #344: stage / jukyu_chart を自由記述から選択式へ寄せるための定型候補。
# DB スキーマは変えず、UI 側で分解入力した値を route 層で 1 本の文字列に畳み込む。
STAGE_OPTIONS = (
    "1S", "2S", "3S", "4S", "1Sor3S",
    "1S~2S", "2S~3S", "3S~4S", "4S~1S",
)
STAGE_T_OPTIONS = ("1", "2", "3", "4", "5")
CHART_STYLE_OPTIONS = (
    "週足低位", "週足CWH", "週足VCP", "週足高値",
    "月足低位", "月足CWH", "月足VCP", "月足高値",
)
CHART_STATE_OPTIONS = ("形成", "ブレイク", "ブレイク済み", "ブレイク失敗", "再ブレイク")

ACTION_LOG_FIELDS = frozenset(
    {
        "code_s",
        "seq",
        "timestamp",
        "action_type",
        "status_from",
        "status_to",
        "reason",
        "review_memo",
        "qty",
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


def _validate_qty(qty: Any) -> None:
    """保有株数を検証する (issue #269)。

    - 整数型 (bool は除外: True/False が 1/0 として通るのを防ぐ)
    - 0 以上
    """
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise TypeError(f"qty must be int, got {type(qty).__name__}")
    if qty < 0:
        raise ValueError(f"qty must be >= 0, got {qty}")


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


_ACTION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_action_date_to_iso(action_date: str) -> str:
    """YYYY-MM-DD を JST 12:00 の ISO 8601 文字列に変換する。

    issue #220: UI 側 <input type="date"> から受け取る日付を action_log の
    timestamp として保存する。00:00 だと JST 表示時に前日扱いになる縁起問題を
    避けるため固定 12:00 を当てる。

    未来日 (`datetime.now(JST).date()` 基準) は ValueError。
    `ks_util.get_price_day()` の業務日は使わない (17:00 前に前日を返すため、
    実カレンダーの今日入力を誤って弾いてしまう)。
    """
    if not isinstance(action_date, str) or not _ACTION_DATE_RE.match(action_date):
        raise ValueError(
            f"action_date は YYYY-MM-DD 形式で指定してください: {action_date!r}"
        )
    try:
        parsed = datetime.strptime(action_date, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"action_date のパースに失敗: {action_date!r} ({e})") from e
    today = datetime.now(JST).date()
    if parsed > today:
        raise ValueError(
            f"action_date に未来日は指定できません: {action_date} (今日={today.isoformat()})"
        )
    return datetime(parsed.year, parsed.month, parsed.day, 12, 0, 0, tzinfo=JST).isoformat()


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
    gyoutai_themes: Optional[List[str]] = None,
    watch_in_reason: str = "",
    trade_idea: str = "",
    inago_origin: str = "",
    takaichi_sensitivity: str = "",
    last_research_update: str = "",
    stage: str = "",
    jukyu_chart: str = "",
) -> Dict[str, Any]:
    """手動メモ dict を生成する。"""
    return {
        "gyoutai_theme": gyoutai_theme,
        "gyoutai_themes": list(gyoutai_themes) if gyoutai_themes else [],
        "watch_in_reason": watch_in_reason,
        "trade_idea": trade_idea,
        "inago_origin": inago_origin,
        "takaichi_sensitivity": takaichi_sensitivity,
        "last_research_update": last_research_update,
        "stage": stage,
        "jukyu_chart": jukyu_chart,
    }


def _normalize_loaded_memo(memo: Any) -> Dict[str, Any]:
    """shelve から読み込んだ memo に新フィールドのデフォルトを補完する。

    旧データに `gyoutai_themes` がない場合 `[]` を埋めて、UI/集計ヘルパーが
    KeyError を踏まないようにする (issue #187 後方互換)。
    """
    if not isinstance(memo, dict):
        return memo
    result = dict(memo)
    if "gyoutai_themes" not in result:
        result["gyoutai_themes"] = []
    return result


def _normalize_loaded_record(record: Any) -> Any:
    """shelve から読み込んだ record の memo / qty を正規化する (issue #187, #269)。"""
    if not isinstance(record, dict):
        return record
    result = dict(record)
    if "memo" in result:
        result["memo"] = _normalize_loaded_memo(result.get("memo"))
    # 旧データに qty が無ければ 0 を補完 (issue #269)
    result.setdefault("qty", 0)
    return result


def create_record(
    code_s: str,
    *,
    status: str = "3監",
    memo: Optional[Dict[str, str]] = None,
    registered_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    qty: int = 0,
) -> Dict[str, Any]:
    """portfolio レコード dict を生成する。

    銘柄名はこのレコードには保存しない (要件 §4: 指標データは保存せず stocks_shelve から
    都度参照する原則を銘柄名にも適用)。

    - code_s は normalize_code_s で大文字化される
    - status はデフォルト "3監" (新規追加用)
    - memo が None なら空メモで埋める
    - registered_at / updated_at が None なら現在時刻を埋める
    - qty: 保有株数 (issue #269、1保 のときのみ意味を持つ、デフォルト 0)
    """
    validate_code_s(code_s)
    normalized_code = normalize_code_s(code_s)
    validate_status(status)
    if memo is None:
        memo = create_memo()
    elif not isinstance(memo, dict):
        raise TypeError(f"memo must be dict, got {type(memo).__name__}")
    _validate_qty(qty)
    timestamp = registered_at or now_iso()
    return {
        "code_s": normalized_code,
        "status": status,
        "registered_at": timestamp,
        "updated_at": updated_at or timestamp,
        "memo": dict(memo),
        "excluded": False,
        "qty": int(qty),
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
        record = db.get(_record_key(normalized))
    return _normalize_loaded_record(record) if record is not None else None


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
            results.append(_normalize_loaded_record(value))
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
    review_memo: str = "",
    qty: Optional[int] = None,
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
    if not isinstance(review_memo, str):
        raise TypeError(f"review_memo must be str, got {type(review_memo).__name__}")
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
                "review_memo": review_memo,
                "qty": qty,
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
            value.setdefault("review_memo", "")
            value.setdefault("qty", None)
            results.append(value)
    results.sort(
        key=lambda r: (r.get("code_s", ""), r.get("seq", 0)),
    )
    return results


def update_action_log_review_memo(
    code_s: str,
    seq: int,
    review_memo: str,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """アクションログエントリの review_memo フィールドを上書きする (issue #351)。

    - 対象エントリが存在しない場合は KeyError
    - review_memo は str のみ許容
    Returns: 更新後のログエントリ
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    if not isinstance(review_memo, str):
        raise TypeError(f"review_memo must be str, got {type(review_memo).__name__}")
    key = _action_log_key(normalized, seq)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            entry = db.get(key)
            if entry is None:
                raise KeyError(f"action_log not found: code_s={code_s!r}, seq={seq}")
            entry["review_memo"] = review_memo
            db[key] = entry
    return entry


# ===========================================
# 高レベル操作
# ===========================================

def add_to_watch(
    code_s: str,
    *,
    memo: Optional[Dict[str, str]] = None,
    reason: str = "",
    action_date: Optional[str] = None,
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
    - action_date (YYYY-MM-DD) を指定すると、action_log の timestamp を
      その日の JST 12:00 に固定する (issue #220)。未指定なら現在時刻

    Returns: 追加または復活したレコード
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    action_ts = _parse_action_date_to_iso(action_date) if action_date else None
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
                timestamp=action_ts,
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
                timestamp=action_ts,
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


def update_qty(
    code_s: str,
    qty: int,
    *,
    reason: str = "",
    action_date: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """既存レコードの保有株数を更新する (issue #269)。

    - 差分なしは no-op で early return (戻り値も現レコード)
    - レコード未登録は KeyError
    - 不正値 (非整数 / 負数) は TypeError / ValueError
    - action_date (YYYY-MM-DD) を指定すると、action_log の timestamp を
      JST 12:00 の ISO 8601 文字列に変換して記録する

    Returns: 更新後のレコード。差分なしの場合は現レコードをそのまま返す。
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    _validate_qty(qty)
    qty_int = int(qty)
    action_ts = _parse_action_date_to_iso(action_date) if action_date else None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _record_key(normalized)
            if key not in db:
                raise KeyError(
                    f"portfolio_shelve: {normalized} はレコード未登録です"
                )
            record = db[key]
            current_qty = record.get("qty", 0)
            if current_qty == qty_int:
                return _normalize_loaded_record(record)
            record["qty"] = qty_int
            db[key] = record
            db[KEY_QTY_GLOBAL_UPDATED_AT] = now_iso()
    log_print("portfolio_shelve: 株数更新", normalized, f"{current_qty} -> {qty_int}")
    append_action_log(
        code_s,
        "株数変更",
        reason=f"{current_qty} → {qty_int}" + (f" ({reason})" if reason else ""),
        timestamp=action_ts,
        db_path=db_path,
    )
    return _normalize_loaded_record(record)


def get_qty_global_updated_at(
    *,
    db_path: Optional[str] = None,
) -> Optional[str]:
    """PF 全体で最後に qty が変化した ISO 8601 タイムスタンプを返す。

    どの銘柄でも qty が変化していない (初期状態) なら None。
    保有銘柄リストの更新タイミングを「PF 全体で 1 つの時刻」として可視化するためのメタ情報。
    """
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        return db.get(KEY_QTY_GLOBAL_UPDATED_AT)


def transition_status(
    code_s: str,
    new_status: str,
    *,
    reason: str = "",
    action_date: Optional[str] = None,
    qty: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """既存レコードのステータスを変更する。

    - 1保 -> 2準 は内部的に「売却」として action_type=売却 で記録
    - それ以外の遷移は action_type=ステータス変更
    - 遷移バリデーション (ALLOWED_TRANSITIONS) を満たさなければ ValueError
    - レコードが存在しない場合は KeyError
    - action_date (YYYY-MM-DD) を指定すると、action_log の timestamp を
      その日の JST 12:00 に固定する (issue #220)。未指定なら現在時刻
    - qty: 1保遷移時のIN株数をログに記録する (issue #357)。売却時は record["qty"] を使用

    Returns: 更新後のレコード
    """
    validate_code_s(code_s)
    normalized = normalize_code_s(code_s)
    validate_status(new_status)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    action_ts = _parse_action_date_to_iso(action_date) if action_date else None

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
        # 1保遷移は引数 qty を優先（update_qty より先に呼ばれるため record["qty"] は旧値）
        # 売却時は record["qty"]（保有株数）をログに記録 (issue #357)
        if new_status == "1保":
            log_qty = qty
        elif new_status == "2準":
            log_qty = record.get("qty")
        else:
            log_qty = None
        append_action_log(
            normalized,
            action_type,
            status_from=old_status,
            status_to=new_status,
            reason=reason,
            qty=log_qty,
            timestamp=action_ts,
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


EXCLUDABLE_STATUSES = frozenset({"2準", "3監"})


def exclude_from_universe(
    code_s: str,
    *,
    reason: str = "",
    db_path: Optional[str] = None,
) -> bool:
    """2準/3監 レコードをユニバースから除外する (物理削除はしない)。

    - 1保 を除外しようとすると ValueError (保有中銘柄の誤除外を防ぐ)
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
            if current_status not in EXCLUDABLE_STATUSES:
                raise ValueError(
                    f"portfolio_shelve: {normalized} は status={current_status!r} のため "
                    "ユニバース除外できません (2準/3監 のみ除外可能)"
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


# ===========================================
# テーママスター (issue #282)
# ===========================================

def _theme_key(name: str) -> str:
    return f"{KEY_THEME_PREFIX}{name}"


def validate_theme_name(name: Any) -> str:
    """テーマ name を検証して正規化済み name を返す。

    - str でない → TypeError
    - strip() 後に空 → ValueError
    - 長さ > THEME_NAME_MAX_LEN → ValueError
    - URL 禁止文字 (/ ? # & % + 制御文字) を含む → ValueError

    正規化: strip() のみ (大文字小文字は維持、内部空白も維持)
    """
    if not isinstance(name, str):
        raise TypeError(f"theme name must be str, got {type(name).__name__}")
    normalized = name.strip()
    if not normalized:
        raise ValueError("theme name は空にできません")
    if len(normalized) > THEME_NAME_MAX_LEN:
        raise ValueError(
            f"theme name は {THEME_NAME_MAX_LEN} 文字以内: {name!r} ({len(normalized)} 文字)"
        )
    m = _THEME_NAME_FORBIDDEN_RE.search(normalized)
    if m:
        raise ValueError(
            f"theme name に使用できない文字が含まれています: {name!r} (禁止: / ? # & % + 制御文字)"
        )
    return normalized


def _validate_theme_description(description: Any) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise TypeError(
            f"theme description must be str or None, got {type(description).__name__}"
        )
    return description


def list_themes(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """テーママスターを name 昇順で取得する。"""
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_THEME_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            results.append(dict(value))
    results.sort(key=lambda r: r.get("name", ""))
    return results


def get_theme(name: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """1件取得 (存在しなければ None)。"""
    normalized = validate_theme_name(name)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        value = db.get(_theme_key(normalized))
    return dict(value) if isinstance(value, dict) else None


def create_theme(
    name: str,
    description: str = "",
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """テーマを新規作成する。重複は ValueError。"""
    normalized = validate_theme_name(name)
    desc = _validate_theme_description(description)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _theme_key(normalized)
            if key in db:
                raise ValueError(f"theme {normalized!r} は既に存在します")
            record = {
                "name": normalized,
                "description": desc,
                "created_at": now_iso(),
            }
            db[key] = record
    log_print("portfolio_shelve: theme 作成", normalized)
    return dict(record)


def update_theme(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """テーマを更新する (リネーム / 説明文編集)。

    - new_name が現行と異なれば旧キー削除 + 新キー作成 + 全 record の memo[gyoutai_themes] 書き換え
    - description が None でなければその値で上書き
    - 同 _flock + 同 ShelveDB セッション内で完結 (途中失敗で不整合を残さない)

    戻り値: 更新後の theme レコード
    """
    normalized = validate_theme_name(name)
    new_normalized = validate_theme_name(new_name) if new_name is not None else None
    desc_value = _validate_theme_description(description) if description is not None else None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            old_key = _theme_key(normalized)
            if old_key not in db:
                raise KeyError(f"theme {normalized!r} は存在しません")
            current = dict(db[old_key])

            renaming = new_normalized is not None and new_normalized != normalized
            if renaming:
                new_key = _theme_key(new_normalized)
                if new_key in db:
                    raise ValueError(f"theme {new_normalized!r} は既に存在します")
                current["name"] = new_normalized
            if desc_value is not None:
                current["description"] = desc_value

            if renaming:
                # 旧キー削除 + 新キー作成 + 銘柄側書き換え
                del db[old_key]
                db[new_key] = current
                affected_codes = _rewrite_theme_in_records(
                    db, normalized, new_normalized
                )
            else:
                db[old_key] = current
                affected_codes = []

    if renaming:
        log_print(
            "portfolio_shelve: theme リネーム",
            f"{normalized} -> {new_normalized}",
            f"affected={len(affected_codes)}",
        )
    else:
        log_print("portfolio_shelve: theme 更新", normalized)
    return dict(current)


def delete_theme(name: str, *, db_path: Optional[str] = None) -> int:
    """テーマを削除する。全 record から該当 name を除去。

    戻り値: 影響を受けた銘柄数
    """
    normalized = validate_theme_name(name)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _theme_key(normalized)
            if key not in db:
                raise KeyError(f"theme {normalized!r} は存在しません")
            del db[key]
            affected_codes = _rewrite_theme_in_records(db, normalized, None)
    log_print(
        "portfolio_shelve: theme 削除",
        normalized,
        f"affected={len(affected_codes)}",
    )
    return len(affected_codes)


def count_theme_usage(*, db_path: Optional[str] = None) -> Dict[str, int]:
    """name -> 使用銘柄数 を返す (編集画面表示用)。"""
    path = _resolve_db_path(db_path)
    counts: Dict[str, int] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_RECORD_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            memo = value.get("memo") or {}
            for theme in (memo.get("gyoutai_themes") or []):
                if isinstance(theme, str) and theme:
                    counts[theme] = counts.get(theme, 0) + 1
    return counts


def _rewrite_theme_in_records(
    db: ShelveDB,
    old_name: str,
    new_name: Optional[str],
) -> List[str]:
    """全 record:* を走査し、memo[gyoutai_themes] 内の old_name を new_name に置換する。

    - new_name=None なら除去 (削除)
    - new_name 指定で同一スロットに新 name が既に存在する場合は重複除去
    - 変更があった record は updated_at を now_iso() に更新
    - 戻り値: 変更があった code_s のリスト (action_log 追記用)

    呼び出し側で _flock + ShelveDB セッションを保持していること前提。
    """
    affected: List[str] = []
    record_keys = [k for k in db.keys() if k.startswith(KEY_RECORD_PREFIX)]
    for key in record_keys:
        record = db[key]
        if not isinstance(record, dict):
            continue
        memo = record.get("memo") or {}
        themes = memo.get("gyoutai_themes") or []
        if not isinstance(themes, list) or old_name not in themes:
            continue
        new_themes: List[str] = []
        for t in themes:
            if t == old_name:
                if new_name is None:
                    continue
                if new_name in new_themes:
                    continue  # リネーム後に重複する場合は除去
                new_themes.append(new_name)
            else:
                if t in new_themes:
                    continue
                new_themes.append(t)
        if new_themes == list(themes):
            continue
        new_memo = dict(memo)
        new_memo["gyoutai_themes"] = new_themes
        new_record = dict(record)
        new_record["memo"] = new_memo
        new_record["updated_at"] = now_iso()
        db[key] = new_record
        affected.append(record.get("code_s") or key[len(KEY_RECORD_PREFIX):])
    return affected


# ===========================================
# 戦略マスター CRUD (issue #335)
# ===========================================

def _trade_idea_key(name: str) -> str:
    return f"{KEY_TRADE_IDEA_PREFIX}{name}"


def validate_strategy_name(name: Any) -> str:
    """戦略 name を検証して正規化済み name を返す (validate_theme_name と同じルール)。"""
    try:
        return validate_theme_name(name)
    except TypeError as e:
        raise TypeError(str(e).replace("theme name", "strategy name")) from e
    except ValueError as e:
        raise ValueError(str(e).replace("theme name", "strategy name")) from e


def list_trade_ideas(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """戦略マスターを name 昇順で取得する。"""
    path = _resolve_db_path(db_path)
    results: List[Dict[str, Any]] = []
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_TRADE_IDEA_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            results.append(dict(value))
    results.sort(key=lambda r: r.get("name", ""))
    return results


def get_trade_idea(name: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """1件取得 (存在しなければ None)。"""
    normalized = validate_strategy_name(name)
    path = _resolve_db_path(db_path)
    with ShelveDB(path) as db:
        value = db.get(_trade_idea_key(normalized))
    return dict(value) if isinstance(value, dict) else None


def _validate_trade_idea_fields(
    time_horizon: Any,
    over_earnings: Any,
) -> tuple:
    """time_horizon / over_earnings の型・値チェック。正規化後の (time_horizon, over_earnings) を返す。"""
    if time_horizon is None:
        time_horizon = ""
    if not isinstance(time_horizon, str):
        raise TypeError(f"time_horizon must be str or None, got {type(time_horizon).__name__}")
    if time_horizon not in VALID_TIME_HORIZONS:
        raise ValueError(f"time_horizon {time_horizon!r} は無効です (許容値: {list(VALID_TIME_HORIZONS)})")
    if over_earnings is None:
        over_earnings = False
    if not isinstance(over_earnings, bool):
        raise TypeError(f"over_earnings must be bool or None, got {type(over_earnings).__name__}")
    return time_horizon, over_earnings


def create_trade_idea(
    name: str,
    description: str = "",
    time_horizon: str = "",
    over_earnings: bool = False,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """戦略を新規作成する。重複は ValueError。"""
    normalized = validate_strategy_name(name)
    desc = _validate_theme_description(description)
    th, oe = _validate_trade_idea_fields(time_horizon, over_earnings)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _trade_idea_key(normalized)
            if key in db:
                raise ValueError(f"strategy {normalized!r} は既に存在します")
            record = {
                "name": normalized,
                "description": desc,
                "time_horizon": th,
                "over_earnings": oe,
                "created_at": now_iso(),
            }
            db[key] = record
    log_print("portfolio_shelve: strategy 作成", normalized)
    return dict(record)


def update_trade_idea(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    time_horizon: Optional[str] = None,
    over_earnings: Optional[bool] = None,
    *,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """戦略を更新する (リネーム / 説明文 / 時間軸 / 決算またぎ編集)。

    - new_name が現行と異なれば旧キー削除 + 新キー作成 + 全 record の memo[trade_idea] 書き換え
    - 同 _flock + 同 ShelveDB セッション内で完結

    戻り値: 更新後の strategy レコード
    """
    normalized = validate_strategy_name(name)
    new_normalized = validate_strategy_name(new_name) if new_name is not None else None
    desc_value = _validate_theme_description(description) if description is not None else None
    if time_horizon is not None or over_earnings is not None:
        th_value, oe_value = _validate_trade_idea_fields(
            time_horizon if time_horizon is not None else "",
            over_earnings if over_earnings is not None else False,
        )
    else:
        th_value, oe_value = None, None

    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            old_key = _trade_idea_key(normalized)
            if old_key not in db:
                raise KeyError(f"strategy {normalized!r} は存在しません")
            current = dict(db[old_key])

            renaming = new_normalized is not None and new_normalized != normalized
            if renaming:
                new_key = _trade_idea_key(new_normalized)
                if new_key in db:
                    raise ValueError(f"strategy {new_normalized!r} は既に存在します")
                current["name"] = new_normalized
            if desc_value is not None:
                current["description"] = desc_value
            if time_horizon is not None:
                current["time_horizon"] = th_value
            if over_earnings is not None:
                current["over_earnings"] = oe_value

            if renaming:
                del db[old_key]
                db[new_key] = current
                affected_codes = _rewrite_trade_idea_in_records(db, normalized, new_normalized)
            else:
                db[old_key] = current
                affected_codes = []

    if renaming:
        log_print(
            "portfolio_shelve: strategy リネーム",
            f"{normalized} -> {new_normalized}",
            f"affected={len(affected_codes)}",
        )
    else:
        log_print("portfolio_shelve: strategy 更新", normalized)
    return dict(current)


def delete_trade_idea(name: str, *, db_path: Optional[str] = None) -> int:
    """戦略を削除する。全 record の memo[trade_idea] を空文字にリセット。

    戻り値: 影響を受けた銘柄数
    """
    normalized = validate_strategy_name(name)
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            key = _trade_idea_key(normalized)
            if key not in db:
                raise KeyError(f"strategy {normalized!r} は存在しません")
            del db[key]
            affected_codes = _rewrite_trade_idea_in_records(db, normalized, None)
    log_print(
        "portfolio_shelve: strategy 削除",
        normalized,
        f"affected={len(affected_codes)}",
    )
    return len(affected_codes)


def count_trade_idea_usage(*, db_path: Optional[str] = None) -> Dict[str, int]:
    """name -> 使用銘柄数 を返す (編集画面表示用)。"""
    path = _resolve_db_path(db_path)
    counts: Dict[str, int] = {}
    with ShelveDB(path) as db:
        for key, value in db.items():
            if not key.startswith(KEY_RECORD_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            memo = value.get("memo") or {}
            idea = memo.get("trade_idea") or ""
            if idea:
                counts[idea] = counts.get(idea, 0) + 1
    return counts


def seed_trade_ideas(*, db_path: Optional[str] = None) -> int:
    """戦略マスターが空のときのみシードデータを投入する（冪等）。投入数を返す。"""
    path = _resolve_db_path(db_path)
    with _flock(db_path):
        with ShelveDB(path) as db:
            existing = [k for k in db.keys() if k.startswith(KEY_TRADE_IDEA_PREFIX)]
            if existing:
                return 0
            for seed in _TRADE_IDEA_SEED:
                record = {
                    "name": seed["name"],
                    "description": seed["description"],
                    "time_horizon": seed["time_horizon"],
                    "over_earnings": seed["over_earnings"],
                    "created_at": now_iso(),
                }
                db[_trade_idea_key(seed["name"])] = record
    log_print("portfolio_shelve: strategy シード投入", f"{len(_TRADE_IDEA_SEED)} 件")
    return len(_TRADE_IDEA_SEED)


def _rewrite_trade_idea_in_records(
    db: ShelveDB,
    old_name: str,
    new_name: Optional[str],
) -> List[str]:
    """全 record:* を走査し、memo[trade_idea] が old_name の銘柄を new_name に書き換える。

    - new_name=None なら空文字にリセット (削除時)
    - 変更があった record は updated_at を更新
    - 戻り値: 変更があった code_s のリスト

    呼び出し側で _flock + ShelveDB セッションを保持していること前提。
    """
    affected: List[str] = []
    record_keys = [k for k in db.keys() if k.startswith(KEY_RECORD_PREFIX)]
    for key in record_keys:
        record = db[key]
        if not isinstance(record, dict):
            continue
        memo = record.get("memo") or {}
        if memo.get("trade_idea") != old_name:
            continue
        new_memo = dict(memo)
        new_memo["trade_idea"] = new_name if new_name is not None else ""
        new_record = dict(record)
        new_record["memo"] = new_memo
        new_record["updated_at"] = now_iso()
        db[key] = new_record
        affected.append(record.get("code_s") or key[len(KEY_RECORD_PREFIX):])
    return affected


def _append_action_log_inner(
    db: ShelveDB,
    code_s: str,
    action_type: str,
    *,
    reason: str = "",
) -> None:
    """既にオープン済みの ShelveDB に action_log を直接書き込む (flock 内専用)。

    append_action_log は内部で _flock + ShelveDB を再オープンするため、
    リネーム/削除のような巨大トランザクション内ではこちらを使って同一セッションに収める。
    """
    validate_action_type(action_type)
    normalized = normalize_code_s(code_s)
    seq = _next_seq(db, normalized)
    entry = {
        "code_s": normalized,
        "seq": seq,
        "timestamp": now_iso(),
        "action_type": action_type,
        "status_from": None,
        "status_to": None,
        "reason": reason,
    }
    db[_action_log_key(normalized, seq)] = entry


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
    - 1 つでも変更があれば updated_at を更新 (action_log は記録しない)

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

    normalized_fields: Dict[str, Any] = {}
    for k, v in fields.items():
        if k in MEMO_LIST_FIELDS:
            # list[str] 専用フィールド (issue #187: gyoutai_themes)
            if not isinstance(v, list):
                raise TypeError(
                    f"portfolio_shelve: memo[{k!r}] must be list[str], "
                    f"got {type(v).__name__}"
                )
            if not all(isinstance(e, str) for e in v):
                raise TypeError(
                    f"portfolio_shelve: memo[{k!r}] must contain only str"
                )
            normalized_fields[k] = list(v)
        elif v is None:
            normalized_fields[k] = ""
        elif isinstance(v, str):
            normalized_fields[k] = v
        else:
            raise TypeError(
                f"portfolio_shelve: memo[{k!r}] must be str or None, "
                f"got {type(v).__name__}"
            )

    def _current_default(k: str) -> Any:
        return [] if k in MEMO_LIST_FIELDS else ""

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

            # gyoutai_themes (list[str]) のマスター整合性チェック (issue #282)
            # - 空文字は除去 (スロットクリア)
            # - マスター登録済み name は採用
            # - 未登録 name で現行レコードに既に存在する場合は保持を許可 (移行漏れ救済)
            # - 純新規の未登録 name は ValueError
            if "gyoutai_themes" in normalized_fields:
                cleaned: List[str] = []
                current_themes = current_memo.get("gyoutai_themes") or []
                master_names = {
                    k[len(KEY_THEME_PREFIX):]
                    for k in db.keys()
                    if k.startswith(KEY_THEME_PREFIX)
                }
                for raw in normalized_fields["gyoutai_themes"]:
                    t = raw.strip()
                    if not t:
                        continue
                    if t in master_names or t in current_themes:
                        if t not in cleaned:
                            cleaned.append(t)
                        continue
                    raise ValueError(
                        f"portfolio_shelve: theme {t!r} はマスター未登録のため新規付与できません"
                    )
                normalized_fields["gyoutai_themes"] = cleaned

            # trade_idea の値チェック (issue #335: shelve マスター参照に移行)
            # - 空文字 (未分類) は常に許容
            # - 現行レコードに既に入っている値は保持を許可 (旧自由記述の救済)
            # - 純新規でマスター未登録の値は ValueError
            if "trade_idea" in normalized_fields:
                idea = normalized_fields["trade_idea"]
                current_idea = current_memo.get("trade_idea", "")
                if idea and idea != current_idea:
                    master_names = {
                        k[len(KEY_TRADE_IDEA_PREFIX):]
                        for k in db.keys()
                        if k.startswith(KEY_TRADE_IDEA_PREFIX)
                    }
                    if idea not in master_names:
                        raise ValueError(
                            f"portfolio_shelve: trade_idea {idea!r} はマスター未登録のため"
                            f"新規付与できません"
                        )

            changed = any(
                current_memo.get(k, _current_default(k)) != v
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
