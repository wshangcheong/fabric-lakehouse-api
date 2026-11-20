# Azure & Fabric Infrastructure Knowledge Transfer (KT) Guide

**Project:** `lakehouse-api` (`caredoct-api`)  
**Purpose:** Operational runbook and architecture reference for maintaining, troubleshooting, and onboarding consumers to the Fabric Lakehouse REST API.

---

## 1. End-to-End Architecture & Security Boundaries

This solution exposes curated Microsoft Fabric Lakehouse tables over REST without giving external consumers direct access to OneLake or storing client secrets in Azure App Settings.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ External Consumer (Script / Backend Service)                            │
│  • Authenticates with Consumer Entra App Reg (client_id + secret)       │
│  • Scope: api://<WEBAPP-AUTH-CLIENT-ID>/.default                        │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ HTTPS + Bearer Token (Consumer JWT)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Azure API Management — Basic v2 (caredoct-api-apim)                     │
│  1. Rate-limits requests per subscription / IP                          │
│  2. Validates Consumer JWT (tenant, audience, & allowed consumer appid) │
│  3. Deletes incoming Authorization header                               │
│  4. Mints & attaches APIM Managed Identity Bearer token                 │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ HTTPS + Bearer Token (APIM MI JWT)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Azure Web App — Linux Container (caredoct-api)                          │
│  1. App Service Easy Auth intercepts all requests at the edge           │
│     • Unauthenticated calls → 401 Unauthorized                          │
│     • Only accepts tokens where appid == caredoct-api-apim MI appId     │
│  2. FastAPI runs query via Web App System-Assigned Managed Identity     │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ TDS / ODBC (port 1433) + Web App MI
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Microsoft Fabric Lakehouse (SQL Analytics Endpoint)                     │
│  • OneLake Security enabled                                             │
│  • Web App Managed Identity (caredoct-api) assigned to read role        │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Azure & Entra ID Resource Inventory

| Component | Resource / Object Name | Purpose & Key Notes |
| :--- | :--- | :--- |
| **Azure Container Registry (ACR)** | `<your-acr-name>.azurecr.io` | Stores Docker images (`caredoct-api:<BuildId>` and `:latest`). |
| **Azure Web App (Linux)** | `caredoct-api` | Hosts the FastAPI container (`WEBSITES_PORT=8000`). Uses System-Assigned Managed Identity for ACR pulls and Fabric SQL auth. |
| **Web App Easy Auth App Reg** | `caredoct-api-auth` | Entra ID App Registration protecting the Web App. Exposes `api://<CLIENT-ID>`. |
| **Azure API Management** | `caredoct-api-apim` *(Basic v2)* | Public API gateway (`/icover`). No static VIP on Basic v2; relies on Easy Auth + Managed Identity instead of IP filtering. |
| **Consumer App Registration** | *(Consumer SPN)* | Existing Entra ID App Registration used by external consumer scripts to request tokens for APIM. |
| **Azure DevOps Service Connection** | `Azure-CareDoct-Connection` | Workload Identity Federation / Service Principal used by `azure-pipelines.yml` to build, deploy, and sync APIM. |

---

## 3. Configuration Breakdown (How Each Piece Is Wired)

### A. Azure Web App (`caredoct-api`)
1. **Identity (`Settings > Identity > System assigned`):**
   - **Status:** `On`
   - **Used for:** Pulling images from ACR (`AcrPull` role) and authenticating to Fabric SQL (`DefaultAzureCredential()`).
2. **Environment Variables (`Settings > Environment variables`):**
   - `WEBSITES_PORT = 8000`
   - `FABRIC_SQL_SERVER = <workspace>.datawarehouse.fabric.microsoft.com,1433`
   - `FABRIC_SQL_DATABASE = <lakehouse-name>`
   - *Important:* Do **not** set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, or `ENABLE_DEV_ENDPOINTS` here. Leaving them unset ensures `DefaultAzureCredential()` uses Managed Identity and dev routes (`/query`, `/tables`, `/queries`) stay disabled.
3. **General Settings (`Settings > Configuration > General settings`):**
   - **SCM Basic Auth Publishing Credentials:** `Off` (deployments happen via ARM in Azure DevOps).
4. **Authentication / Easy Auth (`Settings > Authentication`):**
   - **Provider:** Microsoft (`caredoct-api-auth`)
   - **App Service authentication:** `Enabled`
   - **Restrict access:** `Require authentication` *(App Service handles sign-in)*
   - **Unauthenticated requests:** `HTTP 401 Unauthorized: recommended for APIs`
   - **Allowed token audiences:** Both `<caredoct-api-auth-client-id>` and `api://<caredoct-api-auth-client-id>`
   - **Client application requirement:** `Allow requests from specific client applications` → set to **`caredoct-api-apim` Managed Identity's Application ID (`appId`)** *(Note: This is the MI's `appId` from Entra ID > Enterprise Applications > Managed Identities, NOT its Object ID)*.
   - **Enterprise App (`caredoct-api-auth`) > Properties > Assignment required?:** `No` *(Access is enforced via the Allowed client applications list above).*

---

### B. Azure API Management (`caredoct-api-apim`)
1. **Managed Identity (`Security > Managed identities > System assigned`):**
   - **Status:** `On`
2. **API Settings (`APIs > caredoct-api > Settings`):**
   - **Web service URL:** `https://azurewebsites.net`
   - **API URL suffix:** `icover`
3. **Inbound Policy (`APIs > caredoct-api > All operations > Inbound processing`):**
   ```xml
   <policies>
       <inbound>
           <base />
           <rate-limit-by-key calls="100" renewal-period="60" counter-key="@(context.Context.Subscription?.Id ?? context.Request.IpAddress)" />
           <validate-azure-ad-token tenant-id="<TENANT-ID>" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized">
               <audiences>
                   <audience>api://<WEBAPP-AUTH-CLIENT-ID></audience>
                   <audience><WEBAPP-AUTH-CLIENT-ID></audience>
               </audiences>
               <required-claims>
                   <claim name="appid" match="any">
                       <value><CONSUMER-APP-REG-CLIENT-ID></value>
                   </claim>
               </required-claims>
           </validate-azure-ad-token>
           <set-header name="Authorization" exists-action="delete" />
           <set-backend-service id="apim-generated-policy" backend-id="WebApp_caredoct-api" />
           " />
       </inbound>
       <backend>
           <base />
       </backend>
       <outbound>
           <base />
       </outbound>
       <on-error>
           <base />
       </on-error>
   </policies>
   ```
   > **Critical Rule:** Never remove `<set-header name="Authorization" exists-action="delete" />`. If the consumer's `Authorization` header is not deleted before ``, Easy Auth on the Web App will see the consumer's token instead of APIM's token and fail with `403 Forbidden`.

---

### C. Microsoft Fabric Lakehouse
1. **Workspace / Item Permissions:**
   - The Web App Managed Identity (`caredoct-api`) is added to the Fabric Workspace (**Viewer**) or Lakehouse (**ReadData**).
   - Fabric Tenant Setting **Service principals can use Fabric APIs** is enabled.
2. **OneLake Security (`Lakehouse > Manage OneLake security`):**
   - `caredoct-api` (Web App Managed Identity) is assigned to the read role covering the exposed `dbo.patient*` tables.

---

### D. CI/CD Pipeline (`azure-pipelines.yml`)
Pushing to `main` in Azure DevOps triggers an automated 3-step pipeline using `Azure-CareDoct-Connection`:
1. **Build & Push (`az acr build`):** Builds the Docker image in ACR tagged with `$(Build.BuildId)` and `latest`.
2. **Deploy (`az webapp config container set`):** Updates `caredoct-api` to run the new `$(Build.BuildId)` image tag.
3. **Sync OpenAPI (`uv run` + `az apim api import`):** Extracts `openapi.json` directly from FastAPI (`app.openapi()`) and imports it into APIM under `--api-id`. This updates endpoints and query parameters in place **without overwriting APIM policies**.

#### Required RBAC Roles for `Azure-CareDoct-Connection`
- **ACR:** `AcrPush` (and `Contributor` for `az acr build`)
- **Web App:** `Website Contributor` (or Resource Group `Contributor`)
- **APIM:** `API Management Service Contributor` (or Resource Group `Contributor`)

---

## 4. Common Day-2 Operational Tasks

### Task 1: Adding a New API Endpoint
1. Add the new `@app.get("/...")` route in `app/main.py` using parameterized `?` SQL queries.
2. Commit and push to `main` on Azure DevOps.
3. Wait for the pipeline to turn green—the new endpoint will automatically appear in APIM under `/icover/<new-route>` with all existing auth and rate-limit policies applied.

### Task 2: Onboarding a New External Consumer
1. Obtain the consumer's Entra ID App Registration **Application (client) ID** (or create a dedicated App Registration for them).
2. In **APIM > APIs > All operations > Inbound processing**, add a `<value><NEW-CONSUMER-CLIENT-ID></value>` line inside `<claim name="appid" match="any">`.
3. Provide the consumer with:
   - **Base URL:** `https://azure-api.net`
