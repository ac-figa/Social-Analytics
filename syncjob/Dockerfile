# Build context is the repo root (not this directory) -- this job needs
# every pipeline directory plus shared/ alongside it, same reasoning as
# webapp/Dockerfile:
#   docker build -f syncjob/Dockerfile -t social-analytics-sync .
FROM python:3.12-slim

WORKDIR /app

COPY syncjob/requirements.txt syncjob/requirements.txt
COPY shared/requirements.txt shared/requirements.txt
COPY instagramanalyticspipeline/requirements.txt instagramanalyticspipeline/requirements.txt
COPY facebookpipeline/requirements.txt facebookpipeline/requirements.txt
COPY youtubepipeline/requirements.txt youtubepipeline/requirements.txt
COPY tiktokpipeline/requirements.txt tiktokpipeline/requirements.txt
RUN pip install --no-cache-dir \
    -r syncjob/requirements.txt \
    -r shared/requirements.txt \
    -r instagramanalyticspipeline/requirements.txt \
    -r facebookpipeline/requirements.txt \
    -r youtubepipeline/requirements.txt \
    -r tiktokpipeline/requirements.txt

COPY syncjob/ syncjob/
COPY shared/ shared/
COPY instagramanalyticspipeline/ instagramanalyticspipeline/
COPY facebookpipeline/ facebookpipeline/
COPY youtubepipeline/ youtubepipeline/
COPY tiktokpipeline/ tiktokpipeline/

# Cloud Run Jobs runs this to completion once per execution (unlike the
# webapp's long-lived gunicorn server) -- Secret Manager credentials are
# fetched at the top of main() itself, not baked in here.
CMD ["python", "syncjob/main.py"]
