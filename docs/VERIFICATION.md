# Verification record

Date: 2026-10-22. This public export uses only synthetic test media, fixture credentials and generic branding.

- 136 Python tests passed in an isolated Linux/Python 3.12 container with FFmpeg, network disabled and no production data mounted. Coverage includes real stream-copy media merges, metadata-only FLV handling, checkpoint reuse, model-output recovery, priority scheduling, private archive signatures/readback, authentication, deployment initialization and interrupted recorder bootstrap recovery.
- 37 JavaScript date/search cases passed; public and admin JavaScript syntax checks passed.
- The tracked-file credential/private-path scanner passed. The source tree was also reviewed for deployment-specific domains, IP addresses, account IDs, room IDs, source paths and personal artwork. A new Git history is used.
- A clean Dockerfile build, official recorder 2.20.0 pull, non-root initialization, real recorder/worker/admin startup, room registration and idempotent Webhook configuration all passed in GitHub CI. The Docker-only Compose helper was forced and successfully validated against the real daemon.
- Verified code revision: `3b11324630af8feb4fb3af216cf4092ea25a5a0d`. [Successful complete CI run](https://github.com/eririmoe/live-summary-docker/actions/runs/39748363190). Subsequent verification-document changes do not alter that runtime.
- A separate existing Docker host also passed isolated regression and real admin login/public-site route checks using synthetic credentials, independent ports and a dedicated temporary project. Its slower mirror-based build was stopped; the successful clean build evidence is the CI run above. Test containers, test networks and test image tags were confirmed absent, and the dedicated temporary directory was removed. Existing services remained running.

No paid ASR/summary request, real Baidu upload, R2 write, public Pages deployment or real recording was performed for this export. Existing live services were not restarted or reconfigured. A production deployment must complete the README's account-specific smoke and receipt checks.

Linux amd64 is the tested runtime target. arm64 release checksums are included but this delivery does not claim arm64 execution validation. The default official Debian mirror may be slow on some networks; a documented build argument permits an explicitly selected mirror without disabling TLS or checksum checks.
