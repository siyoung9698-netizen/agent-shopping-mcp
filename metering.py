"""Per-call API key metering for the shopping MCP server.

Keys and quotas come from the environment. Usage is a SQLite counter keyed by
a hash of the API key, so the database does not store the secret itself.
One calendar month (UTC) is one quota window.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

METERED_TOOLS = (
    "search_products",
    "check_price_timing",
    "get_buy_link",
    "find_pet_food",
    "compare_nutrition",
)

DEFAULT_MONTHLY_QUOTA = 1000

# HTTP tools/call maps these to 401. Quota and server_unconfigured stay tool results.
AUTH_FAILURE_CODES = frozenset({"missing_api_key", "invalid_api_key", "expired_api_key"})

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class MeterError(Exception):
    """A caller-facing metering failure. `payload` is JSON-serializable."""

    def __init__(self, code: str, message: str, **extra: object):
        self.code = code
        self.payload: dict[str, object] = {"error": code, "message": message, **extra}
        super().__init__(message)


@dataclass(frozen=True)
class UsageView:
    key_id: str
    period: str
    calls_used: int
    calls_remaining: int
    quota: int
    resets_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key_id": self.key_id,
            "period": self.period,
            "window": "calendar_month_utc",
            "calls_used": self.calls_used,
            "calls_remaining": self.calls_remaining,
            "quota": self.quota,
            "resets_at": self.resets_at,
            "metered_tools": list(METERED_TOOLS),
        }


def load_local_env(path: str | None = None) -> None:
    """Load KEY=VALUE lines from `.env` without overriding real environment variables."""
    env_path = path or _ENV_PATH
    try:
        fh = open(env_path, encoding="utf-8")
    except OSError:
        return
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)


def _parse_non_negative_int(value: str, name: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name}는 0 이상의 정수여야 합니다. 받은 값: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{name}는 0 이상의 정수여야 합니다. 받은 값: {value!r}")
    return parsed


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_key_config(
    api_keys: str,
    key_quotas: str,
    default_quota: int,
) -> dict[str, int]:
    """Return raw API key → monthly quota.

    `api_keys` is a comma-separated allow-list. `key_quotas` is
    `key:quota` pairs (quota after the last colon) and overrides the default.
    Keys may contain colons but not commas.
    """
    if default_quota < 0:
        raise ValueError("MCP_MONTHLY_QUOTA는 0 이상의 정수여야 합니다.")
    keys: dict[str, int] = {}
    for key in _split_csv(api_keys):
        keys[key] = default_quota
    for part in _split_csv(key_quotas):
        key, sep, quota_raw = part.rpartition(":")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"MCP_KEY_QUOTAS 항목은 '키:한도' 형식이어야 합니다. 받은 값: {part!r}"
            )
        quota = _parse_non_negative_int(quota_raw, "MCP_KEY_QUOTAS")
        if key not in keys:
            raise ValueError(
                f"MCP_KEY_QUOTAS의 {key!r} 가 MCP_API_KEYS에 없습니다."
            )
        keys[key] = quota
    return keys


def config_from_env() -> tuple[str, dict[str, int]]:
    load_local_env()
    default_quota = _parse_non_negative_int(
        os.environ.get("MCP_MONTHLY_QUOTA", str(DEFAULT_MONTHLY_QUOTA)),
        "MCP_MONTHLY_QUOTA",
    )
    keys = parse_key_config(
        os.environ.get("MCP_API_KEYS", ""),
        os.environ.get("MCP_KEY_QUOTAS", ""),
        default_quota,
    )
    db_path = os.environ.get("MCP_USAGE_DB_PATH", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "usage.db",
    )
    return db_path, keys


def key_fingerprint(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def public_key_id(raw_key: str) -> str:
    """Short display id. Not the secret, and not enough to authenticate."""
    return key_fingerprint(raw_key)[:16]


def period_window(now: datetime) -> tuple[str, str]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    period = f"{now.year:04d}-{now.month:02d}"
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return period, reset.strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_api_key(
    *,
    headers: Mapping[str, str] | None,
    query_params: Mapping[str, str] | None,
) -> str | None:
    """Pull a caller key from an HTTP request.

    Precedence: Authorization Bearer, then X-API-Key, then the `api_key` query
    parameter. The query form exists for MCP clients that can only set a URL.
    """
    auth = _header(headers, "authorization")
    if auth:
        scheme, _, token = auth.strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    header_key = _header(headers, "x-api-key")
    if header_key and header_key.strip():
        return header_key.strip()
    if query_params is not None:
        query_key = query_params.get("api_key")
        if query_key and str(query_key).strip():
            return str(query_key).strip()
    return None


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    direct = headers.get(name)
    if direct:
        return str(direct)
    # Starlette headers are case-insensitive. Plain dicts are not.
    lowered = name.lower()
    items = getattr(headers, "items", None)
    if items is None:
        return None
    for key, value in items():
        if str(key).lower() == lowered and value:
            return str(value)
    return None


class UsageMeter:
    def __init__(
        self,
        db_path: str,
        keys: dict[str, int],
        clock: Callable[[], datetime] | None = None,
        expires_at: Mapping[str, datetime] | None = None,
    ):
        self.db_path = db_path
        self.keys = dict(keys)
        self.expires_at = dict(expires_at or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        con = self._connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS usage_counters ("
                "key_id TEXT NOT NULL, "
                "period TEXT NOT NULL, "
                "calls INTEGER NOT NULL, "
                "updated_at TEXT NOT NULL, "
                "PRIMARY KEY (key_id, period))"
            )
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def _now(self) -> datetime:
        return self._clock()

    def _require_key(self, raw_key: str | None) -> tuple[str, int, str]:
        if not self.keys:
            raise MeterError(
                "server_unconfigured",
                "서버에 유효한 API 키가 없습니다. MCP_API_KEYS 환경변수를 설정한 뒤 프로세스를 다시 시작하세요.",
            )
        if raw_key is None or not str(raw_key).strip():
            raise MeterError(
                "missing_api_key",
                "API 키가 필요합니다. HTTP는 Authorization: Bearer <키> 또는 X-API-Key 헤더를 보내고, "
                "헤더를 설정할 수 없으면 URL에 ?api_key=<키> 를 붙이세요. "
                "로컬 stdio는 환경변수 MCP_API_KEY를 사용합니다.",
            )
        raw_key = str(raw_key).strip()
        quota = self.keys.get(raw_key)
        if quota is None:
            raise MeterError(
                "invalid_api_key",
                "등록되지 않은 API 키입니다. 서버의 MCP_API_KEYS에 포함된 키만 사용할 수 있습니다.",
            )
        expiry = self.expires_at.get(raw_key)
        if expiry is not None and _as_utc(self._now()) >= _as_utc(expiry):
            raise MeterError(
                "expired_api_key",
                "만료된 API 키입니다. 서버 운영자에게 새 키를 요청하세요.",
            )
        return raw_key, quota, key_fingerprint(raw_key)

    def auth_failure(self, raw_key: str | None) -> MeterError | None:
        """Return a missing, invalid, or expired key error.

        Does not read or increment quota. An unconfigured server returns None
        so the tool can keep reporting server_unconfigured.
        """
        try:
            self._require_key(raw_key)
        except MeterError as err:
            if err.code in AUTH_FAILURE_CODES:
                return err
            return None
        return None

    def _read_calls(self, con: sqlite3.Connection, key_id: str, period: str) -> int:
        row = con.execute(
            "SELECT calls FROM usage_counters WHERE key_id=? AND period=?",
            (key_id, period),
        ).fetchone()
        return int(row[0]) if row else 0

    def _view(self, raw_key: str, quota: int, used: int, now: datetime) -> UsageView:
        period, resets_at = period_window(now)
        remaining = quota - used
        if remaining < 0:
            remaining = 0
        return UsageView(
            key_id=public_key_id(raw_key),
            period=period,
            calls_used=used,
            calls_remaining=remaining,
            quota=quota,
            resets_at=resets_at,
        )

    def inspect(self, raw_key: str | None) -> UsageView:
        """Return this key's usage. Does not increment the counter."""
        raw_key, quota, key_id = self._require_key(raw_key)
        now = self._now()
        period, _resets_at = period_window(now)
        con = self._connect()
        try:
            used = self._read_calls(con, key_id, period)
        finally:
            con.close()
        return self._view(raw_key, quota, used, now)

    def consume(self, raw_key: str | None) -> UsageView:
        """Count one metered call, or reject when the monthly quota is spent.

        A rejected call is not counted, so retrying after the window resets
        starts from the real total.
        """
        raw_key, quota, key_id = self._require_key(raw_key)
        now = self._now()
        period, _resets_at = period_window(now)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            used = self._read_calls(con, key_id, period)
            if used >= quota:
                con.execute("ROLLBACK")
                view = self._view(raw_key, quota, used, now)
                raise MeterError(
                    "quota_exceeded",
                    "이번 달 호출 한도를 모두 사용했습니다. 초기화 시각 이후에 다시 호출할 수 있습니다.",
                    **view.to_dict(),
                )
            new_used = used + 1
            updated_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if used == 0:
                con.execute(
                    "INSERT INTO usage_counters(key_id, period, calls, updated_at) VALUES (?,?,?,?)",
                    (key_id, period, new_used, updated_at),
                )
            else:
                con.execute(
                    "UPDATE usage_counters SET calls=?, updated_at=? WHERE key_id=? AND period=?",
                    (new_used, updated_at, key_id, period),
                )
            con.execute("COMMIT")
        except MeterError:
            raise
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        return self._view(raw_key, quota, new_used, now)


_meter: UsageMeter | None = None


def get_meter() -> UsageMeter:
    global _meter
    if _meter is None:
        db_path, keys = config_from_env()
        _meter = UsageMeter(db_path, keys)
    return _meter


def reset_meter(
    db_path: str | None = None,
    keys: dict[str, int] | None = None,
    clock: Callable[[], datetime] | None = None,
    expires_at: Mapping[str, datetime] | None = None,
) -> UsageMeter:
    """Replace the process-wide meter. Tests pass an explicit db and key map."""
    global _meter
    if db_path is None or keys is None:
        env_path, env_keys = config_from_env()
        db_path = db_path or env_path
        keys = env_keys if keys is None else keys
    _meter = UsageMeter(db_path, keys, clock=clock, expires_at=expires_at)
    return _meter
