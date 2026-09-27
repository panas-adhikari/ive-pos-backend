# Production readiness audit — 2026-09-27

This records the state **before** the changes in this delivery. See README.md in this
folder for implementation and VALIDATION.md for observed results and remaining gates.

## 1. Current architecture

- Workspace root contains development Compose and documentation, but its `.git` metadata
  is unavailable in this environment. `backend/` and `frontend/` are separate Git repositories,
  both on `main`. Deployment workflows must live in each repository's `.github/workflows`.
- Frontend: React 19, Vite 8, TypeScript, React Query; Dexie and Zustand dependencies.
  `App.tsx` selects screens through React state. There is no React Router dependency,
  BrowserRouter, or path-to-screen mapping. Identity email links use URL fragments and
  remove the token from history. Three fetch sites assume relative `/api` URLs.
- Backend: FastAPI, Pydantic settings, async SQLAlchemy with asyncpg, PostgreSQL.
  Routes cover identity, platform administration, organizations, stores, employees,
  products, cash sales, stock, receipts and reporting.
- PostgreSQL 17 Alpine in development Compose, named `postgres_data` volume mounted
  at `/var/lib/postgresql/data`, no host database port. API has a random loopback port.
  Native launcher can also initialize a local PostgreSQL 16 cluster under `.postgres_data`.
- API database sessions are context-managed; business writes use transactions.
  API engine already uses pre-ping, connection/query/lock timeouts, parameter hiding,
  and disposal on shutdown. Pool sizes were implicit SQLAlchemy defaults.
- Twelve linked Alembic migrations, including the pre-existing untracked item-preset
  migration, create the current schema. There is no runtime metadata.create_all/drop_all.
  Alembic reads DATABASE_URL. Existing downgrades intentionally drop tables/columns.
- Mail is a separate process using an encrypted transactional PostgreSQL outbox,
  row locking with SKIP LOCKED, backoff, expiration, and SIGTERM handling.
  Delivery is at least once: a crash after provider acceptance can cause duplicates.
  No Redis/Celery needed. Organization deletion runs every minute within API lifespan.

## 2. What works correctly

Existing persistent local volume, private database networking, Alembic history,
transaction/session cleanup, Argon2 passwords, rotating opaque sessions, refresh replay
revocation, MFA, exact-origin CSRF, and separate mail processing should be retained.
Existing `/api/v1/health` is liveness; `/api/v1/ready` performs a bounded SELECT 1.
Database exceptions and validation inputs are not returned by health/input handlers.

## 3. Deployment blockers

- Relative frontend fetch URLs, same-origin credentials, and explicitly disabled CORS.
- Fixed Docker API port 8000 and disabled proxy headers (all clients behind one proxy
  otherwise share an IP rate-limit bucket).
- No hosted Compose, deploy workflows, external backup schedule, retention, or restore drill.
- Runtime requirements partly ranged; Docker ignore omits Git metadata and local DB files.
- Pages custom-domain asset and error-document handling not configured.
- Hosted domain, DNS, hosting account, external backup credentials and billing are not
  available in this workspace; no live deployment can be claimed.
- GitHub Pages' published restrictions exclude commercial SaaS and discourage password
  transactions. Pages compatibility alone does not make it suitable for this POS service:
  https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits
- Heroku dyno filesystems are ephemeral and cannot host this persistent PostgreSQL setup.
  Use a persistent VM for the database (and preferably API/worker initially):
  https://devcenter.heroku.com/articles/container-registry-and-runtime

## 4. Production data risks

- Local named volume survives ordinary down but can be deleted by down -v.
- No off-server recovery copy. MFA/outbox encryption key must also survive a host loss.
- Development launcher accepts arbitrary DATABASE_URL and runs migrations automatically.
- Test truncation/seed guards check only `_test` naming; smoke guard uses removable assert.
- Root `.env` is a symlink to backend/.env and was explicitly exempted by root ignore rules.
- `backupcodes.txt` exists at the workspace root. Its contents were not printed/copied.
  Backend/frontend tracked-file inspection found no tracked .env beyond the example;
  this is not certification of unavailable root Git metadata or all historical commits.
- Root-level deployment files may not be pushed when only backend/frontend repos are pushed.
- Database downgrade functions are destructive by design. Never automatically downgrade.

## 5. Required changes implemented

Public API-origin configuration; exact-origin credentialed CORS; portable PORT/proxy
startup; bounded pools and mail reconnection; separate hosted deletion worker; production
Compose with external volumes and separate admin/app roles; independent encrypted
backups with retention and safe restore; test/development safeguards; pinned runtime
packages; static build workflow; deployment and recovery runbooks. No historical schema
revision is edited or replaced by this task.

## 6. Optional improvements

Managed RDS after revenue; object storage when uploads actually exist; provider uptime
alerts; point-in-time recovery if the acceptable data loss becomes shorter than the
backup interval; image digest automation, dependency vulnerability scanning and staged
promotion. These do not require Kubernetes, a new queue, or a business-logic rewrite.

## 7. Preserve and storage classification

Preserve existing operations/model/UI edits, all business routes, schema history,
authentication architecture, outbox and local Compose port discovery. No React Router
or HashRouter is introduced. Persistent data lives in PostgreSQL; organization image
fields contain external URLs. Receipts render/print in the browser. No server-side upload,
PDF, export, contract, or report files are currently written. Development cluster/log/port
files are local tooling state. Production application containers can be read-only with
/tmp as transient memory. When uploads arrive, introduce a small storage interface
(put/open/delete objects by opaque key) and a durable local-volume or S3 adapter; store
keys in PostgreSQL and back up objects separately. There is no unused storage framework now.
