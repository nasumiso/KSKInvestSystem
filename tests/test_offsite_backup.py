"""offsite_backup.py のテスト (tmp_path 上にデータを作る。本番の KS_DATA_DIR には触れない)"""

import zipfile

import pytest

import offsite_backup as ob


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """世代2日分の shelve バックアップと、複製対象のフォルダを持つデータルート"""
    monkeypatch.setattr(ob, "is_dev_data_dir", lambda: False)
    stock_data = tmp_path / "stock_data"
    stock_data.mkdir()
    for name in ob.SHELVE_NAMES:
        for day in ("261008", "261009"):
            for ext in ob.SHELVE_EXTENSIONS:
                (stock_data / f"{name}_{day}{ext}").write_text(f"{name}{day}{ext}")
        (stock_data / f"{name}.dat").write_text("live")  # 稼働中の本体は読まない
    (tmp_path / "trade_history").mkdir()
    (tmp_path / "trade_history" / "fills.csv").write_text("fills")
    (tmp_path / "trade_review").mkdir()
    (tmp_path / "trade_review" / "2026-09.md").write_text("review")
    (tmp_path / "trade_review" / ".trade_review.lock").write_text("")
    (tmp_path / "theme_news" / "history").mkdir(parents=True)
    (tmp_path / "theme_news" / "history" / "2026-10-09.md").write_text("news")
    return tmp_path


def test_run_puts_latest_generation_and_dirs_into_drive_zip(data_dir):
    """最新世代を日付なしの名前で入れ、展開すればそのまま KS_DATA_DIR として使える形にする"""
    drive = data_dir / ob.DRIVE_DIRNAME
    drive.mkdir()
    (drive / ob.ZIP_FILENAME).write_text("前回の zip")

    assert ob.run(str(data_dir)) == 0

    with zipfile.ZipFile(drive / ob.ZIP_FILENAME) as zf:
        contents = {name: zf.read(name).decode() for name in zf.namelist()}
    assert contents == {
        **{
            f"stock_data/{name}{ext}": f"{name}261009{ext}"
            for name in ob.SHELVE_NAMES
            for ext in ob.SHELVE_EXTENSIONS
        },
        "trade_history/fills.csv": "fills",
        "trade_review/2026-09.md": "review",
        "theme_news/history/2026-10-09.md": "news",
    }
    # 一時ファイルを残さない
    assert [p.name for p in drive.iterdir()] == [ob.ZIP_FILENAME]
    assert not (data_dir / (ob.ZIP_FILENAME + ".tmp")).exists()


@pytest.mark.parametrize("case", ["no_drive", "no_generation"])
def test_run_fails_without_touching_previous_zip(data_dir, case):
    """複製先や世代が無ければ 1 を返し、前回の zip を壊さない"""
    drive = data_dir / ob.DRIVE_DIRNAME
    if case == "no_generation":
        drive.mkdir()
        (drive / ob.ZIP_FILENAME).write_text("前回の zip")
        for path in (data_dir / "stock_data").glob("portfolio_shelve_*"):
            path.unlink()

    assert ob.run(str(data_dir)) == 1

    if case == "no_generation":
        assert (drive / ob.ZIP_FILENAME).read_text() == "前回の zip"
    else:
        assert not drive.exists()
