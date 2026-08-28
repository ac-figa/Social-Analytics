# Build context is the repo root (not this directory), same reasoning as
# instagramanalyticspipeline/Dockerfile:
#   docker build -f facebookpipeline/Dockerfile -t fb-pipeline .
FROM python:3.12-slim

WORKDIR /app

COPY facebookpipeline/requirements.txt facebookpipeline/requirements.txt
COPY shared/requirements.txt shared/requirements.txt
RUN pip install --no-cache-dir \
    -r facebookpipeline/requirements.txt \
    -r shared/requirements.txt

COPY facebookpipeline/ facebookpipeline/
COPY shared/ shared/

ENTRYPOINT ["python", "-m", "facebookpipeline.src.pipeline"]
