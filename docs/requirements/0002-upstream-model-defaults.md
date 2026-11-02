# Upstream model and reasoning defaults

Status: implementing

## Problem and outcome

Administrators need to choose the upstream model for Web image generation and a default reasoning effort without changing every client request.

## Scope and ownership

- Settings Management owns validation, persistence and projection of the two settings.
- Web `gpt-image-2` generation uses the configured upstream model and reasoning effort. Codex image generation is unchanged.
- Chat Completions and Responses text requests use the configured effort only when no effort is supplied. Search and Anthropic requests retain their existing behavior.
- Existing installations retain `gpt-5-3` and automatic effort by default. Explicit text request values take precedence.
- Chat completion cache keys include the effective effort.

## Failure and acceptance criteria

Reject empty or invalid model names and unsupported efforts. Settings must round-trip; changing the effort must not return a cached response for the previous effort. The settings controls must remain usable on desktop and mobile. Upstream model availability is determined by the remote service and is not guaranteed by saving settings.

## Verification

Exercise settings round-trip, validation, request precedence, cache keys and the Web image request payload. Build the console and inspect the settings controls on desktop and mobile. Confirm no real upstream requests or account mutations are required for automated checks.
