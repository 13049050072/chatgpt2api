# 0001: Model Catalog Editor

Status: pending-verification

- Owner: ChatGPT2API
- Created: 2026-10-18
- Updated: 2026-10-18
- Related issues/ADRs: none

## Problem and user outcome

The shared model catalogue is currently fixed by configuration or fallback
values. Administrators cannot discover upstream ChatGPT Web model IDs and choose
which chat models are exposed by the console and `/v1/models`. The administrator
can fetch the common upstream catalogue from one valid Web account, select the
IDs to enable, and save that selection through Settings.

## Current behavior and evidence

`ModelCatalogService` projects configured or fallback chat and image model IDs
for Studio and `/v1/models`. `OpenAIBackendAPI.list_models()` already reads the
authenticated Web model endpoint. Settings mutations are revisioned and persist
through `SettingsManagementService` and the existing system settings store.

The user confirmed that the upstream model directory is identical across their
accounts, so discovery reads one eligible ChatGPT Web account. Image model
discovery and enablement remain out of scope because image support uses a
separate catalogue and request path.

## Scope

- Add an admin-only model discovery endpoint that reuses the existing upstream
  model-list request through one valid ChatGPT Web account.
- Add a Settings tab to select, manually add, and persist enabled chat model IDs.
- Keep currently enabled IDs visible if a discovery response does not include
  them, and require at least one enabled chat model.
- Add `gpt-6` to the fallback chat model list requested earlier in this task.
- Make the saved catalogue the source consumed by the console and `/v1/models`.

## Non-goals

- Do not discover or edit image models through the chat model endpoint.
- Do not compare or query every account in the pool.
- Do not change upstream request routing, account selection for completions, or
  model display-name metadata.
- Do not deploy this implementation as part of the model-catalog feature work.

## Domain language

Use existing terms from [`../../CONTEXT.md`](../../CONTEXT.md): Model Catalog,
Upstream Account, Settings, and Application Database. No new domain term is
introduced.

## Unique owners

| Concern | Authoritative owner | Interface consumed by others | Forbidden mirror or fallback |
| --- | --- | --- | --- |
| Chat model catalogue and fallback | `ModelCatalogService` | `ModelCatalogView`, `/v1/models`, Settings projection | Page-local model catalogue |
| Upstream model discovery | `ModelCatalogDiscoveryService` | Admin-only discovery response | Fetching separately from each account |
| Settings persistence | `SettingsManagementService` and runtime configuration | Revisioned Settings API | Browser-only saved selection |
| Loading and selection | Settings page and `SettingsModelCatalogPanel` | `modelsApi` and `Settings` draft | Mutating catalogue from discovery response |
| Image model support | Existing image catalogue and image protocol | `ModelCatalogView.image_models` | Reclassifying discovered chat IDs as image models |

## Backend Modules and Interfaces

- Add `POST /api/model-catalog/discover`, protected by administrator
  authentication. It selects one selectable ChatGPT Web account, refreshes its
  access token when necessary, and invokes `OpenAIBackendAPI.list_models()`.
- Return only model IDs and fetch time. Exclude known image-only identifiers;
  never return account identity or credentials.
- Persist enabled IDs as `model_catalog.chat_models` through the existing
  revisioned Settings API and system settings store.
- `ModelCatalogService` remains the sole owner of the effective catalogue and
  its fallback list. Both the Settings projection and `/v1/models` use that
  owner.
- Return 409 when no eligible Web account exists and 502 for upstream read
  failures. Sanitize surfaced diagnostic text.

## Frontend interaction and responsive behavior

- Add a Settings tab for chat models with a fetch action, a selectable list,
  manual model-ID entry, fetched timestamp, and an inline error state.
- Fetching does not automatically enable models. Selection edits the existing
  Settings draft; the page-level Save Settings action persists it.
- Existing active IDs remain visible even when absent from the latest upstream
  response. At least one model must stay enabled.
- Explain that image models use a separate catalogue. Keep the list responsive
  with wrapping IDs and no additional scroll owner.

## Persistence impact

The selected list is stored in the existing `model_catalog.chat_models`
configuration subtree through `SettingsManagementService`. No new table,
external database, backup record, or migration is introduced. Upstream discovery
results remain transient in the Settings page.

## Concurrency, security, and failure behavior

- Require administrator authentication for discovery and settings mutation.
- Reuse the account processing slot and existing token-refresh path.
- Query only one selectable Web account, preferring status `正常`; do not fan out
  requests over the pool.
- Close the upstream backend session on success and failure. Do not expose
  account email, access token, or refresh token in the response.
- Discovery failure keeps the current candidate list and unsaved selection.
- Contract validation rejects blank IDs and empty enabled lists.

## Acceptance criteria

- [ ] Settings includes a model editor that fetches candidates from one eligible
      Web account and allows choosing which chat IDs are enabled.
- [ ] Fetch failure leaves the current list and draft selection intact and shows
      a safe error message.
- [ ] Saving persists the enabled chat catalogue, and the console and
      `/v1/models` return the same enabled IDs.
- [ ] At least one model remains enabled; image model configuration is
      unaffected.
- [ ] Desktop and narrow layouts keep IDs readable without page-level overflow.
- [ ] Current maps, README, and CHANGELOG match the implementation.

## Verification matrix

| Acceptance criterion | Verification level | Evidence required | Status |
| --- | --- | --- | --- |
| Settings editor and discovery endpoint | Build/static | `npx tsc --noEmit`, `npx vite build`, and Python compile | passed |
| Shared saved catalogue | Contract/runtime | Model catalogue and Settings API behavior | pending |
| Failure, account selection, and secret redaction | Contract/manual | Request behavior against eligible and unavailable account states | pending |
| Responsive Settings behavior | Browser | Desktop and narrow viewport inspection | pending |
| Deployment | Live runtime smoke after deployment | Deployment authorization and live smoke | not requested |

Automated tests and browser verification have not yet been run. Python compilation,
TypeScript checking, and Vite production bundling passed. The full `npm run build`
script stops at `icons:check` because the local generated Lucide set is stale
against the installed package. `npm ci` also cannot install from the current lock
file because its `picomatch` versions are inconsistent.

## Documentation and CHANGELOG impact

- Update `docs/maps/backend-map.md` and `docs/maps/frontend-map.md`.
- Update README model catalogue guidance and `CHANGELOG.md`.
- Keep the existing Model Catalog term in `CONTEXT.md`; no ownership change is
  needed.

## ADR requirement

Not required. The feature reuses the current Settings store, model catalogue,
upstream adapter, and account processing boundary without adding a new durable
architecture boundary.

## Rollback

Remove the discovery endpoint and Settings panel and stop persisting
`model_catalog.chat_models`. Existing configuration can be edited or removed to
restore the fallback catalogue; no database migration or remote resource needs
cleanup.

## Unresolved questions

- None.
