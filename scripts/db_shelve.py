#!/usr/bin/env python3
"""
shelve-based database abstraction layer.
Provides thread-safe, pickle-compatible interface for stock database.

pickleからshelveへの移行用モジュール。
スレッドセーフで、既存のpickle APIと互換性のあるインターフェースを提供。
"""

import shelve
import dbm.dumb
import threading
import pickle
import os
from contextlib import contextmanager
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

    def __init__(self, db_path: str, writeback: bool = False):
        """
        Initialize ShelveDB.

        Args:
            db_path: Path to shelve database (without extension)
            writeback: Enable writeback mode (caches all accessed entries)
        """
        self._db_path = db_path
        self._writeback = writeback
        self._lock = threading.RLock()
        self._db: Optional[shelve.Shelf] = None
        self._memo_cache: Dict[str, Any] = {}
        self._memo_enabled = False

    def open(self) -> "ShelveDB":
        """Open the database connection."""
        with self._lock:
            if self._db is None:
                log_print(f"shelveDB open: {self._db_path}")
                # Ensure directory exists
                db_dir = os.path.dirname(self._db_path)
                if db_dir and not os.path.exists(db_dir):
                    os.makedirs(db_dir)
                # dbm.dumbを使用（macOSのdbm.ndbmはハッシュ衝突でキー消失するため）
                dumb_db = dbm.dumb.open(self._db_path, flag="c")
                self._db = shelve.Shelf(
                    dumb_db,
                    protocol=pickle.HIGHEST_PROTOCOL,
                    writeback=self._writeback,
                )
        return self

    def close(self) -> None:
        """Close the database connection and sync changes."""
        with self._lock:
            if self._db is not None:
                log_print(f"shelveDB close: {self._db_path}")
                self._db.close()
                self._db = None
                self._memo_cache.clear()

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
            self._db[key] = value
            if self._memo_enabled:
                self._memo_cache[key] = value

    def __delitem__(self, key: str) -> None:
        """Dict-like deletion: del db[key]"""
        with self._lock:
            self._ensure_open()
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
            for key in keys:
                if key in self._db:
                    del self._db[key]
                    self._memo_cache.pop(key, None)
                    deleted += 1
            self.sync()
        return deleted

    def sync(self) -> None:
        """Synchronize database to disk."""
        with self._lock:
            if self._db is not None:
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
            for key, value in data.items():
                self._db[str(key)] = value  # Ensure string keys
            self.sync()

    def replace_from_dict(self, data: Dict[str, Any]) -> None:
        """Replace entire database contents with dict data (deletes keys not in data)."""
        with self._lock:
            self._ensure_open()
            for key in list(self._db.keys()):
                del self._db[key]
            for key, value in data.items():
                self._db[str(key)] = value
            self.sync()

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
