#!/usr/bin/env python3
"""
Migration script: pickle to shelve database conversion.

pickleからshelveへのデータベース移行スクリプト。
バックアップ、検証、ロールバック機能を提供。

Usage:
    python migrate_pickle_to_shelve.py              # 移行実行
    python migrate_pickle_to_shelve.py --dry-run    # ドライラン（変更なし）
    python migrate_pickle_to_shelve.py --verify     # 検証のみ
    python migrate_pickle_to_shelve.py --rollback   # ロールバック
"""

import argparse
import os
import shutil
from datetime import datetime
from typing import Dict, Any, Tuple, List

from ks_util import load_pickle, DATA_DIR, log_print, log_warning, log_error
from db_shelve import (
    ShelveDB,
    STOCKS_SHELVE,
    MARKET_SHELVE,
    KESSAN_SHELVE,
    SECTOR_SHELVE,
)


# ===========================================
# Migration Configuration
# ===========================================

MIGRATION_TARGETS = [
    {
        "name": "stocks",
        "pickle_path": os.path.join(DATA_DIR, "stock_data", "stocks.pickle"),
        "shelve_path": STOCKS_SHELVE,
        "backup_suffix": "_backup_before_shelve",
    },
    {
        "name": "market",
        "pickle_path": os.path.join(DATA_DIR, "market_data", "market_db.pickle"),
        "shelve_path": MARKET_SHELVE,
        "backup_suffix": "_backup_before_shelve",
    },
    {
        "name": "kessan",
        "pickle_path": os.path.join(
            DATA_DIR, "todays_kessan_data", "pf_kessan.pickle"
        ),
        "shelve_path": KESSAN_SHELVE,
        "backup_suffix": "_backup_before_shelve",
    },
    {
        "name": "sector",
        "pickle_path": os.path.join(
            DATA_DIR, "stock_data", "sector", "sector_db.pickle"
        ),
        "shelve_path": SECTOR_SHELVE,
        "backup_suffix": "_backup_before_shelve",
    },
]


# ===========================================
# Core Migration Functions
# ===========================================


def migrate_single_db(
    pickle_path: str, shelve_path: str, name: str, dry_run: bool = False
) -> Tuple[bool, str]:
    """
    Migrate a single pickle database to shelve.

    Args:
        pickle_path: Path to source pickle file
        shelve_path: Path to target shelve database
        name: Database name for logging
        dry_run: If True, only simulate migration

    Returns:
        Tuple of (success: bool, message: str)
    """
    if not os.path.exists(pickle_path):
        return False, f"Pickle file not found: {pickle_path}"

    log_print(f"[{name}] Loading pickle: {pickle_path}")

    if dry_run:
        data = load_pickle(pickle_path)
        log_print(f"[{name}] Would migrate {len(data)} records")
        return True, f"Dry run: {len(data)} records"

    try:
        # Load pickle data
        data = load_pickle(pickle_path)
        record_count = len(data)
        log_print(f"[{name}] Loaded {record_count} records")

        # Ensure target directory exists
        shelve_dir = os.path.dirname(shelve_path)
        if shelve_dir and not os.path.exists(shelve_dir):
            os.makedirs(shelve_dir)

        # Write to shelve
        log_print(f"[{name}] Writing to shelve: {shelve_path}")
        with ShelveDB(shelve_path) as db:
            for key, value in data.items():
                db[str(key)] = value  # Ensure string keys
            db.sync()

        log_print(f"[{name}] Migration complete: {record_count} records")
        return True, f"Migrated {record_count} records"

    except Exception as e:
        log_error(f"[{name}] Migration failed: {e}")
        return False, str(e)


def verify_migration(
    pickle_path: str, shelve_path: str, name: str
) -> Tuple[bool, str]:
    """
    Verify that shelve data matches original pickle.

    Args:
        pickle_path: Path to original pickle file
        shelve_path: Path to shelve database
        name: Database name for logging

    Returns:
        Tuple of (success: bool, message: str)
    """
    if not os.path.exists(pickle_path):
        return False, f"Pickle file not found: {pickle_path}"

    log_print(f"[{name}] Verifying migration...")

    try:
        # Load both sources
        pickle_data = load_pickle(pickle_path)

        with ShelveDB(shelve_path) as db:
            shelve_keys = set(db.keys())
            pickle_keys = set(str(k) for k in pickle_data.keys())

            # Check key counts
            if len(shelve_keys) != len(pickle_keys):
                return (
                    False,
                    f"Key count mismatch: pickle={len(pickle_keys)}, "
                    f"shelve={len(shelve_keys)}",
                )

            # Check key sets
            missing_keys = pickle_keys - shelve_keys
            extra_keys = shelve_keys - pickle_keys
            if missing_keys:
                return False, f"Missing keys in shelve: {list(missing_keys)[:10]}"
            if extra_keys:
                return False, f"Extra keys in shelve: {list(extra_keys)[:10]}"

            # Sample value verification (check 10% or max 100)
            sample_size = min(100, len(pickle_keys) // 10 + 1)
            sample_keys = list(pickle_keys)[:sample_size]

            for key in sample_keys:
                # Handle both int and string keys in pickle
                if key in pickle_data:
                    pickle_val = pickle_data[key]
                elif int(key) in pickle_data if key.isdigit() else False:
                    pickle_val = pickle_data[int(key)]
                else:
                    continue

                shelve_val = db.get(key)

                if shelve_val is None:
                    return False, f"Value not found in shelve for key {key}"

                # Deep comparison for critical fields
                for field in ["code_s", "stock_name", "price", "access_date_price"]:
                    if field in pickle_val:
                        if pickle_val.get(field) != shelve_val.get(field):
                            return (
                                False,
                                f"Value mismatch for key {key}, field {field}: "
                                f"pickle={pickle_val.get(field)}, "
                                f"shelve={shelve_val.get(field)}",
                            )

        log_print(f"[{name}] Verification passed: {len(pickle_keys)} records")
        return True, f"Verified {len(pickle_keys)} records"

    except Exception as e:
        log_error(f"[{name}] Verification failed: {e}")
        return False, str(e)


# ===========================================
# Backup and Rollback
# ===========================================


def create_backup(pickle_path: str, backup_suffix: str) -> str:
    """
    Create timestamped backup of pickle file.

    Args:
        pickle_path: Path to pickle file
        backup_suffix: Suffix for backup file

    Returns:
        Path to backup file, or empty string if file doesn't exist
    """
    if not os.path.exists(pickle_path):
        return ""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{pickle_path}{backup_suffix}_{timestamp}"

    log_print(f"Creating backup: {backup_path}")
    shutil.copy2(pickle_path, backup_path)

    return backup_path


def rollback_migration(shelve_path: str, name: str) -> bool:
    """
    Rollback: remove shelve files.

    Args:
        shelve_path: Path to shelve database
        name: Database name for logging

    Returns:
        True if successful
    """
    log_print(f"[{name}] Rolling back migration...")

    try:
        # Remove shelve files (shelve creates multiple files with extensions)
        for ext in ["", ".db", ".dir", ".bak", ".dat"]:
            path = shelve_path + ext
            if os.path.exists(path):
                log_print(f"Removing: {path}")
                os.remove(path)

        log_print(f"[{name}] Rollback complete")
        return True

    except Exception as e:
        log_error(f"[{name}] Rollback failed: {e}")
        return False


# ===========================================
# Main Migration Flow
# ===========================================


def run_migration(
    verify_only: bool = False,
    rollback: bool = False,
    dry_run: bool = False,
    targets: List[str] = None,
) -> bool:
    """
    Run the full migration process.

    Args:
        verify_only: Only verify existing migration
        rollback: Rollback migration (remove shelve files)
        dry_run: Simulate migration without changes
        targets: List of target names to process (None = all)

    Returns:
        True if successful
    """
    log_print("=" * 60)
    log_print("Pickle to Shelve Migration")
    mode = (
        "Verify"
        if verify_only
        else "Rollback"
        if rollback
        else "Dry-run"
        if dry_run
        else "Migrate"
    )
    log_print(f"Mode: {mode}")
    log_print("=" * 60)

    results = []

    for target in MIGRATION_TARGETS:
        name = target["name"]

        # Filter by target names if specified
        if targets and name not in targets:
            continue

        pickle_path = target["pickle_path"]
        shelve_path = target["shelve_path"]
        backup_suffix = target["backup_suffix"]

        log_print(f"\n--- Processing: {name} ---")

        if rollback:
            success = rollback_migration(shelve_path, name)
            results.append((name, success, "Rollback"))
            continue

        if verify_only:
            success, msg = verify_migration(pickle_path, shelve_path, name)
            results.append((name, success, msg))
            continue

        # Normal migration flow
        # 1. Create backup
        if not dry_run:
            create_backup(pickle_path, backup_suffix)

        # 2. Migrate
        success, msg = migrate_single_db(pickle_path, shelve_path, name, dry_run)
        if not success:
            results.append((name, False, msg))
            continue

        # 3. Verify
        if not dry_run:
            verify_success, verify_msg = verify_migration(
                pickle_path, shelve_path, name
            )
            if not verify_success:
                log_warning(f"[{name}] Verification failed, rolling back...")
                rollback_migration(shelve_path, name)
                results.append((name, False, f"Verification failed: {verify_msg}"))
                continue

        results.append((name, True, msg))

    # Summary
    log_print("\n" + "=" * 60)
    log_print("Migration Summary")
    log_print("=" * 60)

    all_success = True
    for name, success, msg in results:
        status = "OK" if success else "FAILED"
        log_print(f"  {name}: {status} - {msg}")
        if not success:
            all_success = False

    return all_success


def main():
    parser = argparse.ArgumentParser(
        description="Migrate pickle databases to shelve format"
    )
    parser.add_argument(
        "--verify", action="store_true", help="Verify migration only (no changes)"
    )
    parser.add_argument(
        "--rollback", action="store_true", help="Rollback migration (remove shelve files)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate migration without changes"
    )
    parser.add_argument(
        "--target",
        type=str,
        nargs="+",
        choices=["stocks", "market", "kessan", "sector"],
        help="Specific targets to process (default: all)",
    )

    args = parser.parse_args()

    success = run_migration(
        verify_only=args.verify,
        rollback=args.rollback,
        dry_run=args.dry_run,
        targets=args.target,
    )

    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
