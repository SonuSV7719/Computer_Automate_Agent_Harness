# Cloud sandbox for Cloud Run: a Linux desktop with a demo app, the Shadow agent driving it,
# and the screen served to the browser (view only). Build from the repository root:
#   docker build -f sandbox/Dockerfile -t shadow-sandbox .
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:1 SHADOW_HOME=/tmp/shadow SHADOW_OCR_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc novnc websockify xdotool wmctrl x11-utils \
      tk fonts-dejavu-core libgl1 libglib2.0-0 procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml /srv/pyproject.toml
COPY shadow /srv/shadow
RUN pip install --no-cache-dir --retries 10 --timeout 60 . rapidocr-onnxruntime opencv-python-headless numpy \
    && python -c "from shadow.cv.ocr import RapidOcr; RapidOcr()"   # fail the build if OCR cannot load
COPY sandbox /srv/sandbox
RUN chmod +x /srv/sandbox/entrypoint.sh /srv/sandbox/app.sh

# SHADOW_BRAIN_URL and SHADOW_DEVICE_TOKEN are set on the Cloud Run service.
ENV SHADOW_DEMO_APP=/srv/sandbox/app.sh SHADOW_TARGET="Legacy Invoice"
CMD ["/srv/sandbox/entrypoint.sh"]
