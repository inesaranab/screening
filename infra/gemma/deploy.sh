#!/usr/bin/env bash
#
# Stand up (or rebuild) the two-container topology described in the repo README
# under "Deployment topology".
#
#   screening-env-swe            managed environment, Sweden Central
#     |- screening-app           CPU, external ingress, the public API
#     '- screening-gemma         A100, INTERNAL ingress, vLLM + Gemma-4-31B
#
# Steps, in order:
#
#   1. env       Create the environment and its GPU workload profile. The GPU
#                profile must exist from the start; it cannot be added later.
#   2. storage   Create the storage account and the 100 GiB file share that
#                holds the model weights.
#   3. link      Register that share with the environment, twice: read-only for
#                vLLM, read-write for the download job. A container can only
#                mount storage the environment already knows by name.
#   4. weights   Run a throwaway CPU container that downloads 62.6GB of model
#                weights from Hugging Face onto the share. Took 7m37s.
#   5. image     Copy the vLLM image from Docker Hub into our own registry, so
#                the app never pulls from the public internet.
#   6. gpu_app   Create screening-gemma: vLLM on the A100, no public address,
#                reading the weights off the share.
#   7. move_api  Recreate screening-app in this environment. An app cannot move
#                between environments, and it has to sit beside the GPU app to
#                be able to reach it privately.
#   8. wire      Tell screening-app the GPU app's private address. Only after
#                this is it safe to merge the branch to main.
#
# Every step is idempotent: it checks whether the resource already exists and
# skips rather than failing. Re-running the whole script after a partial failure
# is the intended recovery path.
#
# Usage:
#   ./deploy.sh              run every step in order
#   ./deploy.sh weights      run one step by name (see STEPS at the bottom)
#

set -euo pipefail

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

RG=screening-rg
REGION=swedencentral
ENV=screening-env-swe
GPU_PROFILE=gpu-a100

STORAGE=screeningweights
SHARE=models

ACR=screeningacr1
VLLM_TAG=v0.26.0
# The image is copied from Docker Hub into our own registry so the app never
# pulls from the public internet. v0.26.0 was the newest stable release; the
# plain tag bundles builds for both Intel/AMD and ARM chips, so it is 1.5GB
# bigger. Azure's GPU machines are Intel/AMD, hence the x86_64-only build.
VLLM_UPSTREAM=docker.io/vllm/vllm-openai:v0.26.0-x86_64

HF_REPO=google/gemma-4-31B-it
MODEL_DIR=gemma-4-31b-it
# Measured total of the completed download, used to tell a finished download
# from a partial one. Slightly under the real 62,578,686,074 so that rounding or
# a stray extra file never makes a complete download look incomplete.
# Update this if the model changes.
WEIGHTS_BYTES=62500000000
# Must match settings.llm_guardrail_model, since this is the name the app sends
# in the `model` field of its OpenAI-compatible request.
SERVED_MODEL_NAME=google/gemma-4-31B-it

API_APP=screening-app
GPU_APP=screening-gemma
KV=screening-kv-7412
UAMI=screening-identity

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
skip() { printf '    (exists, skipping) %s\n' "$*"; }

# Fill ${...} placeholders in a template and echo the path of the result.
# Deliberately not envsubst: it is not installed by default on macOS.
#
# The `|| return 1` is load-bearing. Without it the function ends in `echo`,
# which succeeds, so a failed substitution would be reported as a success and
# the caller would go on to apply a half-written file. Found the hard way: a
# literal "$KEY" in a template comment blew up the substituter, the traceback
# was printed, and the script cheerfully continued to the next command.
render() {
  local src="$1" dst="$WORK/$(basename "$1")"
  python3 -c "
import os, string, pathlib, sys
pathlib.Path(sys.argv[2]).write_text(
    string.Template(pathlib.Path(sys.argv[1]).read_text()).substitute(os.environ))
" "$src" "$dst" || return 1
  echo "$dst"
}

# --------------------------------------------------------------------------
# 1. Managed environment
# --------------------------------------------------------------------------
step_env() {
  log "1. Managed environment $ENV"
  if az containerapp env show -n "$ENV" -g "$RG" &>/dev/null; then
    skip "$ENV"
  else
    az containerapp env create -n "$ENV" -g "$RG" -l "$REGION" \
      --enable-workload-profiles -o none
  fi

  # The GPU profile may not be addable after the environment exists: the portal
  # warns that GPU workload profiles can only be added at creation time. This
  # `add` is here for completeness, but if it fails, recreate the environment
  # with the profile rather than fighting the CLI.
  if az containerapp env workload-profile list -n "$ENV" -g "$RG" \
       --query "[?name=='$GPU_PROFILE']" -o tsv | grep -q .; then
    skip "workload profile $GPU_PROFILE"
  else
    az containerapp env workload-profile add -n "$ENV" -g "$RG" \
      --workload-profile-name "$GPU_PROFILE" \
      --workload-profile-type Consumption-GPU-NC24-A100 -o none
  fi
}

# --------------------------------------------------------------------------
# 2. Storage for the model weights
# --------------------------------------------------------------------------
step_storage() {
  log "2. Storage account $STORAGE + share $SHARE"

  # Premium SSD file share rather than the image: ACR would work (its 10GB is a
  # billing allowance, not a cap) but a 62GB image would have to be pulled on
  # every cold start, whereas the share is mounted and read in place.
  #
  # Premium_LRS is the v1 provisioned model. The portal additionally offers
  # "Provisioned v2", which lets throughput be bought independently of capacity
  # -- useful here, since we need only ~63GB but want it read fast. If you need
  # v2, create the account in the portal; this flag is the v1 equivalent.
  if az storage account show -n "$STORAGE" -g "$RG" &>/dev/null; then
    skip "$STORAGE"
  else
    az storage account create -n "$STORAGE" -g "$RG" -l "$REGION" \
      --sku Premium_LRS --kind FileStorage --https-only true -o none
  fi

  # 100 GiB is the Premium minimum and comfortably fits 62.6GB of weights.
  if az storage share-rm show --storage-account "$STORAGE" -g "$RG" -n "$SHARE" &>/dev/null; then
    skip "share $SHARE"
  else
    az storage share-rm create --storage-account "$STORAGE" -g "$RG" \
      -n "$SHARE" --quota 100 -o none
  fi
}

# --------------------------------------------------------------------------
# 3. Register the share with the environment
# --------------------------------------------------------------------------
step_link() {
  log "3. Link $SHARE to $ENV (read-only + read-write)"

  # A container can only mount storage the environment already knows about, by
  # the name registered here -- this is the bridge, and it is CLI-only.
  #
  # The same share is registered twice under different access modes so the two
  # consumers get least privilege: vLLM mounts `models` read-only and cannot
  # corrupt the weights; only the download job gets `models-rw`.
  local key
  key=$(az storage account keys list -n "$STORAGE" -g "$RG" --query "[0].value" -o tsv)

  # Note this uses the account key, so "Allow storage account key access" must
  # stay enabled on the storage account. Disabling it breaks the mount.
  az containerapp env storage set -n "$ENV" -g "$RG" \
    --storage-name "$SHARE" --azure-file-account-name "$STORAGE" \
    --azure-file-account-key "$key" --azure-file-share-name "$SHARE" \
    --access-mode ReadOnly -o none

  az containerapp env storage set -n "$ENV" -g "$RG" \
    --storage-name "${SHARE}-rw" --azure-file-account-name "$STORAGE" \
    --azure-file-account-key "$key" --azure-file-share-name "$SHARE" \
    --access-mode ReadWrite -o none
}

# --------------------------------------------------------------------------
# 4. Download the weights onto the share
# --------------------------------------------------------------------------
step_weights() {
  log "4. Download $HF_REPO onto the share"

  # Guard on the bytes, not on whether the job exists: the job resource sticks
  # around after a successful run, so checking for it would skip a download that
  # never happened, and starting it unconditionally would re-download 62.6GB.
  # Only the share can answer "are the weights already here?".
  #
  # Compares against the total on Hugging Face. A partial share -- an earlier
  # run that timed out halfway -- is smaller, so it correctly re-runs.
  local key bytes
  key=$(az storage account keys list -n "$STORAGE" -g "$RG" --query "[0].value" -o tsv)
  bytes=$(az storage file list --account-name "$STORAGE" --account-key "$key" \
            --share-name "$SHARE" --path "$MODEL_DIR" \
            --query "sum([].properties.contentLength)" -o tsv 2>/dev/null || echo 0)
  bytes=${bytes:-0}
  if [ "${bytes%%.*}" -ge "$WEIGHTS_BYTES" ]; then
    skip "weights already on the share ($((bytes / 1000000000))GB)"
    return
  fi

  # Runs inside the region rather than locally: 62.6GB down a home connection
  # and back up again would take hours, where datacentre-to-datacentre took
  # 7m37s when this was first run. Uses the CPU profile -- it is pure I/O, and
  # scheduling it on the A100 would burn GPU minutes on a network copy.
  local yaml
  yaml=$(ENV_ID="$(az containerapp env show -n "$ENV" -g "$RG" --query id -o tsv)" \
         REGION="$REGION" HF_REPO="$HF_REPO" MODEL_DIR="$MODEL_DIR" \
         render "$HERE/download-weights-job.yaml")

  if az containerapp job show -n download-weights -g "$RG" &>/dev/null; then
    skip "job download-weights"
  else
    az containerapp job create -n download-weights -g "$RG" --yaml "$yaml" -o none
  fi

  az containerapp job start -n download-weights -g "$RG" -o none
  echo "    started; watch with:"
  echo "      az containerapp job execution list -n download-weights -g $RG -o table"
}

# --------------------------------------------------------------------------
# 5. Mirror the vLLM image into ACR
# --------------------------------------------------------------------------
step_image() {
  log "5. Import $VLLM_UPSTREAM as $ACR.azurecr.io/vllm-openai:$VLLM_TAG"

  # Server-side copy: nothing is pulled to this machine. Mirroring rather than
  # pulling Docker Hub directly avoids anonymous rate limits and pins exactly
  # what production runs.
  if az acr repository show-tags -n "$ACR" --repository vllm-openai -o tsv 2>/dev/null \
       | grep -qx "$VLLM_TAG"; then
    skip "vllm-openai:$VLLM_TAG"
  else
    az acr import -n "$ACR" --source "$VLLM_UPSTREAM" --image "vllm-openai:$VLLM_TAG"
  fi
}

# --------------------------------------------------------------------------
# 6. The GPU app  ***REQUIRES QUOTA***
# --------------------------------------------------------------------------
step_gpu_app() {
  log "6. Create $GPU_APP on $GPU_PROFILE"

  # Uses the EXISTING user-assigned identity, not a system-assigned one, and the
  # order matters. A system-assigned identity is born with the app, so it cannot
  # be granted AcrPull until after the app has already tried its first pull --
  # that pull fails, the revision fails, provisioningState goes to Failed, and a
  # Failed app cannot be updated, only deleted. A user-assigned identity is a
  # separate resource that already exists, so it can be granted first and handed
  # to the app at creation. Learned by hitting exactly that wall.
  local uami_id uami_pid
  uami_id=$(az identity show -n "$UAMI" -g "$RG" --query id -o tsv)
  uami_pid=$(az identity show -n "$UAMI" -g "$RG" --query principalId -o tsv)

  # Idempotent: re-granting an existing role assignment errors harmlessly.
  az role assignment create --assignee "$uami_pid" --role AcrPull \
    --scope "$(az acr show -n "$ACR" --query id -o tsv)" -o none 2>/dev/null || true

  if az containerapp show -n "$GPU_APP" -g "$RG" &>/dev/null; then
    skip "$GPU_APP"
    return
  fi

  local yaml
  yaml=$(ENV_ID="$(az containerapp env show -n "$ENV" -g "$RG" --query id -o tsv)" \
         REGION="$REGION" ACR_LOGIN_SERVER="$ACR.azurecr.io" VLLM_TAG="$VLLM_TAG" \
         MODEL_DIR="$MODEL_DIR" SERVED_MODEL_NAME="$SERVED_MODEL_NAME" \
         UAMI_ID="$uami_id" \
         render "$HERE/vllm-app.yaml")

  # Quota note: a green create is not proof the GPU works. Quota is enforced when
  # a replica is scheduled, not at creation, so confirm a replica actually starts.
  az containerapp create -n "$GPU_APP" -g "$RG" --yaml "$yaml" -o none
}

# --------------------------------------------------------------------------
# 7. Move the API app into this environment
# --------------------------------------------------------------------------
step_move_api() {
  log "7. Create $API_APP in $ENV"

  # An app cannot move between environments, so this recreates it. The old
  # West Europe app keeps serving until DNS is switched -- delete it only after
  # verifying this one.
  #
  # Co-location is not cosmetic: internal ingress resolves only within an
  # environment, so the GPU app can only stay off the public internet if the
  # caller lives beside it.
  if az containerapp show -n "$API_APP" -g "$RG" \
       --query "properties.environmentId" -o tsv | grep -q "$ENV"; then
    skip "$API_APP already in $ENV"
    return
  fi

  # Secrets are read from Key Vault at deploy time and never written to disk.
  local api_key portkey_key
  api_key=$(az keyvault secret show --vault-name "$KV" -n screening-service-api-key --query value -o tsv)
  portkey_key=$(az keyvault secret show --vault-name "$KV" -n portkey-api-key --query value -o tsv)

  az containerapp create -n "$API_APP" -g "$RG" \
    --environment "$ENV" --workload-profile-name Consumption \
    --image "$ACR.azurecr.io/screening:latest" \
    --user-assigned "$UAMI" --registry-server "$ACR.azurecr.io" \
    --registry-identity "$(az identity show -n "$UAMI" -g "$RG" --query id -o tsv)" \
    --ingress external --target-port 8000 \
    --cpu 2 --memory 4Gi --min-replicas 0 --max-replicas 10 \
    --secrets "service-api-key=$api_key" "portkey-api-key=$portkey_key" \
    --env-vars \
      "SCREENING_SERVICE_API_KEY=secretref:service-api-key" \
      "SCREENING_PORTKEY_API_KEY=secretref:portkey-api-key" \
      "SCREENING_LLM_BASE_URL=https://api.portkey.ai/v1" \
      "SCREENING_LLM_MODEL=google/gemini-3.5-flash-lite" \
      "SCREENING_PORTKEY_VIRTUAL_KEY=screening-openrouter" \
    -o none
}

# --------------------------------------------------------------------------
# 8. Point the API at the GPU app  ***REQUIRES STEP 6***
# --------------------------------------------------------------------------
step_wire() {
  log "8. Wire $API_APP -> $GPU_APP"

  # The internal FQDN resolves only inside the environment. Nothing in the app
  # is hardcoded: config.py reads this from the environment, which is why
  # swapping the detector endpoint never needed a code change.
  local fqdn
  fqdn=$(az containerapp show -n "$GPU_APP" -g "$RG" \
         --query properties.configuration.ingress.fqdn -o tsv)

  az containerapp update -n "$API_APP" -g "$RG" \
    --set-env-vars "SCREENING_LLM_GUARDRAIL_BASE_URL=http://$fqdn/v1" -o none

  echo "    guardrail endpoint: http://$fqdn/v1"
  echo
  echo "    Only now is it safe to merge gemma4-guardrails into main."
  echo "    deploy.yml fires on push to main and the branch fails closed when"
  echo "    this endpoint is unreachable -- merging earlier takes prod down."
}

# --------------------------------------------------------------------------

STEPS=(env storage link weights image gpu_app move_api wire)

main() {
  if [ $# -gt 0 ]; then
    "step_$1"
  else
    for s in "${STEPS[@]}"; do "step_$s"; done
  fi
  log "done"
}

main "$@"
