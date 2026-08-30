# Automated daily sync (Cloud Run Job + Cloud Scheduler)

Runs the exact same sequence as the dashboard's "Update all datasets"
button (every platform pipeline, the Calcio Bros accounts, cross-platform
matching, the Instagram Duration backfill) automatically every day at
6:00 AM Toronto time -- no laptop required to be on.

This is separate from `webapp/deploy/README.md`: that deploys the
dashboard you browse; this deploys a one-shot job that just runs the
sync and exits. They share the same BigQuery project and Artifact
Registry repo, but are otherwise independent -- you can set this up
whether or not the dashboard is deployed to Cloud Run.

Do this from the repo root (`~/Social-Analytics`), after `webapp/deploy/README.md`'s
Step 2 (Docker + gcloud already installed and authenticated). If you
haven't set `PROJECT_ID`/`REGION`/`REPO` in this terminal session yet:

```bash
export PROJECT_ID=project-6f3dedab-dbda-4261-90d
export REGION=us-central1
export REPO=social-analytics-dashboard
```

## 1. Move your local credentials into Secret Manager

The job can't read your Mac's `.env` files -- it runs in Google's cloud
with nobody's laptop involved. Each `.env` file gets uploaded once, as
its own secret, in one piece (nothing needs to be split apart or
retyped):

```bash
gcloud services enable secretmanager.googleapis.com run.googleapis.com cloudscheduler.googleapis.com

gcloud secrets create sync-env-shared --data-file=shared/.env
gcloud secrets create sync-env-instagram --data-file=instagramanalyticspipeline/.env
gcloud secrets create sync-env-facebook --data-file=facebookpipeline/.env
gcloud secrets create sync-env-youtube --data-file=youtubepipeline/.env
gcloud secrets create sync-env-tiktok --data-file=tiktokpipeline/.env
```

Only run these two if you've already set up the Calcio Bros accounts
(skip whichever you haven't gotten to yet -- the job logs a line and
skips that account too, it won't fail the whole run):

```bash
gcloud secrets create sync-env-instagram-calciobros --data-file=instagramanalyticspipeline/.env.calciobros
gcloud secrets create sync-env-tiktok-calciobros --data-file=tiktokpipeline/.env.calciobros
```

If you ever need to update one of these later (e.g. you manually
refreshed a token), re-run with `versions add` instead of `create`:

```bash
gcloud secrets versions add sync-env-instagram --data-file=instagramanalyticspipeline/.env
```

## 2. Create the job's service account

Separate from the dashboard's `dashboard-runner` account -- this one
also needs Secret Manager access, which the dashboard never should.

```bash
gcloud iam service-accounts create sync-runner --display-name="Social Analytics Sync Job"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:sync-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:sync-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

Grant read access to every secret you created in Step 1 (only list the
Calcio Bros ones here if you actually created them):

```bash
for secret in sync-env-shared sync-env-instagram sync-env-facebook sync-env-youtube sync-env-tiktok \
              sync-env-instagram-calciobros sync-env-tiktok-calciobros; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:sync-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

TikTok's refresh token rotates on every run (see
`tiktokpipeline/src/config.py`), so the job also needs permission to
*write* a new version of just those two secrets -- nothing else:

```bash
for secret in sync-env-tiktok sync-env-tiktok-calciobros; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:sync-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretVersionAdder"
done
```

## 3. Build and push the image

Same reasoning as the dashboard's Dockerfile -- built from the repo root
so every pipeline directory can be copied in:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build --platform linux/amd64 -f syncjob/Dockerfile \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/sync:latest" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/sync:latest"
```

## 4. Create the Cloud Run Job

```bash
gcloud run jobs create social-analytics-sync \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/sync:latest" \
  --region="$REGION" \
  --service-account="sync-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID}" \
  --max-retries=0 \
  --task-timeout=1800 \
  --memory=512Mi
```

Test it once by hand before scheduling anything, so you can see it
actually sync in Cloud Logging rather than waiting until tomorrow morning:

```bash
gcloud run jobs execute social-analytics-sync --region="$REGION"
```

That prints an execution name; watch its progress and logs either at
`https://console.cloud.google.com/run/jobs/details/${REGION}/social-analytics-sync/executions`
or with:

```bash
gcloud run jobs executions describe <execution-name> --region="$REGION"
```

## 5. Schedule it for 6:00 AM every day (Toronto time)

Cloud Scheduler needs permission to trigger the job:

```bash
gcloud run jobs add-iam-policy-binding social-analytics-sync \
  --region="$REGION" \
  --member="serviceAccount:sync-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http social-analytics-sync-daily \
  --location="$REGION" \
  --schedule="0 6 * * *" \
  --time-zone="America/Toronto" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/social-analytics-sync:run" \
  --http-method=POST \
  --oauth-service-account-email="sync-runner@${PROJECT_ID}.iam.gserviceaccount.com"
```

Done -- from tomorrow on, the sync runs on its own every morning. The
dashboard's manual "Update all datasets" button still works exactly as
before if you ever want to trigger one right now instead of waiting.

## Changing the time later

```bash
gcloud scheduler jobs update http social-analytics-sync-daily \
  --location="$REGION" \
  --schedule="0 7 * * *" \
  --time-zone="America/Toronto"
```

## Updating after a code change

Repeat Steps 3 and the `gcloud run jobs create` command in Step 4, but
with `gcloud run jobs update` instead of `create` (same flags):

```bash
docker build --platform linux/amd64 -f syncjob/Dockerfile \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/sync:latest" .
docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/sync:latest"

gcloud run jobs update social-analytics-sync \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/sync:latest" \
  --region="$REGION"
```

## Costs

A Cloud Run Job only bills for the minutes it's actually running (once a
day, likely a few minutes total) -- this is typically pennies a month,
same free-tier logic as the dashboard in `webapp/deploy/README.md`.
