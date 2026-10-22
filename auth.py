"""
Google OAuth & Email Verification for Billora Cloud Billing.

Environment Variables (set on Render):
  GOOGLE_CLIENT_ID       - OAuth 2.0 client ID from Google Cloud Console
  GOOGLE_CLIENT_SECRET   - OAuth 2.0 client secret
  MAIL_SENDER_EMAIL      - Gmail address for sending verification emails
  MAIL_SENDER_PASSWORD   - Gmail app password (NOT your Google account password)
  APP_BASE_URL           - e.g. https://dheena-billing.onrender.com
"""

import os
import json
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, redirect, request, session, url_for, render_template, jsonify
from itsdangerous import URLSafeTimedSerializer

from paths import APP_DIR

DATA_DIR = os.path.join(APP_DIR, "data")

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------
auth_bp = Blueprint("auth", __name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _get_env(key, default=""):
    return os.environ.get(key, default)


def _get_serializer():
    secret = os.environ.get("SECRET_KEY", "billora-secret-key-change-me")
    return URLSafeTimedSerializer(secret)


def get_google_config():
    client_id = _get_env("GOOGLE_CLIENT_ID")
    client_secret = _get_env("GOOGLE_CLIENT_SECRET")

    if not client_id:
        cfg_file = os.path.join(DATA_DIR, "google_auth.json")
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                    client_id = cfg_data.get("client_id", "")
                    client_secret = cfg_data.get("client_secret", "")
            except Exception:
                pass

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "userinfo_uri": "https://www.googleapis.com/oauth2/v3/userinfo",
    }


def _base_url():
    """Return the application's public URL."""
    return _get_env("APP_BASE_URL", request.host_url.rstrip("/"))


# ---------------------------------------------------------------------------
# Email sending (Gmail SMTP)
# ---------------------------------------------------------------------------

def send_verification_email(to_email, token, store_name="your store"):
    """Send a verification email with a unique token link."""
    sender = _get_env("MAIL_SENDER_EMAIL")
    password = _get_env("MAIL_SENDER_PASSWORD")

    if not sender or not password:
        smtp_file = os.path.join(DATA_DIR, "smtp.json")
        if os.path.exists(smtp_file):
            try:
                with open(smtp_file, "r", encoding="utf-8") as f:
                    smtp_data = json.load(f)
                    sender = smtp_data.get("sender_email", "")
                    password = smtp_data.get("sender_password", "")
            except Exception:
                pass

    if not sender or not password:
        # SMTP not configured – skip verification in development
        print(f"[Billora] SMTP not configured. Verification link for {to_email}: /verify-email/{token}")
        return False

    base = _base_url()
    verify_url = f"{base}/verify-email/{token}"

    subject = f"Billora - Verify your email for {store_name}"

    html_body = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 32px 24px; background: #ffffff; border-radius: 16px; border: 1px solid #e5e7eb;">
        <div style="text-align: center; margin-bottom: 24px;">
            <h1 style="font-size: 28px; font-weight: 900; color: #111827; margin: 0 0 4px;">Billora</h1>
            <p style="color: #6b7280; font-size: 13px; margin: 0;">Smart Billing for Growing Businesses</p>
        </div>

        <p style="font-size: 15px; color: #374151;">Hi there!</p>
        <p style="font-size: 14px; color: #374151; line-height: 1.7;">
            Thank you for registering <strong>{store_name}</strong> on Billora.
            Please click the button below to verify your email and activate your account.
        </p>

        <div style="text-align: center; margin: 28px 0;">
            <a href="{verify_url}" style="display: inline-block; background: #2563eb; color: #ffffff; padding: 14px 36px; border-radius: 10px; text-decoration: none; font-weight: 700; font-size: 15px; box-shadow: 0 4px 12px rgba(37,99,235,0.3);">
                Verify My Email
            </a>
        </div>

        <p style="font-size: 12px; color: #9ca3af; line-height: 1.6;">
            Or copy and paste this link: <br>
            <a href="{verify_url}" style="color: #2563eb; word-break: break-all;">{verify_url}</a>
        </p>

        <p style="font-size: 12px; color: #9ca3af; margin-top: 20px;">
            This link expires in 24 hours. If you did not register on Billora, please ignore this email.
        </p>

        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
        <p style="font-size: 11px; color: #9ca3af; text-align: center;">
            Billora Cloud Billing - Smart Billing for Growing Businesses
        </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Billora <{sender}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(sender, password)
            server.sendmail(sender, to_email, msg.as_string())
        print(f"[Billora] Verification email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[Billora] Email send error: {e}")
        return False


# ---------------------------------------------------------------------------
# Verification token helpers
# ---------------------------------------------------------------------------

def generate_verification_token(email):
    """Create a signed token embedding the email."""
    s = _get_serializer()
    return s.dumps(email, salt="email-verify")


def confirm_verification_token(token, max_age=86400):
    """Decode the token. Returns email if valid, None if expired/invalid."""
    s = _get_serializer()
    try:
        email = s.loads(token, salt="email-verify", max_age=max_age)
        return email
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Google OAuth routes
# ---------------------------------------------------------------------------

@auth_bp.route("/auth/google/status")
def google_status():
    cfg = get_google_config()
    return jsonify({
        "configured": bool(cfg["client_id"] and cfg["client_secret"]),
        "client_id": cfg["client_id"] or ""
    })


@auth_bp.route("/auth/google")
def google_login():
    """Redirect user to Google's consent screen."""
    cfg = get_google_config()
    action = request.args.get("action", "login")

    if not cfg["client_id"]:
        # When not configured, redirect with google_prompt=1 so the page opens the Google modal
        if action == "register":
            return redirect("/register?google_prompt=1")
        return redirect("/login?google_prompt=1")

    session["oauth_action"] = action
    callback = f"{_base_url()}/auth/google/callback"
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": callback,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{cfg['auth_uri']}?{qs}")


@auth_bp.route("/auth/google/callback")
def google_callback():
    """Handle the OAuth callback from Google."""
    import requests as http_requests

    cfg = get_google_config()
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or state != session.pop("oauth_state", None):
        return redirect("/login?error=oauth_failed")

    callback = f"{_base_url()}/auth/google/callback"

    # Exchange code for tokens
    try:
        token_resp = http_requests.post(cfg["token_uri"], data={
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": callback,
            "grant_type": "authorization_code",
        }, timeout=10)
        tokens = token_resp.json()
    except Exception:
        return redirect("/login?error=oauth_failed")

    if "access_token" not in tokens:
        return redirect("/login?error=oauth_failed")

    # Get user info
    try:
        userinfo_resp = http_requests.get(cfg["userinfo_uri"], headers={
            "Authorization": f"Bearer {tokens['access_token']}"
        }, timeout=10)
        userinfo = userinfo_resp.json()
    except Exception:
        return redirect("/login?error=oauth_failed")

    google_email = userinfo.get("email", "")
    google_name = userinfo.get("name", "")
    email_verified_by_google = userinfo.get("email_verified", False)

    if not google_email:
        return redirect("/login?error=no_email")

    action = session.pop("oauth_action", "login")

    # Check if user already exists
    from database import get_connection

    conn = get_connection()
    existing_user = conn.execute(
        "SELECT u.*, s.store_code, s.store_name, s.business_category FROM users u JOIN stores s ON u.store_id = s.id WHERE u.email = ?",
        (google_email,)
    ).fetchone()
    conn.close()

    if existing_user:
        # User exists - log them in
        import time
        session.clear()
        session["logged_in"] = True
        session["user_id"] = existing_user["id"]
        session["store_id"] = existing_user["store_id"]
        session["username"] = existing_user["username"]
        session["owner_name"] = existing_user["owner_name"]
        session["store_code"] = existing_user["store_code"]
        session["store_name"] = existing_user["store_name"]
        session["business_category"] = existing_user["business_category"] or ""
        session["role"] = existing_user["role"] or "admin"
        session["last_active"] = int(time.time())
        session["email_verified"] = True  # Google accounts are verified

        if session["role"] == "staff":
            return redirect("/billing")
        return redirect("/dashboard")
    else:
        if action == "login":
            return redirect("/login?error=no_account")

        # New user - go to register with pre-filled Google info
        session["google_email"] = google_email
        session["google_name"] = google_name
        session["google_verified"] = email_verified_by_google
        return redirect("/register?from_google=1")


# ---------------------------------------------------------------------------
# Email verification route
# ---------------------------------------------------------------------------

@auth_bp.route("/verify-email/<token>")
def verify_email(token):
    """Verify the email from the link sent to the user."""
    email = confirm_verification_token(token)

    if email is None:
        return render_template("login.html",
                               error="Verification link is invalid or has expired. Please register again.")

    from database import get_connection

    conn = get_connection()
    # Mark user as verified
    conn.execute("UPDATE users SET email_verified = 1 WHERE email = ?", (email,))
    conn.commit()

    # Auto-login after verification
    user = conn.execute(
        "SELECT u.*, s.store_code, s.store_name, s.business_category FROM users u JOIN stores s ON u.store_id = s.id WHERE u.email = ?",
        (email,)
    ).fetchone()
    conn.close()

    if user:
        import time
        session.clear()
        session["logged_in"] = True
        session["user_id"] = user["id"]
        session["store_id"] = user["store_id"]
        session["username"] = user["username"]
        session["owner_name"] = user["owner_name"]
        session["store_code"] = user["store_code"]
        session["store_name"] = user["store_name"]
        session["business_category"] = user["business_category"] or ""
        session["role"] = user["role"] or "admin"
        session["email_verified"] = True
        session["last_active"] = int(time.time())
        return redirect("/dashboard")

    return render_template("login.html",
                           error="Email verified successfully! Please login to continue.")


@auth_bp.route("/resend-verification")
def resend_verification():
    email = request.args.get("email", "").strip()
    if not email:
        return redirect("/login?error=no_email")

    from database import get_connection
    conn = get_connection()
    user = conn.execute("SELECT u.email, s.store_name FROM users u JOIN stores s ON s.id = u.store_id WHERE u.email = ? AND u.email_verified = 0", (email,)).fetchone()
    conn.close()

    if not user:
        return render_template("login.html", error="No pending email verification found for this address. Please login.")

    token = generate_verification_token(email)
    send_verification_email(email, token, user["store_name"])
    return render_template("verify_sent.html", email=email, token=token)

