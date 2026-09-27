# Validation — 2026-09-27

All database operations below used newly created, explicitly named audit volumes and
fake credentials. Existing development containers, databases and user feature changes
were not reset or migrated. Nothing was deployed to a hosting provider.

## Passed

| Area | Evidence |
| --- | --- |
| Frontend dependencies | Clean `npm ci --ignore-scripts`; 402 packages installed, npm reported no vulnerabilities in that run. Build also succeeded after this install. CI uses normal `npm ci`. |
| Frontend build | TypeScript + Vite production build with `VITE_API_URL=https://api.example.com`, custom-domain CNAME, root assets, 404.html and .nojekyll. |
| Browser behavior | Chromium loaded `/`, direct `/stores/example`, and refreshed the nested URL using a Pages-style 404 server; app shell/sign-in rendered, configured API host was used, no page errors. API was mocked for this artifact test. Reproduce with frontend/scripts/check-hosted.mjs. |
| Frontend lint | No errors. Six existing React refresh/effect warnings remain. |
| Backend tests | Full PostgreSQL-backed suite: **75 passed** after correcting five pre-existing stale tests. Three subsequently added guard cases (downgrade, stamp, placeholder secret) passed in a targeted deployment/health/shutdown run. |
| Baseline failures | Temporary pre-change source copy reproduced the same five failures: broad password-string assertion, outdated invite-onboarding assumptions, and a helper clearing MFA step-up before protected deletion. Only test expectations/fixtures were corrected. |
| CORS/CSRF | Exact allowed HTTPS app origin, credentialed preflight, forbidden origin, missing CSRF header, same-site acceptance and cross-site rejection tested. Existing cookie/session/auth integration tests passed. |
| Container image | Python 3.12 image builds with pinned runtime packages and a non-root user. Final configuration uses a read-only application filesystem and temporary /tmp. |
| Runtime | API liveness and DB readiness succeeded on default port and independently on `PORT=8123`. Worker starts separately when configured, exits cleanly when unconfigured, and stops with exit 0 on SIGTERM. |
| Compose | Development and standalone production files pass `config -q`. Production layout started against an isolated database/volume override, with no published database port. |
| Database roles | Production initialization creates a separate application role; PostgreSQL reports `rolsuper=false`. Alembic succeeds under that role. |
| Migrations | All preserved revisions applied through `b2c3d4e5f6a7` on PostgreSQL 17. Historical revisions were not rewritten. |
| Persistence | Forced DB container recreation retained a sentinel row. API readiness recovered afterward. External volumes are explicitly declared. |
| Business fixture | Operations smoke passed: checkout, bill correction, inventory, report, idempotency; two sales and seven stock movements verified. |
| Dump/restore | pg_dump custom archive restored with deploy/restore.sh into a fresh database. Source/restored verify.sql reports matched exactly, including counts and monetary/stock aggregates. Second restore into that occupied database was rejected. |
| Backup encryption | Restic encrypted a real fixture dump, passed repository check, accepted the 14-daily/8-weekly/6-monthly retention policy, and restored a byte-identical archive. This test repository was local/disposable; it is not a production backup. |
| Proxy | Caddy validated the supplied TLS reverse-proxy/static-host configuration without requesting public certificates. |
| Safety checks | Development remote/production-name DB rejection, disposable test DB guard, and example-secret rejection tested. Migration downgrade/stamp guard tests mock engine creation and prove rejection before any DB connection. |
| Repository checks | Shell syntax, targeted Python lint, JavaScript script syntax and Git diff whitespace checks pass. Current tracked source recognized-key-pattern scan found no private keys/AWS access IDs/GitHub tokens; available backend/frontend history filenames contain no .env, backupcodes.txt, .pem or .key. |

An early sandboxed TestClient run stalled because of runtime restrictions; replacement
runs outside that sandbox passed. Automatic review rejected a CLI downgrade guard check
with a production-labeled URL. No downgrade was executed; a mock-only test verified the
guard safely instead.

## Required live deployment checks

- Configure actual custom domains, DNS, HTTPS and provider account settings. GitHub Pages
  is technically supported by the artifact, but its published SaaS/password restrictions
  mean it should not host this authenticated commercial service. Use the provided VM
  static-host alternative for real users.
- Configure the real independent backup repository and secrets, initialize it, enable the
  timer, verify successful remote upload, download a remote snapshot and repeat the restore
  drill. Test remote credentials, retention and failure/age alerts. No external backup
  credentials were available in this task, so that live path is **not yet verified**.
- Test login, session refresh, MFA and CSRF in a browser across the actual HTTPS sibling
  subdomains. Local CORS/cookie tests do not validate DNS/certificates/browser policy on
  a provider you have not yet configured.
- Verify real SMTP/Brevo sender authentication/delivery. No real email was sent.
- Configure external uptime, disk, DB, worker-backlog and backup-age alerts. The timer
  and healthchecks do not configure an alert recipient by themselves.
- Test the chosen image digests and resource limits under expected concurrency; no load
  capacity or live recovery-time guarantee is claimed.
- RDS/Secrets Manager/ECS integration is documented, not deployed or integration-tested.

The repository audit is scoped: root Git metadata was unavailable, and the pattern scan
is not an exhaustive secret-history audit. Root `backupcodes.txt` was not read or removed;
it is now ignored and should be stored in the owner's secure password manager. Real
backend .env values were neither copied to examples nor printed. Preserve both repositories
and root-level local-development files in source control/backups before deployment.

Audit containers/networks were stopped after validation. The explicitly named disposable
volumes `ive_readiness_audit_data` and `ive_production_audit_data` were retained; no volume
prune/delete command was run. Test dumps/reports are under `/tmp/ive-readiness-audit`.
