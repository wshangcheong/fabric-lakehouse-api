# lakehouse-api

REST API exposing curated patient data from a **Microsoft Fabric Lakehouse** over
the lakehouse's SQL analytics endpoint.

In Azure, the app authenticates to Fabric using its **App Service System-Assigned
Managed Identity** (`DefaultAzureCredential`) with **OneLake Security** enabled.
Inbound traffic is locked down with **App Service Authentication (Easy Auth)** and
fronted by **Azure API Management (APIM)** using subscriptionless Entra ID JWT
authentication.

---

## Architecture Overview

```text
External Consumer (Entra ID JWT — Certificate Auth, No Subscription Key)
        │
        ▼
Azure API Management (APIM)
  ├─ Validates consumer Entra ID token & rate-limits per appid (60 req/min)
  └─ Swaps Authorization header for APIM Managed Identity Bearer token
        │
        ▼
Azure Web App (Linux Container — FastAPI)
  ├─ App Service Easy Auth rejects unauthenticated traffic (401)
  └─ Queries Fabric SQL endpoint via Web App Managed Identity
        │
        ▼
Microsoft Fabric Lakehouse (OneLake Security enabled)
```

---

## Prerequisites (Local)

- [uv](https://astral.sh) (`uv --version` should report a version)
- **Microsoft ODBC Driver 18 for SQL Server** installed locally
  (needed by `pyodbc` at connect time; `pyodbc` itself installs as a wheel)
- Azure CLI (`az login`) or an Entra service principal with access to the Fabric lakehouse

### Fabric Side

1. In the Fabric workspace, open the Lakehouse → **Settings → SQL analytics
   endpoint** and copy the server value
   (e.g. `<FABRIC-WORKSPACE-SQL-ENDPOINT>.datawarehouse.fabric.microsoft.com,1433`).
2. Grant the Web App's **System-Assigned Managed Identity** (`<WEBAPP-NAME>`)
   access to the Fabric workspace/lakehouse (**Viewer** or **ReadData**) and
   assign it to the appropriate **OneLake Security** role.
3. Ensure the Fabric tenant setting **Service principals can use Fabric APIs**
   is enabled.

---

## Run Locally

```powershell
Copy-Item .env.example .env
# Edit .env:
#   FABRIC_SQL_SERVER=<FABRIC-WORKSPACE-SQL-ENDPOINT>.datawarehouse.fabric.microsoft.com,1433
#   FABRIC_SQL_DATABASE=<FABRIC-LAKEHOUSE-NAME>
#   ENABLE_DEV_ENDPOINTS=true
#   (Optional for local SPN auth if not using `az login`: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)

uv sync                                      # create .venv + uv.lock, install deps
uv run python -m uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive Swagger UI.

---

## Endpoints

### Production Endpoints (Always Active)

| Method | Path                             | Parameters                                | Purpose |
|--------|----------------------------------|-------------------------------------------|---------|
| `GET`  | `/`                              | —                                         | Service info & active endpoint list |
| `GET`  | `/healthz`                       | —                                         | Liveness probe (no data access) |
| `GET`  | `/readyz`                        | —                                         | Readiness probe (Managed Identity auth + `SELECT 1`) |
| `POST` | `/patient`                       | JSON body: `lookups` array of `FacilityCode|PID|VisitNumber` strings | Query `dbo.patient` |
| `POST` | `/patient-admission`             | Same `lookups` body                       | Query `dbo.patient_admission` |
| `POST` | `/patient-diagnosis`             | Same `lookups` body                       | Query `dbo.patient_diagnosis` |
| `POST` | `/patient-billheader`            | Same `lookups` body                       | Query `dbo.patient_billheader` |
| `GET`  | `/patient-billdetail`            | `FacilityCode`, `BillHeaderID`            | Query `dbo.patient_billdetail` |
| `POST` | `/patient-progressnotes`         | Same `lookups` body                       | Query `dbo.patient_progressnotes` |
| `GET`  | `/patient-progressnotes-freetext`| `FacilityCode`, `ProgressNoteID`          | Query `dbo.patient_progressnotes_freetext` |

All five batch endpoints accept the same request body (up to 700 lookups per request):

```json
{
  "lookups": [
    "FACILITY_CODE|PATIENT_ID|VISIT_NUMBER",
    "FACILITY_CODE_2|PATIENT_ID_2|VISIT_NUMBER_2"
  ]
}
```

The response contains all matching records in the generic `rows` array:

```json
{
  "rows": [
    {
      "PID": "PATIENT_ID",
      "FacilityCode": "FACILITY_CODE"
    }
  ]
}
```

### Dev-Only Endpoints (`ENABLE_DEV_ENDPOINTS=true`)

These routes are **disabled by default** (`ENABLE_DEV_ENDPOINTS=False`), so they
are not registered in Azure and are excluded from `openapi.json` during CI/CD:

| Method | Path              | Purpose |
|--------|-------------------|---------|
| `GET`  | `/tables`         | List base tables via `INFORMATION_SCHEMA` |
| `GET`  | `/queries`        | List named queries in `app/queries.py` |
| `GET`  | `/queries/{name}` | Run a named query (`?param=value`) |
| `POST` | `/query`          | Run ad-hoc read-only `SELECT`/`WITH` SQL |

---

## Deploy to Azure (App Service + APIM + Azure DevOps)

The container installs MS ODBC Driver 18 and runs `uvicorn` on port `8000`.
No client secrets are stored in App Settings or Key Vault—the App Service uses
its **System-Assigned Managed Identity** (`DefaultAzureCredential`) to pull
images from ACR and authenticate to Fabric.

### 1. App Settings

```powershell
az webapp config appsettings set -n <WEBAPP-NAME> -g <RESOURCE-GROUP-NAME> --settings `
  WEBSITES_PORT=8000 `
  FABRIC_SQL_SERVER=<FABRIC-WORKSPACE-SQL-ENDPOINT>.datawarehouse.fabric.microsoft.com,1433 `
  FABRIC_SQL_DATABASE=<FABRIC-LAKEHOUSE-NAME>
```

*(Ensure `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, and `ENABLE_DEV_ENDPOINTS` are
**not** set in production App Settings, and ensure **Always On** is enabled under
Web App **General settings**).*

### 2. App Service Easy Auth & APIM Integration

1. **Web App Authentication (Easy Auth):**
   - Identity provider: **Microsoft Entra ID** (`<WEBAPP-AUTH-APP-REG-NAME>`).
   - Restrict access: **Require authentication** (`HTTP 401 Unauthorized: recommended for APIs`).
   - Allowed client applications: restricted to APIM's System-Assigned Managed Identity (`<APIM-SERVICE-NAME>`).
2. **APIM Inbound Policy:**
   - Validates incoming consumer Entra ID tokens (`validate-azure-ad-token`) and enforces per-SPN rate limits (`rate-limit-by-key` at 60 requests/min).
   - Replaces the consumer `Authorization` header with APIM's Managed Identity token (`authentication-managed-identity`) before forwarding to the Web App.
   - Subscription keys are disabled (`--subscription-required false`).

### 3. CI/CD Pipeline (`azure-pipelines.yml`)

Pushing to `main` triggers **Azure DevOps** to:
1. Build and push the container image to ACR (`az acr build`) tagged with `$(Build.BuildId)` and `latest`.
2. Deploy the new container tag to Azure Web App (`az webapp config container set`).
3. Generate `openapi.json` via `uv` (`app.openapi()`).
4. Import `openapi.json` into APIM (`az apim api import --subscription-required false`), automatically syncing new endpoints while preserving APIM policies.

### 4. Sharing API Specs with Consumers

Because the APIM Developer Portal requires authentication, export the latest
OpenAPI v3 spec directly from APIM (`APIs → <APIM-API-ID> → ... → Export → OpenAPI v3 (JSON)`)
or via Azure CLI (`az apim api export`) when onboarding or updating external consumers.

---

## Security Notes

- **Zero stored secrets in production:** Both ACR pulls and Fabric SQL connections authenticate via Azure Managed Identities; external consumers authenticate via X.509 certificates.
- **Defense in depth:** Direct public calls to `*.azurewebsites.net` are blocked at the edge with `401 Unauthorized` by App Service Easy Auth; external callers must go through APIM.
- **Dev routes isolated:** `/query`, `/tables`, and `/queries` require `ENABLE_DEV_ENDPOINTS=true` and never exist in production builds.
- **Parameterized queries:** All user inputs are bound via `?` placeholders in `pyodbc`.
