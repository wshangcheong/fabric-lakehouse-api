import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pyodbc
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field, field_validator

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


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan, openapi_version="3.0.2")


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version="3.0.2",
        description=app.description,
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


class PatientLookupRequest(BaseModel):
    lookups: list[str] = Field(min_length=1, max_length=700)

    @field_validator("lookups")
    @classmethod
    def validate_lookups(cls, lookups: list[str]) -> list[str]:
        for lookup in lookups:
            parts = [part.strip() for part in lookup.split("|")]
            if len(parts) != 3 or any(not part for part in parts):
                raise ValueError(
                    "each lookup must contain non-empty FacilityCode, PID, and VisitNumber "
                    "values separated by '|'."
                )
        return lookups


@dataclass
class BillDetailLookupParams:
    facility_code: str = Query(alias="FacilityCode", min_length=1)
    bill_header_id: str = Query(alias="BillHeaderID", min_length=1)


@dataclass
class ProgressNoteFreeTextLookupParams:
    facility_code: str = Query(alias="FacilityCode", min_length=1)
    progress_note_id: str = Query(alias="ProgressNoteID", min_length=1)


def _fetch_patient_table(
    table_name: str,
    params: PatientLookupRequest,
    conn: pyodbc.Connection,
) -> dict[str, list[dict[str, Any]]]:
    lookups = [
        tuple(part.strip() for part in lookup.split("|"))
        for lookup in params.lookups
    ]
    predicates = [
        "([FacilityCode] = ? AND [PID] = ? AND [VisitNumber] = ?)"
        for _ in lookups
    ]
    values = [value for lookup in lookups for value in lookup]
    cur = conn.execute(
        f"SELECT * FROM [dbo].[{table_name}] WHERE " + " OR ".join(predicates),
        *values,
    )
    return {"rows": rows_to_json(cur)}


@app.get("/")
def root():
    endpoints = [
        "/healthz",
        "/readyz",
        "/patient",
        "/patient-admission",
        "/patient-diagnosis",
        "/patient-billheader",
        "/patient-billdetail",
        "/patient-progressnotes",
        "/patient-progressnotes-freetext"
    ]
    if settings.ENABLE_DEV_ENDPOINTS:
        endpoints.extend(["/tables", "/queries", "/query"])
    return {
        "name": settings.APP_NAME,
        "endpoints": endpoints,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Verifies the data path: authenticates via Managed Identity and runs SELECT 1.

    Opens the connection (and acquires the token) inside the try block so
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


@app.post("/patient")
def get_patient(
    params: PatientLookupRequest,
    conn: pyodbc.Connection = Depends(get_db),
):
    return _fetch_patient_table("patient", params, conn)


@app.post("/patient-admission")
def get_patient_admission(
    params: PatientLookupRequest,
    conn: pyodbc.Connection = Depends(get_db),
):
    return _fetch_patient_table("patient_admission", params, conn)


@app.post("/patient-diagnosis")
def get_patient_diagnosis(
    params: PatientLookupRequest,
    conn: pyodbc.Connection = Depends(get_db),
):
    return _fetch_patient_table("patient_diagnosis", params, conn)


@app.post("/patient-billheader")
def get_patient_billheader(
    params: PatientLookupRequest,
    conn: pyodbc.Connection = Depends(get_db),
):
    return _fetch_patient_table("patient_billheader", params, conn)


@app.get("/patient-billdetail")
def get_patient_billdetail(
    params: BillDetailLookupParams = Depends(),
    conn: pyodbc.Connection = Depends(get_db),
):
    cur = conn.execute(
        "SELECT * FROM [dbo].[patient_billdetail] "
        "WHERE [FacilityCode] = ? AND [BillHeaderID] = ?",
        params.facility_code,
        params.bill_header_id,
    )
    return rows_to_json(cur)


@app.post("/patient-progressnotes")
def get_patient_progressnotes(
    params: PatientLookupRequest,
    conn: pyodbc.Connection = Depends(get_db),
):
    return _fetch_patient_table("patient_progressnotes", params, conn)


@app.get("/patient-progressnotes-freetext")
def get_patient_progressnotes_freetext(
    params: ProgressNoteFreeTextLookupParams = Depends(),
    conn: pyodbc.Connection = Depends(get_db),
):
    cur = conn.execute(
        "SELECT * FROM [dbo].[patient_progressnotes_freetext] "
        "WHERE [FacilityCode] = ? AND [ProgressNoteID] = ?",
        params.facility_code,
        params.progress_note_id,
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


if settings.ENABLE_DEV_ENDPOINTS:

    @app.get("/tables")
    def list_tables(conn: pyodbc.Connection = Depends(get_db)):
        cur = conn.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
        return rows_to_json(cur)

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
        try:
            bind = bind_params(q, request.query_params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            cur = conn.execute(q["sql"], *bind)
            return {"name": name, "columns": [c[0] for c in cur.description], "rows": rows_to_json(cur)}
        except pyodbc.Error as exc:
            raise HTTPException(status_code=400, detail=str(exc))
