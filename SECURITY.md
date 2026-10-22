# Private data and reports

Do not open public issues containing API keys, recorder passwords, Webhook tokens, cookies, raw transcripts, signed download URLs, server addresses or expanded Compose configuration. Revoke exposed credentials at their issuer; deleting a Git file does not revoke it or erase history.

This repository is a fresh source-only export. It includes synthetic fixtures and a small synthetic vocabulary, with no original private Git history, recordings, transcripts, account configuration, avatars or deployment logs. Generated state stays outside the build allowlist and Git index. Run `python scripts/scan_public.py --tracked` before publishing changes, and inspect the staged diff manually. The scanner detects common formats and risky filenames; it cannot prove the absence of every possible secret.

Application containers have no Docker socket. Worker recordings are read-only, admin state is read-only. All exposed ports default to loopback. The optional compose.sh CLI fallback alone temporarily accesses the Docker socket to execute deployment operations. Maintenance explicitly has write access and must only be invoked after reviewing its plan.

The public website intentionally publishes summaries, titles, dates, room/display metadata and original source filenames for provenance. Choose input content and publication permissions accordingly. Raw transcripts remain private and require authenticated access. Treat short-lived R2 links as credentials.

For a sensitive vulnerability, contact the repository owner privately through an existing trusted channel. Submit only redacted reproduction details in public issues.
