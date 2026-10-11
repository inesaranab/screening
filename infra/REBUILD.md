# Rebuilding the screening service on Azure

The Azure deployment was taken down on 2026-10-11 to stop its costs (about €15 a month while idle: €9 the
weights share, €4 the registry, €1 the backup). By then the subscription was disabled, so the apps and jobs had
already lost their containers. This page and `snapshot-2026-10-11/` are what is needed to build it again.

**What was kept:** every resource's name, region and SKU, and the settings of everything except the two apps
and two jobs (`snapshot-2026-10-11/inventory.md`, and
`resource-group.arm.json`, Azure's export of the resource group with the Log Analytics default tables and
saved searches removed — 1,452 auto-generated resources that come back with any workspace — and the errors
listed in `export-warnings.txt`), and the templates already in this repository (`infra/worker-job.yaml`,
`infra/gemma/*.yaml`, the `Dockerfile`, `.github/workflows/deploy.yml`).

**What was not kept, and how it comes back:**

| Lost | Comes back from |
|---|---|
| The app image in `screeningacr1` | `az acr build` from the `Dockerfile` (the deploy workflow does it) |
| The vLLM image in `screeningacr1` (`vllm-openai:v0.26.0`, a mirror) | `az acr import` from the public image, step 4 below; `az acr build` does not bring it back |
| The Gemma weights (62.6 GB on the `models` share) | the `download-weights` job (`infra/gemma/download-weights-job.yaml`) |
| The secret values | you. The templates read two: `screening-service-api-key` and `portkey-api-key` (`infra/worker-job.yaml`). `screening-openrouter` was also in the vault, but nothing deploys it: the worker passes that string as a plain env value (`SCREENING_PORTKEY_VIRTUAL_KEY`) — it is the name of a Portkey virtual key, configured in Portkey, not a secret the app fetches |
| The apps' and jobs' configuration (containers, env, secret references, scale, ingress, volumes). The export ran after the subscription was disabled, so `app-*.yaml`, `job-*.yaml` and the ARM entries hold `template: {}`, and the `triggerType: Schedule` they show on both jobs is the empty export's default, not what ran | the templates: `infra/gemma/vllm-app.yaml` (detector), `infra/gemma/download-weights-job.yaml` (Manual trigger), `infra/worker-job.yaml` (Event trigger). `screening-app` has no template in this repository; step 10 says what it needs |
| Job history (table and queue in `screeningjobs`) and the share's backups | not recoverable |

## Order

Each step needs the one before it. Names are the old ones; any free name works if the templates' placeholders
are filled to match — except in `.github/workflows/deploy.yml`, which hard-codes `screeningacr1`, `screening-app`
and `screening-rg` (step 10).

1. **Resource group** `screening-rg`, in Sweden Central (the GPU region) — the old one mixed West Europe and
   Sweden Central; one region is simpler.
2. **User-assigned identity** `screening-identity`. It must exist before the apps: an app given a
   system-assigned identity tries its first image pull before it can be granted `AcrPull`, fails, and a
   Failed app can only be deleted (`infra/gemma/README.md`, "The image pull must use a user-assigned identity").
3. **Key Vault** with the three secrets above; grant the identity **Key Vault Secrets User** on it.
4. **Container registry** (Basic); grant the identity **AcrPull**; build the app image and mirror the vLLM one
   (`infra/gemma/vllm-app.yaml` pulls `vllm-openai:${VLLM_TAG}` from this registry, not from Docker Hub):
   `az acr build --registry <acr> --image screening:latest .`
   `az acr import --name <acr> --source docker.io/vllm/vllm-openai:v0.26.0 --image vllm-openai:v0.26.0`
   The build command is the one `deploy.yml` runs. The import command is untested: `infra/gemma/README.md`
   records that the image was mirrored, not how, and the old registry could not be listed when the
   inventory ran. The export shows the registry with `adminUserEnabled: true`; the pull uses the identity, so
   create it with the admin user off.
5. **Storage:**
   - `screeningjobs` (StorageV2, Standard LRS) with table `jobs` and queue `screenings` (the defaults in
     `app/config.py`); grant the identity **Storage Table Data Contributor** and **Storage Queue Data
     Contributor**. The export records this account with `minimumTlsVersion: TLS1_0`; create it with
     `TLS1_2`, as `screeningweights` was.
   - `screeningweights` (FileStorage, Premium) with a 100 GiB share `models`. The size is from
     `infra/gemma/README.md`, not the export: `fileServices` is among the types it could not read
     (`export-warnings.txt`), and the account was already disabled when the inventory ran.
6. **Container Apps environment** with workload profiles `Consumption` and `Consumption-GPU-NC24-A100`
   (named `gpu-a100`). Link the `models` share to it twice: as environment storage `models` (ReadOnly, used by
   `vllm-app.yaml`) and `models-rw` (ReadWrite, used by `download-weights-job.yaml`). Linking an Azure Files
   share takes the storage account's key (`az containerapp env storage set --azure-file-account-key`, the
   tutorial cited in `infra/gemma/README.md`); the export carries no keys, so read it from the new account
   with `az storage account keys list`. GPU quota in the region must be requested first if it is not already
   granted.
7. **The weights:** run the `download-weights` job once.
8. **The detector** `screening-gemma` from `infra/gemma/vllm-app.yaml` (vLLM serving `google/gemma-4-31B-it`
   from the share, scale to zero). Read `infra/gemma/README.md`'s rules about revisions before any update.
9. **The worker** `screening-worker` from `infra/worker-job.yaml` (event-triggered by the queue, 0 to 3
   executions).
10. **The API** `screening-app` (Consumption profile). No template for it exists in this repository and the
    export kept none of its settings, so it is built from what the code requires: the worker's image with
    the image's own `CMD` (`fastapi run`, port 8000 — `Dockerfile`), external ingress on that port, the
    user-assigned identity for the registry pull and Key Vault, the secrets `service-api-key` and
    `portkey-api-key`, and the `SCREENING_*` and `AZURE_CLIENT_ID` env that `infra/worker-job.yaml` gives
    the worker — both construct the same `Settings`. Its old replica range and CPU/memory were not
    recorded. Then the deploy workflow: its secrets
    (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) name an Entra app registration with a
    federated credential for this GitHub repository, which lives in the tenant, not the resource group, and
    is not in the export, and its old role assignment was not recorded. It needs a role that can push to the
    registry and update the app; Contributor on the resource group covers both. Edit the three names
    hard-coded in `deploy.yml` if they changed. Two more are hard-coded in the tests: `conftest.py` reads
    the key from vault `screening-kv-7412`, and `tests/integration/test_prod.py` calls
    `screening-app.grayhill-6c021b7d.westeurope.azurecontainerapps.io` — a new environment gets a new
    domain, and that one was already the West Europe environment's (`env-screening-env.yaml`), while the
    app ran in `screening-env-swe` (`whitesand-6e33b8c1.swedencentral`).

Not needed: the export also holds a Language service `screening-pii-lang` (TextAnalytics F0). Nothing in
`app/` calls it; it was the "consider Azure AI Language" idea in the README, never wired in.

The templates take these placeholders: `${ACR_LOGIN_SERVER} ${ENV_ID} ${GEMMA_URL} ${HF_REPO} ${HF_REVISION}
${IMAGE_TAG} ${JOBS_ACCOUNT} ${JOBS_QUEUE} ${KV_URL} ${MODEL_DIR} ${REGION} ${SERVED_MODEL_NAME}
${UAMI_CLIENT_ID} ${UAMI_ID} ${VLLM_TAG}`.

## Costs to expect

Idle, with every app scaled to zero: about €15 a month (share, registry, backup if kept). The GPU (A100) costs
only while the detector is awake; a screening that wakes it pays for the minutes it runs.
