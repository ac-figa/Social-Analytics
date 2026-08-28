# Deploying the refresh as a scheduled Cloud Run Job

This runs the pipeline on a schedule without a server to maintain. Each run
is a fresh container that exits when the pipeline finishes.

## 1. One-time setup

```bash
export PROJECT_ID=your-gcp-project
export REGION=us-central1
export REPO=instagram-analytics

gcloud config set project "$PROJECT_ID"

# Artifact Registry to hold the container image
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION"

# Service account the job will run as
gcloud iam service-accounts create ig-analytics-runner \
  --display-name="Instagram Analytics Pipeline"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:ig-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:ig-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

## 2. Store the Meta access token as a secret (never bake it into the image)

```bash
echo -n "YOUR_LONG_LIVED_SYSTEM_USER_TOKEN" | \
  gcloud secrets create meta-access-token --data-file=-

gcloud secrets add-iam-policy-binding meta-access-token \
  --member="serviceAccount:ig-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## 3. Build and push the image

The Dockerfile now lives in a subdirectory and the build needs the **repo
root** as its context (to bundle the shared cross-platform content layer
alongside this pipeline), so `gcloud builds submit --tag` (which only
looks for a Dockerfile at the source root) no longer applies directly --
build and push with plain `docker` instead, from the repo root:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -f instagramanalyticspipeline/Dockerfile \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/ig-pipeline:latest" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/ig-pipeline:latest"
```

## 4. Create the Cloud Run Job

```bash
gcloud run jobs create ig-analytics-refresh \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/ig-pipeline:latest" \
  --region="$REGION" \
  --service-account="ig-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="IG_USER_ID=YOUR_IG_BUSINESS_ACCOUNT_ID,GRAPH_API_VERSION=v21.0,BQ_PROJECT_ID=${PROJECT_ID},BQ_DATASET=instagram_analytics" \
  --set-secrets="META_ACCESS_TOKEN=meta-access-token:latest" \
  --max-retries=1 \
  --task-timeout=20m
```

## 5. Schedule it (e.g. daily at 6am UTC)

```bash
gcloud scheduler jobs create http ig-analytics-daily \
  --location="$REGION" \
  --schedule="0 6 * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/ig-analytics-refresh:run" \
  --http-method=POST \
  --oauth-service-account-email="ig-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com"
```

## Running it manually

```bash
gcloud run jobs execute ig-analytics-refresh --region="$REGION"
```

## Why a Job instead of a Service

There's no request to serve -- this is a batch task that runs, does its
work, and exits. A Cloud Run Job bills only for actual run time, retries
on failure automatically, and needs no HTTP endpoint or authentication
layer of its own.
