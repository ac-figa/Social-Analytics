# Authentication setup

TikTok is the most involved of the four platforms -- no permanent
System User token like Meta, no simple API key like YouTube. Every
third-party app on TikTok, without exception, requires the actual
account owner to log in through a real OAuth consent screen. This doc
captures the full path, including several non-obvious gotchas hit while
setting this up (Aug 2026).

## Why this is different from the other pipelines

- **Login Kit** (OAuth) and **Display API** (`video.list`, the actual
  data endpoint) are not alternatives -- Login Kit is the login step
  *for* the Display API. There is no way to call `video.list` without
  first sending the account owner through Login Kit's OAuth flow.
- The resulting token is short-lived (24h) and must be refreshed using a
  refresh token, which TikTok may itself rotate on every refresh. See
  "Ongoing token refresh" below -- this is handled automatically by the
  pipeline, but it's worth understanding why `.env`'s
  `TIKTOK_REFRESH_TOKEN` value will change over time on its own.

## 1. Create a developer account and app

1. Sign up at `developers.tiktok.com/signup`.
2. **Manage apps -> Connect an app**. Fill in basic info (name, category,
   description, icon).
3. This portal validates the *entire* app form together -- you can't save
   just a redirect URI without also having a real app icon (1024x1024),
   category, description (<=120 chars), Terms of Service URL, Privacy
   Policy URL, and a "Web/Desktop URL". All of these need to exist (even
   as simple static pages) before the form will save at all.

## 2. Add Login Kit and a redirect URI

1. **Products -> Add products -> Login Kit**.
2. Under Login Kit, turn on **"Configure for Web"** (redirect URIs are
   configured per-platform; the field to enter one doesn't appear until
   this is on).
3. Add your redirect URI. TikTok requires it to be:
   - **HTTPS only** -- `localhost` is not supported at all, unlike most
     other OAuth providers.
   - On a domain you can verify ownership of.
   - Static, no query parameters, no `#` fragments.
4. Under Scopes, add `user.info.basic` and `video.list`.

If you don't have a web app to host this redirect URI on, a single
static HTML page works fine -- see "The redirect page" below.

## 3. Verify domain ownership

TikTok will ask you to verify the domain your redirect URI lives on
(**Domain** property type, via a DNS TXT record, is easier than **URL
prefix** verification -- it covers the whole domain, not just one path).
Add the TXT record TikTok gives you (`tiktok-developers-site-
verification=...`) at your domain's DNS provider (Name/Host: `@`), then
click Verify. Can take anywhere from a few minutes to a couple hours to
propagate.

## 4. The redirect page

The redirect URI just needs to display the `code` query parameter TikTok
puts there after login, so you can copy it. A minimal static page with
this script works on any host (WordPress, GitHub Pages, anything):

```html
<script>
(function() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');
  const error = params.get('error');
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed; top:0; left:0; width:100%; min-height:100%; background:#ffffff; color:#000000; z-index:999999; padding:40px; font-family:monospace; font-size:18px; word-break:break-all; box-sizing:border-box;';
  if (error) {
    overlay.innerHTML = '<strong style="color:red;">Error: ' + error + '</strong>';
  } else if (code) {
    overlay.innerHTML = '<strong>Copy this code:</strong><br><br><div style="background:#eee; padding:15px; border:2px solid #333; user-select:all;">' + code + '</div>';
  } else {
    overlay.innerHTML = 'No authorization code found in URL.';
  }
  document.body.appendChild(overlay);
})();
</script>
```

The full-screen overlay (built and appended via JS rather than relying on
page styling) matters -- a plain styled `<div>` can get visually buried
by CMS theme CSS.

## 5. Test before submitting for review: Sandbox mode

TikTok's full review requires a demo video showing the integration
*actually working*, which creates a chicken-and-egg problem: the app
won't save without the demo video, but you need working credentials to
produce the demo video. The way out is **Sandbox mode**:

1. On your app page, toggle from **Production** to **Sandbox**.
2. **Create Sandbox**, choosing to clone your existing configuration.
3. **Important**: the sandbox gets its **own separate Client Key and
   Client Secret**, different from your production app's. Use the
   sandbox credentials for testing, not the production ones -- using the
   wrong pair produces a generic, unhelpful "client_key" error at login.
4. **Sandbox settings -> Target users -> Add account**, log in with the
   account you want to pull data for, accept the Developer Terms. This
   can take **up to an hour** to actually activate.
5. Once active, run the OAuth flow (Step 6 below) using the sandbox
   Client Key/Secret to get a real access token and prove `video.list`
   works -- screen-record this for the review's demo video.
6. Switch back to **Production**, make sure `video.list` is added there
   too (scopes aren't shared between Production and Sandbox), upload the
   demo video, fill in the review explanation, and **Submit for review**.

Sandbox access itself is legitimate for your own ongoing use (you're the
account owner acting as your own target user) -- this pipeline runs on
sandbox credentials until the Production app is approved, at which point
you'd redo the one-time login (Step 6) with the production Client
Key/Secret to get a production-scoped token instead.

## 6. One-time OAuth login to get a refresh token

This is the only manual step, and only needs doing once (per credential
set -- sandbox now, production later once approved):

1. Build the authorization URL:
   ```
   https://www.tiktok.com/v2/auth/authorize/?client_key=<CLIENT_KEY>&response_type=code&scope=user.info.basic,video.list&redirect_uri=<URL-ENCODED_REDIRECT_URI>&state=<ANY_RANDOM_STRING>
   ```
2. Open it, log in, approve.
3. Copy the `code` shown on your redirect page (single-use, expires
   quickly -- use it right away).
4. Exchange it for tokens:
   ```bash
   curl -s -X POST "https://open.tiktokapis.com/v2/oauth/token/" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "client_key=<CLIENT_KEY>" \
     -d "client_secret=<CLIENT_SECRET>" \
     -d "code=<CODE>" \
     -d "grant_type=authorization_code" \
     --data-urlencode "redirect_uri=<REDIRECT_URI>"
   ```
5. The response's `refresh_token` is what goes in `.env` as
   `TIKTOK_REFRESH_TOKEN` (valid ~1 year). The `access_token` itself
   (valid 24h) is not needed in `.env` -- the pipeline re-derives one
   from the refresh token on every run.

## 7. Fill in `.env`

```bash
cp .env.example .env
```

Fill in `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN`
(from Step 6), `TIKTOK_USERNAME` (your `@handle`, no `@` -- not returned
by the `user.info.basic` scope, so it's configured directly rather than
fetched), `BQ_PROJECT_ID`, and `BQ_DATASET`.

## Ongoing token refresh

Every pipeline run calls `TikTokClient.authenticate()` first, which
exchanges the current refresh token for a fresh access token. TikTok may
return a *new* refresh token in that response -- when it does,
`src/config.py`'s `update_refresh_token()` writes it back into `.env`
automatically. Nothing to do by hand across normal runs; only redo the
Step 6 login if the refresh token itself is ever revoked or expires
(after ~1 year of inactivity, or if access is manually revoked in your
TikTok account settings).

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| Generic `client_key` error at login | Using Production client key against Sandbox (or vice versa) | Match the credentials to the mode you're testing in |
| `scope_not_authorized` on `user/info/` | Requested a field (e.g. `username`) not covered by `user.info.basic` | Expected -- that's why `TIKTOK_USERNAME` is configured manually instead |
| `TokenExpiredError` on a normal run | Refresh token itself expired/revoked | Redo the Step 6 one-time login |
| App form won't save | Some required field still missing (icon, category, ToS/Privacy URLs, review explanation, demo video) | Check every section on the app page -- TikTok validates the whole form together |
