# Cloud brain for Cloud Run. Build from the repository root:
#   docker build -f brain/Dockerfile -t shadow-brain .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY brain/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt
# The brain reuses the agent's Gemini client and prompts, nothing else from the agent.
COPY shadow/__init__.py shadow/net.py /srv/shadow/
COPY shadow/llm /srv/shadow/llm
COPY brain /srv/brain
# Cloud Run sets PORT. One worker: long-polling agents are woken in-process.
CMD ["sh", "-c", "exec uvicorn brain.app.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 75"]
