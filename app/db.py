"""Database access via the Fabric Lakehouse SQL analytics endpoint.

Authentication: the app authenticates to the lakehouse as an Entra service
principal using the client-credentials flow. An access token is acquired and
handed to the ODBC driver via the SQL_COPT_SS_ACCESS_TOKEN pre-connection
attribute, so no secret is placed in the connection string.

Connections are opened per request; the ODBC driver manager pools them, so
this is cheap and avoids sharing a connection across threads (pyodbc is
blocking, and FastAPI runs sync handlers in a threadpool).
"""
from __future__ import annotations

import struct
from typing import Any, Iterator

import pyodbc
from azure.identity import ClientSecretCredential

from .config import settings

# SQL_COPT_SS_ACCESS_TOKEN: pre-connection attribute carrying an Entra token.
SQL_COPT_SS_ACCESS_TOKEN = 1256


def _access_token_bytes() -> bytes:
    credential = ClientSecretCredential(
        tenant_id=settings.AZURE_TENANT_ID,
        client_id=settings.FABRIC_CLIENT_ID,
        client_secret=settings.FABRIC_CLIENT_SECRET.get_secret_value(),
    )
    token = credential.get_token(settings.FABRIC_SQL_SCOPE)
    # The driver expects the token encoded as UTF-16-LE, prefixed by a
    # 4-byte little-endian byte length.
    encoded = token.token.encode("utf-16-le")
    return struct.pack("<I", len(encoded)) + encoded


def _connection_string() -> str:
    parts = [
        f"Driver={{{settings.ODBC_DRIVER}}}",
        f"Server={settings.FABRIC_SQL_SERVER},1433",
        "Encrypt=yes",
        "TrustServerCertificate=no",
    ]
    if settings.FABRIC_SQL_DATABASE:
        parts.append(f"Database={settings.FABRIC_SQL_DATABASE}")
    return ";".join(parts) + ";"


def get_connection() -> pyodbc.Connection:
    return pyodbc.connect(
        _connection_string(),
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _access_token_bytes()},
    )


def get_db() -> Iterator[pyodbc.Connection]:
    """FastAPI dependency: yield a connection, close it after the request."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def quote_identifier(name: str) -> str:
    """Quote a T-SQL identifier safely (square brackets, doubled ']')."""
    if any(ch in name for ch in ('"', "'", "\x00")):
        raise ValueError(f"invalid identifier: {name!r}")
    return "[" + name.replace("]", "]]") + "]"


def rows_to_json(cur: pyodbc.Cursor) -> list[dict[str, Any]]:
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, tuple(r))) for r in cur.fetchall()]
