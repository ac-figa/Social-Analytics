# Build context is the repo root (not this directory) so the shared
# cross-platform layer can be copied in alongside this pipeline:
#   docker build -f instagramanalyticspipeline/Dockerfile -t ig-pipeline .
FROM python:3.12-slim

WORKDIR /app

COPY instagramanalyticspipeline/requirements.txt instagramanalyticspipeline/requirements.txt
COPY shared/requirements.txt shared/requirements.txt
RUN pip install --no-cache-dir \
    -r instagramanalyticspipeline/requirements.txt \
    -r shared/requirements.txt

COPY instagramanalyticspipeline/ instagramanalyticspipeline/
COPY shared/ shared/

ENTRYPOINT ["python", "-m", "instagramanalyticspipeline.src.pipeline"]
