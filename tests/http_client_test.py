import datetime
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from gspread.cell import Cell
from gspread.exceptions import APIError
from gspread.http_client import BackOffHTTPClient, HTTPClient
from gspread.utils import ValueInputOption
from gspread.worksheet import Worksheet


class HTTPClientSerializerTest(TestCase):
    def _make_client(self):
        session = Mock()
        session.request.return_value.ok = True  # so request() returns, doesn't raise
        return HTTPClient(auth=None, session=session), session

    def test_default_uses_json_kwarg(self):
        """Without default_serializer, the body goes out via json= (unchanged behavior)."""
        client, session = self._make_client()
        body = {"values": [[1, 2, 3]]}

        client.request("post", "http://example.com", json=body)

        _, kwargs = session.request.call_args
        self.assertEqual(kwargs["json"], body)
        self.assertIsNone(kwargs["data"])

    def test_default_serializer_uses_data_and_header(self):
        """With default_serializer, the body is encoded into data= with a JSON header."""
        client, session = self._make_client()
        body = {"values": [[1, 2, 3]]}

        client.request("post", "http://example.com", json=body, default_serializer=str)

        _, kwargs = session.request.call_args
        self.assertIsNone(kwargs["json"])
        self.assertEqual(kwargs["data"], json.dumps(body, default=str))
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")

    def test_default_serializer_handles_non_native_types(self):
        """The actual use case from the issue: encode a date the stdlib can't."""
        client, session = self._make_client()
        body = {"values": [[datetime.date(2026, 6, 19)]]}

        client.request(
            "post",
            "http://example.com",
            json=body,
            default_serializer=lambda o: o.isoformat(),
        )

        _, kwargs = session.request.call_args
        self.assertIn("2026-06-19", kwargs["data"])


class WorksheetForwardsSerializerTest(TestCase):
    """default_serializer must reach the client from each write method."""

    MARKER = staticmethod(lambda o: str(o))

    def _make_worksheet(self):
        client = HTTPClient(auth=None, session=Mock())
        client.values_update = Mock()
        client.values_append = Mock()
        client.values_batch_update = Mock()
        properties = {
            "title": "Sheet1",
            "gridProperties": {"rowCount": 5, "columnCount": 5},
        }
        worksheet = Worksheet(
            spreadsheet=None,
            properties=properties,
            spreadsheet_id="abc123",
            client=client,
        )
        return worksheet, client

    def test_update_forwards_serializer(self):
        worksheet, client = self._make_worksheet()
        worksheet.update([[1, 2, 3]], "A1", default_serializer=self.MARKER)
        self.assertIs(
            client.values_update.call_args.kwargs["default_serializer"], self.MARKER
        )

    def test_batch_update_forwards_serializer(self):
        worksheet, client = self._make_worksheet()
        worksheet.batch_update(
            [{"range": "A1", "values": [[1]]}], default_serializer=self.MARKER
        )
        self.assertIs(
            client.values_batch_update.call_args.kwargs["default_serializer"],
            self.MARKER,
        )

    def test_append_rows_forwards_serializer(self):
        worksheet, client = self._make_worksheet()
        worksheet.append_rows([[1, 2, 3]], default_serializer=self.MARKER)
        # values_append receives default_serializer as the last positional arg
        self.assertIs(client.values_append.call_args.args[4], self.MARKER)

    def test_append_row_forwards_serializer(self):
        worksheet, client = self._make_worksheet()
        worksheet.append_row([1, 2, 3], default_serializer=self.MARKER)
        self.assertIs(client.values_append.call_args.args[4], self.MARKER)


class BackOffHTTPClientTest(TestCase):
    @staticmethod
    def _error_response(code, domain="global"):
        response = Mock()
        response.ok = False
        response.text = "request failed"
        response.json.return_value = {
            "error": {
                "code": code,
                "message": "request failed",
                "errors": [{"domain": domain}],
            }
        }
        return response

    def test_terminal_error_does_not_increase_next_retry_delay(self):
        session = Mock()
        success = Mock(ok=True)
        session.request.side_effect = [
            self._error_response(403),
            self._error_response(500),
            success,
        ]
        client = BackOffHTTPClient(auth=None, session=session)

        with self.assertRaises(APIError):
            client.request("get", "http://example.com/forbidden")

        with patch("gspread.http_client.time.sleep") as sleep:
            self.assertIs(
                client.request("get", "http://example.com/retryable"), success
            )

        sleep.assert_called_once_with(2)

    def test_gives_up_after_max_backoff(self):
        # A permanently failing retryable request must stop once the backoff
        # reaches the maximum, instead of retrying without bound.
        session = Mock()
        session.request.return_value = self._error_response(429)
        client = BackOffHTTPClient(auth=None, session=session)

        with patch("gspread.http_client.time.sleep"):
            with self.assertRaises(APIError):
                client.request("get", "http://example.com/rate-limited")

        # 2 ** n stops being <= _MAX_BACKOFF (128) once n reaches 8, i.e. after
        # the initial call plus 7 retries.
        self.assertEqual(session.request.call_count, 8)

    def test_gives_up_after_max_backoff_on_usage_limits(self):
        # A 403 usageLimits error is retryable too, so it must also stop once
        # the backoff reaches the maximum rather than retrying without bound.
        session = Mock()
        session.request.return_value = self._error_response(403, domain="usageLimits")
        client = BackOffHTTPClient(auth=None, session=session)

        with patch("gspread.http_client.time.sleep"):
            with self.assertRaises(APIError):
                client.request("get", "http://example.com/usage-limits")

        # Same bound as the other retryable errors: initial call plus 7 retries.
        self.assertEqual(session.request.call_count, 8)


class ValuesBatchUpdateBytesTest(TestCase):
    def _make_client(self):
        session = Mock()
        session.request.return_value.ok = True
        return HTTPClient(auth=None, session=session), session

    def test_sends_payload_verbatim(self):
        client, session = self._make_client()
        payload = b'{"valueInputOption":"RAW","data":[]}'

        client.values_batch_update_bytes("abc123", payload)

        args, kwargs = session.request.call_args
        self.assertEqual(kwargs["method"], "post")
        self.assertIn("abc123", kwargs["url"])
        self.assertIsNone(kwargs["json"])
        self.assertEqual(kwargs["data"], payload)
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(session.request.call_count, 1)


class UpdateCellsByteLimitTest(TestCase):
    """update_cells(..., max_bytes=...) splits cells and sends one compact
    values.batchUpdate request."""

    def _make_worksheet(self):
        client = HTTPClient(auth=None, session=Mock())
        client.values_update = Mock()
        client.values_batch_update_bytes = Mock()
        properties = {
            "title": "Sheet1",
            "gridProperties": {"rowCount": 1000, "columnCount": 30},
        }
        worksheet = Worksheet(
            spreadsheet=None,
            properties=properties,
            spreadsheet_id="abc123",
            client=client,
        )
        return worksheet, client

    def _payload(self, client):
        self.assertEqual(client.values_batch_update_bytes.call_count, 1)
        args, _ = client.values_batch_update_bytes.call_args
        self.assertEqual(args[0], "abc123")
        payload = args[1]
        self.assertIsInstance(payload, bytes)
        # compact JSON: no spaces after separators
        self.assertNotIn(b'": ', payload)
        self.assertNotIn(b", ", payload)
        return payload

    def test_scattered_cells_split_into_blocks(self):
        worksheet, client = self._make_worksheet()
        cells = [
            Cell(1, 1, "top"),
            Cell(500, 3, "bottom"),
        ]

        worksheet.update_cells(cells, max_bytes=100000)

        body = json.loads(self._payload(client))
        self.assertEqual(body["valueInputOption"], "RAW")
        self.assertEqual(
            body["data"],
            [
                {"range": "'Sheet1'!A1:A1", "values": [["top"]]},
                {"range": "'Sheet1'!C500:C500", "values": [["bottom"]]},
            ],
        )
        client.values_update.assert_not_called()

    def test_block_uses_minimal_bounding_rect_with_null_gaps(self):
        worksheet, client = self._make_worksheet()
        cells = [
            Cell(1, 1, "a"),
            Cell(1, 3, "c"),
            Cell(2, 2, ""),
        ]

        worksheet.update_cells(cells, max_bytes=100000)

        body = json.loads(self._payload(client))
        # one block: rows 1-2, columns A-C; empty positions are null,
        # the empty string is kept (clears the cell)
        self.assertEqual(
            body["data"],
            [
                {
                    "range": "'Sheet1'!A1:C2",
                    "values": [["a", None, "c"], [None, "", None]],
                }
            ],
        )

    def test_value_input_option_user_entered(self):
        worksheet, client = self._make_worksheet()
        cells = [Cell(1, 1, "=1+1")]

        worksheet.update_cells(
            cells,
            value_input_option=ValueInputOption.user_entered,
            max_bytes=100000,
        )

        body = json.loads(self._payload(client))
        self.assertEqual(body["valueInputOption"], "USER_ENTERED")

    def test_duplicate_coordinates_last_value_wins(self):
        worksheet, client = self._make_worksheet()
        cells = [
            Cell(1, 1, "first"),
            Cell(1, 1, "last"),
        ]

        worksheet.update_cells(cells, max_bytes=100000)

        body = json.loads(self._payload(client))
        self.assertEqual(
            body["data"], [{"range": "'Sheet1'!A1:A1", "values": [["last"]]}]
        )

    def test_exceeding_limit_raises_and_sends_nothing(self):
        worksheet, client = self._make_worksheet()
        cells = [Cell(1, 1, "a"), Cell(2, 1, "b")]

        with self.assertRaises(ValueError):
            worksheet.update_cells(cells, max_bytes=10)

        client.values_batch_update_bytes.assert_not_called()
        client.values_update.assert_not_called()

    def test_limit_is_checked_against_exact_utf8_body_size(self):
        worksheet, client = self._make_worksheet()
        cells = [Cell(1, 1, "日本語")]

        worksheet.update_cells(cells, max_bytes=100000)
        payload = self._payload(client)
        # 3 non-ASCII chars take 9 UTF-8 bytes, counted (and sent) as-is
        self.assertIn("日本語".encode("utf-8"), payload)

        exact = len(payload)
        worksheet2, client2 = self._make_worksheet()
        worksheet2.update_cells([Cell(1, 1, "日本語")], max_bytes=exact)
        self.assertEqual(client2.values_batch_update_bytes.call_count, 1)

        worksheet3, client3 = self._make_worksheet()
        with self.assertRaises(ValueError):
            worksheet3.update_cells([Cell(1, 1, "日本語")], max_bytes=exact - 1)
        client3.values_batch_update_bytes.assert_not_called()

    def test_empty_cell_list_raises(self):
        worksheet, client = self._make_worksheet()

        with self.assertRaises(ValueError):
            worksheet.update_cells([], max_bytes=100000)

        client.values_batch_update_bytes.assert_not_called()

    def test_default_mode_unchanged(self):
        worksheet, client = self._make_worksheet()
        cells = [Cell(1, 1, "a"), Cell(2, 2, "b")]

        worksheet.update_cells(cells)

        self.assertEqual(client.values_update.call_count, 1)
        client.values_batch_update_bytes.assert_not_called()
