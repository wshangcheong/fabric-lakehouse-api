# lakehouse-api

Minimal REST API exposing data from a **Microsoft Fabric Lakehouse** over the
lakehouse's SQL analytics endpoint. The app authenticates to Fabric as an
Entra service principal (the same SPN that already has data access). REST
client auth is intentionally left out — it will be handled by Azure API
Management in front of this service later.

## Prerequisites (local)

- [uv](https://docs.astral.sh/uv/) (`uv --version` should report a version)
- **Microsoft ODBC Driver 18 for SQL Server** installed locally
  (needed by pyodbc at connect time; pyodbc itself installs as a wheel)
- An Entra service principal with access to the Fabric lakehouse

### Fabric side

1. In the Fabric workspace, open the Lakehouse → **Settings → SQL analytics
   endpoint** and copy the server value
   (e.g. `your-workspace.datawarehouse.fabric.microsoft.com,1433`).
2. Grant the SPN access to the workspace/lakehouse (add it as a Contributor,
   or via the lakehouse's SQL security model) and ensure the tenant setting
   that allows service principals to access Fabric SQL analytics endpoints
   is enabled.

## Run locally

```powershell
Copy-Item .env.example .env
# Edit .env: set AZURE_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET,
#           FABRIC_SQL_SERVER (+ FABRIC_SQL_DATABASE if needed).

uv sync                                      # create .venv + uv.lock, install deps
uv run python -m uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive Swagger UI.

### Verify the data path

- `GET /readyz` — authenticates as the SPN and runs `SELECT 1`.
- `GET /tables` — lists base tables (via `INFORMATION_SCHEMA`).
- `GET /queries/table_count` — runs the built-in sample named query.

### Run your own queries two ways

- **Ad-hoc (dev only):** `POST /query` with a body like
  `{"sql": "SELECT TOP (10) * FROM sales.fact_sales", "params": []}`.
  Restricted to single `SELECT`/`WITH` statements; the Fabric SQL analytics
  endpoint is read-only regardless. Disable this route before production.
- **Named:** add entries to `app/queries.py`. Each entry has a `sql` string
  (use `?` placeholders) plus, optionally:
  - `params`: a list of fixed values bound to `?` in order.
  - `request_params`: parameters taken from the caller at call time. Each item
    is a name (defaults to `str`) or `{"name": ..., "type": ...}` where type is
    `str`, `int`, `float`, `bool`, `date`, or `datetime`.

  `?` are bound in order — fixed `params` first, then `request_params`. Call:

  - `GET /queries/{name}?param=value` — params from the query string
    (curl/APM/browser). Swagger doesn't render these, so in the UI use POST.
  - `POST /queries/{name}` with a JSON body like `{"param": value}` — renders
    in Swagger as an editable field; values are coerced to their declared type,
    and missing/bad values return 400.

  Example:
  ```python
  "patient_notes_top": {
      "sql": "SELECT TOP (?) * FROM dbo.patient_progressnotes_freetext ORDER BY (SELECT NULL)",
      "request_params": [{"name": "limit", "type": "int"}],
  }
  ```
  → `POST /queries/patient_notes_top` body `{"limit": 25}`,
  or `GET /queries/patient_notes_top?limit=25`.

  User values only ever go through `?` placeholders (parameterized, so safe);
  keep table/column identifiers hardcoded in the `sql` string.

### Endpoints

| Method | Path              | Purpose                                  |
|--------|-------------------|------------------------------------------|
| GET    | `/`               | Service info                             |
| GET    | `/healthz`        | Liveness (no data access)                |
| GET    | `/readyz`         | Readiness (SPN auth + `SELECT 1`)        |
| GET    | `/tables`         | List base tables                         |
| GET    | `/queries`        | List named queries                       |
| GET    | `/queries/{name}` | Run a named query (params in query string) |
| POST   | `/queries/{name}` | Run a named query (params in JSON body)    |
| POST   | `/query`          | Dev-only ad-hoc read query                |

## Deploy to Azure (App Service, container)

```powershell
az group create -n rg-lakehouse-api -l southeastasia
az acr create -n <unique-acr> -g rg-lakehouse-api --sku Basic --admin-enabled true
az acr build --registry <unique-acr> --image lakehouse-api:latest .

az appservice plan create -n plan-lakehouse-api -g rg-lakehouse-api --is-linux --sku B2
az webapp create -n app-lakehouse-api -g rg-lakehouse-api `
  --plan plan-lakehouse-api `
  --deployment-container-image-name <unique-acr>.azurecr.io/lakehouse-api:latest

az webapp config appsettings set -n app-lakehouse-api -g rg-lakehouse-api `
  --settings WEBSITES_PORT=8000 `
  AZURE_TENANT_ID=... FABRIC_CLIENT_ID=... FABRIC_CLIENT_SECRET=... `
  FABRIC_SQL_SERVER=... FABRIC_SQL_DATABASE=...
```

Link ACR to the webapp (or use its admin creds) so it can pull the image.
Prefer a **managed identity** + Key Vault for the secret in production rather
than app settings.

## Hosting on Azure Functions instead?

The data layer (`app/config.py`, `app/db.py`, `app/queries.py`) is
host-agnostic. Moving to Functions later means replacing only the thin HTTP
layer in `app/main.py` with Functions HTTP triggers (or mounting FastAPI via
ASGI), leaving the SPN-auth/connection/query code unchanged.

## Security notes

- No auth in-app by design — APM/APIM will front it. Until then, keep this
  service off the public internet (private endpoint / VNet integration /
  restricted access).
- The SPN secret lives in env / Key Vault, never in code.
- The `/query` route is dev-only; remove or gate it behind APIM for prod.
- The Fabric SQL analytics endpoint is read-only, which limits blast radius,
  but still keep network and APIM controls tight.
