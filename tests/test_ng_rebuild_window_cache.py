import unittest
from unittest.mock import patch

from mssql_mcp_server.cs_tools.ng_window import ng_rebuild_window_cache


class FakeCursor:
    def __init__(self, restore_error=None):
        self.restore_error = restore_error
        self.fetchone_value = None
        self.statements = []
        self.jsonsave_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, *params):
        self.statements.append((sql, params))
        if sql.startswith("select top 1"):
            self.fetchone_value = (17, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 20, "main")
        elif "exec dbo.csNGAppWindowDataSetsJSONSave" in sql:
            self.jsonsave_count += 1
            self.fetchone_value = (
                self.restore_error if self.jsonsave_count == 2 else None,
            )
        elif sql.startswith("select d.pageSize"):
            self.fetchone_value = (20, 1, 1000, 1000)
        else:
            self.fetchone_value = None
        return self

    def fetchone(self):
        return self.fetchone_value


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def close(self):
        self.closed = True


class RebuildWindowCacheTests(unittest.TestCase):
    def test_rebuild_toggles_and_restores_in_one_transaction(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)

        with patch("mssql_mcp_server.cs_tools.ng_window.connect", return_value=connection):
            result = ng_rebuild_window_cache("connection", "csCustomersAddresses")

        self.assertIn("OK: rebuilt dataSets cache", result)
        self.assertTrue(connection.closed)
        sql = [statement for statement, _params in cursor.statements]
        self.assertEqual(sql[0], "begin transaction")
        self.assertEqual(sql[-1], "commit transaction")
        payloads = [params[0] for statement, params in cursor.statements
                    if "exec dbo.csNGAppWindowDataSetsJSONSave" in statement]
        self.assertIn('"pageSize": 21', payloads[0])
        self.assertIn('"pageSize": 20', payloads[1])

    def test_restore_failure_rolls_back(self):
        cursor = FakeCursor(restore_error="validation error")
        connection = FakeConnection(cursor)

        with patch("mssql_mcp_server.cs_tools.ng_window.connect", return_value=connection):
            result = ng_rebuild_window_cache("connection", "csCustomersAddresses")

        self.assertIn("rolled back", result)
        sql = [statement for statement, _params in cursor.statements]
        self.assertIn("if @@trancount > 0 rollback transaction", sql)
        self.assertNotIn("commit transaction", sql)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
