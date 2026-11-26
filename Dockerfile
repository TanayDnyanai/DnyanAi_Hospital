FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY chatbot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The chatbot imports the calendar's canonical schema at runtime.
COPY calendar/ ./calendar/
COPY chatbot/ ./chatbot/

WORKDIR /app/chatbot

# Hugging Face Spaces sets PORT (usually 7860); local Docker defaults to 8001.
EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port \"${PORT:-7860}\""]
