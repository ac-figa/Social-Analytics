# Deploying the refresh as a scheduled Cloud Run Job

Same pattern as `instagramanalyticspipeline/deploy/README.md` -- read that
first if this is your first deploy. This file only calls out what
differs for YouTube.

## 1. One-time setup

```bash
export PROJECT_ID=your-gcp-project
export REGION=us-central1
export REPO=youtube-analytics

gcloud config set project "$PROJECT_ID"

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION"

gcloud iam service-accounts create yt-analytics-runner \
  --display-name="YouTube Analytics Pipeline"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:yt-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:yt-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

## 2. Store the API key as a secret

```bash
echo -n "YOUR_YOUTUBE_API_KEY" | \
  gcloud secrets create youtube-api-key --data-file=-

gcloud secrets add-iam-policy-binding youtube-api-key \
  --member="serviceAccount:yt-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## 3. Build and push the image

From the **repo root**:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -f youtubepipeline/Dockerfile \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/yt-pipeline:latest" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/yt-pipeline:latest"
```

## 4. Create the Cloud Run Job

```bash
gcloud run jobs create yt-analytics-refresh \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/yt-pipeline:latest" \
  --region="$REGION" \
  --service-account="yt-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="YOUTUBE_CHANNEL_ID=YOUR_CHANNEL_ID,BQ_PROJECT_ID=${PROJECT_ID},BQ_DATASET=youtube_analytics" \
  --set-secrets="YOUTUBE_API_KEY=youtube-api-key:latest" \
  --max-retries=1 \
  --task-timeout=20m
```

## 5. Schedule it

```bash
gcloud scheduler jobs create http yt-analytics-daily \
  --location="$REGION" \
  --schedule="30 6 * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/yt-analytics-refresh:run" \
  --http-method=POST \
  --oauth-service-account-email="yt-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com"
```

(Staggered after the Instagram/Facebook jobs so all three aren't hitting
BigQuery/quota limits simultaneously.)
