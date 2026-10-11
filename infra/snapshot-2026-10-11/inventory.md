# Inventory of screening-rg, read 2026-10-11 (names and settings only; no secret values)

## Resources
Name                       Location       Sku
-------------------------  -------------  -------------
screeningacr1              westeurope     Basic
workspace-screeningrgUumw  westeurope
screening-env              westeurope
screening-kv-7412          westeurope
screening-identity         westeurope
screening-app              swedencentral
screening-pii-lang         westeurope     F0
workspacescreeningrg9322   swedencentral
screening-env-swe          swedencentral
screeningweights           swedencentral  PremiumV2_LRS
vault-msllyxvh             swedencentral  RS0
download-weights           swedencentral
screening-gemma            swedencentral
screeningjobs              swedencentral  Standard_LRS
screening-worker           swedencentral

## Key Vault secret names (values are not recorded)
portkey-api-key
screening-openrouter
screening-service-api-key

## Container registry repositories and tags
ERROR: Looks like you don't have access to registry 'screeningacr1.azurecr.io'. To see configured firewall rules, run 'az acr show --query networkRuleSet --name screeningacr1'. To see if public network access is enabled, run 'az acr show --query publicNetworkAccess'.Please refer to https://aka.ms/acr/errors#connectivity_forbidden_error for more information.

## Storage accounts
Name              Sku            Kind         Location
----------------  -------------  -----------  -------------
screeningjobs     Standard_LRS   StorageV2    swedencentral
screeningweights  PremiumV2_LRS  FileStorage  swedencentral

## File shares
(empty: the listing returned nothing — the account was disabled, as the queue and table calls below report.
The share was `models`, 100 GiB Premium, per `infra/gemma/README.md`.)

## Queues and tables (screeningjobs)
WARNING: Command group 'storage queue' is in preview and under development. Reference and support levels: https://aka.ms/CLI_refstatus
ERROR: The specified account is disabled.
RequestId:424bc659-3003-0036-4c82-59f689000000
Time:2026-10-11T13:11:57.6585738Z
ErrorCode:AccountIsDisabled
ERROR: The specified account is disabled.
RequestId:ed4ace39-a002-000b-6782-598092000000
Time:2026-10-11T13:11:58.4123238Z
ErrorCode:AccountIsDisabled

## Managed identity and its role assignments
ClientId                              PrincipalId
------------------------------------  ------------------------------------
4f01947b-e5c3-49ed-88b6-0d131c6d2211  c61f80d6-3f4b-4f3e-a600-a2c5f9b59c0c
Role                            Scope
------------------------------  ----------------------------------------------------------------------------------------------------------------------------------------------
Key Vault Secrets User          /subscriptions/c226c9ff-22c8-43d1-986b-b0ccdc8924e7/resourceGroups/screening-rg/providers/Microsoft.KeyVault/vaults/screening-kv-7412
AcrPull                         /subscriptions/c226c9ff-22c8-43d1-986b-b0ccdc8924e7/resourceGroups/screening-rg/providers/Microsoft.ContainerRegistry/registries/screeningacr1
Storage Table Data Contributor  /subscriptions/c226c9ff-22c8-43d1-986b-b0ccdc8924e7/resourceGroups/screening-rg/providers/Microsoft.Storage/storageAccounts/screeningjobs
Storage Queue Data Contributor  /subscriptions/c226c9ff-22c8-43d1-986b-b0ccdc8924e7/resourceGroups/screening-rg/providers/Microsoft.Storage/storageAccounts/screeningjobs

## Language service (PII)
Kind           Sku    Location    Endpoint
-------------  -----  ----------  -------------------------------------------------------
TextAnalytics  F0     westeurope  https://screening-pii-lang.cognitiveservices.azure.com/

## Backup vault protected items
Name    State
------  ---------
models  Protected
