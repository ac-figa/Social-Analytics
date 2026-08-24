"""
Google sign-in, restricted to an explicit email allowlist -- built into
the app itself rather than Google Cloud's Identity-Aware Proxy, since IAP
requires a Load Balancer + static IP + managed SSL cert (real
infrastructure to stand up and pay for) for what's ultimately a
single-user app. This gets the same guarantee -- only your Google
account can get in -- as a plain OAuth login screen instead.

Session is a signed cookie (Flask's default) -- no server-side session
store needed, which matters on Cloud Run since a scale-to-zero service
can hand different requests to different container instances.
"""
from functools import wraps

from authlib.integrations.flask_client import OAuth
from flask import redirect, session, url_for

from . import config

oauth = OAuth()
google = oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def is_logged_in() -> bool:
    return bool(session.get("user_email"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def handle_callback():
    """Called from the /auth/callback route. Returns the verified email on
    success, or None if the token was invalid or the email isn't on the
    allowlist (session is left unset either way for the caller to redirect
    somewhere safe)."""
    token = google.authorize_access_token()
    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email")

    if not email or not userinfo.get("email_verified"):
        return None
    if email.lower() not in config.ALLOWED_EMAILS:
        return None

    session["user_email"] = email
    return email


def logout():
    session.pop("user_email", None)
