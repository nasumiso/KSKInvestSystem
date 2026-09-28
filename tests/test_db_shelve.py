"""db_shelve.py の ShelveDB CRUD テスト（tmp_path で一時DB作成）"""

import os
import sys
import time
import pytest

import db_shelve
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


# ==================================================
# コンパクション (issue #194)
# ==================================================
class TestCompactShelve:
    """compact_shelve のテスト"""

    def _bloat(self, db_path, records=50, rounds=20):
        """replace_from_dict の繰り返しで .dat にゴミを溜める (本番と同じ肥大パターン)"""
        data = {str(i): {"n": i, "pad": "x" * 500} for i in range(records)}
        with ShelveDB(db_path) as db:
            db.import_from_dict(data)
        for _ in range(rounds):
            with ShelveDB(db_path) as db:
                db.replace_from_dict(data)
        return data

    def test_compact_preserves_records_and_shrinks(self, db_path):
        """全レコードが保たれ、ファイルサイズが減る"""
        data = self._bloat(db_path)
        size_before = db_shelve.get_shelve_size(db_path)

        result = db_shelve.compact_shelve(db_path)

        assert result["record_count"] == len(data)
        assert result["size_after"] < size_before
        with ShelveDB(db_path) as db:
            assert len(db) == len(data)
            for key, value in data.items():
                assert db[key] == value
        # 退避はデフォルトで残さない
        assert not os.path.exists(db_path + ".compact_backup.dat")

    def test_compact_refuses_when_previous_run_was_interrupted(self, db_path):
        """中断の痕跡 (退避) が残っていたら、消さずに停止する

        差し替え途中で電源断すると完全な元DBは退避側にしかない。
        そのまま再実行すると空同然のライブDBを圧縮し、唯一の退避を消してしまう。
        """
        data = {str(i): {"v": i} for i in range(20)}
        with ShelveDB(db_path) as db:
            db.import_from_dict(data)

        # 「退避完了 → 差し替え途中で中断」の状態を作る
        backup = db_path + ".compact_backup"
        db_shelve._move_shelve_files(db_path, backup)
        open(db_path + ".dat", "w").close()  # 不完全なライブ側

        with pytest.raises(RuntimeError, match="中断"):
            db_shelve.compact_shelve(db_path)

        # 退避が消えていないこと (消えるとデータが回復不能になる)
        assert os.path.exists(backup + ".dat")
        db_shelve._move_shelve_files(backup, db_path)
        with ShelveDB(db_path) as db:
            assert db.export_to_dict() == data

    def test_compact_keeps_backup_when_requested(self, db_path):
        """keep_backup=True なら退避が別名で残り、次回実行を妨げない"""
        self._bloat(db_path, records=10, rounds=5)
        db_shelve.compact_shelve(db_path, keep_backup=True)

        base = os.path.basename(db_path)
        kept = [
            f
            for f in os.listdir(os.path.dirname(db_path))
            if f.startswith(base + ".compact_kept_") and f.endswith(".dat")
        ]
        assert len(kept) == 1
        # 中断の印 (.compact_backup) と紛れないこと
        assert not os.path.exists(db_path + ".compact_backup.dat")
        assert db_shelve.compact_shelve(db_path) is not None

    @pytest.mark.parametrize(
        "fail_on",
        [
            "backup",  # ライブDB → 退避 の途中で失敗
            "swap",  # 一時DB → ライブ名 の途中で失敗
        ],
    )
    def test_compact_restores_live_db_on_failure(self, db_path, monkeypatch, fail_on):
        """退避・差し替えのどこで失敗してもライブDBが完全復元される"""
        data = self._bloat(db_path, records=20, rounds=5)

        real_replace = os.replace

        def flaky_replace(src, dst):
            # .dir は最後に動くので、途中で落ちる状況を作れる
            if fail_on == "backup" and ".compact_backup" in str(dst):
                raise OSError("injected backup failure")
            if fail_on == "swap" and ".compact_tmp" in str(src):
                raise OSError("injected swap failure")
            return real_replace(src, dst)

        monkeypatch.setattr(db_shelve.os, "replace", flaky_replace)

        with pytest.raises(OSError):
            db_shelve.compact_shelve(db_path)

        monkeypatch.undo()
        with ShelveDB(db_path) as db:
            assert db.export_to_dict() == data
        assert not os.path.exists(db_path + ".compact_backup.dat")


# ==================================================
# プロセス間ロック (issue #174)
# ==================================================
_READER_OPEN_SRC = """
import sys, time
sys.path.insert(0, %r)
from db_shelve import ShelveDB
path = sys.argv[1]
with ShelveDB(path, read_only=True) as db:
    print("OPENED", flush=True)
    time.sleep(float(sys.argv[2]))   # この間に親が compact を走らせる
    bad = 0
    for k in list(db.keys()):
        try:
            rec = db.get(k)
        except Exception:
            bad += 1
            continue
        if not isinstance(rec, dict) or rec.get("key") != k:
            bad += 1
    print("BAD", bad, flush=True)
"""


class TestShelveDBFlock:
    """compact とその最中の読み取りの排他 (issue #174)。

    いずれも実プロセス並走でのみ再現する (同一プロセスでは GIL に隠れる)。
    なお「日次バッチの書き込み途中を読むと壊れる」問題は dbm.dumb の構造上
    このロックでは塞げないため、ここでは検証しない (SQLite 移行で解決予定)。
    """

    @staticmethod
    def _scripts_dir():
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

    def test_リエントラントとread_onlyのopen_close(self, db_path):
        """同一ロックの入れ子が固まらず、read_only が昇格エラーを出さずに完走する"""
        import fcntl
        with ShelveDB(db_path) as db:
            db["a"] = {"v": 1}
        # 同一パスの入れ子 (research/portfolio が外側で取るのと同じ形)
        with db_shelve._flock(db_path, fcntl.LOCK_EX):
            with ShelveDB(db_path) as db:
                db["b"] = {"v": 2}
        # read_only は open〜close で SH を保持したまま閉じる (EX へ昇格しない)
        with ShelveDB(db_path, read_only=True) as db:
            assert db["a"] == {"v": 1}
        with pytest.raises(RuntimeError):
            with ShelveDB(db_path, read_only=True) as db:
                db["c"] = {"v": 3}

    def test_compact中の書き込みが失われない(self, db_path, tmp_path):
        """compact の export〜swap 中に別プロセスが書いた更新が消えないこと。

        ロック無しでは compact が古いスナップショットで差し替えるため、
        その更新が silent に消える (compact は成功を報告する)。
        """
        import subprocess
        with ShelveDB(db_path) as db:
            db.import_from_dict({"k%d" % i: {"n": i, "pad": "z" * 400} for i in range(300)})
            db["CANARY"] = {"v": "ORIGINAL"}

        src = tmp_path / "w2.py"
        src.write_text(
            "import sys, time\n"
            "sys.path.insert(0, %r)\n" % self._scripts_dir() +
            "from db_shelve import ShelveDB\n"
            "path = sys.argv[1]\n"
            "time.sleep(0.05)\n"
            "with ShelveDB(path) as db:\n"
            "    db['CANARY'] = {'v': 'UPDATED'}\n"
            "    db['NEWKEY'] = {'v': 'ADDED'}\n"
        )
        proc = subprocess.Popen([sys.executable, str(src), db_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        db_shelve.compact_shelve(db_path)
        proc.wait(timeout=30)

        with ShelveDB(db_path, read_only=True) as db:
            assert db.get("CANARY") == {"v": "UPDATED"}
            assert db.get("NEWKEY") == {"v": "ADDED"}

    def test_compact中に開いている読み手が壊れたデータを読まない(self, db_path, tmp_path):
        """open 済みの読み手の索引は古くなる。compact の差し替えを SH で待たせる。

        ロック無しでは 177 回中 176 回が pickle 破損か別レコード混入になる
        (17 回は例外すら出さず他銘柄のデータを返す)。
        断片化 (大→小の上書き) が無いと offset がズレず再現しないので seed に含める。
        """
        import subprocess
        big = {"k%d" % i: {"key": "k%d" % i, "pad": "a" * 4000} for i in range(200)}
        with ShelveDB(db_path) as db:
            db.import_from_dict(big)
        with ShelveDB(db_path) as db:   # 大→小で .dat に穴を開ける
            db.import_from_dict({k: {"key": k, "pad": "b" * 50} for k in big})

        src = tmp_path / "r.py"
        src.write_text(_READER_OPEN_SRC % self._scripts_dir())
        proc = subprocess.Popen([sys.executable, str(src), db_path, "1.5"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        assert proc.stdout.readline().strip() == "OPENED"
        db_shelve.compact_shelve(db_path)
        out = proc.stdout.read()
        proc.wait(timeout=30)
        assert "BAD 0" in out, out

    def test_compact中に開いている書き手がDBを壊さない(self, db_path, tmp_path):
        """書き手が open 済みのまま compact が始まっても破損しないこと。

        書き込みの実データ変更は close 前に起きるため、close 時だけ排他しても
        compact の export 中に .dat が変わる。ロック無しでは 401件中100件が破損した。
        """
        import subprocess
        big = {"k%d" % i: {"key": "k%d" % i, "pad": "a" * 4000} for i in range(200)}
        with ShelveDB(db_path) as db:
            db.import_from_dict(big)
        with ShelveDB(db_path) as db:   # 大→小で .dat に穴を開ける
            db.import_from_dict({k: {"key": k, "pad": "b" * 50} for k in big})

        src = tmp_path / "w.py"
        src.write_text(
            "import sys, time\n"
            "sys.path.insert(0, %r)\n" % self._scripts_dir() +
            "from db_shelve import ShelveDB\n"
            "db = ShelveDB(sys.argv[1]).open()\n"
            "print('OPENED', flush=True)\n"
            "time.sleep(0.5)\n"
            "for i in range(150):\n"
            "    db['k%d' % i] = {'key': 'k%d' % i, 'pad': 'Z' * 3000}\n"
            "db.close()\n"
        )
        proc = subprocess.Popen([sys.executable, str(src), db_path],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        assert proc.stdout.readline().strip() == "OPENED"
        db_shelve.compact_shelve(db_path)
        proc.wait(timeout=30)

        with ShelveDB(db_path, read_only=True) as db:
            for k in db.keys():
                assert db.get(k)["key"] == k
