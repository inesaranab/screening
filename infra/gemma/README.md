# Container 2 — the Article 9 detector

Infrastructure for `screening-gemma`: vLLM serving Gemma-4-31B-it on one A100,
consumed by `LLMGuardrailRecognizer` in the main app.

Read the "Deployment topology" section of the repo README first — it explains *why*
this is a separate container and why its ingress is internal.

## State as of 2026-08-09

| # | Step | Status |
|---|---|---|
| 1 | Model licence / HF token | ✅ not needed — `google/gemma-4-31B-it` is Apache-2.0 and ungated |
| 2 | Storage account + file share for the weights | ✅ `screeningweights` / share `models`, 100 GiB Premium SSD, Sweden Central |
| 3 | Link the share to the environment | ✅ `models` (ReadOnly) + `models-rw` (ReadWrite) |
| 4 | Download the weights onto the share | ✅ 62.58 GB in `/models/gemma-4-31b-it`, both safetensors byte-exact against HF |
| 5 | Mirror the vLLM image into ACR | ✅ `screeningacr1.azurecr.io/vllm-openai:v0.26.0` |
| 6 | Create `screening-gemma` on the A100 | ✅ serving; `Application startup complete`, `/health` 200 |
| 7 | Move `screening-app` into `screening-env-swe` | ⬜ **next** |
| 8 | Point `SCREENING_LLM_GUARDRAIL_BASE_URL` at the internal FQDN | ⬜ needs 7 |
| 9 | Merge `gemma4-guardrails` → `main` | ⛔ needs 8 |

**There was never a quota problem.** The subscription had A100 quota in Sweden Central all
along (`ManagedEnvironmentConsumptionNCA100Gpus 0/2`); the zeros seen in the portal were
West Europe, which offers no A100 at any quota. Check with
`az containerapp env list-usages -n <env> -g <rg>` before ever filing a support case again.

`deploy.sh` automates all of it and every step is idempotent, so re-running after a
partial failure is the intended recovery path. `./deploy.sh` runs everything;
`./deploy.sh weights` runs one step.

The weights download took **7m37s**, not the 30–45 minutes first estimated — Xet plus
in-region bandwidth, rather than the share's 135 MiB/s write ceiling, is what governs.
That also makes the cold-start figure below pessimistic; the first GPU boot will settle it.

### The blocker

Support request **2608090050000148**, opened 2026-08-09, amended the same day to ask for
`Managed Environment Consumption NC24-A100 Gpus` = 1 in **Sweden Central** against
environment `screening-env-swe`.

The original request was for a T4 in West Europe and was wrong twice over: a T4 has 16 GB
of VRAM against the ~62 GB this model needs in bf16, and West Europe does not offer A100
at all. Sweden Central and Italy North do; both are EU regions, so the data-residency
argument that motivated self-hosting in the first place still holds.

**Check the ticket before doing anything else.** Nothing past step 5 can proceed without it.

### Do not merge the branch early

`deploy.yml` fires on push to `main`, and the branch code fails closed when the Gemma
endpoint is unreachable. Merging before step 8 takes production down. This is by design —
a guardrail that silently passes unredacted text through would be worse.

## Existing resources

| Resource | Region | Notes |
|---|---|---|
| `screening-env-swe` | Sweden Central | Workload profiles; `Consumption` + `gpu-a100` (`Consumption-GPU-NC24-A100`) |
| `screeningweights` | Sweden Central | Premium SSD file share `models`, 100 GiB, ~135 MiB/s |
| `screening-env` | West Europe | The old environment. Holds live `screening-app`; retired after step 7 |
| `screeningacr1` | West Europe | Basic SKU. Its 10 GB is a billing *allowance*, not a cap (real limit 40 TB) — the registry already holds 27 GB. Weights are still on a share, because a 62 GB image would be pulled on every cold start |
| `placeholder` app | Sweden Central | Throwaway created only to reach the environment form. **Delete it.** |

### The image pull must use a user-assigned identity

`screening-gemma` pulls from a private registry. Creating it with a *system-assigned*
identity does not work: that identity is born with the app, so it cannot be granted AcrPull
until after the app has already attempted its first pull. That pull fails, the revision
fails, `provisioningState` becomes `Failed` — and a Failed app can only be deleted, not
updated. The `screening-identity` user-assigned identity exists independently, so it can be
granted AcrPull first and handed to the app at creation. Hit and fixed on 2026-08-09.

### Other notes

The `gpu-a100` profile had to be defined at environment-creation time — Azure does not allow
adding a GPU profile to an existing environment. It was accepted despite zero quota, because
quota is enforced when a replica is scheduled, not when the profile is declared.

## Cold start — measured, 2026-08-09

| Phase | Time |
|---|---|
| Pull the 8.9 GB vLLM image | ~2 min |
| vLLM engine init | ~1 min |
| Load 58.25 GiB of weights off the share | **~10 min** |
| **Total** | **~13 min** |

Weight loading dominates, and the share is the bottleneck: 49.8 GB in 343s ≈ 145 MB/s,
which is the share's provisioned 135 MiB/s. Two levers, neither tried yet:

- **Raise the share's provisioned throughput** to 550 MiB/s. Possible in place, no
  recreation — that is what Provisioned v2 buys.
- **Force vLLM's prefetch.** Its own log says: *"Auto-prefetch is disabled because the
  filesystem (CIFS) is not a recognized network FS (NFS/Lustre). If you want to force
  prefetching, start vLLM with --sa…"* (flag truncated in the log; find the full name in
  `vllm serve --help`). Azure Files is SMB/CIFS, so this optimisation is off by default.

13 minutes is the accepted trade for an A100 that costs nothing while idle. Making it
invisible to callers means `/screen` should return 202 and be polled rather than blocking —
screening is asynchronous work and nobody waits on a transcript in real time. Not built.

## Files

- `deploy.sh` — every step, idempotent. Run it whole or one step at a time.
- `download-weights-job.yaml` — one-shot job that fills the share. Runs on the CPU profile;
  using the GPU profile would burn A100 minutes on pure I/O.
- `vllm-app.yaml` — the `screening-gemma` app. Internal ingress, `gpu-a100`, scale to zero.

The two YAML files are templates with `${...}` placeholders and are not directly appliable —
`deploy.sh` substitutes them into a temp directory before calling `az`.

## Where every field comes from

Nothing here is invented. The schema is Azure's own ARM resource body for
`Microsoft.App/jobs` and `Microsoft.App/containerApps`, which `az containerapp [job] create
--yaml` accepts almost verbatim. Field-by-field provenance, so none of it has to be taken
on trust:

| Field | Source |
|---|---|
| `properties.configuration.triggerType`, `replicaTimeout`, `replicaRetryLimit` | [ARM/YAML spec](https://learn.microsoft.com/en-us/azure/container-apps/azure-resource-manager-api-spec) → *Container Apps job* → `properties.configuration` |
| `manualTriggerConfig.parallelism`, `.replicaCompletionCount` | same page, job YAML example |
| `template.containers[].volumeMounts[].{volumeName,mountPath}` | [Azure Files tutorial](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts-azure-files) step 8, with a property table |
| `template.volumes[].{name,storageName,storageType: AzureFile}` | same tutorial, step 7, with a property table |
| `template.containers[].command` / `args` | ARM/YAML spec, `initContainers` example |
| `properties.workloadProfileName` | ARM/YAML spec, container app example |
| `probes[].type: Startup` + `failureThreshold` | [Health probes](https://learn.microsoft.com/en-us/azure/container-apps/health-probes) — Startup is one of three supported types; `failureThreshold` is documented as optional |
| `az containerapp env storage set` and its flags | [Azure Files tutorial](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts-azure-files), *Create the storage mount* |
| `vllm serve <model> --served-model-name --max-model-len --gpu-memory-utilization` | vLLM online-serving docs / `vllm serve --help` |

Two statements from those pages that this setup depends on, quoted rather than paraphrased:

- *"When you configure a container app to mount an Azure Files volume by using Azure CLI, you
  must use a YAML definition"* — why two steps are YAML and the rest is plain `az`.
- *"Container Apps does not support identity-based access to Azure file shares"* — why the
  storage account key is used, and why key access must stay enabled on the account.

**A quicker route than reading any of this:** create a resource with plain CLI flags, then
`az containerapp show -n <app> -g <rg> -o yaml`. That dumps a complete working body to edit
down, which is what the tutorial itself does. Existing resources are the best templates.
