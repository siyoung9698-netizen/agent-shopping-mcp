"""Secret-free diagnostics for HTTP /mcp tools/call 401s.

Every credential in this module is a synthetic canary. The tests do not read
operational secrets and do not call Naver or FoodSafetyKorea.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import functional_food
import metering
import server
from mcp.types import CallToolResult

VALID_BEARER = "SYNTH_BEARER_KEY_9f3c2a7e"
VALID_XAPI = "SYNTH_XAPI_KEY_1b6d4c8a"
EXPIRED = "SYNTH_EXPIRED_KEY_0e5a7d2c"
WRONG_BEARER = "SYNTH_WRONG_BEARER_4c8b1f6d"
WRONG_XAPI = "SYNTH_WRONG_XAPI_7a2e9c0b"
WRONG_QUERY = "SYNTH_WRONG_QUERY_3d5f8a1c"
SCHEME_CANARY = "SYNTH_SCHEME_c0ffee\r\nINJECTED_AUTH_LOG"
TOOL_CANARY = "SYNTH_TOOL_deadbeef\nINJECTED_TOOL_LOG"
BODY_CANARY = "SYNTH_BODY_param_aa11bb22"
URL_CANARY = "https://user:SYNTH_URL_SECRET_ee99@example.test/path?x=1"
DATE_CANARY = "SYNTH_DATE_Tue, 01 Jan 2099 00:00:00 GMT"
RPC_CANARY = "SYNTH_RPC_ID_SHOULD_NOT_LOG"
BOOL_CANARY = "SYNTH_BOOL_CANARY"
SOURCE_CANARY = "SYNTH_SOURCE_CANARY\nINJECTED_SOURCE_LOG"
ERROR_CANARY = "not_an_auth_error\nSYNTH_ERROR_INJECT"
TAB_CANARY = "SYNTH_TAB_TOKEN_bb77"
EMIT_BOOM = "SYNTH_EMIT_BOOM_should_not_leak"
DESCRIBE_BOOM = "SYNTH_DESCRIBE_BOOM_should_not_leak"

ALL_CANARIES = (
    VALID_BEARER,
    VALID_XAPI,
    EXPIRED,
    WRONG_BEARER,
    WRONG_XAPI,
    WRONG_QUERY,
    SCHEME_CANARY,
    TOOL_CANARY,
    BODY_CANARY,
    URL_CANARY,
    DATE_CANARY,
    RPC_CANARY,
    BOOL_CANARY,
    SOURCE_CANARY,
    ERROR_CANARY,
    TAB_CANARY,
    EMIT_BOOM,
    DESCRIBE_BOOM,
    "INJECTED_AUTH_LOG",
    "INJECTED_TOOL_LOG",
    "INJECTED_SOURCE_LOG",
    "SYNTH_URL_SECRET_ee99",
)

CHALLENGE = {
    "missing_api_key": (
        'Bearer realm="agent-shopping", error="invalid_request", '
        'error_description="API key required"'
    ),
    "invalid_api_key": (
        'Bearer realm="agent-shopping", error="invalid_token", '
        'error_description="API key is invalid"'
    ),
    "expired_api_key": (
        'Bearer realm="agent-shopping", error="invalid_token", '
        'error_description="API key expired"'
    ),
}

LOG_RECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.output = []
        self.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))

    def emit(self, record):
        self.records.append(record)
        self.output.append(self.format(record))


@contextmanager
def _capture_root():
    handler = _ListHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


def _forbid_upstream(*_args, **_kwargs):
    raise AssertionError("live upstream")


def _rpc(client, method, params=None, headers=None, query=""):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    hdrs = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if headers:
        hdrs.update(headers)
    return client.post("/mcp" + query, json=payload, headers=hdrs)


def _none_selection() -> metering.AuthSelection:
    return metering.AuthSelection(
        metering.AUTH_SOURCE_NONE,
        metering.AUTHORIZATION_SCHEME_ABSENT,
        False,
    )


class AuthDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "usage.db")
        names = (
            "MCP_API_KEYS",
            "MCP_MONTHLY_QUOTA",
            "MCP_KEY_QUOTAS",
            "MCP_USAGE_DB_PATH",
            "MCP_API_KEY",
            "NAVER_CLIENT_ID",
            "NAVER_CLIENT_SECRET",
            "FOODSAFETYKOREA_API_KEY",
        )
        self.previous = {name: os.environ.get(name) for name in names}
        os.environ["MCP_API_KEYS"] = f"{VALID_BEARER},{VALID_XAPI},{EXPIRED}"
        os.environ["MCP_MONTHLY_QUOTA"] = "100"
        os.environ.pop("MCP_KEY_QUOTAS", None)
        os.environ["MCP_USAGE_DB_PATH"] = self.db_path
        os.environ.pop("MCP_API_KEY", None)
        os.environ["NAVER_CLIENT_ID"] = ""
        os.environ["NAVER_CLIENT_SECRET"] = ""
        os.environ.pop("FOODSAFETYKOREA_API_KEY", None)
        self.prices = server.DB_PATH
        self.naver = (server.NAVER_ID, server.NAVER_SECRET)
        self.affiliate = server.AFFILIATE_ID
        self.naver_search = server.naver_search
        self.fetch = functional_food.fetch_i0030
        self.httpx_level = logging.getLogger("httpx").level
        self.httpcore_level = logging.getLogger("httpcore").level
        # TestClient is an httpx client. Its request log is not the diagnostic.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        server.DB_PATH = os.path.join(self.tmp.name, "prices.db")
        server.NAVER_ID = ""
        server.NAVER_SECRET = ""
        server.AFFILIATE_ID = ""
        server.naver_search = _forbid_upstream
        functional_food.fetch_i0030 = _forbid_upstream
        self._install_meter()
        server.mcp._session_manager = None
        self.app = server.mcp.streamable_http_app()

    def tearDown(self):
        server.DB_PATH = self.prices
        server.NAVER_ID, server.NAVER_SECRET = self.naver
        server.AFFILIATE_ID = self.affiliate
        server.naver_search = self.naver_search
        functional_food.fetch_i0030 = self.fetch
        logging.getLogger("httpx").setLevel(self.httpx_level)
        logging.getLogger("httpcore").setLevel(self.httpcore_level)
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _install_meter(self, quotas=None):
        metering.reset_meter(
            self.db_path,
            quotas or {VALID_BEARER: 100, VALID_XAPI: 100, EXPIRED: 100},
            expires_at={EXPIRED: datetime(2020, 1, 1, tzinfo=timezone.utc)},
        )

    def _client(self, app=None):
        from starlette.testclient import TestClient

        return TestClient(app or self.app, base_url="http://127.0.0.1:8765")

    def _used(self, key: str) -> int:
        return metering.get_meter().inspect(key).calls_used

    def _counter_rows(self) -> int:
        with sqlite3.connect(self.db_path) as con:
            return con.execute("select count(*) from usage_counters").fetchone()[0]

    def _price_rows(self) -> int:
        if not os.path.exists(server.DB_PATH):
            return 0
        con = sqlite3.connect(server.DB_PATH)
        try:
            return con.execute("select count(*) from price_history").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            con.close()

    def _assert_no_canaries(self, parts, secrets):
        text = "\n".join(parts)
        for secret in secrets:
            self.assertNotIn(secret, text)
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            self.assertNotIn(digest, text)
            self.assertNotIn(digest[:16], text)
            self.assertNotIn(metering.key_fingerprint(secret), text)
            self.assertNotIn(metering.public_key_id(secret), text)
            self.assertNotIn(f'"length":{len(secret)}', text)
            self.assertNotIn(f'"key_length":{len(secret)}', text)
        for forbidden in (
            "api_key=",
            "Authorization",
            "X-API-Key",
            "Bearer ",
            "MCP_",
            "FOODSAFETY",
            "NAVER_",
            "dev-demo-key",
            "https://",
            "http://",
            "sha256",
            "key_id",
            "INJECTED_",
            "query_string",
            "request_id",
        ):
            self.assertNotIn(forbidden, text)

    def _record_parts(self, record, formatted: str) -> list[str]:
        exc_view = logging.Formatter("%(message)s|exc=%(exc_text)s|args=%(args)s")
        timed = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        return [
            formatted,
            record.getMessage(),
            exc_view.format(record),
            timed.format(record),
            repr(record.args),
            repr(record.exc_info),
            repr(record.exc_text),
            repr(record.stack_info),
            repr(record.msg),
        ]

    def _assert_record(self, record, formatted: str, secrets):
        self.assertEqual(record.name, server.AUTH_DIAGNOSTIC_LOGGER_NAME)
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(record.args, ())
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(record.stack_info)
        self.assertEqual(record.funcName, "emit_auth_rejection")
        self.assertTrue(str(record.pathname).endswith("server.py"))
        self.assertLessEqual(set(record.__dict__), LOG_RECORD_KEYS)
        line = record.getMessage()
        self.assertEqual(
            formatted,
            f"WARNING:{server.AUTH_DIAGNOSTIC_LOGGER_NAME}:{line}",
        )
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\n", formatted)
        self.assertNotIn("\r", formatted)
        payload = json.loads(line)
        self.assertEqual(list(payload), list(server.AUTH_REJECTION_LOG_FIELDS))
        self.assertEqual(payload["event"], server.AUTH_REJECTION_EVENT)
        self.assertIn(payload["tool"], set(server.AUTH_DIAGNOSTIC_TOOLS) | {server.UNKNOWN_AUTH_TOOL})
        self.assertIn(payload["error"], set(metering.AUTH_FAILURE_CODES))
        self.assertIn(payload["auth_source"], set(metering.AUTH_SOURCES))
        self.assertIn(payload["authorization_scheme"], set(metering.AUTHORIZATION_SCHEMES))
        self.assertIn(payload["x_api_key_present"], (True, False))
        self.assertRegex(payload["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(line, json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        for key, value in payload.items():
            if key == "x_api_key_present":
                self.assertIs(type(value), bool)
            else:
                self.assertIs(type(value), str)
            lowered = key.lower()
            for banned in ("hash", "length", "len", "secret", "header", "query", "url", "body", "token"):
                self.assertNotIn(banned, lowered)
        self._assert_no_canaries(self._record_parts(record, formatted), secrets)
        return payload

    def _server_log_parts(self, root) -> list[str]:
        """Logs other than the TestClient's httpx access line."""
        parts = []
        for record, formatted in zip(root.records, root.output):
            if record.name == "httpx" or record.name.startswith("httpcore"):
                continue
            parts.append(formatted)
            parts.append(record.getMessage())
            parts.append(repr(record.args))
            parts.append(repr(record.exc_info))
            parts.append(repr(record.exc_text))
            parts.append(repr(record.stack_info))
        return parts

    def _assert_logs(self, caught, root, secrets, count=1):
        self.assertEqual(len(caught.records), count)
        self.assertEqual(len(caught.output), count)
        payloads = []
        parts = self._server_log_parts(root)
        for record, formatted in zip(caught.records, caught.output):
            self.assertEqual(record.name, server.AUTH_DIAGNOSTIC_LOGGER_NAME)
            payloads.append(self._assert_record(record, formatted, secrets))
            parts.extend(self._record_parts(record, formatted))
        self._assert_no_canaries(parts, secrets)
        return payloads

    def _assert_401(self, response, code: str, tool: str, presented_key, secrets):
        self.assertEqual(response.status_code, 401)
        failure = metering.get_meter().auth_failure(presented_key)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, code)
        self.assertEqual(response.json(), {**failure.payload, "tool": tool})
        self.assertEqual(response.headers["www-authenticate"], CHALLENGE[code])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("application/json", response.headers["content-type"])
        self.assertNotIn(server.AUTH_REJECTION_EVENT, response.text)
        self.assertNotIn("auth_source", response.text)
        self.assertNotIn("authorization_scheme", response.text)
        for secret in secrets:
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, response.headers["www-authenticate"])

    def test_diagnostic_logger_has_no_handler_of_its_own(self):
        logger = logging.getLogger(server.AUTH_DIAGNOSTIC_LOGGER_NAME)
        self.assertEqual(logger.handlers, [])
        self.assertTrue(logger.propagate)
        self.assertEqual(logger.level, logging.WARNING)
        self.assertNotEqual(logging.getLogger().level, logging.DEBUG)

    def test_logged_tool_names_are_exactly_the_key_gated_tools(self):
        gated = {
            name
            for name, obj in vars(server).items()
            if getattr(obj, "_requires_api_key", False)
        }
        self.assertEqual(gated, set(server.AUTH_DIAGNOSTIC_TOOLS))
        self.assertNotIn(server.UNKNOWN_AUTH_TOOL, gated)
        params = inspect.signature(server.emit_auth_rejection).parameters
        self.assertEqual(list(params), ["tool_name", "error_code", "selection", "clock"])
        self.assertEqual(params["clock"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(
            set(metering.AuthSelection.__dataclass_fields__),
            {"auth_source", "authorization_scheme", "x_api_key_present"},
        )

    def test_extract_precedence_keeps_an_invalid_bearer(self):
        headers = {
            "AuThOrIzAtIoN": f"BeArEr {WRONG_BEARER}",
            "X-API-Key": VALID_XAPI,
        }
        query = {"api_key": VALID_BEARER, "next": URL_CANARY}
        self.assertEqual(
            metering.extract_api_key(headers=headers, query_params=query),
            WRONG_BEARER,
        )
        facts = metering.describe_auth_selection(headers=headers, query_params=query)
        self.assertEqual(facts.auth_source, "bearer")
        self.assertEqual(facts.authorization_scheme, "bearer")
        self.assertIs(facts.x_api_key_present, True)
        self.assertNotIn(WRONG_BEARER, repr(facts))
        self.assertNotIn(VALID_XAPI, repr(facts))
        self.assertNotIn(VALID_BEARER, repr(facts))
        self.assertNotIn(URL_CANARY, repr(facts))

        non_bearer = {"Authorization": f"Basic {SCHEME_CANARY}", "X-API-Key": VALID_XAPI}
        self.assertEqual(
            metering.extract_api_key(headers=non_bearer, query_params=query),
            VALID_XAPI,
        )
        fell_through = metering.describe_auth_selection(headers=non_bearer, query_params=query)
        self.assertEqual(fell_through.auth_source, "x-api-key")
        self.assertEqual(fell_through.authorization_scheme, "non_bearer")
        self.assertIs(fell_through.x_api_key_present, True)
        self.assertNotIn(SCHEME_CANARY, repr(fell_through))
        self.assertNotIn("INJECTED_AUTH_LOG", repr(fell_through))

        blank = {"Authorization": "Bearer   ", "X-API-Key": "   "}
        self.assertEqual(
            metering.extract_api_key(headers=blank, query_params={"api_key": WRONG_QUERY}),
            WRONG_QUERY,
        )
        query_facts = metering.describe_auth_selection(
            headers=blank,
            query_params={"api_key": WRONG_QUERY},
        )
        self.assertEqual(query_facts.auth_source, "query")
        self.assertEqual(query_facts.authorization_scheme, "bearer")
        self.assertIs(query_facts.x_api_key_present, False)
        self.assertNotIn(WRONG_QUERY, repr(query_facts))

        tabbed = {"Authorization": f"Bearer\t{TAB_CANARY}"}
        self.assertIsNone(metering.extract_api_key(headers=tabbed, query_params=None))
        tab_facts = metering.describe_auth_selection(headers=tabbed, query_params=None)
        self.assertEqual(tab_facts.auth_source, "none")
        self.assertEqual(tab_facts.authorization_scheme, "non_bearer")
        self.assertNotIn(TAB_CANARY, repr(tab_facts))

        self.assertIsNone(metering.extract_api_key(headers=None, query_params=None))
        empty = metering.describe_auth_selection(headers=None, query_params=None)
        self.assertEqual(empty.auth_source, "none")
        self.assertEqual(empty.authorization_scheme, "absent")
        self.assertIs(empty.x_api_key_present, False)

    def test_emitter_writes_the_formatted_record_in_utc(self):
        selection = metering.AuthSelection("query", "absent", False)
        when = datetime(2026, 11, 8, 15, 26, 45, 999999, tzinfo=timezone(timedelta(hours=9)))
        with _capture_root() as root:
            with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                server.emit_auth_rejection(
                    "get_usage",
                    "expired_api_key",
                    selection,
                    clock=lambda: when,
                )
        payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
        self.assertEqual(payload["timestamp"], "2026-11-08T06:26:45Z")
        self.assertEqual(payload["tool"], "get_usage")
        self.assertEqual(payload["error"], "expired_api_key")
        self.assertEqual(payload["auth_source"], "query")
        self.assertEqual(payload["authorization_scheme"], "absent")
        self.assertIs(payload["x_api_key_present"], False)
        line = caught.records[0].getMessage()
        self.assertEqual(
            caught.output[0],
            "WARNING:agent_shopping.auth:" + line,
        )

        naive = datetime(2026, 1, 2, 3, 4, 5)
        with _capture_root() as root:
            with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                server.emit_auth_rejection(
                    "search_products",
                    "missing_api_key",
                    _none_selection(),
                    clock=lambda: naive,
                )
        payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
        self.assertEqual(payload["timestamp"], "2026-01-02T03:04:05Z")

        before = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
        with _capture_root() as root:
            with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                server.emit_auth_rejection(
                    "find_pet_food",
                    "invalid_api_key",
                    _none_selection(),
                )
        payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
        logged = datetime.strptime(payload["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(logged, before)
        self.assertLessEqual(logged, datetime.now(timezone.utc) + timedelta(seconds=2))

    def test_emitter_sanitizes_untrusted_fields_and_drops_other_errors(self):
        class _NeverEquals(str):
            def __eq__(self, _other):
                return False

        bad_selection = metering.AuthSelection(
            SOURCE_CANARY,
            SCHEME_CANARY,
            BOOL_CANARY,  # type: ignore[arg-type]
        )
        with _capture_root() as root:
            with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                server.emit_auth_rejection(_NeverEquals(TOOL_CANARY), "invalid_api_key", bad_selection)
        payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
        self.assertEqual(payload["tool"], "unknown")
        self.assertEqual(payload["error"], "invalid_api_key")
        self.assertEqual(payload["auth_source"], "none")
        self.assertEqual(payload["authorization_scheme"], "non_bearer")
        self.assertIs(payload["x_api_key_present"], False)
        self.assertEqual(caught.output[0].count("\n"), 0)

        class _PretendsToBeSearch(str):
            def __eq__(self, other):
                return other == "search_products"

            def __str__(self):
                return TOOL_CANARY

            def __repr__(self):
                return TOOL_CANARY

        with _capture_root() as root:
            with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                server.emit_auth_rejection(
                    _PretendsToBeSearch("ignored"),
                    "missing_api_key",
                    _none_selection(),
                )
        payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
        self.assertEqual(payload["tool"], "search_products")

        with _capture_root() as root:
            with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                server.emit_auth_rejection("search_products", "quota_exceeded", _none_selection())
                server.emit_auth_rejection("search_products", "server_unconfigured", _none_selection())
                server.emit_auth_rejection("search_products", ERROR_CANARY, _none_selection())
                server.emit_auth_rejection(TOOL_CANARY, "not-a-real-code", bad_selection)
        self._assert_no_canaries(
            [line for line in root.output] + [record.getMessage() for record in root.records],
            ALL_CANARIES,
        )

    def test_http_missing_wrong_and_expired_log_the_selected_source(self):
        with self._client() as client:
            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    missing = _rpc(
                        client,
                        "tools/call",
                        {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(missing, "missing_api_key", "search_products", None, ALL_CANARIES)
            self.assertEqual(payload["tool"], "search_products")
            self.assertEqual(payload["error"], "missing_api_key")
            self.assertEqual(payload["auth_source"], "none")
            self.assertEqual(payload["authorization_scheme"], "absent")
            self.assertIs(payload["x_api_key_present"], False)

            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    invalid = _rpc(
                        client,
                        "tools/call",
                        {"name": "get_usage", "arguments": {}},
                        headers={"Authorization": f"Bearer {WRONG_BEARER}"},
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(invalid, "invalid_api_key", "get_usage", WRONG_BEARER, ALL_CANARIES)
            self.assertEqual(payload["error"], "invalid_api_key")
            self.assertEqual(payload["auth_source"], "bearer")
            self.assertEqual(payload["authorization_scheme"], "bearer")
            self.assertIs(payload["x_api_key_present"], False)

            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    expired = _rpc(
                        client,
                        "tools/call",
                        {"name": "find_pet_food", "arguments": {"pet": BODY_CANARY}},
                        headers={"X-API-Key": EXPIRED},
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(expired, "expired_api_key", "find_pet_food", EXPIRED, ALL_CANARIES)
            self.assertEqual(payload["error"], "expired_api_key")
            self.assertEqual(payload["auth_source"], "x-api-key")
            self.assertEqual(payload["authorization_scheme"], "absent")
            self.assertIs(payload["x_api_key_present"], True)

        self.assertEqual(self._counter_rows(), 0)
        self.assertEqual(self._price_rows(), 0)
        self.assertEqual(self._used(VALID_BEARER), 0)
        self.assertEqual(self._used(VALID_XAPI), 0)

    def test_bearer_and_x_api_key_keep_precedence_for_different_synthetic_keys(self):
        query = "?" + urllib.parse.urlencode({"api_key": VALID_BEARER, "next": URL_CANARY})
        with self._client() as client:
            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    denied = _rpc(
                        client,
                        "tools/call",
                        {"name": "compare_nutrition", "arguments": {"product_a": BODY_CANARY, "product_b": BODY_CANARY}},
                        headers={
                            "Authorization": f"Bearer {WRONG_BEARER}",
                            "X-API-Key": VALID_XAPI,
                        },
                        query=query,
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(denied, "invalid_api_key", "compare_nutrition", WRONG_BEARER, ALL_CANARIES)
            self.assertEqual(payload["auth_source"], "bearer")
            self.assertEqual(payload["authorization_scheme"], "bearer")
            self.assertIs(payload["x_api_key_present"], True)
            self.assertEqual(self._used(VALID_XAPI), 0)
            self.assertEqual(self._used(VALID_BEARER), 0)

            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    expired = _rpc(
                        client,
                        "tools/call",
                        {"name": "lookup_functional_food_filing", "arguments": {"report_no": BODY_CANARY}},
                        headers={
                            "Authorization": f"Bearer {EXPIRED}",
                            "X-API-Key": VALID_XAPI,
                        },
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(
                expired,
                "expired_api_key",
                "lookup_functional_food_filing",
                EXPIRED,
                ALL_CANARIES,
            )
            self.assertEqual(payload["auth_source"], "bearer")
            self.assertIs(payload["x_api_key_present"], True)
            self.assertEqual(self._used(VALID_XAPI), 0)

            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    fallback = _rpc(
                        client,
                        "tools/call",
                        {"name": "check_price_timing", "arguments": {"product_name": BODY_CANARY}},
                        headers={"Authorization": "Bearer   ", "X-API-Key": WRONG_XAPI},
                        query=query,
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(fallback, "invalid_api_key", "check_price_timing", WRONG_XAPI, ALL_CANARIES)
            self.assertEqual(payload["auth_source"], "x-api-key")
            self.assertEqual(payload["authorization_scheme"], "bearer")
            self.assertIs(payload["x_api_key_present"], True)
            self.assertEqual(self._used(VALID_BEARER), 0)

            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    ok = _rpc(
                        client,
                        "tools/call",
                        {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                        headers={
                            "Authorization": f"{SCHEME_CANARY} {WRONG_BEARER}",
                            "X-API-Key": VALID_XAPI,
                        },
                    )
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertIn(BODY_CANARY, ok.text)
            self.assertEqual(self._used(VALID_XAPI), 1)
            self.assertEqual(self._used(VALID_BEARER), 0)
            self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

            self.assertEqual(self._counter_rows(), 1)

    def test_query_is_selected_only_without_a_header_key(self):
        query = "?" + urllib.parse.urlencode({"api_key": WRONG_QUERY, "next": URL_CANARY})
        with self._client() as client:
            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    queried = _rpc(
                        client,
                        "tools/call",
                        {"name": "get_buy_link", "arguments": {"product_url": URL_CANARY}},
                        query=query,
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(queried, "invalid_api_key", "get_buy_link", WRONG_QUERY, ALL_CANARIES)
            self.assertEqual(payload["auth_source"], "query")
            self.assertEqual(payload["authorization_scheme"], "absent")
            self.assertIs(payload["x_api_key_present"], False)

            valid_query = "?" + urllib.parse.urlencode({"api_key": VALID_BEARER})
            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    header_wins = _rpc(
                        client,
                        "tools/call",
                        {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                        headers={"X-API-Key": WRONG_XAPI},
                        query=valid_query,
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(header_wins, "invalid_api_key", "search_products", WRONG_XAPI, ALL_CANARIES)
            self.assertEqual(payload["auth_source"], "x-api-key")
            self.assertIs(payload["x_api_key_present"], True)
            self.assertEqual(self._used(VALID_BEARER), 0)

            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    expired = _rpc(
                        client,
                        "tools/call",
                        {"name": "get_usage", "arguments": {}},
                        query="?" + urllib.parse.urlencode({"api_key": EXPIRED}),
                    )
            payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
            self._assert_401(expired, "expired_api_key", "get_usage", EXPIRED, ALL_CANARIES)
            self.assertEqual(payload["auth_source"], "query")
            self.assertEqual(payload["authorization_scheme"], "absent")
            self.assertIs(payload["x_api_key_present"], False)

        self.assertEqual(self._counter_rows(), 0)
        self.assertEqual(self._price_rows(), 0)

    def test_malicious_scheme_and_tool_name_cannot_leak(self):
        query = "?" + urllib.parse.urlencode(
            {"api_key": VALID_BEARER, "next": URL_CANARY, "note": SCHEME_CANARY}
        )
        body = [
            {
                "jsonrpc": "2.0",
                "id": RPC_CANARY,
                "method": "tools/call",
                "params": {
                    "name": TOOL_CANARY,
                    "arguments": {"keyword": BODY_CANARY, "url": URL_CANARY},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_products",
                    "arguments": {"keyword": BODY_CANARY},
                },
            },
        ]
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"{SCHEME_CANARY} {WRONG_BEARER}",
            "X-API-Key": WRONG_XAPI,
            "Date": DATE_CANARY,
        }
        with self._client() as client:
            with _capture_root() as root:
                with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                    denied = client.post("/mcp" + query, json=body, headers=headers)
        payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
        self._assert_401(denied, "invalid_api_key", "search_products", WRONG_XAPI, ALL_CANARIES)
        self.assertEqual(payload["tool"], "search_products")
        self.assertEqual(payload["auth_source"], "x-api-key")
        self.assertEqual(payload["authorization_scheme"], "non_bearer")
        self.assertIs(payload["x_api_key_present"], True)
        self.assertNotIn("2099", payload["timestamp"])
        logged = datetime.strptime(payload["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLess(abs((datetime.now(timezone.utc) - logged).total_seconds()), 30)
        self.assertEqual(len(caught.output[0].splitlines()), 1)
        self.assertEqual(self._used(VALID_BEARER), 0)
        self.assertEqual(self._counter_rows(), 0)
        self.assertNotIn(query, caught.output[0])
        self.assertNotIn(urllib.parse.quote(SCHEME_CANARY), caught.output[0])

    def test_each_allowlisted_tool_logs_its_own_name(self):
        with self._client() as client:
            for name in server.AUTH_DIAGNOSTIC_TOOLS:
                with _capture_root() as root:
                    with self.assertLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG") as caught:
                        denied = _rpc(client, "tools/call", {"name": name, "arguments": {}})
                payload = self._assert_logs(caught, root, ALL_CANARIES)[0]
                self._assert_401(denied, "missing_api_key", name, None, ALL_CANARIES)
                self.assertEqual(payload["tool"], name)
                self.assertEqual(payload["error"], "missing_api_key")
                self.assertEqual(payload["auth_source"], "none")

    def test_open_success_quota_and_unconfigured_are_unlogged(self):
        self._install_meter({VALID_BEARER: 0, VALID_XAPI: 100, EXPIRED: 100})
        init = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        }
        open_query = "?" + urllib.parse.urlencode({"api_key": WRONG_QUERY, "next": URL_CANARY})
        with self._client() as client:
            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    health = client.get("/health")
                    opened = _rpc(client, "initialize", init, query=open_query)
                    listed = _rpc(client, "tools/list", {})
                    quota = _rpc(
                        client,
                        "tools/call",
                        {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                        headers={"Authorization": f"Bearer {VALID_BEARER}"},
                    )
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            self.assertEqual(opened.status_code, 200, opened.text)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertIn("search_products", listed.text)
            self.assertEqual(quota.status_code, 200, quota.text)
            self.assertIn("quota_exceeded", quota.text)
            self.assertNotEqual(quota.status_code, 401)
            self.assertEqual(self._counter_rows(), 0)
            self.assertEqual(self._price_rows(), 0)
            self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

            self._install_meter()
            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    bearer = _rpc(
                        client,
                        "tools/call",
                        {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                        headers={
                            "Authorization": f"Bearer {VALID_BEARER}",
                            "X-API-Key": VALID_XAPI,
                        },
                    )
                    queried = _rpc(
                        client,
                        "tools/call",
                        {"name": "find_pet_food", "arguments": {"pet": "강아지"}},
                        query="?" + urllib.parse.urlencode({"api_key": VALID_XAPI}),
                    )
            self.assertEqual(bearer.status_code, 200, bearer.text)
            self.assertIn(BODY_CANARY, bearer.text)
            self.assertNotIn(VALID_BEARER, bearer.text)
            self.assertNotIn(VALID_XAPI, bearer.text)
            self.assertEqual(queried.status_code, 200, queried.text)
            self.assertEqual(self._used(VALID_BEARER), 1)
            self.assertEqual(self._used(VALID_XAPI), 1)
            self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

        os.environ["MCP_API_KEYS"] = ""
        metering.reset_meter()
        server.mcp._session_manager = None
        app = server.mcp.streamable_http_app()
        with self._client(app) as client:
            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    unconfigured = _rpc(
                        client,
                        "tools/call",
                        {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                        headers={"Authorization": f"Bearer {VALID_BEARER}"},
                    )
        self.assertNotEqual(unconfigured.status_code, 401)
        self.assertIn("server_unconfigured", unconfigured.text)
        self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

    def test_usage_route_401_is_not_the_tools_call_diagnostic(self):
        with self._client() as client:
            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    missing = client.get("/usage")
                    invalid = client.get("/usage", headers={"Authorization": f"Bearer {WRONG_BEARER}"})
                    expired = client.get("/usage", params={"api_key": EXPIRED})
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"], "missing_api_key")
        self.assertNotIn("tool", missing.json())
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["error"], "invalid_api_key")
        self.assertEqual(invalid.headers["www-authenticate"], CHALLENGE["invalid_api_key"])
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()["error"], "expired_api_key")
        self.assertNotIn(WRONG_BEARER, invalid.text)
        self.assertNotIn(EXPIRED, expired.text)
        self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

    def test_logging_failure_does_not_change_the_401(self):
        with self._client() as client:
            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    with mock.patch.object(
                        server,
                        "emit_auth_rejection",
                        side_effect=RuntimeError(EMIT_BOOM),
                    ):
                        denied = _rpc(
                            client,
                            "tools/call",
                            {"name": "search_products", "arguments": {"keyword": BODY_CANARY}},
                        )
            self._assert_401(denied, "missing_api_key", "search_products", None, ALL_CANARIES)
            self.assertNotIn(EMIT_BOOM, denied.text)
            self.assertNotIn("Traceback", denied.text)
            self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    with mock.patch.object(
                        metering,
                        "describe_auth_selection",
                        side_effect=RuntimeError(DESCRIBE_BOOM),
                    ):
                        denied = _rpc(
                            client,
                            "tools/call",
                            {"name": "get_usage", "arguments": {}},
                            headers={"X-API-Key": WRONG_XAPI},
                        )
            self._assert_401(denied, "invalid_api_key", "get_usage", WRONG_XAPI, ALL_CANARIES)
            self.assertNotIn(DESCRIBE_BOOM, denied.text)
            self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

    def test_in_process_auth_failure_is_not_logged(self):
        os.environ["MCP_API_KEY"] = WRONG_BEARER

        async def run():
            with _capture_root() as root:
                with self.assertNoLogs(server.AUTH_DIAGNOSTIC_LOGGER_NAME, level="DEBUG"):
                    result = await server.mcp.call_tool(
                        "search_products",
                        {"keyword": BODY_CANARY},
                    )
            self.assertIsInstance(result, CallToolResult)
            self.assertTrue(result.isError)
            self.assertIn("invalid_api_key", result.content[0].text)
            self.assertNotIn(WRONG_BEARER, result.content[0].text)
            self._assert_no_canaries(self._server_log_parts(root), ALL_CANARIES)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
