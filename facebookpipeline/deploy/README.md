# Deploying the refresh as a scheduled Cloud Run Job

Same pattern as `instagramanalyticspipeline/deploy/README.md` -- read that
first if this is your first deploy, it has the fuller explanation. This
file only calls out what differs for Facebook.

## 1. One-time setup

```bash
export PROJECT_ID=your-gcp-project
export REGION=us-central1
export REPO=facebook-analytics

gcloud config set project "$PROJECT_ID"

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION"

gcloud iam service-accounts create fb-analytics-runner \
  --display-name="Facebook Analytics Pipeline"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:fb-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:fb-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

## 2. Reuse the same Meta token secret

If you already deployed the Instagram pipeline, its `meta-access-token`
secret is the same token this pipeline needs -- just grant the new
service account access to it, no need to create it again:

```bash
gcloud secrets add-iam-policy-binding meta-access-token \
  --member="serviceAccount:fb-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## 3. Build and push the image

From the **repo root**:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -f facebookpipeline/Dockerfile \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/fb-pipeline:latest" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/fb-pipeline:latest"
```

## 4. Create the Cloud Run Job

```bash
gcloud run jobs create fb-analytics-refresh \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/fb-pipeline:latest" \
  --region="$REGION" \
  --service-account="fb-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="FB_PAGE_ID=YOUR_FACEBOOK_PAGE_ID,GRAPH_API_VERSION=v21.0,BQ_PROJECT_ID=${PROJECT_ID},BQ_DATASET=facebook_analytics" \
  --set-secrets="META_ACCESS_TOKEN=meta-access-token:latest" \
  --max-retries=1 \
  --task-timeout=20m
```

## 5. Schedule it

```bash
gcloud scheduler jobs create http fb-analytics-daily \
  --location="$REGION" \
  --schedule="15 6 * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/fb-analytics-refresh:run" \
  --http-method=POST \
  --oauth-service-account-email="fb-analytics-runner@${PROJECT_ID}.iam.gserviceaccount.com"
```

(Staggered 15 minutes after the Instagram job so both aren't hitting the
same Meta App's rate limits simultaneously.)
