# ADR-009: Single Authoritative Default Branch (main), master Retired with Backup

- **Status:** Accepted (2026-08-25)
- **Context:** `main` and `master` diverged (merge-base `d1d0334`). `master` was the GitHub default but lacked 6 live commits; `main` lacked 2. Dual long-lived branches caused the divergence.
- **Decision:** `main` is the authoritative default. Merge master's unique doc + dataset-text into `main`; merge `main` -> `master` once; set GitHub default to `main`. **Before deleting `master`, create and push a restorable backup branch `backup/master-pre-delete-20260825` (copy of master @ `79d2e45`).** Then delete `master` (local + remote). Maintain exactly one long-lived branch thereafter.
- **Consequences:**
  - (+) Clones of the default get the true tree; no forked history.
  - (+) Single branch of record simplifies merge/CI/review.
  - (+) `backup/master-pre-delete-20260825` on the remote preserves the retired line indefinitely.
  - (-) External consumers hard-coding `master` must be repointed (checked before deletion).