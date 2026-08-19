# Authentication setup

Getting a working, long-lived access token is almost always the hardest
part of this project. Follow these in order.

## Prerequisites

- Your Instagram account must be a **Business** or **Creator** account
  (not Personal). Check: Instagram app -> Settings -> Account type.
- It must be **linked to a Facebook Page** you (or your business) control.
  Instagram app -> Settings -> Sharing to other apps -> Connected accounts
  -> Facebook, or via the Facebook Page's Settings -> Linked Accounts.

## 1. Create a Meta App

1. Go to https://developers.facebook.com/apps and click **Create App**.
2. Choose type **Business**.
3. Once created, on the app dashboard, click **Add Product** and add
   **Instagram Graph API** (it may appear under "Instagram" -- Meta has
   reorganized this UI more than once; search "Instagram" in Add Product
   if it isn't listed directly).

## 2. Create a System User with the right permissions

Using a **System User** token (not a personal user token) is what makes
this reliable for an unattended pipeline -- it doesn't expire after 60
days the way a normal user token does.

1. Go to https://business.facebook.com/settings -> **Users -> System
   Users**.
2. Click **Add**, name it something like `ig-analytics-pipeline`, role
   **Admin** (or Employee, if you'll scope asset access tightly below).
3. Click **Add Assets** on the new System User and assign:
   - The **Facebook Page** linked to your Instagram account (Full control)
   - The **Instagram account** itself, if it's listed as a separate asset
4. Click **Generate New Token** on the System User:
   - Select the app you created in Step 1
   - Select permissions: `instagram_basic`, `instagram_manage_insights`,
     `pages_show_list`, `pages_read_engagement`, `business_management`
   - Set expiration to **Never** if given the option
5. Copy the token immediately -- Meta only shows it once. This is your
   `META_ACCESS_TOKEN`.

If your app is still in **Development mode**, tokens for System Users
your business controls work fine for testing. To run this against
Instagram accounts outside your own Business Manager, you'd need App
Review for these permissions -- not needed for pulling your own account's
data.

## 3. Find your Instagram Business Account ID

Run this with your new token (replace `<TOKEN>`):

```bash
curl -s "https://graph.facebook.com/v21.0/me/accounts?access_token=<TOKEN>"
```

This lists the Facebook Pages the System User can access. Take the `id`
of your Page, then:

```bash
curl -s "https://graph.facebook.com/v21.0/<PAGE_ID>?fields=instagram_business_account&access_token=<TOKEN>"
```

The `instagram_business_account.id` in the response is your `IG_USER_ID`.

## 4. Verify access end-to-end

```bash
curl -s "https://graph.facebook.com/v21.0/<IG_USER_ID>?fields=id,username,name&access_token=<TOKEN>"
```

You should get back your username. If you get an error, see
**Troubleshooting** below before moving on.

## 5. Fill in `.env`

```bash
cp .env.example .env
```

Fill in `META_ACCESS_TOKEN`, `IG_USER_ID`, `BQ_PROJECT_ID` (a GCP project
with the BigQuery API enabled and billing set up), and `BQ_DATASET`
(defaults to `instagram_analytics` -- created automatically on first run).

You'll also need to authenticate the BigQuery client itself, separate from
the Instagram token:

```bash
gcloud auth application-default login
```

(For the Cloud Run Job deployment, this isn't needed -- the job's service
account handles BigQuery auth. See `deploy/README.md`.)

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `code 190` (OAuthException) | Token invalid, expired, or revoked | Regenerate the System User token (Step 2) |
| `code 10` / "does not have permission" | Missing scope, or the System User isn't assigned the Page/IG asset | Re-check Step 2's asset assignment and permission list |
| `code 100`, "Unsupported get request" on a specific field/metric | That field/metric doesn't apply to this media type or API version | Expected for some posts -- the pipeline logs it per Post_ID and continues |
| `(#4) Application request limit reached` | Rate limited | The pipeline retries automatically with backoff; if it persists, you're running too many manual test calls concurrently |
| Empty `instagram_business_account` in Step 3 | Page isn't linked to an IG professional account | Re-check the linking step in Prerequisites |
