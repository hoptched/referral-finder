"""Send email through the user's Gmail account.

OAuth scopes are the minimum: send mail, plus the account's address (for
"send test to me"). The tool cannot read the mailbox.

credentials.json = the OAuth client the user downloads from Google Cloud.
token.json       = the user's grant, written by `refer gmail-login`.
Both are gitignored; token.json is chmod 600.
"""

import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from referral.config import project_path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
CREDENTIALS_PATH = project_path("credentials.json")
TOKEN_PATH = project_path("token.json")
REDIRECT_URI = "http://localhost:8090/"
PENDING_PATH = project_path(".cache/gmail_login.json")  # state + PKCE verifier between the two login steps


class GmailNotConnected(Exception):
    pass


def _email_from_id_token(id_token: str) -> str | None:
    # The token came straight from Google's token endpoint over TLS; we only read the claim.
    payload = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return claims.get("email")


def _save(creds: Credentials, account: str | None) -> None:
    data = json.loads(creds.to_json())
    data["account_email"] = account
    TOKEN_PATH.write_text(json.dumps(data, indent=2))
    TOKEN_PATH.chmod(0o600)


def _flow(**kwargs) -> Flow:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Put the OAuth client file from Google Cloud at {CREDENTIALS_PATH}")
    return Flow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES, redirect_uri=REDIRECT_URI, **kwargs)


def start_login() -> str:
    """Step 1: return the URL to approve, saving the state needed to finish.

    Nothing listens on REDIRECT_URI: after approving, the browser shows
    "can't be reached", and the user pastes that address into finish_login().
    This avoids keeping a process alive and WSL/Windows localhost quirks.
    """
    flow = _flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    PENDING_PATH.parent.mkdir(exist_ok=True)
    PENDING_PATH.write_text(json.dumps({"state": state, "code_verifier": flow.code_verifier}))
    PENDING_PATH.chmod(0o600)
    return url


def finish_login(redirected_url: str) -> str:
    """Step 2: exchange the code in the pasted localhost URL for a token. Returns the account email."""
    if not PENDING_PATH.exists():
        raise RuntimeError("No login in progress. Run `refer gmail-login` first.")
    pending = json.loads(PENDING_PATH.read_text())
    flow = _flow(state=pending["state"], code_verifier=pending["code_verifier"])
    # oauthlib rejects http:// callback URLs; this one is only parsed locally (the token exchange is https).
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow.fetch_token(authorization_response=redirected_url.strip())
    creds = flow.credentials
    account = _email_from_id_token(creds.id_token) if creds.id_token else None
    _save(creds, account)
    PENDING_PATH.unlink()
    return account


def _load() -> tuple[Credentials, str | None]:
    if not TOKEN_PATH.exists():
        raise GmailNotConnected("Gmail isn't connected. Run `refer gmail-login`.")
    data = json.loads(TOKEN_PATH.read_text())
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Apps in Google's "Testing" mode get refresh tokens that expire after 7 days.
            raise GmailNotConnected(f"Gmail login expired ({e}). Run `refer gmail-login` again.")
        _save(creds, data.get("account_email"))
    return creds, data.get("account_email")


def status() -> dict:
    try:
        _, account = _load()
        return {"connected": True, "account": account, "error": None}
    except GmailNotConnected as e:
        return {"connected": False, "account": None, "error": str(e)}


def account_email() -> str:
    _, account = _load()
    if not account:
        raise GmailNotConnected("Couldn't tell which Gmail account is connected. Run `refer gmail-login` again.")
    return account


def build_message(to: str, subject: str, body: str, attachment: Path, attachment_name: str) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment_name)
    return message


def send(message: EmailMessage) -> str:
    """Send and return Gmail's message id. Blocking; call via asyncio.to_thread."""
    creds, _ = _load()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()["id"]
