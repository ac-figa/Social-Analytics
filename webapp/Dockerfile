# Build context is the repo root (not this directory) so the shared
# cross-platform layer can be copied in alongside this app:
#   docker build -f webapp/Dockerfile -t social-analytics-dashboard .
FROM python:3.12-slim

WORKDIR /app

COPY webapp/requirements.txt webapp/requirements.txt
COPY shared/requirements.txt shared/requirements.txt
RUN pip install --no-cache-dir \
    -r webapp/requirements.txt \
    -r shared/requirements.txt

COPY webapp/ webapp/
COPY shared/ shared/

WORKDIR /app/webapp

# Cloud Run sets $PORT and expects the container to listen on it.
# 2 workers is plenty for a single-user dashboard; more would just waste
# the Cloud Run instance's memory limit for no benefit here. --timeout 60
# gives slower BigQuery queries room without leaving a hung worker
# unbounded -- the Sync feature itself runs in a background thread
# outside the request/response cycle (see src/sync.py), so it's not
# affected by this at all.
CMD exec gunicorn --bind :$PORT --workers 2 --timeout 60 app:app
