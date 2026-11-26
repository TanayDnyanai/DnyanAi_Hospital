# Medical Calendar

Calendar / reception desk UI for the clinic. In production it runs entirely on
**Neon Postgres** — there is **no local SQLite database** and **no dependency on
the separate voice-agent/chatbot app**.

## Run against Neon (recommended)

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set DATA_SOURCE=neon plus DATABASE_URL / DATABASE_URL_UNPOOLED
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8100
```

Health check: `GET /health` returns `{"status":"ok","data_source":"neon"}`.

- `DATABASE_URL` (pooled) is used for application read/write traffic.
- `DATABASE_URL_UNPOOLED` (direct) is used for schema setup.
- The canonical schema is in `backend/schema.py`. It is imported by the
  calendar adapter, the receptionist adapter and the migration script, so all
  three agree on columns, indexes and shared tables.
- No `clinic.db` is read or written in this mode. The legacy adapter is not
  imported by the data-source factory when `DATA_SOURCE=neon`.

## Other modes

- `DATA_SOURCE=production` — self-contained SQLite reference adapter
  (`production.db`). Useful for local development without Neon.
- `DATA_SOURCE=legacy` — reads the receptionist's `clinic.db` and delegates
  writes to `../chatbot/database.py`. This mode requires the
  receptionist project to be present; it is a compatibility fallback, not the
  production path.

## Tests

```bash
python -m pytest -q
```

The default suite uses a throwaway copy of the legacy `clinic.db`, so it needs
`../chatbot/clinic.db` and `database.py` available locally. The
live Neon round-trip test is opt-in:

```bash
DATA_SOURCE=neon python -m pytest tests/test_neon_smoke.py -q
```

Never run the smoke tests against production: they insert and delete rows. Use
a temporary Neon branch instead.

## Schema changes

Do not hand-edit database structure in more than one place. Update
`backend/schema.py`, validate on a temporary Neon branch, then promote to the
production branch deliberately. `scripts/migrate_sqlite_to_neon.py` imports the
same schema for one-time data migration.

## Security note

The current role headers (`X-Role`, `X-Doctor-Id`) are a demo mechanism and are
not authentication. Do not expose this API publicly without adding real
authentication, an explicit CORS allowlist and role-aware PII redaction.
