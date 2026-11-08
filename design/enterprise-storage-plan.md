# Hermes Enterprise Storage — Detailed Plan

Status: **Phase 1 increment shipped** (commit `f709cc6a78`, branch `enterprise-storage-plan`, 2026-11-07) — facade + Postgres backend + SOUL.md as first document consumer, live-verified. See §2.1 for as-built notes.
Decisions locked in brainstorm 2025-07; see §2.

## 1. Motivation

Hermes today is a personal, single-machine agent: every artifact lives in the local
filesystem under `~/.hermes/<profile>/` (SQLite databases + Markdown files). This breaks
on ephemeral container infrastructure (Azure Container Apps, Web Apps):

- Pods are disposable — scale events, rollouts and replacements destroy local state.
- Mounting network storage (Azure Files) for *everything* is slow, fragile, and does not
  scale out (SQLite file locks across a shared mount are a corruption hazard).
- Horizontal scaling is impossible while file locks are the concurrency mechanism.

Goal: **PostgreSQL as the system of record**, the filesystem reduced to a disposable
scratch/execution cache, so Hermes deploys as **stateless containers** that scale out.

## 2. Locked decisions (brainstorm)

| # | Question | Decision |
|---|---|---|
| D1 | Backend approach | New **storage facade** above the existing facades; two backends: `local` (today's behavior, stays default) and `postgres`. No big-bang replacement. |
| D2 | Agent memory | **Built-in file memory** (`memories/MEMORY.md`, `USER.md`) moves to the facade's documents surface. The `MemoryProvider` ABC and external providers (Honcho, OpenViking, retaindb, …) are **untouched** — they keep their own storage. |
| D3 | Cron / kanban / ledgers | Migrated to postgres **later** through the same facade ops-tables surface (Phase 4). |
| D4 | Container mode | Postgres-primary. **No local write-ahead cache** — scratch is disposable, reads go straight to PG through the facade. |
| D5 | Tenant isolation | **Per-tenant schema** (one schema per profile/tenant) + shared `public` catalog. No RLS, no `tenant_id` columns. |
| D6 | Vector search | **pgvector** enabled on the postgres backend. Search is hybrid: tsvector (lexical) + pgvector (semantic). |

## 2.1 Implementation status — as built (Phase 1 increment, `f709cc6a78`)

**Shipped:**
- `storage/` package: `base` (frozen `Document`, `StorageBackend` ABC), `config` (`storage:` section), `schema` (v1 migrations), `postgres` (SQLAlchemy Core + psycopg, tenant-pinned `search_path`, self-provisioning on construction).
- **Local mode has no `LocalBackend` class** (deviation from §7, deliberate): `get_backend()` returns `None` and callers keep the existing file/SQLite paths unchanged — zero behavior change by construction, zero new code on the hot path. This satisfies the original Phase 1 exit (no behavior/perf change) with less surface than a delegating class.
- `hermes storage` CLI: `status` / `provision` / `soul show|set|import` (DSN redacted; `soul show` falls back to the local file in local mode).
- **SOUL.md is the first document consumer** (originally Phase 3): postgres mode reads the identity document from `tenant_<x>.documents` at session-start prompt build (same timing as today's file read → per-conversation prompt caching untouched), and `hermes_cli/config.py` seeds the default SOUL into the DB instead of a file. Postgres-mode SOUL.md is **user-owned**: the DB is the source of truth and nothing distribution-planted writes to it, so no manifest trust check; local mode keeps the distribution-manifest check byte-for-byte.
- `documents` table as built (refines §8): `kind, key, version, content, meta jsonb, checksum, created_by, created_at timestamptz`, PK `(kind, key, version)`, append-only. `superseded_by` from §8 **not created** (YAGNI until rollback/admin versioning lands).
- `[postgres]` extra: `sqlalchemy==2.1.7`, `psycopg==3.3.7` **pure-Python wheel** — the `[binary]` wheel set has no coverage for every locked platform and breaks PM's universal `uv lock`. Pins respect the supply-chain cutoff `2026-11-04T08:27:04Z` (newer psycopg exists but postdates it).
- Tests: config/schema unit tests, CLI tests, and **live Postgres integration tests** gated on `HERMES_STORAGE_TEST_DSN` (skipped cleanly without it; `scripts/run_tests.sh` now forwards the var). Canonical regression: 344 passed / 0 failed, live tests green against a real pgvector instance; E2E CLI verified end-to-end (`status` → `ready: true`, `soul set` → version bump, `soul show` → readback).

**Implementation decisions that constrained the code (keep these when extending):**
- `get_storage_config()` never calls `load_config()`: that takes the **non-reentrant** config lock and runs `ensure_hermes_home()` (which seeds SOUL → `get_backend` → recursion/deadlock). It reads the **frozen worker config snapshot first** (safe-worker audit contract: bound workers never read `config.yaml` from disk), then a raw lock-free `read_config_doc`. Missing/unreadable/invalid `storage:` section → local mode; only section-parsing errors raise.
- Teardown hygiene (see segfault note below): `atexit`-registered idempotent `close()` on every backend; the `hermes storage` CLI disposes the pool in a `finally` before interpreter finalization; the `connect` event listener captures only strings (no engine↔backend reference cycle).
- `uv.lock` was refreshed with plain `uv lock` in the dev sandbox (PM runtime not provisioned there) — re-run `hermes pm lock` in a full environment before merge if the lockfile policy requires it.

**Known environmental issue (not a code defect — root-caused, evidence in commit message):** a one-shot process using psycopg 3.3.7 on the WSL2 dev box can SIGSEGV *after* completing, during C-extension finalization. Measured: ~3%/run on the native filesystem, ~25%/run from a `/mnt/d` (drvfs) mount, **0/60 with the pure-Python pg8000 driver running the identical workload**, and the fault never occurs while any SIGSEGV handler is installed (signature of a library finalization race, not an application memory bug). Output and data are always correct. Containers on native filesystems + the long-lived agent process are not the exposed surface. Watch for a psycopg bump once 3.3.8 clears the supply-chain cutoff.

**Still to do (maps to §13):** sessions/messages/search + `hermes migrate` (Phase 2), skills/memories/config → documents + snapshot pinning (§9, Phase 3), ops tables (Phase 4), container hardening (Phase 5).

## 3. Non-goals

- Not replacing the `local` backend (dev/test/personal use + repo test policy keep using temp `HERMES_HOME`).
- Not touching the `MemoryProvider` plugin mechanism or external provider internals.
- Not putting secrets in the DB (Key Vault / container env, always).
- Not solving multi-region DR in v1 (Phase 5 backlog item).
- Not changing agentskills.io skill *format* — only where skill bytes live.

## 4. Current-state inventory

### SQLite stores (one file each, per profile)

| Store | Location | Owner | Contents |
|---|---|---|---|
| Session state (main) | `<home>/state.db` | `hermes_state.py` + ~70 siblings | transcripts, FTS5 search (+trigram probe), usage, compression, lineage, rewind, timeline |
| Cron deliveries | `<home>/cron/deliveries.db` | `cron/delivery_queue.py` | delivery queue |
| Cron executions | `<home>/cron/executions.db` | `cron/executions.py`, `cron/incidents.py` | run history, incidents |
| Cron notepad | `<home>/cron/notepad.db` | `cron/notepad.py` | scheduler scratch |
| Kanban | `<home>/kanban.db` (per board) | `hermes_cli/kanban_db*.py` | boards, cards |
| Gateway ledgers | various `<home>/*.db` | `gateway/delivery_ledger.py`, `lifecycle_ledger.py`, hosted-room stores | ledgers, room state, replicas |

Central connection factory: `hermes_state_dbfile.py` (`connect_tracked`, `open_db`).
Concurrency today = SQLite file locks/WAL (`hermes_state_wal.py`, `lockguard`).
Repair/portability machinery: `hermes_state_repair.py`, `hermes_state_portability.py`
(SQLite-specific — not ported to PG).

### Filesystem artifacts

| Artifact | Location | Role |
|---|---|---|
| `config.yaml` | `<home>/config.yaml` | per-profile behavior settings |
| `.env` | `<home>/.env` | secrets (repo policy: secrets only) |
| `SOUL.md` | `<home>/SOUL.md` | persona/soul, seeded by `hermes_cli/config.py` |
| Built-in memory | `<home>/memories/MEMORY.md`, `USER.md` | agent notes + user profile (`tools/memory_tool.py`, write-gated) |
| Skills | `skills/`, `optional-skills/` | Markdown + frontmatter, optionally multi-file (scripts, assets) |
| Memory providers | `plugins/memory/*` | loaded by name via `plugins/memory/load_memory_provider()` |
| Logs | `<home>/logs/` | file logs (→ stdout in container mode) |
| PM environments | managed venvs via `hermes pm` | Python deps (→ baked into image in container mode) |

### Memory provider seam (D2 boundary)

`agent/memory_provider.py` → `MemoryProvider` ABC: `name`, `is_available`, `initialize`,
`system_prompt_block`, `prefetch`, `sync_turn`, `get_tool_schemas`, `handle_tool_call`,
`shutdown`, `on_turn_start`, `identity_signature`, `on_session_end`, `on_session_switch`.
Resolved in `agent/agent_init_memory_provider.py`; built-in (empty/"default") provider
reads/writes `memories/` files via `tools/memory_tool.py`.

## 5. Target architecture

```
 CLI / TUI / Desktop / Web / API
 └──────────► Gateway (multiplexed) + Agent core
                │
        ┌───────▼──────────────────────────────────────┐
        │            STORAGE FACADE (new)              │
        │  documents · sessions · search · ops · locks │
        │  tenant-scoped: every call carries tenant    │
        └───────┬──────────────────────────┬───────────┘
      local backend (default)       postgres backend (enterprise)
   ┌────────────────────────┐   ┌──────────────────────────────────────┐
   │ ~/.hermes/<profile>/   │   │  PostgreSQL                           │
   │ state.db, cron/*.db,   │   │  public: tenants, migration_state     │
   │ SOUL.md, skills/,      │   │  tenant_<x>/: (one schema per tenant) │
   │ memories/, kanban.db   │   │   documents · snapshots               │
   └────────────────────────┘   │   sessions · messages · usage         │
                                │   fts (tsvector + pg_trgm)            │
                                │   cron_* · kanban · ledgers           │
                                │   media_meta · audit_log · locks      │
                                └──────────────────────────────────────┘
```

External memory providers (Honcho, OpenViking, …) bypass the facade entirely — they plug
into the unchanged `MemoryProvider` ABC and own their storage (D2).

## 6. Hard constraints (from repo invariants)

1. **Prompt caching is sacred.** System prompt state may only change between sessions.
   DB-backed content must therefore be **snapshot-pinned per session** (§9). The ONE
   allowed mid-session mutation is context compression.
2. **No new `HERMES_*` env vars for non-secret config.** Backend selection + DSN live in
   `config.yaml`; the connection *secret* (password) may come from `.env` / container env.
3. **Facade + siblings layout.** New behavior goes in siblings; the facade
   (`hermes_state.py`, `tools/memory_tool.py`, cron modules) stays the seam. Siblings
   late-import the facade inside functions.
4. **Profile scoping.** Code outside a turn must bind the owning tenant/profile explicitly
   (HX002) — the facade's tenant parameter is that binding.
5. **Tests.** `scripts/run_tests.sh` only; never bare pytest; tests never write
   `~/.hermes/`; E2E against temp `HERMES_HOME` for resolution/config/IO changes.
6. **Dependencies.** Upper bounds; `hermes pm lock` after `pyproject.toml` edits (psycopg or
   equivalent driver lands here).
7. **Footprint ladder.** This is fundamental infrastructure (allowed as core), but no new
   user-facing *tools* — admin surface is CLI commands + existing web routers.

## 7. Storage facade contract (Phase 0 freezes this)

New package `storage/` (name TBD in Phase 0). One abstract backend, two implementations.

```
StorageBackend
  lifecycle:   open(tenant) / close()
  documents:   get(kind, key, version=HEAD) -> Document
               put(kind, key, content, meta) -> version      # append-only versions
               list(kind, prefix) / delete(key)              # admin
               checksum(kind, key, version) -> sha254…       # integrity
  snapshots:   create(refs) -> snapshot_id
               load(snapshot_id) -> pinned refs              # read-only, immutable
  sessions:    append_message / load_transcript / rewrite(transcript)  # compression path
               usage(record) / usage(query)
  search:      text(query, filters) -> hits                   # lexical (tsvector)
               semantic(query, kind, k) -> hits                # pgvector KNN
               hybrid(query, filters) -> hits                  # fused (RRF)
  ops:         generic transactional table access for
               cron jobs/executions/deliveries/notepad, kanban, ledgers
  locks:       advisory(name, timeout) -> token               # replaces file locks
```

- **LocalBackend** delegates to the existing SQLite/file code (Phase 1 = pure wiring).
- **PostgresBackend** holds a tenant→schema resolver + pooled connections
  (in-process pool keyed by tenant, or pgbouncer upstream).
- Mapping of existing stores to surfaces:

| Existing store | Facade surface |
|---|---|
| `state.db` (transcripts/usage) | sessions |
| `state.db` FTS5 | search |
| `SOUL.md`, `skills/**`, `memories/*.md` | documents (+ snapshots) |
| `config.yaml` | documents (kind=CONFIG) in PG mode; local file in local mode |
| `cron/*.db`, `kanban.db`, ledgers | ops |
| file locks/WAL | locks (advisory) |
| media (currently files) | documents (kind=MEDIA_META) + `bytea`/blob (`media` table) |

## 8. PostgreSQL data model (per-tenant schema, D5)

```
public/
  tenants(schema_name, name, created, status)        # registry
  migration_state(schema_name, version, applied_at)  # per-schema migration tracking

tenant_<x>/                                           # one schema per tenant
  documents(key PK: kind|path, version, content, meta jsonb,
            checksum, created_by, created_at, superseded_by)
  snapshots(id, refs jsonb, created_at)
  sessions / messages / session_model_usage          # from hermes_state_schema.py
  fts: generated tsvector columns; pg_trgm for fuzzy (mirrors FTS5 trigram probe)
  embeddings(target_kind, target_ref, version, model, dim, vector)  # pgvector
  cron_jobs / cron_executions / cron_deliveries / cron_notepad
  kanban_boards / kanban_cards
  ledger_delivery / ledger_lifecycle / hosted_room_* (as census dictates)
  media(id, kind, key, bytes, size, created_at)      # bytea; Blob Storage later if needed
  audit_log(actor, action, target, at, detail jsonb)
  locks(name, held_by, token, acquired_at)           # advisory-lock table fallback
```

- Tenant provisioning = `CREATE SCHEMA tenant_<x>` → run migration set → seed defaults.
- Tenant deletion (GDPR) = `DROP SCHEMA tenant_<x> CASCADE` — no row-scrubbing.
- Cross-tenant admin queries loop schemas (accepted trade-off of D5).
- Migrations: one canonical set, versioned; runner applies to `public` + every tenant
  schema; tracked in `migration_state`. Requires `vector` extension (created at
  provisioning; host must offer pgvector — D6).

### 8.1 Embedding pipeline (D6)

- **Write-time:** persisting a message/document enqueues its embedding as a background
  job (auxiliary-client pattern; the repo proxy already forwards `/embeddings` to
  providers) — the write path never blocks on the embedding API.
- **Read-time:** embed the query once, pgvector KNN over `embeddings`.
- **Hybrid:** lexical + vector fused (reciprocal rank fusion; weights in `config.yaml`).
- **Model pinning:** `model` + `dim` recorded per row; switching embedding model is a
  recorded version change with a backfill (re-embed) job, never a silent overwrite.
- **Config:** embedding model/endpoint in `config.yaml` (behavior); API keys in `.env`
  / container env. Prompt caching unaffected — embeddings are read-side search only.

## 9. Prompt-cache safety: snapshot pinning

1. Session start: facade resolves HEAD of `SOUL`, persona, active skills, `MEMORY.md`,
   `USER.md`, `CONFIG` → `snapshots.create(refs)` → **pin `snapshot_id` on the session**.
2. System prompt is built **only** from pinned rows (via facade), never live HEAD reads.
3. Content updates (admin edits, agent memory writes) create new versions → apply to the
   **next session** (same defer pattern as today's `/skills install`).
4. Compression remains the only mid-session context mutation (existing invariant).
5. Acceptance test: system prompt built from DB == system prompt built from files,
   **byte-identical**, for the same content set.

Agent-side memory writes (`memory_tool` gates/ops) operate on document text exactly as
today, but commit through `documents.put` (new version) instead of `write_text`.

## 10. Memory design (D2)

- **Built-in memory**: `tools/memory_tool.py` I/O (`memories/MEMORY.md`, `USER.md`) routed
  through facade documents. Write gates, staged/pending ops, destructive-op gating all
  unchanged — they operate on content text; only the persistence call differs.
- **Providers**: `MemoryProvider` ABC, `plugins/memory/` loader, and external providers
  (Honcho, OpenViking, retaindb, byterover) **unchanged**. In container mode their
  credentials come from container env; their traffic is egress from the pod.
- `memory_context.py` / `memory_manager.py` orchestration unchanged.

## 11. Container deployment (D4) — Azure Container Apps

```
 Telegram / Discord / Slack / web / API
              │
 ┌────────────▼────────────────────────────────────────────┐
 │  Azure Container Apps — Hermes (stateless pods, 1..N)   │
 │  scratch /tmp/hermes = HERMES_HOME (disposable)         │
 │  logs → stdout → Azure Monitor                          │
 └────────────┬────────────────────────────────────────────┘
 ┌────────────▼───────┐  ┌────────────────┐  ┌──────────────────────┐
 │ PostgreSQL (RDB/   │  │ Azure Blob     │  │ Key Vault / container │
 │ Flexible Server)   │  │ (media, later) │  │ env (API keys, DSN)   │
 └────────────────────┘  └────────────────┘  └──────────────────────┘
```

Semantics:

- **Reads** go straight through the facade to PG. No boot-time data materialization.
- **Materialize-on-demand** only when a real path is required: skill scripts about to
  execute, media handed to a tool, terminal cwd. TTL-cleaned from scratch.
- **Writes** are write-through to PG; scratch is never source of truth.
- **Locks**: file locks → `pg_advisory_lock` (or `locks` table). Cron uses leader
  election: exactly one pod runs scheduler ticks at a time.
- **Gateway role** (holds platform connections, e.g. Telegram polling): keep at
  1 replica in v1, or adopt the existing `gateway/` replica/token-lock machinery —
  Phase 0 census decides. Agent-execution path scales freely.
- **Image**: multi-stage build with PM venv baked in (`hermes pm` envs are not built
  at pod boot).
- **Never in DB**: secrets (Key Vault/env), the venv (image layer).
- **Turn drain**: SIGTERM → finish in-flight turn, release locks, exit; readiness probe
  = PG reachable + schema migration current.
- **Cold-home boot**: `HERMES_HOME` starts empty and must reach full function from PG
  alone — this is the master acceptance test for the project.
- **Terminal backend caveat (ops decision)**: "local" terminal = inside the ephemeral
  pod. Durable terminal work should use the Docker/Modal/SSH/Daytona backends.

## 12. Migration (`hermes migrate`)

- Per-store importers: `state.db` → sessions/usage/fts; `SOUL.md`/skills/`memories/`
  → documents (v1, checksums); `cron/*.db`, `kanban.db`, ledgers → ops tables.
- Two-pass: import (idempotent, resumable) → **verify** (row counts + checksums + search
  parity sample). Importer is read-only on the source home.
- Existing repair/portability machinery (`hermes_state_repair.py`,
  `hermes_state_portability.py`) stays for the local backend; PG integrity is covered by
  checksums + `audit_log` instead.
- Rollback during transition: run local as source of truth, PG as replica? **No** (D4) —
  migration is one-shot with verification; keep the old home untouched as backup until
  cutover is confirmed.

## 13. Phased plan

### Phase 0 — Design freeze (docs only) — ✅ done (this doc, `b1278a4a3b`, `7b57a27221`)
- Full store census: every sqlite path, every file artifact, every lock (validate §4).
- Freeze `storage/` contract (§7); map every call site to a surface.
- PG migration set v0; per-schema runner design; pooling choice (in-proc vs pgbouncer).
- Gateway replica census (existing token-lock/replica code).
- E2E test strategy for PG (containerized Postgres for `scripts/run_tests.sh`).
- **Exit:** design doc reviewed; contract frozen; per-store mapping table agreed.

### Phase 1 — Local backend (zero behavior change) — ✅ done, simplified (see §2.1)
- `storage/` package; local mode = `get_backend()` returns `None`, existing paths untouched (no `LocalBackend` class — less surface, same zero-change guarantee).
- Routing of `hermes_state` / `memory_tool` I/O through the facade happens per-store as each store gets a postgres implementation (below), so local paths are never rewritten twice.
- No behavior/perf change; full suite green via `scripts/run_tests.sh`.
- **Exit:** suite green; byte-identical behavior; no added latency (measured).

### Phase 2 — Postgres: sessions + search — ⏳ in progress (backend core ✅)
- `PostgresBackend`: tenant→schema resolver, pool, migrations runner, provisioning. ✅ (shipped in the Phase 1 increment: `storage/postgres.py` + `storage/schema.py`, self-provisioning, idempotent v1 migrations, in-process pool keyed by tenant)
- ⏳ Remaining: sessions/messages/usage tables; append/load/rewrite paths.
- Sessions/messages/usage tables; append/load/rewrite paths.
- FTS5 → tsvector + pg_trgm; lexical parity test (same queries, same rank order).
- pgvector (D6): `embeddings` table, write-time background embedding pipeline,
  query-time KNN, hybrid (RRF) ranking, model/dim config + pinning.
- `hermes migrate` for `state.db` (two-pass, verify; embeddings backfilled post-import).
- **Exit:** full agent turns E2E in PG mode against temp `HERMES_HOME`; lexical parity +
  semantic search working; migration verified on a real home.

### Phase 3 — Content store — ⏳ partially done (SOUL.md ✅, see §2.1)
- `documents` + versions + checksums ✅ (as-built columns in §2.1); SOUL.md → documents ✅ (read + seed paths wired).
- ⏳ Remaining: skills/`memories`/config → documents surface.
- Snapshot pinning at session start (§9); system prompt from pinned rows.
- Materialize-on-demand to scratch (skill scripts, media).
- Admin versioning: CLI commands (list/promote/rollback versions) via existing
  `hermes_cli` mixin pattern.
- **Exit:** cold empty `HERMES_HOME` boots fully from PG; prompt byte-identity test green;
  memory write-gates behave identically in PG mode.

### Phase 4 — Ops tables (D3)
- cron (jobs/executions/deliveries/notepad), kanban, gateway ledgers → ops surface.
- Advisory locks; cron leader election.
- `hermes migrate` for these stores.
- **Exit:** two replicas, no lost updates; single-leader scheduler tick proven.

### Phase 5 — Container + enterprise hardening
- Container image (venv baked), cold-home boot E2E, readiness/liveness, turn drain.
- Secrets via env/Key Vault; DSN in `config.yaml`.
- Audit log writes on admin ops; retention/deletion (`DROP SCHEMA` flow).
- Cross-tenant admin queries; backup/DR (pg_dump per schema; DR = backlog).
- **Exit:** deployed on Azure Container Apps; scale-out E2E; zero data loss on pod
  kill/redeploy.

Sequencing note: a single-replica container deployment can ship after Phases 2+3
(cron stays file-based on a mounted volume or single pod); multi-replica requires Phase 4.

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Per-turn PG latency on hot path | connection reuse, batching, prepared statements; measure in Phase 2; local stays available |
| Backend behavior drift (local vs PG) | parity E2E suite run against **both** backends (same scenarios, temp homes) |
| Migration edge cases (WAL, corrupt FTS) | two-pass verify; checksums; source home untouched until cutover |
| Skills assuming file presence | materialize-on-demand; census of skill scripts that read CWD-relative paths |
| Gateway message duplication under scale-out | v1: gateway role = 1 replica; later: existing token-lock/replica machinery |
| Schema proliferation at many tenants | pool sizing in Phase 0; per-tenant DB is the escape hatch (D5 leaves the door open) |
| Ephemeral terminal state | ops guidance: non-local terminal backends for durable work |
| External providers in containers | egress + Key Vault credentials; provider behavior itself out of scope (D2) |

## 15. Open items for continued brainstorming

1. **Scale targets** — tenants, concurrent sessions, msg/s → pool sizing & pgbouncer call.
2. **Media strategy trigger** — when do we move `bytea` → Blob Storage (size/count thresholds)?
3. **Gateway scale-out** — 1-replica policy vs adopting existing replica machinery (Phase 0 census answers, then decide).
4. **Admin surface scope** — CLI-only first vs web-dashboard tenant management in v1.
5. **Multi-region / DR** — required for v1 or backlog?
6. **Per-tenant metering/billing** — usage data exists (`session_model_usage`); who reads it, cross-tenant?
7. **Honcho/external providers in container mode** — egress allowances, credential rotation.
8. **Embedding model choice** — which provider/model via the proxy (dims, cost per
   message at scale), and the backfill strategy for model swaps (D6).
9. **psycopg 3.3.8 bump** — blocked by the supply-chain cutoff (published
   `2026-11-04T09:37:35Z`, after the `2026-11-04T08:27:04Z` cutoff); revisit when the
   cutoff moves, may resolve the WSL2 exit-finalization SIGSEGV noted in §2.1.
