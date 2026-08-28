# Build context is the repo root (not this directory), same reasoning as
# instagramanalyticspipeline/Dockerfile:
#   docker build -f youtubepipeline/Dockerfile -t yt-pipeline .
FROM python:3.12-slim

WORKDIR /app

COPY youtubepipeline/requirements.txt youtubepipeline/requirements.txt
COPY shared/requirements.txt shared/requirements.txt
RUN pip install --no-cache-dir \
    -r youtubepipeline/requirements.txt \
    -r shared/requirements.txt

COPY youtubepipeline/ youtubepipeline/
COPY shared/ shared/

ENTRYPOINT ["python", "-m", "youtubepipeline.src.pipeline"]
