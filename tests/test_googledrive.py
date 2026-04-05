"""
googledrive モジュールのテスト

- _col_num_to_letter: 列番号→列記号変換
- upload_csv_async: SHEETS_CONFIG による分岐
- upload_csv_via_sheets: Sheets API 呼び出しフロー（モック）
"""

from unittest.mock import patch, MagicMock, mock_open
import csv
import io


class TestColNumToLetter:
    """列番号→列記号変換のテスト"""

    def test_single_letters(self):
        from googledrive import _col_num_to_letter
        assert _col_num_to_letter(1) == "A"
        assert _col_num_to_letter(2) == "B"
        assert _col_num_to_letter(26) == "Z"

    def test_double_letters(self):
        from googledrive import _col_num_to_letter
        assert _col_num_to_letter(27) == "AA"
        assert _col_num_to_letter(28) == "AB"
        assert _col_num_to_letter(52) == "AZ"
        assert _col_num_to_letter(53) == "BA"
        assert _col_num_to_letter(702) == "ZZ"

    def test_triple_letters(self):
        from googledrive import _col_num_to_letter
        assert _col_num_to_letter(703) == "AAA"


class TestUploadCsvAsyncDispatch:
    """upload_csv_async の分岐ロジックテスト"""

    @patch("googledrive._upload_with_lock")
    def test_sheets_config_target_uses_sheets_api(self, mock_lock):
        """SHEETS_CONFIG に含まれる場合は upload_csv_via_sheets が使われる"""
        from googledrive import upload_csv_async, upload_csv_via_sheets
        t = upload_csv_async("dummy.csv", "shintakane_result")
        t.join(timeout=5)
        mock_lock.assert_called_once()
        args = mock_lock.call_args[0]
        assert args[0] is upload_csv_via_sheets
        assert args[1] == "dummy.csv"
        assert args[2] == "shintakane_result"

    @patch("googledrive._upload_with_lock")
    def test_non_sheets_target_uses_drive_api(self, mock_lock):
        """SHEETS_CONFIG に含まれない場合は upload_csv が使われる"""
        from googledrive import upload_csv_async, upload_csv
        t = upload_csv_async("dummy.csv", "market_data")
        t.join(timeout=5)
        mock_lock.assert_called_once()
        args = mock_lock.call_args[0]
        assert args[0] is upload_csv
        assert args[1] == "dummy.csv"
        assert args[2] == "market_data"


class TestUploadCsvViaSheets:
    """upload_csv_via_sheets の Sheets API 呼び出しフローテスト（モック）"""

    def _make_csv_content(self, rows):
        """テスト用CSV文字列を生成"""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(rows)
        return buf.getvalue()

    @patch("googledrive.get_sheets_service")
    def test_basic_update_flow(self, mock_get_service):
        """基本的な更新フロー: get → update → 余剰なしでclearスキップ"""
        from googledrive import upload_csv_via_sheets

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        # spreadsheets().get() のレスポンス
        test_rows = [["code", "name"], ["1234", "テスト銘柄"]]
        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {
                "title": "shintakane_result",
                "gridProperties": {"rowCount": 2, "columnCount": 2}
            }}]
        }

        csv_content = self._make_csv_content(test_rows)
        m = mock_open(read_data=csv_content)
        with patch("builtins.open", m):
            upload_csv_via_sheets("test.csv", "shintakane_result")

        # values().update() が呼ばれたことを確認
        mock_service.spreadsheets().values().update.assert_called_once()
        call_kwargs = mock_service.spreadsheets().values().update.call_args[1]
        assert call_kwargs["valueInputOption"] == "USER_ENTERED"
        assert call_kwargs["range"] == "'shintakane_result'!A1"

    @patch("googledrive.get_sheets_service")
    def test_clears_excess_rows(self, mock_get_service):
        """前回より行数が減った場合、余剰行をクリアする"""
        from googledrive import upload_csv_via_sheets

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        # 前回5行、今回2行
        test_rows = [["code", "name"], ["1234", "テスト"]]
        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {
                "title": "shintakane_result",
                "gridProperties": {"rowCount": 5, "columnCount": 2}
            }}]
        }

        csv_content = self._make_csv_content(test_rows)
        m = mock_open(read_data=csv_content)
        with patch("builtins.open", m):
            upload_csv_via_sheets("test.csv", "shintakane_result")

        # clear が呼ばれ、行3〜5がクリア対象
        clear_calls = mock_service.spreadsheets().values().clear.call_args_list
        assert len(clear_calls) >= 1
        clear_kwargs = clear_calls[0][1]
        assert "A3" in clear_kwargs["range"]

    @patch("googledrive.get_sheets_service")
    def test_raises_on_missing_tab(self, mock_get_service):
        """対象タブが見つからない場合 ValueError を送出"""
        import pytest
        from googledrive import upload_csv_via_sheets

        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {
                "title": "別のシート",
                "gridProperties": {"rowCount": 10, "columnCount": 10}
            }}]
        }

        csv_content = self._make_csv_content([["a"]])
        m = mock_open(read_data=csv_content)
        with patch("builtins.open", m):
            with pytest.raises(ValueError, match="タブ 'shintakane_result' が見つかりません"):
                upload_csv_via_sheets("test.csv", "shintakane_result")
