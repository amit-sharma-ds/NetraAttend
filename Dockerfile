# NetraAttend - cloud "web" role: website + API + employee panel + field attendance.
# The CCTV camera stays on the office PC (ROLE "all"); both use the same MongoDB.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    NETRA_ROLE=web

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# face models are not in git (InsightFace license) - fetched from the official release
COPY download_models.py .
RUN python download_models.py

COPY . .
# settings come from environment variables (NETRA_*), see .env.example
RUN cp config.example.py config.py

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=4)"
CMD ["python", "-u", "app.py"]
