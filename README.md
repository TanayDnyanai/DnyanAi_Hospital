# Medical Calendar + Receptionist

One repo containing both services. They share a single Neon Postgres database and
one canonical schema — there is no local database and no sync step in normal
operation.

```
main/
├── calendar/    # FastAPI calendar / scheduling API (port 8000)
└── chatbot/     # LangGraph receptionist, text + voice (port 8001)
```

The canonical Postgres schema lives in `calendar/backend/schema.py`. The chatbot
imports it rather than defining its own tables.

## Run locally

From the repo root (`main/`):

```bash
# Terminal 1 - calendar
cd calendar
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

# Terminal 2 - chatbot
cd ../chatbot
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

Health checks:

```bash
curl http://127.0.0.1:8000/health   # {"status":"ok","data_source":"neon"}
curl http://127.0.0.1:8001/health   # {"status":"ok"}
```

Chat UI: http://127.0.0.1:8001/

## Configuration

Copy `.env.example` to `.env.local` in this directory (or set the variables as
real environment variables when deploying). Never commit filled-in env files.

- `DATA_SOURCE=neon` — required for both services
- `DATABASE_URL` — pooled Neon URL, used for app traffic
- `DATABASE_URL_UNPOOLED` — direct Neon URL, used for DDL / migrations
- `MEMORY_BACKEND=neon` — checkpoints and patient memory in Postgres
- `LANGGRAPH_AES_KEY` — key used to encrypt checkpoints (keep it safe; losing it
  makes old checkpoints unreadable)
- `GROQ_API_KEY`, `GROQ_MODEL` — chatbot model
- `WHATSAPP_*` — notification settings

App-local `.env` files are also read, with real environment variables taking
precedence over every file.

## Behaviour notes

- Chatbot bookings/cancellations write to the same `appointments` and `patients`
  tables the calendar reads. Changes appear on the calendar immediately.
- Cancellation is enforced in code: the appointment must belong to the current
  patient, and the patient must explicitly confirm it. The prompt alone is not
  trusted for that write.
- Voice and text messages use separate LangGraph threads but share one patient
  profile. Only the voice channel speaks replies aloud.
- Voice handling still needs work (speaker recognition quality, interruption,
  noisy environments, mobile browsers). Text chat is the more stable path.
- `DATA_SOURCE=legacy` still exists for tests, but `clinic.db` is deliberately
  not included in this repo. Legacy-mode tests require that file locally.

## Tests

```bash
cd calendar && python -m pytest -q
cd ../chatbot && python -m pytest -q
DATABASE_URL=... DATA_SOURCE=neon python -m pytest -q tests/test_neon_smoke.py
```

The Neon smoke tests create and remove temporary rows; run them only against a
branch you are comfortable writing to.
