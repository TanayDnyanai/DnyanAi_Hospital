# Deploy the chatbot to a Hugging Face Space (text chat first)

This deploys **both apps** as a private Docker Space. The root `portal/app.py`
mounts the calendar at `/calendar/` and the chatbot at `/chat/`, with a toggle
in the header. Both use the same Neon database.

## 1. Create the Space

- Go to https://huggingface.co/new-space
- Owner: `TanayDnyanai` (or your org)
- Space name: e.g. `dnyanai-receptionist`
- SDK: **Docker**
- Visibility: **Private** (recommended — see the security note below)
- Create it **empty** (no generated README), so the push below is clean.

## 2. Log in locally (do not paste tokens into chat)

```bash
pip install -U "huggingface_hub[cli]"
hf auth login
```

The token is stored in your local Hugging Face cache. Nothing is written to the repo.

## 3. Set Space secrets

In the Space page: **Settings → Variables and secrets → Add new secret**.
Add these with the exact values from your local `.env.local` / `medical-receptionist/.env`:

| Name | Notes |
|---|---|
| `DATA_SOURCE` | `neon` |
| `DATABASE_URL` | pooled Neon URL (app traffic) |
| `DATABASE_URL_UNPOOLED` | direct Neon URL (startup DDL) |
| `MEMORY_BACKEND` | `neon` |
| `LANGGRAPH_AES_KEY` | checkpoint encryption key — keep safe |
| `GROQ_API_KEY` | chatbot model key |
| `GROQ_MODEL` | e.g. `openai/gpt-oss-120b` |
| `WHATSAPP_MODE` | `link` — avoids sending real messages |
| `TIMEZONE` | `Asia/Kolkata` |
| `PORTAL_BASE_URL` | `https://<your-space-url>` after the first deploy |

Never add these to a file in the repo. The root `.env.example` lists the names only.

## 4. Push this repo to the Space

From `C:\Users\ULHAS ASWAR\OneDrive\Desktop\Voice_Agent\main`:

```bash
git remote add hf https://huggingface.co/spaces/<OWNER>/<SPACE-NAME>
git push hf main:main
```

If the Space already has a generated initial commit, use `git push -f hf main:main`
(only on the Space remote — never force-push the GitHub remote).

Hugging Face builds the Docker image from the root `Dockerfile`. The portal listens
on `PORT` (7860 on Spaces); open the root URL and switch apps with the toggle.

## 5. Verify

- Wait for the build to show "Running"
- Open the **App** tab → portal loads with Calendar active
- Use the header toggle to switch to Chatbot
- `https://<space-url>/health` → `{"status":"ok","apps":["calendar","chatbot"]}`
- Send a normal text message. Voice works in browsers that support speech APIs,
  but voice quality work is intentionally deferred.

## Security note

The app currently has **no authentication**: anyone who can reach it can supply a
`user_id` and read/cancel that patient's data. Keep the Space **private** and share
it only with testers until real auth (or at least a shared access code) is added.
WhatsApp is set to `link` mode so the deployed scheduler will not send real messages.
