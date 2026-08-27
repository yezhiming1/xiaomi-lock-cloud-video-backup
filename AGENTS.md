# Project collaboration rules

## Before every project change

1. Read this file and `docs/STATE.md`, `docs/HANDOFF.md`, `docs/CHANGELOG.md`,
   `docs/RELEASE.md`, and `docs/PENDING.md` when those files exist.
2. For an existing remote, run `git fetch --prune origin`, switch to `main`,
   pull with rebase, and verify local `HEAD == origin/main` with a clean tree
   before creating an `agent/<task>` branch.
3. Preserve all dirty, divergent, untracked, or unclear-owner work. Never
   discard, clean, reset, force-push, or guess-merge it.

## Public-repository security boundary

- This repository is public. Never commit credentials, account identifiers,
  signed URLs, cookies, Home Assistant tokens, Xiaomi session values, raw cloud
  responses, device IDs, file IDs, private media, event timestamps from a real
  home, RFC1918 addresses, local hostnames, mount names, or machine-specific
  paths.
- Runtime logs and persisted state use fixed error codes, counts, UTC cursors,
  and SHA-256 identifiers only. They never include remote URLs, upstream bodies,
  ffmpeg stderr, authentication fields, device IDs, or file IDs.
- The integration may reuse an already-loaded `hass-xiaomi-miot` in-memory cloud
  session. It must not request, export, persist, or display Xiaomi passwords,
  cookies, Home Assistant access tokens, or Xiaomi Miot authentication files.
- Media writes are restricted to a validated subdirectory beneath Home
  Assistant's `/media`. Retention deletes only files previously recorded in this
  integration's own state and never follows symlinks.

## Project identity and validation

- Integration domain: `xiaomi_lock_cloud_backup`.
- Version starts at `V0.0.1`; custom-integration manifest uses `0.0.1`.
- Main focused test command: `python -m unittest discover -s tests -p "test_*.py" -v`.
- Target validation uses the pinned Home Assistant 2026.8.2 container image in
  `scripts/test-in-home-assistant.ps1` and must run without network access.
- Before public push, run `scripts/audit-public-tree.ps1`; any secret-like
  literal, real media file, runtime state, private endpoint, or generated cache
  is a blocking failure.

## Release and deployment boundary

- Use `agent/<short-task-name>`, stage only task files, inspect the complete
  diff, push the branch, create a Pull Request, and merge through GitHub.
- A release requires current test evidence, project records, artifact hashes,
  and rollback instructions.
- Installing into a real Home Assistant, adding network storage, scheduling a
  real cloud run, or deleting retained recordings requires explicit target
  authorization and post-deployment verification. Public-repository authority
  alone grants none of those actions.
- Keep the previous runnable version and its verified recovery path until a
  replacement has passed target-environment and real-consumer checks.
