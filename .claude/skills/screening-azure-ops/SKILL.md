---
name: screening-azure-ops
description: Operating the Screening service's Azure infrastructure without accidentally starting a GPU. Rules for Container Apps revisions, scale-to-zero, checking what is billing, and the traps that have cost real money on this subscription. Use before ANY az containerapp command, before deploying, and whenever asked whether something is running or costing money.
---

# Screening — Azure operations

The GPU app (`screening-gemma`) runs an A100 at **~€2.16/hour**. Every rule here exists
because it was broken once and billed for it.

## Rule 0 — Name the subscription explicitly in every command

Without `--subscription`, every command below runs against whatever the CLI's default
happens to be. A preflight check can then report on one subscription while the GPU is
billing on another, which makes "nothing is running" a false negative rather than an answer.

```bash
export AZURE_SUBSCRIPTION_ID=<id>   # once per session
```

Append `--subscription "$AZURE_SUBSCRIPTION_ID"` to every `az containerapp` command in this
file, and use the same id in the `az rest` URL. The id is not written here — this file is in
a public repository.

## Rule 1 — Check what is running BEFORE starting work, not only after

Two separate incidents cost money because a session began without checking state: an A100
left running for 1h38m (~€3.50), and an app left at `minReplicas: 1` overnight.

```bash
for a in screening-app screening-gemma; do
  echo "$a: replicas=$(az containerapp revision list -n $a -g screening-rg --subscription "$AZURE_SUBSCRIPTION_ID" --query 'sum([].properties.replicas)' -o tsv)"
  echo "  app-template min=$(az containerapp show -n $a -g screening-rg --subscription "$AZURE_SUBSCRIPTION_ID" --query 'properties.template.scale.minReplicas' -o tsv)"
done
```

Both lines matter — see Rule 3.

## Rule 2 — PURGE revisions before updating, never deactivate after

`az containerapp update` **reactivates deactivated revisions**. A revision that was born with
`minReplicas: 1` immediately starts a replica when reactivated. This has happened five times.

Wrong:
```bash
az containerapp update ...                  # resurrects old revisions
az containerapp revision deactivate ...     # clean up after noticing
```

Right:
```bash
# 1. find every revision carrying min>0
az containerapp revision list -n <app> -g screening-rg \
  --query "[].{rev:name, active:properties.active, min:properties.template.scale.minReplicas}" -o table
# 2. deactivate those FIRST
az containerapp revision deactivate -n <app> -g screening-rg --revision <NAME>
# 3. then update
# 4. then verify replicas again
```

## Rule 3 — The app template and its revisions are different things

Deactivating every revision stops everything running. It does **not** change the app
template. If the template says `minReplicas: 1`, the next revision created from it — by any
update, for any reason — starts a replica.

Fixing the template is itself an update, so it starts a GPU. Expect that and plan for it:
either use the boot for something needed, or deactivate immediately after.

## Rule 4 — `replica list` without `--revision` lies

It reports only the newest revision. It once showed an empty list while an A100 billed for
two more hours. Always use `revision list` with a `sum()`, or pass `--revision` explicitly.

## Rule 5 — An interrupted command may still have run

A rejected/interrupted tool call reached Azure anyway on 2026-08-10 and started an A100 that
ran unnoticed for 1h38m. After any interrupted `az` command, **verify state** rather than
assuming it did not execute.

## Rule 6 — A new revision always boots once, even with minReplicas: 0

It must prove itself healthy. On the GPU app that is a ~13 minute, ~€0.50 boot. Never create
a revision on `screening-gemma` casually.

## Verifying independently

When the answer matters, check by a second path:

```bash
# every container app in the whole subscription, not just the ones you remember
az containerapp list --query "[].{name:name, rg:resourceGroup, min:properties.template.scale.minReplicas}" -o table

# actual spend, a completely separate data path
az rest --method post \
  --url "https://management.azure.com/subscriptions/<SUB>/providers/Microsoft.CostManagement/query?api-version=2023-11-01" \
  --body '{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"Daily","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}},"grouping":[{"type":"Dimension","name":"ServiceName"}]}}'
```

Idle baseline for this subscription is **~€0.17–0.60/day**. A day above that means something
ran.

## Known costs

| Thing | Cost |
|---|---|
| A100 (`Consumption NC24-A100`) | €2.16/hour, only while a replica runs |
| Premium file share, 100 GiB @ 135 MiB/s | ~€25/month, always |
| Everything else idle | ~€5/month |

Full reasoning and measurements: `infra/gemma/README.md`.
