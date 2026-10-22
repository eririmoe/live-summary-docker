# Verification record

Date: 2026-10-22. This public export uses only synthetic test media, fixture credentials and generic branding.

- 135 Python tests passed in an isolated Linux/Python 3.12 container with FFmpeg, network disabled and no production data mounted. Coverage includes real stream-copy media merges, metadata-only FLV handling, checkpoint reuse, model-output recovery, priority scheduling, private archive signatures/readback, authentication and deployment initialization.
- 37 JavaScript date/search cases passed; public and admin JavaScript syntax checks passed.
- The tracked-file credential/private-path scanner passed. The source tree was also reviewed for deployment-specific domains, IP addresses, account IDs, room IDs, source paths and personal artwork. A new Git history is used.
- A clean Dockerfile build and live Compose startup are being validated separately from regression tests. CI builds the actual Dockerfile and runs the offline suite; consult the latest successful workflow for the current commit.

No paid ASR/summary request, real Baidu upload, R2 write, public Pages deployment or real recording was performed for this export. Existing live services were not restarted or reconfigured. A production deployment must complete the README's account-specific smoke and receipt checks.

Linux amd64 is the tested runtime target. arm64 release checksums are included but this delivery does not claim arm64 execution validation. The default official Debian mirror may be slow on some networks; a documented build argument permits an explicitly selected mirror without disabling TLS or checksum checks.
