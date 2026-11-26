FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY chatbot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The chatbot imports the calendar's canonical schema at runtime, and the
# portal mounts both apps behind one URL with a toggle.
COPY calendar/ ./calendar/
COPY chatbot/ ./chatbot/
COPY portal/ ./portal/

EXPOSE 7860

# Hugging Face Spaces sets PORT (usually 7860); local Docker defaults to 7860.
CMD ["sh", "-c", "uvicorn portal.app:app --host 0.0.0.0 --port \"${PORT:-7860}\""]
