from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy.engine import URL

from app.database import engine_url_and_connect_args


class DatabaseTLSConfigurationTests(unittest.TestCase):
    def test_managed_mysql_json_ssl_is_converted_to_strict_pymysql_options(self):
        original = URL.create(
            "mysql+pymysql",
            username="user",
            password="test-only-password",
            host="db.example.test",
            database="family",
            query={"ssl": json.dumps({"rejectUnauthorized": False})},
        )
        with patch.dict(os.environ, {"DATABASE_URL": original.render_as_string(hide_password=False)}):
            url, connect_args = engine_url_and_connect_args()
        self.assertNotIn("ssl", url.query)
        self.assertEqual(connect_args["ssl"]["verify_mode"], "required")
        self.assertIs(connect_args["ssl"]["check_hostname"], True)

    def test_sqlite_has_no_mysql_tls_options(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///./test.db"}):
            url, connect_args = engine_url_and_connect_args()
        self.assertEqual(url.get_backend_name(), "sqlite")
        self.assertEqual(connect_args, {})


if __name__ == "__main__":
    unittest.main()
