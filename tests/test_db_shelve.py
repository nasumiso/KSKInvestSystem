"""db_shelve.py の ShelveDB CRUD テスト（tmp_path で一時DB作成）"""

import os
import pytest

from db_shelve import ShelveDB


@pytest.fixture
def db_path(tmp_path):
    """テスト用一時DBパスを返す"""
    return str(tmp_path / "test_db")


# ==================================================
# 基本 CRUD
# ==================================================
class TestShelveDBCrud:
    """ShelveDB の基本 CRUD 操作テスト"""

    def test_create_and_read(self, db_path):
        """作成と読み取り"""
        with ShelveDB(db_path) as db:
            db["1234"] = {"stock_name": "テスト株式", "price": 1000}
            data = db["1234"]
            assert data["stock_name"] == "テスト株式"
            assert data["price"] == 1000

    def test_update(self, db_path):
        """更新"""
        with ShelveDB(db_path) as db:
            db["1234"] = {"price": 1000}
            data = db["1234"]
            data["price"] = 1100
            db["1234"] = data
            assert db["1234"]["price"] == 1100

    def test_delete(self, db_path):
        """削除"""
        with ShelveDB(db_path) as db:
            db["1234"] = {"price": 1000}
            assert "1234" in db
            del db["1234"]
            assert "1234" not in db

    def test_contains(self, db_path):
        """存在チェック"""
        with ShelveDB(db_path) as db:
            assert "9999" not in db
            db["9999"] = {"test": True}
            assert "9999" in db

    def test_len(self, db_path):
        """件数"""
        with ShelveDB(db_path) as db:
            assert len(db) == 0
            db["1234"] = {"a": 1}
            db["5678"] = {"b": 2}
            assert len(db) == 2

    def test_keys(self, db_path):
        """キー一覧"""
        with ShelveDB(db_path) as db:
            db["AAA"] = 1
            db["BBB"] = 2
            keys = db.keys()
            assert set(keys) == {"AAA", "BBB"}

    def test_get_default(self, db_path):
        """get のデフォルト値"""
        with ShelveDB(db_path) as db:
            assert db.get("missing") is None
            assert db.get("missing", "default") == "default"

    def test_getitem_keyerror(self, db_path):
        """存在しないキーで KeyError"""
        with ShelveDB(db_path) as db:
            with pytest.raises(KeyError):
                _ = db["nonexistent"]


# ==================================================
# バッチ操作
# ==================================================
class TestShelveDBBatch:
    """バッチ操作のテスト"""

    def test_update_batch(self, db_path):
        """一括更新"""
        with ShelveDB(db_path) as db:
            db.update_batch(
                {
                    "1111": {"name": "A"},
                    "2222": {"name": "B"},
                    "3333": {"name": "C"},
                }
            )
            assert len(db) == 3
            assert db["2222"]["name"] == "B"

    def test_delete_batch(self, db_path):
        """一括削除"""
        with ShelveDB(db_path) as db:
            db["1111"] = 1
            db["2222"] = 2
            db["3333"] = 3
            deleted = db.delete_batch(["1111", "3333", "9999"])  # 9999 は存在しない
            assert deleted == 2
            assert "1111" not in db
            assert "2222" in db
            assert "3333" not in db


# ==================================================
# export / import ラウンドトリップ
# ==================================================
class TestShelveDBExportImport:
    """export_to_dict / import_from_dict のラウンドトリップテスト"""

    def test_round_trip(self, db_path, tmp_path):
        """export → 別DBに import → 一致確認"""
        original_data = {
            "1234": {"stock_name": "Alpha", "price": 500},
            "5678": {"stock_name": "Beta", "price": 1500},
        }
        # DB1 に書き込み
        with ShelveDB(db_path) as db:
            db.import_from_dict(original_data)
            exported = db.export_to_dict()

        # DB2 に import
        db_path2 = str(tmp_path / "test_db2")
        with ShelveDB(db_path2) as db2:
            db2.import_from_dict(exported)
            for key in original_data:
                assert db2[key] == original_data[key]

    def test_import_preserves_existing(self, db_path):
        """import は既存キーを上書き、新規キーを追加"""
        with ShelveDB(db_path) as db:
            db["AAA"] = {"val": 1}
            db.import_from_dict({"AAA": {"val": 2}, "BBB": {"val": 3}})
            assert db["AAA"]["val"] == 2
            assert db["BBB"]["val"] == 3


# ==================================================
# コンテキストマネージャ
# ==================================================
class TestShelveDBContext:
    """コンテキストマネージャの開閉テスト"""

    def test_context_manager_closes(self, db_path):
        """with 文を抜けた後は RuntimeError"""
        db = ShelveDB(db_path)
        with db:
            db["test"] = "value"
        with pytest.raises(RuntimeError):
            _ = db["test"]

    def test_persistence(self, db_path):
        """close 後に再度開いてデータが残っている"""
        with ShelveDB(db_path) as db:
            db["persist"] = {"data": 42}
        with ShelveDB(db_path) as db:
            assert db["persist"]["data"] == 42

    def test_not_open_raises(self, db_path):
        """open せずに操作すると RuntimeError"""
        db = ShelveDB(db_path)
        with pytest.raises(RuntimeError):
            _ = db["key"]


# ==================================================
# メモ化
# ==================================================
class TestShelveDBMemo:
    """メモ化キャッシュのテスト"""

    def test_memo_caches(self, db_path):
        """enable_memo 中は同じキーの読み取りがキャッシュされる"""
        with ShelveDB(db_path) as db:
            db["key"] = {"val": 1}
            with db.enable_memo():
                v1 = db.get("key")
                v2 = db.get("key")
                assert v1 == v2
                assert v1["val"] == 1

    def test_memo_cleared_after_exit(self, db_path):
        """enable_memo を抜けるとキャッシュがクリアされる"""
        with ShelveDB(db_path) as db:
            db["key"] = {"val": 1}
            with db.enable_memo():
                _ = db.get("key")
            # メモが無効になった後も正常にアクセスできる
            assert db.get("key")["val"] == 1
