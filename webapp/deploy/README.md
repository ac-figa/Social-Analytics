# Deploying the dashboard to Cloud Run

Gets you a real `https://` URL you can open from anywhere, protected by
Google sign-in restricted to just your own account. No Load Balancer or
static IP needed -- the login is built into the app itself (see
`../src/auth.py`).

**Sync doesn't work on this deployment** -- see `deploy/README.md`'s note
in the app's own Sync tab. It needs every platform's live API
credentials, which never get bundled into the image on purpose. Keep
running the pipelines locally; this dashboard reads the same BigQuery
project either way, so it reflects new data immediately.

## 1. Create a Google OAuth Client ID

This is what lets the app show a "Sign in with Google" screen.

1. Go to https://console.cloud.google.com/apis/credentials (make sure
   your project is selected in the top bar -- same project as your
   BigQuery data).
2. If prompted, configure the **OAuth consent screen** first: User Type
   **External** is fine (you'll restrict who can actually log in via
   `ALLOWED_EMAILS` below, not via Google's own user list) -- App name
   anything (e.g. "Social Analytics Dashboard"), your email for the
   support/developer contact fields. Publishing status can stay
   "Testing" -- add your own Google account under **Test users** on that
   screen if it asks.
3. Click **Create Credentials -> OAuth client ID**. Application type:
   **Web application**.
4. Under **Authorized redirect URIs**, you won't know the final Cloud Run
   URL yet -- do the first deploy below with a placeholder, note the real
   URL Cloud Run gives you, then come back here and add:
   `https://YOUR-CLOUD-RUN-URL/auth/callback`
5. Copy the **Client ID** and **Client Secret** -- these are `GOOGLE_CLIENT_ID`
   / `GOOGLE_CLIENT_SECRET` below. Treat the secret like a password.

## 2. One-time GCP setup

```bash
export PROJECT_ID=your-gcp-project
export REGION=us-central1
export REPO=social-analytics-dashboard

gcloud config set project "$PROJECT_ID"

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION"

gcloud iam service-accounts create dashboard-runner \
  --display-name="Social Analytics Dashboard"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:dashboard-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:dashboard-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

## 3. Store the OAuth client secret and Flask secret key

Never bake either into the image or commit them:

```bash
echo -n "YOUR_GOOGLE_OAUTH_CLIENT_SECRET" | \
  gcloud secrets create google-client-secret --data-file=-

python3 -c "import secrets; print(secrets.token_hex(32))" | \
  gcloud secrets create flask-secret-key --data-file=-

for secret in google-client-secret flask-secret-key; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:dashboard-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 4. Build and push the image

The Dockerfile lives in `webapp/` but the build needs the **repo root**
as its context (to bundle `shared/` alongside the app), so
`gcloud builds submit --tag` (which only looks for a Dockerfile at the
source root) doesn't apply directly -- build and push with plain
`docker`, from the repo root:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -f webapp/Dockerfile \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/dashboard:latest" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/dashboard:latest"
```

## 5. Deploy

Replace `you@gmail.com` with the Google account(s) that should be allowed
to log in (comma-separated for more than one):

```bash
gcloud run deploy social-analytics-dashboard \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/dashboard:latest" \
  --region="$REGION" \
  --service-account="dashboard-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --set-env-vars="BQ_PROJECT_ID=${PROJECT_ID},GOOGLE_CLIENT_ID=YOUR_CLIENT_ID,ALLOWED_EMAILS=you@gmail.com" \
  --set-secrets="GOOGLE_CLIENT_SECRET=google-client-secret:latest,FLASK_SECRET_KEY=flask-secret-key:latest"
```

`--allow-unauthenticated` looks alarming but is correct here: it means
Cloud Run itself doesn't block requests at the network level -- the
app's own login screen (Step 1) is what actually gates access, checked
against `ALLOWED_EMAILS`. Nobody gets past that screen without being on
the list, regardless of this flag.

The command prints a `Service URL`. Go back to Step 1 and add
`<that URL>/auth/callback` as an authorized redirect URI, then reload
the app -- sign-in will work from then on.

## Updating after a code change

Repeat steps 4-5 (same commands) -- Cloud Run deploys the new image as
soon as `gcloud run deploy` finishes, with no downtime.

## Costs

Cloud Run bills per request/compute time and scales to zero when nobody's
using it -- for a single-user dashboard checked a few times a day, this
is typically a few dollars a month at most, often within the free tier.
