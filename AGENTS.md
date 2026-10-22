# Deployment contract for automated agents

Read README.md, docs/CONFIGURATION.md and docs/OPERATIONS.md before operating this project.

## Inputs to obtain from the deployer

- Linux Docker target, project directory, writable dedicated data/recording paths and disk budget.
- Canonical room ID (not short alias), display name, recording subdirectory; new or existing recorder mode.
- Credentials through a secure interactive terminal or mounted private file. Never ask for API keys/cookies/passwords in chat or put them in command arguments.
- Explicit scope for enabling actual recording, paid smoke tests, automatic historical processing, external website publication and media deletion. Code installation alone does not grant these actions.
- For Pages: NEW project name, account ID, optional domain/zone and scoped token. For R2: existing PRIVATE bucket and bucket-scoped S3 credentials.

## Execution sequence

1. Inspect Docker availability, CPU/memory, free disk and existing port/container conflicts. Do not install host Python or overwrite a live deployment.
2. Obtain reviewed source into a dedicated directory. Run scripts/setup.py inside the documented Python container with --user matching the host UID:GID so the host user can read the private .env file. Noninteractive input follows deployment-input.example.json and is named *.private.json. Defaults MUST remain paused with deletion disabled. If configuration exists, inspect and preserve it rather than rerunning initialization destructively.
3. Build Dockerfile. Do not copy .env, data, records or secrets into an image; the explicit .dockerignore allowlist is part of the boundary.
4. Run the README offline tests with network disabled and no deployment data mounted. All tests must pass. Compose config validation must use --quiet because expanded config includes the recorder password.
5. Start worker/admin. Start the recorder profile only for a new recorder, never to replace an existing container. Use register-room and wire-recorder only on the authorized recorder; both preserve unrelated settings. Validate exact filename/record_dir agreement.
6. Deployer performs Baidu authentication. Fill model credentials through hidden input. doctor must eventually exit 0; an empty recording directory before first recording is a known precondition, not a reason to fake success.
7. After recording authorization, enable recorder and obtain a closed test file. After paid-call authorization, run app.py smoke. Confirm valid timed transcript and schema-valid summary, with no sample published.
8. If automatic processing is authorized, audit ALL stable files in the configured directory before app.py enable. This also admits old unclaimed recordings. For a bounded start, use a dedicated input directory or remain paused.
9. Verify a real complete job: SQLite done, transcript checkpoints reused on resume, summary.json/result.json, published.json, backup.json with remote_exact_bytes. R2 mode additionally requires transcript-archive.json with verified hashes. A healthy container, HTTP 200, model task ID or CLI exit 0 alone is not completion.
10. Leave cleanup disabled unless separately requested. Review its dry-run plan and apply only to approved media, retaining XML, source evidence, transcripts, summaries and paid checkpoints. Do not mount the Docker socket into worker/admin/maintenance.

## Operations and reporting

- Use sh compose.sh for a Docker-only host. Do not run multiple worker processes or use different container paths for an existing queue's records/state.
- Pause + wait for running=0 before image replacement. pause stops new admissions, not an in-flight paid request. Never delete checkpoints to retry an ambiguous ASR submission.
- Do not run experiments against production state. Use a separate project, paths, ports and synthetic credentials; record every resource created and remove only those resources after testing.
- Public output may contain titles, original recording names and generated summaries. Publication requires the deployer's content approval; raw transcript endpoints require login. No personal source data or credentials belong in Git.
- Before Git push: inspect git diff --cached, run scripts/scan_public.py --tracked and review the tracked file list. Never push the original private Git history or bundle.
- Report exact commit/image, checks performed, observed failures, and untested provider/platform paths. Do not claim successful real ASR, cloud publication or archive verification from mock tests.
