# Hosting, recovery and AWS migration

Read [the audit](AUDIT.md) and [validation results](VALIDATION.md) first. This directory
belongs to the **backend Git repository**; frontend Actions belong to the frontend repo.
The workspace-root development Compose/README must also be retained in your source
backup or an outer repository. Do not accidentally omit the existing item-preset migration.

## Local development

Keep the root `docker-compose.yml` and `backend/script.sh --container` workflow.
Copy `backend/.env.example` to `backend/.env`, generate AUTH_SECRET, then from the workspace:

```sh
./backend/script.sh --container
cd frontend
npm ci
npm run dev -- --port 5173 --strictPort
```

Vite discovers the loopback API port and proxies `/api`; leave VITE_API_URL blank locally.
Open `http://www.localhost:5173` for marketing and `http://app.localhost:5173/login`
for sign-in. Set `PUBLIC_ORIGIN=http://app.localhost:5173` and
`TENANT_BASE_DOMAIN=localhost` in the backend environment. Registered organizations use
`http://<slug>.localhost:5173/login`. The workspace-root Compose identity environment
must forward `TENANT_BASE_DOMAIN` to the API, migration process, and mail worker.
Cookies are host-only, so different loopback hosts have separate sessions.
No secrets go in VITE_*; they are embedded in public JavaScript.

Local Compose includes a one-shot migration process and a separate mail-worker.
Native development uses `backend/script.sh`; it now refuses production APP_ENV, remote
DB hosts, and names ending `_production`/`_prod`. Test tools require local `_test` databases
and reject APP_ENV=production. These checks complement separate credentials/network
boundaries; operators must never give developers production credentials or rename a
production DB to bypass a guard. Run pytest with APP_ENV=development and a disposable
AUTH_TEST_DATABASE_URL after migrating that database. Tests truncate its data.

## Choose initial hosting

A persistent Linux VM with Docker Compose runs API, mail, scheduled deletions, PostgreSQL,
and a TLS proxy. PostgreSQL on Docker is intentional and supported here. The same VM can
serve the static frontend with Caddy, avoiding another compute instance. Size RAM/disk
from measured workload, database growth, and pg_dump needs; don't assume free capacity.

GitHub Pages output/workflow is provided in the frontend repo for allowed demonstrations.
However, [Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
exclude commercial SaaS and advise against password transactions. Use the VM static host
for authenticated early users, or another host whose terms allow this application.
The static bundle needs no code changes between these hosts.

Heroku can run the **application** image with `python -m app.serve`, mail with
`python -m app.auth.mail`, and deletions with `python -m app.maintenance` as separate
processes. Run Alembic as a release/one-off task. Use RUN_DELETION_WORKER=false on hosted
web processes. Configure each process through environment variables. Heroku does not
run this Compose stack or provide persistent dyno storage: never put PostgreSQL in a
dyno. An external DB must be reachable through private connectivity or TLS with verified
certificates and restricted ingress. Do not publish the VM's 5432 to the Internet just
to attach Heroku. Co-location on the VM avoids that initial complexity and cost.

## Production secrets and separation

From the backend repository, copy `deploy/.env.production.example` and
`deploy/.env.backup.example` to `/etc/ive/production.env` and `/etc/ive/backup.env`.
Use permissions 0600, owned by the deployment operator. Example values are placeholders.
Generate new independent values (do not reuse development secrets):

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# In an environment with backend dependencies installed:
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Use separate strong admin and app DB passwords; the app role is not a superuser.
Set DATABASE_URL to that application role (percent-encode special password characters).
Name the production DB `ive_production` or another name ending `_production`.
In native mode the application connects through DATABASE_URL and accepts `postgresql://`,
`postgres://`, or `postgresql+asyncpg://`. It does not discover a host or load a .env file
in production. The production init script creates the app role only on a new volume.
Changing env passwords later does not rotate existing PostgreSQL roles; use ALTER ROLE
through a controlled admin session, update secrets, and restart clients.

## Supabase provider

Set `DB_PROVIDER=supabase`, `SUPABASE_DATABASE_URL` to the direct or session pooler URI
from Supabase Connect (port 5432, with `?sslmode=require`), and
`COMPOSE_PROFILES=${DB_PROVIDER:-postgres}` in the production env file. Leave
`DATABASE_URL` configured for native PostgreSQL so switching back is an env change.
All API, mail, maintenance, bootstrap, and migration processes use the same selector.
This requires Docker Compose 2.20+; the `db` service belongs to the `postgres` profile
and its health dependency is optional when that profile is disabled. Keep the existing
POSTGRES_* / APP_DATABASE_* placeholders in the env file for Compose interpolation.
The native database volume is preserved, but no new native database container is started
in Supabase mode. Stop an already running native `db` container explicitly after cutover
if you no longer need it; changing profiles does not stop existing containers.
For Supabase deployment, skip the native `db` startup and `backup` build commands in the
VM instructions below. Run `prod build api`, `prod run --rm migrate`, then
`prod up -d api mail-worker maintenance-worker proxy`. Targeting `migrate` explicitly
enables its tools profile; no `--profile tools` flag is needed. If using explicit
`--profile` flags, include the selected provider too: CLI profiles override
`COMPOSE_PROFILES`.

For a fresh Supabase database, disable its Data API if only this backend accesses it,
and use a dedicated database role with permissions to create/migrate the app's tables.
Application tenant authorization and authentication continue to run in this backend.
Do not expose these app tables through the Supabase Data API without suitable permissions
and RLS policies. Choose pool sizes within the destination's connection budget, accounting
for every API process and worker (`DB_POOL_SIZE` / `DB_MAX_OVERFLOW`).

For an existing database, stop writers before the final dump, restore only the application
schema/data (including `alembic_version`) using `--no-owner --no-privileges`, and preserve
Supabase's managed schemas. Restore into a fresh destination; do not apply migrations first
and then attempt to restore the same tables. Grant privileges to the app role after restore.
Run the existing Compose `migrate` task with the new env file, compare row counts, sequences,
financial/stock aggregates and schema revision, then start the API and workers. Keep the
existing authentication/encryption secrets. The native `backup` task refuses Supabase mode;
configure and verify Supabase backups/recovery separately. Provider switching does not
automatically replicate or transfer data, and switching back after new writes needs a fresh
data transfer. Never delete the old volume as part of cutover.

## Platform-only transfer

For a platform-only transfer into a fresh Supabase project, export the backend env and
preview with `python -m app.platform.migrate` from `backend/`. Then run the same command
with `--apply`. The tool reads native Docker PostgreSQL by default (or
`SOURCE_DATABASE_URL` if configured), applies the current schema, and copies only
`super_admin`/`employee` platform users and their audit events without organization
references. UUIDs, password hashes, MFA secrets, and recovery hashes are preserved;
audit session references are cleared and old login sessions are not copied. All
organization and business tables stay empty. It refuses a populated destination or
schema mismatch and verifies the copied records before committing. Keep the original
`IDENTITY_ENCRYPTION_KEY`, pause writers during the transfer, and switch `DB_PROVIDER`
only after verification. Direct IPv6 endpoints need an IPv6 route; use Supabase's
session pooler URI on port 5432 for an IPv4-only host.

## Application origin and secrets

PUBLIC_ORIGIN=https://app.your-domain is the sole CORS/CSRF origin and email-link base;
there is no redundant CORS_ORIGINS setting. Production uses Secure, HttpOnly,
SameSite=Strict, `__Host-` cookies with Path=/ and **no Domain**. HTTPS sibling subdomains
are same-site. Frontend requests use credentials:include. Preserve CSRF Origin and
X-POS-CSRF checks; unrelated domains (github.io frontend + unrelated API domain) will
not work with this security model. Configure both custom domains before testing login.
Never weaken to wildcard credentialed CORS or SameSite=None for convenience.

Independently save AUTH_SECRET, IDENTITY_ENCRYPTION_KEY, RESTIC_PASSWORD and recovery
access in a password manager. MFA secrets and pending mail cannot be decrypted without
the original identity key. Backup storage credentials belong only to the backup process.
The web/worker environment does not include PostgreSQL admin or backup repository secrets.

## First VM deployment

Install Docker Engine and Compose on a maintained Linux host; restrict SSH, patch the OS,
and permit only 80/443 publicly. Provision external volumes explicitly **once**:

```sh
docker volume create ive_production_postgres_data
docker volume create ive_production_caddy_data
docker volume create ive_production_caddy_config
```

External volumes have lifecycles outside Compose; missing volumes cause startup failure.
They survive `docker compose down`, even `down -v` for this particular production file.
**Never run** `docker volume rm`, `docker system prune --volumes`, or blanket cleanup on
production. Other Compose volumes can still be destroyed by `down -v`. External volumes
are not a substitute for backups, and Docker administrators can still delete them.
Do not attach the production volume to a development or test stack.

Define a shell helper in an operator session, from the backend checkout:

```sh
prod() { docker compose --env-file /etc/ive/production.env -f deploy/compose.production.yml "$@"; }
prod config -q                # Do not print expanded configuration containing secrets.
prod build api backup
prod up -d --wait db
prod run --rm migrate         # Only one migrator at a time.
prod up -d api mail-worker maintenance-worker proxy
prod ps
```

Set API_DOMAIN/APP_DOMAIN and DNS A/AAAA to this VM for the Caddy option; Caddy obtains
TLS certificates on ports 80/443. Set FRONTEND_DIST_DIR to the extracted frontend build
artifact directory containing index.html. Do not point AAAA at a server that isn't serving
IPv6. For a Pages demo, point app-domain DNS to Pages, not the VM; the API stays on the VM.

PostgreSQL has no host port and uses an internal Docker network. API and workers have
outbound access for email/geocoding. Only Caddy shares the internal proxy network with
the API. It has a fixed private address; FORWARDED_ALLOW_IPS must match PROXY_IP
(default 172.30.90.2). Change PROXY_SUBNET, PROXY_IP, API_PROXY_IP and FORWARDED_ALLOW_IPS together if they
conflict with host networking. The proxy uses a network-specific API alias so outbound
network DNS cannot accidentally bypass this trust boundary. The initial Compose stack
runs a single API container.
Never trust `*` unless the hosting platform guarantees every incoming connection comes
from a sanitizing proxy. Verify client-IP rate limiting after any hosting change.
Administrative DB access: SSH to the host and `prod exec db psql ...`; for remote tools
use a deliberate loopback-only tunnel/private network and firewall controls.

Provision the first account interactively:

```sh
prod exec api python -m app.auth.platform_bootstrap
```

Enable MFA on first login. Configure SMTP with STARTTLS or Brevo as in the development
docs, then verify an actual signup/recovery message. Mail delivery is at least once.
The optional mail worker exits if delivery is not configured; readiness alone does not
prove email works. Maintenance worker handles scheduled tenant deletion separately.

API/worker root filesystems are read-only with disposable /tmp. No persistent application
files currently exist. Database and Caddy certificate volumes are the persistent state.
Set DB_POOL_SIZE/DB_MAX_OVERFLOW to fit the DB connection budget (default API maximum 10
per process, plus one connection per worker and migration/backup/admin headroom).
Uvicorn runs one process per container, respects PORT, binds 0.0.0.0, trusts only specified
proxy peers, drains requests for 30 seconds, and logs to standard streams. Mail has 60
seconds to finish on shutdown. Request URL access logging is disabled to avoid query PII.
Container logs rotate at 10 MB × 3. Don't log request bodies, cookies, or expanded env.

## Frontend deployment

The separate frontend repo's `main` workflow runs npm ci, lint, and a production build.
Configure repository variables VITE_API_URL=https://api.your-domain and
PAGES_CUSTOM_DOMAIN=app.your-domain. Every VITE_* value is public. For the allowed Pages
path, choose Settings → Pages → GitHub Actions; configure/verify the custom domain in
repository settings and its DNS, enforce HTTPS, then set ENABLE_PAGES=true. CNAME in the
artifact alone does not configure the custom domain for an Actions deployment.
Protect main and the github-pages environment with the reviews appropriate to your team.
Pull requests build but cannot deploy. For VM hosting, leave ENABLE_PAGES unset, download
frontend-dist from the workflow, and atomically replace the directory served by Caddy.

Assets use `/` for a custom-domain root, not `/repository/`. The post-build script creates
404.html and .nojekyll. Pages nested navigation/refresh loads the app shell with an HTTP
404 status; Caddy's SPA fallback returns 200. The current application has **no URL-based
screen routes**; a nested URL opens the default workspace, not a newly invented screen.
If path-based navigation is added later, keep that route mapping in the app and retain
this fallback (or use a host with rewrites). Identity URL fragments stay client-only.

## Routine releases

Build immutable application images (tag with the backend commit and ideally digest);
record PostgreSQL/Caddy digests after testing. Python runtime packages are pinned, but
base image tags intentionally receive security updates when rebuilt. Store the successful
image digest for rollback instead of assuming a rebuild is byte-identical.

1. Review migration upgrades and test them against restored representative data.
2. Build/pull images before downtime. Confirm a recent restorable off-server backup.
3. Stop ingress/API and both writing workers: `prod stop proxy api mail-worker maintenance-worker`.
4. Run `prod run --rm --no-deps backup` successfully; save verification report if migrating.
5. Run `prod run --rm migrate` once. Never run downgrade/stamp in production.
6. Start API/workers/proxy and check readiness, login/refresh, permissions, a sale,
   stock changes, receipts, reports and mail. Reopen use only after these succeed.

The production Compose file never automatically migrates or recreates storage during
startup. On failure keep writers stopped. Roll back an image only if schema compatible;
otherwise repair with a reviewed forward migration or restore into a **new** database.
Never restore over the only live copy. Restoring an older backup discards newer writes;
record the recovery point and business reconciliation before reopening.

## External backups and recovery

[backup.sh](backup.sh) runs pg_dump in custom format, checks its table of contents,
adds SHA-256, and uploads through restic encryption to an independent remote repository.
It rejects local repositories and unencrypted HTTP endpoints. Files have UTC timestamps
plus a unique suffix; temporary dumps are removed after the attempt. The backup container
uses memory-backed /work: ensure RAM covers the compressed dump, or mount a dedicated
protected staging disk when data grows. This staging area is not the recovery copy.

Choose an off-server S3-compatible repository (or configure another supported remote),
restricted credentials, and strong RESTIC_PASSWORD in backup.env. Initialize once:

```sh
prod run --rm --no-deps backup restic init
prod run --rm --no-deps backup
prod run --rm --no-deps backup restic snapshots --host ive-production --tag postgres
prod run --rm --no-deps backup restic check
```

Install the provided service/timer (adjust WorkingDirectory if not `/opt/ive/backend`):

```sh
sudo install -m 644 deploy/ive-backup.service deploy/ive-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ive-backup.timer
sudo systemctl start ive-backup.service
systemctl list-timers ive-backup.timer
journalctl -u ive-backup.service
```

Backups run daily at 23:00 UTC plus up to five minutes of jitter. Retention keeps 14 daily,
8 weekly, and 6 monthly snapshots grouped by host/tag, despite unique dump paths.
Monitor failure **and absence**: alert when the last successful remote snapshot exceeds
26 hours. Keep an independent credential/password copy off the VM. Run restic check
weekly; restore a downloaded snapshot to a disposable DB monthly and before major upgrades.
Use storage-side versioning/immutability or a separately controlled replica to protect
against a compromised backup credential deleting all snapshots. Match object retention
with restic prune; don't silently set conflicting object-lock policies.
Daily dumps give up to about 24 hours of data loss (more if failures go unnoticed), not
point-in-time recovery. Change the schedule or add WAL archiving when the business needs
less. Measure restore time on realistic data; there is no invented recovery-time promise.

Disaster recovery on a clean host with PostgreSQL 17 tools and restic installed:

```sh
# Load backup credentials securely into the operator environment, without echoing.
restic snapshots --host ive-production --tag postgres
restic restore SNAPSHOT_ID --target /secure/recovery
# Find the timestamped dump and SHA256SUMS under the recovered work/backup.* directory.
cd /secure/recovery/work/backup.ACTUAL_SUFFIX
sha256sum -c SHA256SUMS
pg_restore --list production-TIMESTAMP.dump
```

Create an empty destination database and its non-superuser owner on a new cluster/volume.
Use a 0600 PGPASSFILE or secret environment for credentials; never put passwords in scripts
or command arguments. Set PGHOST, PGPORT, PGUSER, PGDATABASE and, for remote connections,
PGSSLMODE=verify-full and PGSSLROOTCERT. Then:

```sh
export PGDATABASE=ive_restored_production
export RESTORE_TARGET_CONFIRM="$PGDATABASE"
/path/to/backend/deploy/restore.sh /secure/recovery/work/backup.SUFFIX/production-TIMESTAMP.dump
psql -X -f /path/to/backend/deploy/verify.sql > /secure/recovery/restored-verification.txt
```

restore.sh checks the target name and refuses existing application relations. It uses
pg_restore --single-transaction --exit-on-error --no-owner --no-privileges; it does not
clean/drop the target. Apply credentials/roles separately; a single DB dump does not
back up cluster roles. The application role becomes owner of restored objects.

With writers stopped before the final backup, save `psql -X -f deploy/verify.sql` on the
source and compare it byte-for-byte with the restored report. It checks all table counts
(including users, organizations, stores, products and customers), schema revision,
sales by organization/store/currency/status, cash collected, sale-line totals and costs,
stock balances, movements, and orphan lines. Bills/receipts are sales + sale_lines;
there are no separate invoice/ledger tables yet. For online daily backups, source counts
can change after the dump; use a maintenance snapshot for exact comparisons.
Also verify MFA decryption with the saved key, account permissions, receipt rendering,
stock/report reconciliation, and a disposable sale/rollback in the recovery environment.
Keep real email delivery disabled during drills. Only then change DATABASE_URL, start
API/workers, check health/readiness, and reopen traffic. Retain the old DB isolated until
reconciliation is complete. Securely remove recovered plaintext dumps after the drill.

## Monitoring and PostgreSQL upgrades

Monitor HTTPS `/api/v1/health` (process liveness) and `/api/v1/ready` (DB connectivity).
Readiness does not prove migrations, disk space, worker activity, or backup success.
Monitor container restarts, host disk/inodes/memory, PostgreSQL connections/locks, and
query latency. Inspect the mail queue for oldest unexpired next_attempt/created and
attempts; alert on sustained backlog. Inspect overdue organization deletions and worker
restart/failure logs. Configure external uptime and backup-age alerts before accepting
users; a Docker healthcheck alone does not page anyone or restart an unhealthy process.

Pin PostgreSQL to major 17. For minor updates: take/verify an external backup, test the
new image on a restored DB, stop writers, gracefully stop DB, update the image, restart,
verify and resume. Never point a new **major** PostgreSQL image at the old volume. For a
major upgrade create a new volume/cluster, pg_dump from the old cluster, pg_restore with
compatible/newer tools, compare verification reports, switch DATABASE_URL, and retain
the previous stopped cluster. Do not change the image tag to 18 and reuse a 17 data dir.

## Future AWS migration (maintenance window)

The app uses ordinary PostgreSQL SQL/SQLAlchemy and no added extensions. No local file
state or queue broker needs migration. Plan a small maintenance window:

1. Build/test the same image for ECS/Fargate. Prepare task roles, logging to CloudWatch,
   secrets injection from Secrets Manager, and private VPC networking.
2. Prepare private RDS PostgreSQL with compatible encoding/collation and PostgreSQL
   version (17 or a tested newer major). Enable RDS backups. Provision a DB owner/application
   role; RDS does not give true superuser access. Permit ingress only from application and
   temporary migration security groups. Install the current RDS CA bundle.
3. Deploy the unchanged static artifact to Amplify or S3 + CloudFront, with SPA rewrites
   and the custom app domain. Rebuild VITE_API_URL only if the API origin changes.
4. Stop public API ingress and **both** workers, then API. This is the maintenance mode;
   no new writes can enter. Keep source DB available only to migration operators.
5. Take the final pg_dump --format=custom --no-owner --no-privileges using PostgreSQL 17
   tools, produce the verification report while writes remain frozen, and upload the dump
   to independent encrypted storage. Keep the source untouched.
6. Reach RDS through a controlled migration host/task in its VPC. Use pg_restore through
   restore.sh into the new empty RDS DB with --no-owner --no-privileges. Reapply required
   owner/grants. Do not copy pg_roles or require unsupported superuser extensions.
7. Compare all counts/financial/stock aggregates and Alembic revision. Verify sequences,
   constraints, locale behavior, and MFA key recovery. Test ANALYZE after a large restore.
8. Set DATABASE_URL on ECS web/mail/maintenance tasks to the RDS endpoint with
   `?sslmode=verify-full`; supply PGSSLROOTCERT pointing to the mounted RDS CA bundle.
   The URL normalizer maps sslmode to asyncpg's ssl parameter. Keep the auth/encryption
   keys and PUBLIC_ORIGIN stable. Configure the ALB/proxy trust boundary explicitly.
9. Start ECS API and workers, smoke-test through controlled ingress, then change API DNS
   and reopen traffic. Monitor application/DB errors, queues and balances.
10. Retain the old database read-only/offline through the rollback window. After new writes
    occur on RDS, switching back is not a safe rollback without reconciling those writes.

Migrate future persistent object files through a storage adapter to S3, with key mapping,
checksums and independent object backups. Until uploads exist there is no file payload to
move. Secrets Manager and CloudWatch are infrastructure integrations; core business logic
remains unchanged. Add ElastiCache/a queue only if measured workload requires it.

Reference behavior: [Docker external volumes](https://docs.docker.com/reference/compose-file/volumes/),
[pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html),
[Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages),
[Heroku containers](https://devcenter.heroku.com/articles/container-registry-and-runtime),
[restic retention](https://github.com/restic/restic/blob/master/doc/060_forget.rst).

## Vercel frontend and Render API

For the Render API and mail worker, use `APP_ENV=production`,
`PUBLIC_ORIGIN=https://app.ivepos.me`, `TENANT_BASE_DOMAIN=ivepos.me`,
`DB_PROVIDER=supabase`, and the existing secret `SUPABASE_DATABASE_URL` using the
direct connection or session pooler on port 5432 with `sslmode=require`. Keep the
authentication and identity encryption secrets stable.

Run `alembic upgrade head` as the Render API pre-deploy command where supported,
with the same environment as the API. Otherwise run it as a separate release step
before deploying code that needs new columns. The current repository head is
`9b0c1d2e3f4a`; verify the deployed database's `alembic_version`. Applying migrations
does not copy local organizations or switch the running service's database provider.

On Vercel, build with `npm run build` and publish `dist`. Set
`VITE_APP_LOGIN_URL=https://app.ivepos.me/login`,
`VITE_PUBLIC_SITE_URL=https://www.ivepos.me`, `VITE_TENANT_BASE_DOMAIN=ivepos.me`,
and `VITE_API_URL` to the exact HTTPS Render API origin or its API custom domain.
Add `www.ivepos.me` and `app.ivepos.me` to the appropriate frontend project(s).
Organization addresses require wildcard domain/DNS/certificate routing.

New organizations start with platform sign-in. In Platform → Organizations, open
the organization and use the separate Subdomain form to enable a dedicated address.
Choose a word from its name, a prefix of at least two characters, or its initials
(for example, Atharva Organization can use `atharva`, `ath`, or `ao`). Reserved or
taken addresses are rejected. Changing or disabling an address requires platform
super-admin verification and is audited; previous links stop working after a change.
The optional-subdomain migration preserves existing addresses, including legacy
multiword slugs, and defaults future organizations to platform sign-in. Apply it
before deploying this backend/frontend change. It does not configure DNS or routing.

Organization hosts deliberately call their own `/api` path to keep cookies host-only.
Their frontend host must proxy these requests to Render and preserve the original
organization Host and HTTPS scheme, or use an explicitly authenticated tenant-aware
proxy integration. An external rewrite must be verified with a same-origin GET to
`https://<slug>.ivepos.me/api/v1/public/site` without an Origin header: it must return
that organization rather than the platform or HTML. The backend does not trust
arbitrary `X-Forwarded-Host` headers. Wildcard DNS and `VITE_API_URL` alone do not
provide this API routing; the supplied Caddy deployment implements it.

Provider references: [Vercel custom domains](https://vercel.com/docs/domains/working-with-domains/add-a-domain),
[Vercel external rewrites](https://vercel.com/docs/routing/rewrites), and
[Render pre-deploy commands](https://render.com/docs/deploys#pre-deploy-command).

## Automatic Supabase migrations through GitHub Actions

The backend workflow `.github/workflows/supabase-migrations.yml` runs on every
push to `main` and can be started manually from Actions on `main`. Migration-related
pull requests run only the disposable database validation job.

Add a repository Actions secret named `SUPABASE_DATABASE_URL` under Settings →
Secrets and variables → Actions. Use the same Supabase project as Render, with a
direct or session-pooler PostgreSQL URL on port 5432 and `sslmode=require`. The URL
needs permission to apply schema changes. Do not put it in workflow YAML or Vercel
public environment variables. IPv4-only runners should use the session pooler.

Before touching Supabase, the workflow migrates a fresh PostgreSQL 17 test database,
checks the no-op path, and runs deployment, database-transfer, and tenant tests.
After validation succeeds, the production job compares Supabase's revision to the checked-out migration head,
applies pending forward migrations, and verifies the final revision. It fails on
unknown/divergent history, missing secrets, or an unsuccessful upgrade. Current
databases are left unchanged. Production migration jobs are serialized and active
upgrades are not canceled by newer pushes.

Merge this workflow into `main` to enable it. This workflow does not deploy Render
or wait for Render automatic deploys. If Render auto-deploys the same push, it can
start before the migration finishes: deploy it after this workflow succeeds. Choose
one migration runner; remove the Render pre-deploy migration command when GitHub
Actions owns migrations. Keep Supabase backups configured independently. GitHub
Actions never copies local development records to Supabase.
