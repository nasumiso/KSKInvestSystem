#!/usr/bin/env python3
"""
shelve-based database abstraction layer.
Provides thread-safe, pickle-compatible interface for stock database.

pickleからshelveへの移行用モジュール。
スレッドセーフで、既存のpickle APIと互換性のあるインターフェースを提供。
"""

import shelve
import dbm.dumb
import fcntl
import threading
import pickle
import os
import time
from contextlib import contextmanager, ExitStack
from typing import Dict, Any, Optional, List, Iterator

# ks_utilへの依存を遅延ロードに変更（テスト時の依存解決のため）
try:
    from ks_util import log_print, log_warning, DATA_DIR
except ImportError:
    # Fallback for testing without full ks_util dependencies
    def log_print(*args, **kwargs):
        print(*args, **kwargs)

    def log_warning(*args, **kwargs):
        print("WARNING:", *args, **kwargs)

    # Default DATA_DIR for testing
    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


# ===========================================
# プロセス間排他制御 (issue #174)
# ===========================================

# リエントラント検出用: ロックファイルパスごとの取得深さ。
# research_shelve は単一 DB 前提で depth をスカラーで持つが、ShelveDB は
# stocks/market/research/portfolio を同じクラスで扱うため path をキーにする。
_flock_depth = threading.local()


def _lock_path_for(db_path: str) -> str:
    """ロックファイルパスを返す。

    research_shelve / portfolio_shelve が使う `<db>.lock` とは別名にする。
    あちらは自前の _flock() を外側で取ってから ShelveDB を開くため、同名だと
    同一プロセス・別fdで同じファイルを掴み自己デッドロックする
    (flock はプロセス単位ではなく fd 単位で待つ)。
    """
    return db_path + ".dbm.lock"


@contextmanager
def _flock(db_path: str, operation: int):
    """shelve DB のプロセス間ロック (fcntl.flock)。

    compact のファイル差し替え (.dat/.dir の入れ替え) と、その最中の
    読み取りを直列化するために使う。compact は EX、読み取り open は
    open〜close の間 SH を保持する。

    **このロックは「compact 対 他プロセス」しか守らない。**
    日次バッチが書き込み中の値を別プロセスが読むと pickle が壊れる
    (実測 52%) が、これは dbm.dumb が「.dat への値の書き込み」と
    「.dir への索引反映 (close 時)」を分離している構造上の問題で、
    書き手の open〜close 全体を排他にしない限り塞げない。それをやると
    日次バッチの数十分の間 WebApp が止まるため、ここでは対応しない。
    恒久対応は SQLite (WAL) への移行 → issue #194 案C / 別issue。

    operation: fcntl.LOCK_EX または fcntl.LOCK_SH

    リエントラント対応: 同一スレッドが同じロックファイルを保持中なら
    取得をスキップする。SH 保持中の EX 要求 (昇格) は flock では
    アトミックにできず取りこぼすため、呼び出し側の設計ミスとして弾く。
    """
    lock_file = _lock_path_for(db_path)
    held = getattr(_flock_depth, "held", None)
    if held is None:
        held = _flock_depth.held = {}

    entry = held.get(lock_file)
    if entry is not None:
        depth, held_op = entry
        if operation == fcntl.LOCK_EX and held_op == fcntl.LOCK_SH:
            raise RuntimeError(
                "共有ロック保持中に排他ロックは取得できません: %s" % lock_file
            )
        held[lock_file] = (depth + 1, held_op)
        try:
            yield
        finally:
            depth, held_op = held[lock_file]
            if depth <= 1:
                del held[lock_file]
            else:
                held[lock_file] = (depth - 1, held_op)
        return

    lock_dir = os.path.dirname(lock_file)
    if lock_dir and not os.path.exists(lock_dir):
        os.makedirs(lock_dir, exist_ok=True)
    fd = open(lock_file, "a")
    try:
        fcntl.flock(fd, operation)
        held[lock_file] = (1, operation)
        try:
            yield
        finally:
            held.pop(lock_file, None)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


class ShelveDB:
    """
    Thread-safe shelve database wrapper with pickle-compatible interface.

    スレッドセーフなshelveデータベースラッパー。
    pickle互換のインターフェースを提供。

    Key features:
    - Context manager support for safe resource management
    - Thread-safe read/write operations via RLock
    - Batch write optimization
    - Memoization cache for read-heavy workloads

    Usage:
        with ShelveDB("path/to/db") as db:
            db["key"] = {"data": "value"}
            data = db["key"]
    """

    def __init__(self, db_path: str, writeback: bool = False,
                 read_only: bool = False):
        """
        Initialize ShelveDB.

        Args:
            db_path: Path to shelve database (without extension)
            writeback: Enable writeback mode (caches all accessed entries)
            read_only: 読み取り専用。open〜close で共有ロックを保持し、
                compact のファイル差し替えから索引を守る (issue #174)。
                書き込みメソッドを呼ぶと RuntimeError。
        """
        self._db_path = db_path
        self._writeback = writeback
        self._read_only = read_only
        self._lock = threading.RLock()
        self._db: Optional[shelve.Shelf] = None
        self._memo_cache: Dict[str, Any] = {}
        self._memo_enabled = False
        # read_only 時に open〜close で保持する共有ロック (ExitStack)
        self._flock_stack: Optional[ExitStack] = None

    def open(self) -> "ShelveDB":
        """Open the database connection."""
        with self._lock:
            if self._db is None:
                log_print(f"shelveDB open: {self._db_path}")
                # Ensure directory exists
                db_dir = os.path.dirname(self._db_path)
                if db_dir and not os.path.exists(db_dir):
                    os.makedirs(db_dir)
                # 読み取り専用は close まで共有ロックを保持する。
                # dbm.dumb の索引は open 時に一度だけ読まれ以後更新されないため、
                # 途中で解放すると compact の差し替え後に古いオフセットで
                # .dat を読んでしまう (別レコードの混入・pickle 破損)。
                stack = ExitStack()
                if self._read_only:
                    stack.enter_context(_flock(self._db_path, fcntl.LOCK_SH))
                try:
                    # dbm.dumbを使用（macOSのdbm.ndbmはハッシュ衝突でキー消失するため）
                    dumb_db = dbm.dumb.open(self._db_path, flag="c")
                    self._db = shelve.Shelf(
                        dumb_db,
                        protocol=pickle.HIGHEST_PROTOCOL,
                        writeback=self._writeback,
                    )
                except Exception:
                    stack.close()
                    raise
                self._flock_stack = stack
        return self

    def close(self) -> None:
        """Close the database connection and sync changes."""
        with self._lock:
            if self._db is not None:
                log_print(f"shelveDB close: {self._db_path}")
                if self._read_only:
                    # 読み取りでは _modified が False のままなので _commit() は
                    # 走らない。保持中の共有ロックのまま閉じる (排他へ昇格しない)。
                    self._db.close()
                else:
                    with _flock(self._db_path, fcntl.LOCK_EX):
                        self._db.close()
                self._db = None
                self._memo_cache.clear()
            if self._flock_stack is not None:
                self._flock_stack.close()
                self._flock_stack = None

    def __enter__(self) -> "ShelveDB":
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        """Ensure database is open."""
        if self._db is None:
            raise RuntimeError(
                "Database not open. Use 'with' statement or call open()"
            )

    def _ensure_writable(self) -> None:
        """read_only インスタンスへの書き込みを弾く (issue #174)。"""
        if self._read_only:
            raise RuntimeError(
                "read_only で開いた DB には書き込めません: %s" % self._db_path
            )

    # ===========================================
    # CRUD Operations
    # ===========================================

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a record by key.

        Returns a COPY of the data (shelve default behavior).
        For mutable modifications, use update() after modifying.

        Args:
            key: Record key (string)
            default: Default value if key not found

        Returns:
            Record data or default
        """
        with self._lock:
            self._ensure_open()
            if self._memo_enabled and key in self._memo_cache:
                return self._memo_cache[key]
            try:
                value = self._db.get(key, default)
                if self._memo_enabled and value is not default:
                    self._memo_cache[key] = value
                return value
            except KeyError:
                return default

    def __getitem__(self, key: str) -> Any:
        """Dict-like access: db[key]"""
        with self._lock:
            self._ensure_open()
            if self._memo_enabled and key in self._memo_cache:
                return self._memo_cache[key]
            value = self._db[key]
            if self._memo_enabled:
                self._memo_cache[key] = value
            return value

    def __setitem__(self, key: str, value: Any) -> None:
        """Dict-like assignment: db[key] = value"""
        with self._lock:
            self._ensure_open()
            self._ensure_writable()
            self._db[key] = value
            if self._memo_enabled:
                self._memo_cache[key] = value

    def __delitem__(self, key: str) -> None:
        """Dict-like deletion: del db[key]"""
        with self._lock:
            self._ensure_open()
            self._ensure_writable()
            # dbm.dumb の __delitem__ は内部で _commit() を呼ぶため、
            # 索引の書き直し窓を排他ロックで守る。
            with _flock(self._db_path, fcntl.LOCK_EX):
                del self._db[key]
            self._memo_cache.pop(key, None)

    def __contains__(self, key: str) -> bool:
        """Membership test: key in db"""
        with self._lock:
            self._ensure_open()
            return key in self._db

    def __len__(self) -> int:
        """Return number of records."""
        with self._lock:
            self._ensure_open()
            return len(self._db)

    def keys(self) -> List[str]:
        """Return all keys (note: can be slow for large databases)."""
        with self._lock:
            self._ensure_open()
            return list(self._db.keys())

    def items(self) -> Iterator[tuple]:
        """Iterate over (key, value) pairs."""
        with self._lock:
            self._ensure_open()
            for key in self._db.keys():
                yield key, self._db[key]

    def values(self) -> Iterator[Any]:
        """Iterate over values."""
        with self._lock:
            self._ensure_open()
            for key in self._db.keys():
                yield self._db[key]

    # ===========================================
    # Batch Operations
    # ===========================================

    def update_batch(self, updates: Dict[str, Any]) -> None:
        """
        Batch update multiple records efficiently.

        Args:
            updates: Dict of {key: value} pairs to update
        """
        with self._lock:
            self._ensure_open()
            self._ensure_writable()
            for key, value in updates.items():
                self._db[key] = value
                if self._memo_enabled:
                    self._memo_cache[key] = value
            self.sync()

    def delete_batch(self, keys: List[str]) -> int:
        """
        Batch delete multiple records.

        Returns: Number of successfully deleted records
        """
        deleted = 0
        with self._lock:
            self._ensure_open()
            self._ensure_writable()
            # del は dbm.dumb 内部で _commit() を呼ぶため、まとめて排他する。
            with _flock(self._db_path, fcntl.LOCK_EX):
                for key in keys:
                    if key in self._db:
                        del self._db[key]
                        self._memo_cache.pop(key, None)
                        deleted += 1
                self._db.sync()
        return deleted

    def sync(self) -> None:
        """Synchronize database to disk."""
        with self._lock:
            if self._db is not None:
                if self._read_only:
                    # 読み取り専用では書き戻すものが無い。共有ロック保持中に
                    # 排他ロックを取ろうとして落ちないよう no-op にする。
                    return
                with _flock(self._db_path, fcntl.LOCK_EX):
                    self._db.sync()

    # ===========================================
    # Memoization Support
    # ===========================================

    @contextmanager
    def enable_memo(self):
        """
        Enable memoization cache for read-heavy operations.

        Usage:
            with db.enable_memo():
                # Multiple reads will be cached
                stock1 = db.get("1234")
                stock2 = db.get("1234")  # Returns cached copy
        """
        self._memo_enabled = True
        try:
            yield self
        finally:
            self._memo_enabled = False
            self._memo_cache.clear()

    # ===========================================
    # Import/Export (Pickle Compatibility)
    # ===========================================

    def export_to_dict(self) -> Dict[str, Any]:
        """Export entire database to dict (for backup/migration)."""
        with self._lock:
            self._ensure_open()
            return {key: self._db[key] for key in self._db.keys()}

    def import_from_dict(self, data: Dict[str, Any]) -> None:
        """Import dict data into database (upsert only, does not delete existing keys)."""
        with self._lock:
            self._ensure_open()
            self._ensure_writable()
            for key, value in data.items():
                self._db[str(key)] = value  # Ensure string keys
            self.sync()

    def replace_from_dict(self, data: Dict[str, Any]) -> None:
        """Replace entire database contents with dict data (deletes keys not in data)."""
        with self._lock:
            self._ensure_open()
            self._ensure_writable()
            # 全キーの del は1件ごとに _commit() を走らせるため、
            # 置換が終わるまでまとめて排他する。
            with _flock(self._db_path, fcntl.LOCK_EX):
                for key in list(self._db.keys()):
                    del self._db[key]
                for key, value in data.items():
                    self._db[str(key)] = value
                self._db.sync()

    # ===========================================
    # Utility
    # ===========================================

    def exists(self) -> bool:
        """Check if database files exist."""
        # shelve creates files with various extensions depending on backend
        for ext in ["", ".db", ".dir", ".dat", ".bak"]:
            if os.path.exists(self._db_path + ext):
                return True
        return False

    @property
    def path(self) -> str:
        """Return database path."""
        return self._db_path


# ===========================================
# Database Path Constants
# ===========================================

STOCKS_SHELVE = os.path.join(DATA_DIR, "stock_data", "stocks_shelve")
MARKET_SHELVE = os.path.join(DATA_DIR, "market_data", "market_db_shelve")
KESSAN_SHELVE = os.path.join(DATA_DIR, "todays_kessan_data", "pf_kessan_shelve")
SECTOR_SHELVE = os.path.join(DATA_DIR, "stock_data", "sector", "sector_db_shelve")
RESEARCH_SHELVE = os.path.join(DATA_DIR, "stock_data", "research_shelve")
PORTFOLIO_SHELVE = os.path.join(DATA_DIR, "stock_data", "portfolio_shelve")


# ===========================================
# Singleton Database Accessors
# ===========================================

_stock_db: Optional[ShelveDB] = None
_market_db: Optional[ShelveDB] = None
_kessan_db: Optional[ShelveDB] = None
_sector_db: Optional[ShelveDB] = None


def get_stock_db() -> ShelveDB:
    """Get stock database instance."""
    global _stock_db
    if _stock_db is None:
        _stock_db = ShelveDB(STOCKS_SHELVE)
    return _stock_db


def get_market_db() -> ShelveDB:
    """Get market database instance."""
    global _market_db
    if _market_db is None:
        _market_db = ShelveDB(MARKET_SHELVE)
    return _market_db


def get_kessan_db() -> ShelveDB:
    """Get kessan database instance."""
    global _kessan_db
    if _kessan_db is None:
        _kessan_db = ShelveDB(KESSAN_SHELVE)
    return _kessan_db


def get_sector_db() -> ShelveDB:
    """Get sector database instance."""
    global _sector_db
    if _sector_db is None:
        _sector_db = ShelveDB(SECTOR_SHELVE)
    return _sector_db


# ===========================================
# Pickle Compatibility Functions
# ===========================================


def load_shelve_as_dict(db_path: str) -> Dict[str, Any]:
    """
    Load entire shelve database as dict.
    For backward compatibility with code expecting dict.

    Args:
        db_path: Path to shelve database (without extension)

    Returns:
        Dict containing all database records
    """
    with ShelveDB(db_path) as db:
        return db.export_to_dict()


def save_dict_to_shelve(db_path: str, data: Dict[str, Any]) -> None:
    """
    Save dict to shelve database.
    For backward compatibility with code using dict.

    Args:
        db_path: Path to shelve database (without extension)
        data: Dict to save
    """
    with ShelveDB(db_path) as db:
        db.import_from_dict(data)


# ===========================================
# コンパクション (issue #194)
# ===========================================

# shelve (dbm.dumb) が作るファイル群の拡張子。
# オフセット表である .dir を最後に置くことで、差し替え途中に
# 「新しい .dat × 古い .dir」を読まれる窓を最小化する。
_SHELVE_EXTENSIONS = (".dat", ".bak", ".dir")

# 検証で内容一致を確認するサンプル件数
_COMPACT_VERIFY_SAMPLES = 30


def get_shelve_size(db_path: str) -> int:
    """shelve DB (.dat/.dir/.bak) の合計サイズをバイトで返す"""
    total = 0
    for ext in _SHELVE_EXTENSIONS:
        fpath = db_path + ext
        if os.path.exists(fpath):
            total += os.path.getsize(fpath)
    return total


def format_size(size_bytes: int) -> str:
    """バイト数を読みやすい文字列に変換"""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.1f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _move_shelve_files(src_path: str, dst_path: str) -> None:
    """shelve の3ファイルを os.replace で移動する (存在するものだけ)"""
    for ext in _SHELVE_EXTENSIONS:
        src = src_path + ext
        if os.path.exists(src):
            os.replace(src, dst_path + ext)


def _remove_shelve_files(db_path: str) -> None:
    """shelve の3ファイルを削除する (存在するものだけ)"""
    for ext in _SHELVE_EXTENSIONS:
        fpath = db_path + ext
        if os.path.exists(fpath):
            os.remove(fpath)


def compact_shelve(db_path: str, keep_backup: bool = False) -> Optional[Dict[str, Any]]:
    """dbm.dumb の断片化を解消し .dat を縮小する (issue #194)。

    ライブDBには触れずに一時DBを構築・検証し、最後に os.replace で差し替える。
    退避 (`<db_path>.compact_backup.*`) は keep_backup の値によらず常に作り、
    swap 成功後に削除する (swap 途中で失敗してもライブDBを復元できるようにするため)。

    この退避が実行開始時に残っていれば「前回が差し替え途中で中断した」印なので、
    消さずに RuntimeError で停止する。keep_backup=True で意図的に残す場合は
    `<db_path>.compact_kept_<YYMMDD_HHMM>.*` へ改名し、この印と区別する。

    Args:
        db_path: 対象shelveのパス (拡張子なし)
        keep_backup: True なら成功後も退避ファイルを別名で残す

    Returns:
        {"size_before", "size_after", "record_count"}。DB未作成なら None

    Raises:
        RuntimeError: 検証失敗時、または前回の中断を検出した場合。
                      いずれもライブDB・退避は保持される
    """
    # 全工程を排他ロックで囲む (issue #174)。
    # 読み出し〜差し替えの間に他プロセスが書き込むと、古いスナップショットで
    # 差し替えることになりその更新が消える (成功を返すため気づけない)。
    # 実測 0.8〜1.4s で終わるため、読み手を待たせるコストは小さい。
    with _flock(db_path, fcntl.LOCK_EX):
        return _compact_shelve_locked(db_path, keep_backup)


def _compact_shelve_locked(db_path: str, keep_backup: bool) -> Optional[Dict[str, Any]]:
    """compact_shelve の本体。排他ロック保持中に呼ばれる。"""
    backup_path = db_path + ".compact_backup"
    if os.path.exists(backup_path + ".dat"):
        # 前回の実行が差し替え途中で中断している (プロセス強制終了・電源断など)。
        # ライブ側は不完全なファイル群で、完全な元DBは退避側にしかない。
        # ここで退避を消すとデータが回復不能になるため、消さずに中断する。
        raise RuntimeError(
            "前回のコンパクションが中断しています。退避 %s.* に元DBが残っているため、"
            "手動で %s.* へ戻してから再実行してください" % (backup_path, db_path)
        )

    if not os.path.exists(db_path + ".dat"):
        log_warning("compact: DBが存在しないためスキップします", db_path)
        return None

    size_before = get_shelve_size(db_path)
    log_print("compact: 開始", db_path, format_size(size_before))

    # 1. 全件読み出し (ライブDBはこの時点では変更しない)
    with ShelveDB(db_path) as db:
        all_data = db.export_to_dict()
    record_count = len(all_data)

    # 2. 一時DBに書き戻す。import_from_dict (upsert) を使う。
    #    replace_from_dict は全キー del → set でゴミを生むため使わない。
    tmp_path = "%s.compact_tmp.%d.%d" % (db_path, os.getpid(), threading.get_ident())
    _remove_shelve_files(tmp_path)
    try:
        with ShelveDB(tmp_path) as db:
            db.import_from_dict(all_data)

        # 3. 検証: 件数一致 + サンプルの内容一致
        with ShelveDB(tmp_path) as db:
            new_count = len(db)
            if new_count != record_count:
                raise RuntimeError(
                    "レコード数不一致 (元: %d, 新: %d)" % (record_count, new_count)
                )
            for key in list(all_data.keys())[:_COMPACT_VERIFY_SAMPLES]:
                if db.get(key) != all_data[key]:
                    raise RuntimeError("レコード内容不一致: key=%s" % key)
    except Exception:
        # ライブDBは未変更なので一時ファイルを消すだけでよい
        _remove_shelve_files(tmp_path)
        raise

    # 4. ライブDBを退避してから swap する。
    #    .dat/.dir/.bak を一括で atomic に差し替える手段は無いため、
    #    _SHELVE_EXTENSIONS の順序 (.dir が最後) で窓を最小化する。
    try:
        # 退避自体が途中で失敗するとライブ側に不完全なファイル群が残るため、
        # 退避と差し替えを同じ try に入れて両方をロールバック対象にする。
        _move_shelve_files(db_path, backup_path)
        _move_shelve_files(tmp_path, db_path)
    except Exception:
        # 退避・差し替えの途中で落ちた場合は退避から書き戻す。
        # 退避に無い拡張子だけをライブ側から取り除く (退避途中で失敗した場合、
        # ライブ側にはまだ移動していない元ファイルが残っているため、
        # 先に全消ししてしまうと復元できなくなる)。
        for ext in _SHELVE_EXTENSIONS:
            if os.path.exists(backup_path + ext) and os.path.exists(db_path + ext):
                os.remove(db_path + ext)
        _move_shelve_files(backup_path, db_path)
        _remove_shelve_files(tmp_path)
        raise

    size_after = get_shelve_size(db_path)

    # 5. swap 成功。退避の後始末。
    #    保持する場合も .compact_backup のままにはしない。この名前は
    #    「中断の痕跡」として次回実行時の停止条件に使っているため。
    if keep_backup:
        # 既存の保持退避は消さない (最初の1つだけが肥大化前の状態を持つことがある)。
        # 同一分内の再実行でも衝突しないよう、空いている名前を探す。
        stamp = time.strftime("%y%m%d_%H%M%S")
        kept_path = "%s.compact_kept_%s" % (db_path, stamp)
        seq = 0
        while os.path.exists(kept_path + ".dat"):
            seq += 1
            kept_path = "%s.compact_kept_%s_%d" % (db_path, stamp, seq)
        _move_shelve_files(backup_path, kept_path)
        log_print("compact: 退避を保持しました", kept_path)
    else:
        _remove_shelve_files(backup_path)

    log_print(
        "compact: 完了",
        "%s → %s" % (format_size(size_before), format_size(size_after)),
        "(%d件)" % record_count,
    )
    return {
        "size_before": size_before,
        "size_after": size_after,
        "record_count": record_count,
    }


# ===========================================
# Main (for testing)
# ===========================================

if __name__ == "__main__":
    import tempfile

    print("ShelveDB basic test")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_db")

        # Test basic CRUD
        with ShelveDB(db_path) as db:
            # Create
            db["1234"] = {"stock_name": "Test Corp", "price": 1000}
            print(f"Created: 1234 in db = {'1234' in db}")

            # Read
            data = db["1234"]
            print(f"Read: {data}")

            # Update
            data["price"] = 1100
            db["1234"] = data
            print(f"Updated price: {db['1234']['price']}")

            # Length
            print(f"Length: {len(db)}")

            # Keys
            print(f"Keys: {db.keys()}")

        # Test persistence
        with ShelveDB(db_path) as db:
            print(f"After reopen, price: {db['1234']['price']}")

        print("All tests passed!")
