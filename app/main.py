import logging
from contextlib import asynccontextmanager
from typing import Any

import pyodbc
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .config import settings
from .db import get_connection, get_db, rows_to_json
from .queries import NAMED_QUERIES, bind_params

logging.basicConfig(level=settings.LOG_LEVEL)
log = logging.getLogger("lakehouse-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting %s", settings.APP_NAME)
    yield
    log.info("shutting down %s", settings.APP_NAME)


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)


@app.get("/")
def root():
    return {
        "name": settings.APP_NAME,
        "endpoints": ["/healthz", "/readyz", "/tables", "/queries", "/query"],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Verifies the data path: authenticates as the SPN and runs SELECT 1.

    Opens the connection (and acquires the SPN token) inside the try block so
    that auth/driver/network failures surface with a real message instead of
    a bare 500.
    """
    try:
        conn = get_connection()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return {"status": "ready"}


@app.get("/tables")
def list_tables(conn: pyodbc.Connection = Depends(get_db)):
    cur = conn.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME "
        "FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME"
    )
    return rows_to_json(cur)


class QueryRequest(BaseModel):
    sql: str
    params: list[Any] = []


def _assert_read_only(sql: str) -> str:
    """Soft guard: reject multi-statement and non-SELECT/WITH input.

    The Fabric SQL analytics endpoint is itself read-only, so this is
    defense-in-depth, not the security boundary. Treat /query as dev-only.
    """
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise HTTPException(status_code=400, detail="only a single statement is allowed")
    upper = stripped.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise HTTPException(status_code=400, detail="only SELECT / WITH queries are allowed")
    return stripped


@app.post("/query")
def adhoc_query(req: QueryRequest, conn: pyodbc.Connection = Depends(get_db)):
    """DEV ONLY: run an ad-hoc read query against the lakehouse.

    Useful while trying things out. Gate or remove this route (e.g. via APIM)
    before production use.
    """
    sql = _assert_read_only(req.sql)
    try:
        cur = conn.execute(sql, *req.params)
        return {"columns": [c[0] for c in cur.description], "rows": rows_to_json(cur)}
    except pyodbc.Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/queries")
def list_queries():
    return [
        {
            "name": k,
            "sql": v["sql"],
            "params": v.get("params", []),
            "request_params": v.get("request_params", []),
        }
        for k, v in NAMED_QUERIES.items()
    ]


@app.get("/queries/{name}")
def run_named_query(
    name: str,
    request: Request,
    conn: pyodbc.Connection = Depends(get_db),
):
    q = NAMED_QUERIES.get(name)
    if q is None:
        raise HTTPException(status_code=404, detail=f"unknown query: {name}")
    # Bind fixed params first, then call-time params from the query string
    # (in declared order). User values only ever go through ? placeholders,
    # so this is parameterized and safe; identifiers stay developer-controlled.
    try:
        bind = bind_params(q, request.query_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        cur = conn.execute(q["sql"], *bind)
        return {"name": name, "columns": [c[0] for c in cur.description], "rows": rows_to_json(cur)}
    except pyodbc.Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))
