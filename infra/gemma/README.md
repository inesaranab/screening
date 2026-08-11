# Container 2 — the Article 9 detector

Infrastructure for `screening-gemma`: vLLM serving Gemma-4-31B-it on one A100,
consumed by `LLMGuardrailRecognizer` in the main app.

Read the "Deployment topology" section of the repo README first — it explains *why*
this is a separate container and why its ingress is internal.

## State as of 2026-08-10

| # | Step | Status |
|---|---|---|
| 1 | Model licence / HF token | ✅ not needed — `google/gemma-4-31B-it` is Apache-2.0 and ungated |
| 2 | Storage account + file share for the weights | ✅ `screeningweights` / share `models`, 100 GiB Premium SSD, Sweden Central |
| 3 | Link the share to the environment | ✅ `models` (ReadOnly) + `models-rw` (ReadWrite) |
| 4 | Download the weights onto the share | ✅ 62.58 GB in `/models/gemma-4-31b-it`, both safetensors byte-exact against HF |
| 5 | Mirror the vLLM image into ACR | ✅ `screeningacr1.azurecr.io/vllm-openai:v0.26.0` |
| 6 | Create `screening-gemma` on the A100 | ✅ boots from cold, serves, scales itself to zero |
| 7 | Move `screening-app` into `screening-env-swe` | ✅ recreated there; old West Europe app deleted |
| 8 | Point `SCREENING_LLM_GUARDRAIL_BASE_URL` at the internal FQDN | ✅ private hop verified from inside the environment |
| 9 | Merge `gemma4-guardrails` → `main` | ⬜ **next — the only step left** |

The full cycle is proven: a `minReplicas: 0` revision boots, loads 58 GiB of weights, serves
`/v1/models` over an address with no public existence, and scales back to zero on its own.

All of it was done by hand with `az`. A `deploy.sh` that wrapped the sequence was written and
then deleted: steps 1–5 were verified idempotent, but 6–8 were never executed by the script,
and doing them manually proved its step 6 was wrong. It would have needed a full teardown and
rebuild to earn the label "working", to end up with something that still has no state file,
no drift detection and no plan step. **Terraform is the intended replacement** — see the repo
tasks on importing `screening-rg`. The commands themselves are recorded per-step below and in
the vault's Azure CLI reference; `git log` has the script if it is ever wanted.

**There was never a quota problem.** The subscription had A100 quota in Sweden Central all
along (`ManagedEnvironmentConsumptionNCA100Gpus 0/2`); the zeros seen in the portal were
West Europe, which offers no A100 at any quota. Support case 2608090050000148 can be closed.
Check with `az containerapp env list-usages -n <env> -g <rg>` before ever filing another.

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

### SOLVED: why every `minReplicas: 0` revision died (2026-08-10)

For two days no scale-to-zero revision could reach a healthy state — each went straight to
`ActivationFailed`, so the app was undeployable and the only way to run the model at all was
`minReplicas: 1`, which then could never scale down and quietly billed an A100.

The answer was in `ContainerAppSystemLogs_CL`, not in anything `revision list` shows:

```
16:05:31  AssigningReplica              replica scheduled — cooldown clock starts here
16:07:23  PulledImage                   111s to pull 8.9 GB
16:08:20  ContainerStarted              vLLM begins loading weights
16:08:20  ProbeFailed (StartUp)  ×9     normal — still loading
16:10:31  ContainerTerminated           reason 'ManuallyStopped'
16:10:31  KEDAScaleTargetDeactivated    "Deactivated ... from 1 to 0"
```

`16:05:31 + 300s = 16:10:31` exactly. **The autoscaler killed it**, because
`cooldownPeriod` defaults to 300s and its clock starts when the replica is *scheduled*, not
when the container starts. The image pull and container creation consumed more than half the
budget before the model loaded a single byte.

**The fix is one line: `cooldownPeriod: 900`.** It was never an activation timeout, a probe
threshold, or anything about GPUs. With `minReplicas: 1` the autoscaler simply isn't allowed
to scale below one, which is why that configuration appeared to work — and why it hid the
bug completely.

The arithmetic that settles it: at the original 135 MiB/s, pull (112s) + start (57s) + load
(576s) = 745s, comfortably inside a 900s window. The bandwidth increase we tried alongside
made it faster but was never required.

**Lesson worth more than the fix:** `minReplicas: 1` was adopted to force a boot for
measurement. It worked, and it removed the exact component that was failing. When a
workaround makes a symptom disappear, it has hidden the evidence, not diagnosed anything.

## Four rules about revisions, and how to check them

Learned the expensive way on 2026-08-09/10/11.

### 0. PURGE old revisions before updating, never deactivate after

This is the rule that would have prevented every billing incident below.

`az containerapp revision deactivate` stops a revision running. It does **not** remove it,
and it does **not** change the app's template. A deactivated revision keeps whatever
`minReplicas` it was born with, and the next update reactivates it -- which starts a replica
immediately, on a GPU if that is what it runs on.

Wrong, and repeated five times:

```bash
az containerapp update ...                    # resurrects old revisions
az containerapp revision deactivate ...       # notice and clean up afterwards
```

Right:

```bash
# 1. list every revision and its baked-in minReplicas
az containerapp revision list -n <app> -g <rg> \
  --query "[].{rev:name, active:properties.active, min:properties.template.scale.minReplicas}" -o table

# 2. deactivate every revision that carries min>0, BEFORE touching the app
az containerapp revision deactivate -n <app> -g <rg> --revision <NAME>

# 3. only now update
az containerapp update ...

# 4. verify, every time
az containerapp revision list -n <app> -g <rg> \
  --query "[?properties.active].{rev:name, replicas:properties.replicas, min:properties.template.scale.minReplicas}" -o table
```

### 0b. The app template and its revisions are two different things

Deactivating every revision stops everything running but leaves the **app template**
unchanged. If the template says `minReplicas: 1`, the next revision created from it -- by any
update, for any reason -- starts a replica. Check both:

```bash
az containerapp show -n <app> -g <rg> --query "properties.template.scale.minReplicas" -o tsv   # the recipe
az containerapp revision list -n <app> -g <rg> --query "sum([].properties.replicas)" -o tsv    # what is running
```

A GPU app was left with a template of `minReplicas: 1` overnight on 2026-08-10. Nothing ran,
because every revision was deactivated -- but fixing the template the next morning
immediately started an A100, because the fix itself is an update.

### 1. Revisions are immutable

Any change to `properties.template` — image, args, `minReplicas`, `cooldownPeriod` — creates a
*new* revision. You can never edit the one that is running.

```bash
az containerapp revision list -n screening-gemma -g screening-rg \
  --query "[].{rev:name, created:properties.createdTime, active:properties.active}" -o table

az containerapp revision show -n screening-gemma -g screening-rg \
  --revision <NAME> --query "properties.template" -o yaml
```

### 2. Each revision carries its own scale settings

A revision you deactivated still remembers `minReplicas: 1`. Anything that reactivates it --
and `az containerapp update --yaml` does -- starts a replica immediately and bills for it.
This happened three times in one morning, twice on an A100.

**Run this after every `--yaml` update.** Any row with `active=True` and `min=1` is billing:

```bash
az containerapp revision list -n screening-gemma -g screening-rg \
  --query "[].{rev:name, active:properties.active, state:properties.runningState, \
replicas:properties.replicas, min:properties.template.scale.minReplicas, \
cooldown:properties.template.scale.cooldownPeriod}" -o table

az containerapp revision deactivate -n screening-gemma -g screening-rg --revision <NAME>
```

### 3. The cooldown clock starts when the replica is SCHEDULED

Not when the container starts. With the default 300s cooldown, the image pull (112s) and
container creation (57s) ate more than half the budget before vLLM began loading, so KEDA
killed the replica mid-load and the revision went to `ActivationFailed` -- every time. The app
was undeployable and the cause was invisible from `revision list` alone.

The system log is where the answer was:

```bash
WS=$(az monitor log-analytics workspace show -g screening-rg \
       -n workspacescreeningrg9322 --query customerId -o tsv)

az monitor log-analytics query -w "$WS" --analytics-query "
ContainerAppSystemLogs_CL
| where RevisionName_s == '<REVISION>'
| where Reason_s in ('AssigningReplica','PulledImage','ContainerStarted',
                     'ContainerTerminated','KEDAScaleTargetDeactivated','ProbeFailed')
| project TimeGenerated, Reason_s, Log_s
| order by TimeGenerated asc" -o table
```

The interval from `AssigningReplica` to `KEDAScaleTargetDeactivated` is your cooldown, and it
must exceed pull + start + weight load. Timestamps are **UTC**; Spain is CEST (UTC+2).

### Am I being charged right now?

```bash
for a in screening-app screening-gemma; do
  echo "=== $a ==="
  az containerapp revision list -n $a -g screening-rg \
    --query "[].{rev:name, state:properties.runningState, replicas:properties.replicas}" -o table
done
```

Every `replicas` column at zero means nothing is running. **Do not use
`az containerapp replica list` without `--revision`** -- it reports only the newest revision,
and once showed an empty list while an A100 billed for two more hours.

### Blue/green: how this should have been done

Step 7 recreated `screening-app` by deleting it first. That is acceptable here only because
nothing depends on the old URL. In production you never delete the thing that is serving.

Container Apps has this built in, via revisions:

```bash
# 1. allow more than one revision to be live at once (default is Single)
az containerapp revision set-mode -n <app> -g <rg> --mode multiple

# 2. deploy the new version; it comes up as a new revision, taking no traffic
az containerapp update -n <app> -g <rg> --image <new-image> --revision-suffix v2

# 3. send it a slice of real traffic
az containerapp ingress traffic set -n <app> -g <rg> \
  --revision-weight <old-revision>=90 <new-revision>=10

# 4. watch, then shift the rest -- or roll back instantly by reverting the weights
az containerapp ingress traffic set -n <app> -g <rg> --revision-weight <new-revision>=100

# 5. retire the old revision once you are confident
az containerapp revision deactivate -n <app> -g <rg> --revision <old-revision>
```

The rollback is the point: step 4 reversed is one command and takes seconds, with no rebuild
and no redeploy. Delete-and-recreate has no equivalent -- if the new app fails to start, the
old one no longer exists.

Note this only works across *revisions of one app*. Moving between environments (what step 7
did) cannot use it, because an app cannot span environments. The blue/green version of a
region move is: create the new app under a temporary name, verify it, repoint DNS, then
delete the old one.

### Other notes

The `gpu-a100` profile had to be defined at environment-creation time — Azure does not allow
adding a GPU profile to an existing environment. It was accepted despite zero quota, because
quota is enforced when a replica is scheduled, not when the profile is declared.

## Share bandwidth: what it buys, what it costs

Measured 2026-08-10. Weight load is the whole cold start, and it runs at exactly the share's
provisioned rate — the read strategy makes no difference (see `vllm-app.yaml`).

| Provisioned | Weight load | Cost/month |
|---|---|---|
| **135 MiB/s** (current) | 4m17s → **9m36s** | **€9.86** |
| 550 MiB/s | **4m17s** | €40.15 |

€0.0001 per MiB/s per hour, SSD LRS, Sweden Central. Charged whether the share is read or not
— it is provisioned capacity, not usage. ~€30/month to halve the cold start; not worth it for
a demo, worth revisiting if real traffic arrives.

```bash
az storage share-rm update --storage-account screeningweights -g screening-rg \
  -n models --provisioned-bandwidth-mibps 550
```

**Increases apply instantly; decreases are blocked for 24 hours.** Check before assuming you
can undo an experiment:

```bash
az storage share-rm show --storage-account screeningweights -g screening-rg -n models \
  --query "{bandwidth:provisionedBandwidthMibps, nextDowngrade:nextAllowedProvisionedBandwidthDowngradeTime}"
```

## Cold start — measured, 2026-08-10

| Phase | Time |
|---|---|
| Pull the 8.9 GB vLLM image | ~2 min |
| vLLM engine init | ~1 min |
| Load 58.25 GiB of weights off the share | **9m36s** at 135 MiB/s |
| **Total** | **~13 min** |

Weight loading dominates completely, and it runs at exactly the share's provisioned rate.
All four measurements, so nobody has to re-run them:

| Bandwidth | Load strategy | First shard | Total load |
|---|---|---|---|
| 135 MiB/s | lazy (default) | 343.6s | 9m36s |
| 135 MiB/s | eager | 386.8s | 8m04s |
| 550 MiB/s | eager | 254.7s | 5m24s |
| 550 MiB/s | lazy (default) | **203.9s** | **4m17s** |

Two conclusions:

- **Bandwidth is the only real lever.** Doubling it roughly halves the load. See "Share
  bandwidth" above for what that costs.
- **`--safetensors-load-strategy eager` is a trap.** vLLM's own startup log recommends it for
  network filesystems and Azure Files is one — but it was *slower* at both bandwidths. Both
  strategies sit at the share's ceiling, so the read pattern was never the limit. Not set;
  do not re-add without a measurement.

13 minutes is the accepted trade for an A100 that costs nothing while idle. But it is not
merely slow — see below, it makes the synchronous design impossible.

## BLOCKER: Azure's ingress cuts every request at 240 seconds

**A synchronous `/screen` against a cold detector cannot work on Consumption ingress.** Not
"is slow" — the platform hangs up at 4 minutes and the model needs 13.

Measured 2026-08-10, from inside `screening-app`, calling a cold `screening-gemma`:

```
15:44:28 UTC  request sent
15:48:28 UTC  urllib.error.HTTPError: HTTP Error 504: Gateway Timeout
              = 240 seconds, to the second
```

A **504** is the edge proxy giving up on the upstream, not our client timing out. Confirmed
against the docs — [Ingress in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)
lists `Request time out is 240 seconds` as a fixed property of HTTP ingress.

### What was ruled out, and why

**Raising `llm_guardrail_timeout_s` does not help.** That controls how long *our client* waits.
The proxy severs the connection at 240s regardless, so the app never gets the chance to be
patient. Both fixes were needed for different reasons and neither solves this one.

**Calling by app name instead of FQDN does not help.** The docs say calls by app name go
"directly to app B" while FQDN calls route via the edge proxy, so this looked like a free fix.
Tested: `http://screening-gemma/v1/models` resolves, routes, and *does* trigger the cold start
— then dies at exactly 240s with the same 504. The timeout applies either way.

**Premium ingress can raise it** (idle request timeout, 4–30 min) but requires a non-Consumption
workload profile, D4–D32, minimum two node instances, billed continuously. That removes the
scale-to-zero economics this whole architecture exists to preserve.

### What remains

| Option | Cost | Keeps scale-to-zero |
|---|---|---|
| **202 + poll** | ~€40/mo (warm CPU app only) | ✅ |
| Keep the GPU warm | ~€2.16/hr ≈ €1,570/mo | ❌ |
| Premium ingress | 2× dedicated D4+ nodes, continuous | ❌ |

**202 + poll is the only one that survives contact with the cost model.** Each HTTP request
returns in milliseconds, so the 240s ceiling never applies; the 13-minute wait happens
*between* polls rather than inside one request.

It needs external job state — `screening-app` runs up to 10 replicas, so an in-memory dict
would let a poll hit a replica that knows nothing about the job. The `screeningweights`
storage account is already there; Table Storage is the cheap fit. The background work also has
to outlive the request, which means either `minReplicas: 1` on the CPU app or a queue-triggered
Container Apps Job like `download-weights`.

This is also the shape that makes batch natural: submit N transcripts, pay one cold start,
amortize it. At 50 transcripts a 13-minute boot is ~15s each; at one transcript it is absurd.

**Not built.** Until it is, any demo must warm the detector first.

### Reproducing it

```bash
az containerapp update -n screening-app -g screening-rg --min-replicas 1   # remember to undo
az containerapp exec -n screening-app -g screening-rg --command /bin/sh
```
```sh
date; python -c "import urllib.request,time; t=time.time(); r=urllib.request.urlopen('http://screening-gemma/v1/models',timeout=1800).read().decode(); print(round(time.time()-t),'s')"; date
```

The two `date` stamps bracket the failure. Anything at ~240s is the proxy, not the app.

## Files

- `download-weights-job.yaml` — one-shot job that fills the share. Runs on the CPU profile;
  using the GPU profile would burn A100 minutes on pure I/O.
- `vllm-app.yaml` — the `screening-gemma` app. Internal ingress, `gpu-a100`, scale to zero.

Both are templates with `${...}` placeholders, so neither is directly appliable. Substitute
the values, then `az containerapp [job] create --yaml <file>`. They exist because volume
mounts and probes have no CLI flag — everything else here was done with plain `az`.

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
