# Azure & Fabric Infrastructure Knowledge Transfer (KT) Guide

**Project:** `lakehouse-api` (`<WEBAPP-NAME>`)  
**Resource Group:** `<RESOURCE-GROUP-NAME>`  
**Purpose:** Operational runbook and architecture reference for maintaining, troubleshooting, and onboarding consumers to the Fabric Lakehouse REST API.

---

## 1. End-to-End Architecture & Security Boundaries

This solution exposes curated Microsoft Fabric Lakehouse tables over REST without giving external consumers direct access to OneLake or storing client secrets in Azure App Settings.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ External Consumer (Script / Backend Service)                            │
│  • Authenticates with Consumer SPN (Client ID + X.509 Certificate)      │
│  • Scope: api://<CONSUMER-SPN-CLIENT-ID>/.default                       │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ HTTPS + Bearer Token (Consumer JWT)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Azure API Management — Basic v2 (<APIM-SERVICE-NAME>)                   │
│  1. Validates Consumer JWT (tenant, audience, & allowed consumer appid) │
│  2. Rate-limits per consumer appid (60 calls / 60 seconds)              │
│  3. Overwrites Authorization header with APIM Managed Identity token    │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ HTTPS + Bearer Token (APIM MI JWT)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Azure Web App — Linux Container (<WEBAPP-NAME>)                         │
│  1. App Service Easy Auth intercepts all requests at the edge           │
│     • Unauthenticated calls → 401 Unauthorized                          │
│     • Only accepts tokens where appid == <APIM-SERVICE-NAME> MI appId   │
│  2. FastAPI runs query via Web App System-Assigned Managed Identity     │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ TDS / ODBC (port 1433) + Web App MI
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Microsoft Fabric Lakehouse (F64 Capacity — SQL Analytics Endpoint)      │
│  • OneLake Security enabled                                             │
│  • Web App Managed Identity (<WEBAPP-NAME>) assigned to read role       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Azure & Entra ID Resource Inventory

| Component | Resource / Object Name | Purpose & Key Notes |
| :--- | :--- | :--- |
| **Azure Container Registry (ACR)** | `<ACR-NAME>.azurecr.io` | Stores Docker images (`<IMAGE-NAME>:<BuildId>` and `:latest`). |
| **Azure Web App (Linux)** | `<WEBAPP-NAME>` *(Basic B1–B3)* | Hosts the FastAPI container (`WEBSITES_PORT=8000`). Uses System-Assigned Managed Identity for ACR pulls and Fabric SQL auth. |
| **Web App Easy Auth App Reg** | `<WEBAPP-AUTH-APP-REG-NAME>` (`<WEBAPP-AUTH-CLIENT-ID>`) | Entra ID App Registration protecting the Web App at the edge. |
| **Azure API Management** | `<APIM-SERVICE-NAME>` *(Basic v2)* | Public API gateway (`https://<APIM-SERVICE-NAME>.azure-api.net/<API-PATH-SUFFIX>`). Subscription keys disabled; relies on Entra ID JWT validation. |
| **Consumer App Registration** | `<CONSUMER-SPN-NAME>` (`<CONSUMER-SPN-CLIENT-ID>`) | Entra ID App Registration used by external consumer with X.509 certificate auth. Exposes `api://<CONSUMER-SPN-CLIENT-ID>`. |
| **Azure DevOps Service Connection** | `<ADO-SERVICE-CONNECTION-NAME>` | Service connection used by `azure-pipelines.yml` to build, deploy, and sync APIM. |

---

## 3. Configuration Breakdown (How Each Piece Is Wired)

### A. Entra ID App Registrations
1. **Consumer App Registration (`<CONSUMER-SPN-CLIENT-ID>`):**
   - **Certificates & secrets:** Stores the consumer's public X.509 certificate (no client secrets stored in production).
   - **Expose an API:** Application ID URI set to `api://<CONSUMER-SPN-CLIENT-ID>` so Entra ID can issue tokens scoped to APIM.
2. **Web App Easy Auth App Registration (`<WEBAPP-AUTH-APP-REG-NAME>` / `<WEBAPP-AUTH-CLIENT-ID>`):**
   - Protects `<WEBAPP-NAME>.azurewebsites.net` so only APIM's Managed Identity can call the backend directly.

---

### B. Azure Web App (`<WEBAPP-NAME>`)
1. **Identity (`Settings > Identity > System assigned`):**
   - **Status:** `On`
   - **Used for:** Pulling images from `<ACR-NAME>` (`AcrPull` role) and authenticating to Fabric SQL (`DefaultAzureCredential()`).
2. **Environment Variables (`Settings > Environment variables`):**
   - `WEBSITES_PORT = 8000`
   - `FABRIC_SQL_SERVER = <FABRIC-WORKSPACE-SQL-ENDPOINT>.datawarehouse.fabric.microsoft.com,1433`
   - `FABRIC_SQL_DATABASE = <FABRIC-LAKEHOUSE-NAME>`
   - *Important:* Do **not** set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, or `ENABLE_DEV_ENDPOINTS` here. Leaving them unset ensures `DefaultAzureCredential()` uses Managed Identity and dev routes (`/query`, `/tables`, `/queries`) stay disabled.
3. **General Settings (`Settings > Configuration > General settings`):**
   - **Always On:** `On` *(Prevents Basic-tier container cold starts after 20 minutes of inactivity).*
   - **SCM Basic Auth Publishing Credentials:** `Off` (deployments happen via ARM in Azure DevOps).
4. **Authentication / Easy Auth (`Settings > Authentication`):**
   - **Provider:** Microsoft (`<WEBAPP-AUTH-APP-REG-NAME>` / `<WEBAPP-AUTH-CLIENT-ID>`)
   - **App Service authentication:** `Enabled`
   - **Restrict access:** `Require authentication` *(App Service handles sign-in)*
   - **Unauthenticated requests:** `HTTP 401 Unauthorized: recommended for APIs`
   - **Allowed token audiences:** `<WEBAPP-AUTH-CLIENT-ID>` and `api://<WEBAPP-AUTH-CLIENT-ID>`
   - **Client application requirement:** `Allow requests from specific client applications` → set to **`<APIM-SERVICE-NAME>` Managed Identity's Application ID (`appId`)** *(from Entra ID > Enterprise Applications > Managed Identities, NOT its Object ID)*.
   - **Enterprise App (`<WEBAPP-AUTH-APP-REG-NAME>`) > Properties > Assignment required?:** `No` *(Access is enforced via the Allowed client applications list above).*

---

### C. Azure API Management (`<APIM-SERVICE-NAME>`)
1. **Managed Identity (`Security > Managed identities > System assigned`):**
   - **Status:** `On`
2. **API Settings (`APIs > <APIM-API-ID> > Settings`):**
   - **Web service URL:** `https://<WEBAPP-NAME>.azurewebsites.net`
   - **API URL suffix:** `<API-PATH-SUFFIX>`
   - **Subscription required:** `Unchecked` (`false`) — authentication is enforced solely via Entra ID JWTs.
   - **Products:** Not attached to any subscription-enforced Product.
3. **Inbound Policy (`APIs > <APIM-API-ID> > All operations > Inbound processing`):**
   ```xml
   <policies>
       <inbound>
           <base />
           <validate-azure-ad-token tenant-id="<ENTRA-TENANT-ID>" header-name="Authorization" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized. Invalid or missing token.">
               <client-application-ids>
                   <application-id><CONSUMER-SPN-CLIENT-ID></application-id>
               </client-application-ids>
               <audiences>
                   <audience>api://<CONSUMER-SPN-CLIENT-ID></audience>
               </audiences>
           </validate-azure-ad-token>
           <rate-limit-by-key calls="60" renewal-period="60" counter-key="@(context.Request.Headers.GetValueOrDefault("Authorization","").AsJwt()?.Claims.GetValueOrDefault("appid", "unknown"))" />
           <set-backend-service id="apim-generated-policy" backend-id="<APIM-BACKEND-ID>" />
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
   > **Critical Execution Order Rule:** `<validate-azure-ad-token>` and `<rate-limit-by-key>` **must** appear **above** ``. APIM executes `<inbound>` sequentially top-to-bottom: `` overwrites the incoming `Authorization` header with APIM's own Managed Identity token for the Web App. If placed first, `<validate-azure-ad-token>` will inspect the wrong token and fail with `Audience is not valid`.

---

### D. Microsoft Fabric Lakehouse
1. **Workspace / Item Permissions:**
   - The Web App Managed Identity (`<WEBAPP-NAME>`) is added to the Fabric Workspace (**Viewer**) or Lakehouse (**ReadData**).
   - Fabric Tenant Setting **Service principals can use Fabric APIs** is enabled.
2. **OneLake Security (`Lakehouse > Manage OneLake security`):**
   - `<WEBAPP-NAME>` (Web App Managed Identity) is assigned to the read role covering the exposed `dbo.patient*` tables.
3. **Post-Cutover Hygiene:**
   - Ensure any legacy consumer SPN (`<CONSUMER-SPN-CLIENT-ID>`) permissions on the Fabric Workspace/Lakehouse are revoked once migrated to APIM.

---

### E. CI/CD Pipeline (`azure-pipelines.yml`)
Pushing to `main` in Azure DevOps triggers an automated 4-step pipeline using `<ADO-SERVICE-CONNECTION-NAME>`:
1. **Build & Push (`az acr build`):** Builds the Docker image in `<ACR-NAME>` tagged with `$(Build.BuildId)` and `latest`.
2. **Deploy (`az webapp config container set`):** Updates `<WEBAPP-NAME>` to run the new `$(Build.BuildId)` image tag.
3. **Generate OpenAPI (`uv run`):** Extracts `openapi.json` directly from FastAPI (`app.openapi()`).
