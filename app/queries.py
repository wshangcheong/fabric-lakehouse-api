"""Named, parameterized queries for the lakehouse.

Add your own queries here. Each entry is a dict with:
    sql:             a T-SQL SELECT/WITH statement. Use ? for parameters.
    params:          optional list of fixed values, one per ? (in order).
    request_params:  optional list of parameters taken from the caller's
                     query string at call time. Each item is a name (str,
                     type defaults to "str") or a dict {"name": ..., "type": ...}.
                     Supported types: str, int, float, bool, date, datetime.

Binding order for ?: fixed `params` first, then `request_params` in the
order declared. User values only ever go through ? placeholders (parameterized),
so this is safe; table/column identifiers stay developer-controlled in the SQL.

Exposed via:
    GET /queries            -> list available named queries
    GET /queries/{name}     -> run a named query (?param=value in the URL)
"""
from __future__ import annotations

from datetime import date, datetime

NAMED_QUERIES: dict[str, dict] = {
    # Counts base tables in the lakehouse. Works against any Fabric SQL
    # analytics endpoint; useful as a "does it connect" smoke test.
    "table_count": {
        "sql": (
            "SELECT COUNT(*) AS n "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE'"
        ),
    },
    # Call-time param: the caller sets the row limit in the URL.
    #   GET /queries/patient_notes_top?limit=25
    "patient_notes_top": {
        "sql": "SELECT TOP (?) * FROM dbo.patient_progressnotes_freetext ORDER BY (SELECT NULL)",
        "request_params": [{"name": "limit", "type": "int"}],
    },
    # --- Examples (replace column names with yours): ---
    # "patient_notes_by_patient": {
    #     "sql": "SELECT * FROM dbo.patient_progressnotes_freetext WHERE patientid = ?",
    #     "request_params": [{"name": "patient_id", "type": "int"}],
    # },
    # "notes_in_range": {
    #     "sql": (
    #         "SELECT * FROM dbo.patient_progressnotes_freetext "
    #         "WHERE createddate BETWEEN ? AND ?"
    #     ),
    #     "request_params": [
    #         {"name": "from", "type": "datetime"},
    #         {"name": "to", "type": "datetime"},
    #     ],
    # },
}

_COERCERS = {
    "str": str,
    "int": int,
    "float": float,
    "bool": lambda v: str(v).strip().lower() in ("1", "true", "yes"),
    "date": date.fromisoformat,
    "datetime": datetime.fromisoformat,
}


def _spec(item):
    """Return (name, type) from a request_params item."""
    if isinstance(item, str):
        return item, "str"
    return item["name"], item.get("type", "str")


def bind_params(query_def, query_params) -> list:
    """Build the ordered bind list for a named query.

    `query_params` is a mapping of the caller's query-string values (e.g. a
    Starlette QueryParams). Fixed `params` are bound first, then declared
    request_params in order, each coerced to its declared type. Raises
    ValueError on a missing or uncoercible parameter.
    """
    values = list(query_def.get("params", []))
    for item in query_def.get("request_params", []):
        name, typ = _spec(item)
        if name not in query_params:
            raise ValueError(f"missing required parameter: {name}")
        coercer = _COERCERS.get(typ)
        if coercer is None:
            raise ValueError(f"unknown type '{typ}' for parameter '{name}'")
        raw = query_params[name]
        try:
            values.append(coercer(raw))
        except (ValueError, TypeError):
            raise ValueError(f"invalid value for '{name}' (expected {typ}): {raw!r}")
    return values
