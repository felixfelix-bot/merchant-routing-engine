# ADR-010: Large Binary Artifacts Published via GitHub Releases, Not Git Objects

- **Status:** Accepted (2026-08-25)
- **Context:** `master` commit `19c78a0` added `datasets/routing-telemetry/scrubbed.db.gz` (14.9 MB gzip) directly to git history; no LFS/`.gitattributes`. A raw binary blob permanently bloats every clone (deletion does not shrink history).
- **Decision:** Commit only text (`README.md`, `SCHEMA.sql`) to `main`. **Publish `scrubbed.db.gz` as a GitHub Release asset (`routing-telemetry-2026-08-22`)** and link it from the README. Policy: binaries too large/immutable to review in a diff go to Releases (or LFS if ever adopted), never raw git objects.
- **Consequences:**
  - (+) Clone/CI weight stays small; artifact versioned and citable via permalink.
  - (-) Artifact availability depends on GitHub Releases, not the git tree.
  - Note: if the dataset is regenerated, commit a regenerate-script instead of the blob.