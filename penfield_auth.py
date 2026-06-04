"""OAuth 2.0 Device Code authentication for Penfield Import (RFC 8628).

Uses the Device Authorization Grant flow so the user authenticates in
their browser while the CLI polls for a token. Tokens are cached locally
and refreshed automatically when expired.

Mirrors the auth flow from penfield-export, using only stdlib (no httpx).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from penfield_import import __version__
except ImportError:
    __version__ = "2.0.0"

BASE_URL = "https://api.penfield.app"
USER_AGENT = f"penfield-import/{__version__}"

# Import needs read+write+offline_access
_SCOPES = "read write offline_access"

# Token cache
_TOKEN_CACHE_DIR = Path.home() / ".config" / "penfield-import"
_TOKEN_CACHE_FILE = _TOKEN_CACHE_DIR / "tokens.json"

# Polling defaults (overridden by server response)
_DEFAULT_INTERVAL = 5
_SLOW_DOWN_INCREMENT = 5


class AuthError(Exception):
    """Raised when authentication fails."""


@dataclass
class AuthResult:
    """Result of a successful authentication."""
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    """GET JSON from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _build_form_request(url: str, data: dict[str, str]) -> urllib.request.Request:
    """Build a POST request with form-encoded data."""
    encoded = urllib.parse.urlencode(data).encode()
    return urllib.request.Request(
        url, data=encoded,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )


def _http_post_json(url: str, body: dict[str, Any], timeout: float = 30.0) -> Any:
    """POST JSON, return parsed response."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

def _save_tokens(api_url: str, token_data: dict[str, Any]) -> None:
    """Persist tokens to the cache file (owner-only permissions)."""
    _TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    cache = _load_cache()

    entry = cache.get(api_url, {})
    for f in ("access_token", "refresh_token", "client_id"):
        if f in token_data:
            entry[f] = token_data[f]
    if "expires_in" in token_data:
        entry["expires_in"] = token_data["expires_in"]
    if "access_token" in token_data:
        entry["saved_at"] = datetime.now(timezone.utc).isoformat()

    cache[api_url] = entry

    _TOKEN_CACHE_FILE.write_text(
        json.dumps(cache, indent=2), encoding="utf-8"
    )
    _TOKEN_CACHE_FILE.chmod(0o600)


def _load_cache() -> dict[str, Any]:
    """Load the full token cache file."""
    if not _TOKEN_CACHE_FILE.exists():
        return {}
    try:
        result: dict[str, Any] = json.loads(
            _TOKEN_CACHE_FILE.read_text(encoding="utf-8")
        )
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _load_tokens(api_url: str) -> dict[str, Any] | None:
    """Load cached tokens for a given API URL."""
    cache = _load_cache()
    return cache.get(api_url)


def _is_token_expired(token_data: dict[str, Any]) -> bool:
    """Check whether the cached access token has expired (5-min buffer)."""
    saved_at = datetime.fromisoformat(
        token_data.get("saved_at", "2000-01-01")
    )
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)
    expires_in = token_data.get("expires_in", 0)
    expiry = saved_at + timedelta(seconds=expires_in - 300)
    return datetime.now(timezone.utc) > expiry


def clear_tokens(api_url: str | None = None) -> bool:
    """Remove cached tokens. Returns True if anything was removed."""
    if not _TOKEN_CACHE_FILE.exists():
        return False

    if api_url is None:
        _TOKEN_CACHE_FILE.unlink()
        return True

    try:
        cache = json.loads(_TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if api_url not in cache:
        return False

    del cache[api_url]
    _TOKEN_CACHE_FILE.write_text(
        json.dumps(cache, indent=2), encoding="utf-8"
    )
    _TOKEN_CACHE_FILE.chmod(0o600)
    return True


def token_status(api_url: str) -> dict[str, Any] | None:
    """Return cache status for display, or None if no cached token."""
    cached = _load_tokens(api_url)
    if cached is None:
        return None
    return {
        "saved_at": cached.get("saved_at", "unknown"),
        "expires_in": cached.get("expires_in", 0),
        "expired": _is_token_expired(cached),
        "has_refresh_token": bool(cached.get("refresh_token")),
        "has_client_id": bool(cached.get("client_id")),
        "cache_file": str(_TOKEN_CACHE_FILE),
    }


# ---------------------------------------------------------------------------
# OAuth endpoint discovery (RFC 8414)
# ---------------------------------------------------------------------------

def _discover_oauth_endpoints(
    api_url: str, timeout: float = 10.0
) -> dict[str, str]:
    """Discover OAuth endpoints from well-known metadata."""
    url = f"{api_url.rstrip('/')}/.well-known/oauth-authorization-server"
    try:
        return _http_get_json(url, timeout)
    except urllib.error.HTTPError as exc:
        raise AuthError(
            f"OAuth discovery failed (HTTP {exc.code}). "
            f"Is {api_url} a valid Penfield API URL?"
        ) from exc
    except urllib.error.URLError as exc:
        raise AuthError(
            f"Network error during OAuth discovery: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------

def _get_or_register_client(
    api_url: str,
    discovery: dict[str, str],
    timeout: float = 30.0,
) -> str:
    """Get a cached client_id or register a new one via DCR."""
    cached = _load_tokens(api_url)
    if cached and cached.get("client_id"):
        return cached["client_id"]

    registration_endpoint = discovery.get("registration_endpoint")
    if not registration_endpoint:
        raise AuthError(
            "Dynamic Client Registration not supported: "
            "registration_endpoint not advertised in OAuth discovery"
        )

    try:
        client_data = _http_post_json(registration_endpoint, {
            "client_name": "penfield-import",
            "redirect_uris": ["http://localhost:8080/callback"],
            "grant_types": [
                "urn:ietf:params:oauth:grant-type:device_code",
                "refresh_token",
            ],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": _SCOPES,
        }, timeout)
    except urllib.error.HTTPError as exc:
        raise AuthError(
            f"Client registration failed (HTTP {exc.code})"
        ) from exc
    except urllib.error.URLError as exc:
        raise AuthError(
            f"Network error during client registration: {exc}"
        ) from exc

    new_client_id: str = client_data["client_id"]
    _save_tokens(api_url, {"client_id": new_client_id})
    return new_client_id


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def _refresh_access_token(
    discovery: dict[str, str],
    refresh_token: str,
    client_id: str,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Exchange a refresh token for a new access token."""
    token_url = discovery["token_endpoint"]
    req = _build_form_request(token_url, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass
    return None


def refresh_oauth_token(api_url: str = BASE_URL) -> Optional[AuthResult]:
    """Refresh an OAuth token using the cached refresh_token.

    Called by PenfieldClient when an OAuth access token expires.
    Returns a new AuthResult on success, None on failure.
    """
    cached = _load_tokens(api_url)
    if not cached:
        return None

    refresh = cached.get("refresh_token")
    client_id = cached.get("client_id")
    if not refresh or not client_id:
        return None

    try:
        discovery = _discover_oauth_endpoints(api_url)
    except AuthError:
        return None

    new_tokens = _refresh_access_token(discovery, refresh, client_id)
    if not new_tokens:
        return None

    new_tokens["client_id"] = client_id
    _save_tokens(api_url, new_tokens)
    return AuthResult(
        access_token=new_tokens["access_token"],
        refresh_token=new_tokens.get("refresh_token"),
        expires_in=new_tokens.get("expires_in"),
    )


# ---------------------------------------------------------------------------
# Device Code flow (RFC 8628)
# ---------------------------------------------------------------------------

def _device_code_auth(
    discovery: dict[str, str],
    client_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Perform the full RFC 8628 Device Authorization Grant.

    Prompts the user to authorise in their browser, then polls
    the token endpoint until the grant is approved or expires.
    """
    device_auth_url = discovery.get("device_authorization_endpoint")
    if not device_auth_url:
        raise AuthError(
            "Device Authorization Grant not supported: "
            "device_authorization_endpoint not advertised in OAuth discovery"
        )

    token_url = discovery["token_endpoint"]

    # Step 1: request a device code
    req = _build_form_request(device_auth_url, {
        "client_id": client_id,
        "scope": _SCOPES,
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            device_data: dict[str, Any] = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise AuthError(
            f"Device authorization request failed (HTTP {exc.code})"
        ) from exc
    except urllib.error.URLError as exc:
        raise AuthError(
            f"Network error during device authorization: {exc}"
        ) from exc

    # Validate required fields (RFC 8628 Section 3.2)
    required_fields = ("user_code", "verification_uri", "device_code", "expires_in")
    missing = [f for f in required_fields if f not in device_data]
    if missing:
        raise AuthError(
            f"Device authorization response missing required fields: {', '.join(missing)}"
        )

    user_code: str = device_data["user_code"]
    verification_uri: str = device_data["verification_uri"]
    verification_uri_complete: str = device_data.get(
        "verification_uri_complete", verification_uri
    )
    expires_in: int = device_data["expires_in"]
    interval: int = device_data.get("interval", _DEFAULT_INTERVAL)
    device_code: str = device_data["device_code"]

    # Step 2: prompt user
    print(f"\n  Open:  {verification_uri}")
    print(f"  Code:  {user_code}\n")
    if verification_uri_complete != verification_uri:
        print(f"  Or visit: {verification_uri_complete}\n")
    print(f"  Code expires in {expires_in} seconds.\n")

    # Step 3: poll for token
    start = time.monotonic()

    while time.monotonic() - start < expires_in:
        time.sleep(interval)

        poll_req = _build_form_request(token_url, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        })

        try:
            with urllib.request.urlopen(poll_req, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode())
            except (json.JSONDecodeError, ValueError):
                continue

            error = error_body.get("error", "")

            if error == "authorization_pending":
                elapsed = int(time.monotonic() - start)
                print(
                    f"\r  Waiting for authorization... ({elapsed}s)",
                    end="", flush=True,
                )
                continue
            elif error == "slow_down":
                interval += _SLOW_DOWN_INCREMENT
                continue
            elif error == "access_denied":
                print()
                raise AuthError("Authorization denied by user.")
            elif error == "expired_token":
                print()
                raise AuthError("Device code expired. Please try again.")
            else:
                print()
                raise AuthError(f"Authentication error: {error}")
        except urllib.error.URLError:
            time.sleep(interval)
            continue

    raise AuthError("Authentication timed out.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_valid_token(
    api_url: str = BASE_URL,
    force_reauth: bool = False,
) -> AuthResult:
    """Obtain a valid access token, using cache -> refresh -> device flow.

    Priority:
    1. Return cached token if still valid.
    2. Use refresh token to obtain a new access token.
    3. Fall back to full device code flow (with DCR if needed).

    Returns an AuthResult with access_token, refresh_token, and expires_in.
    Raises AuthError on failure.
    """
    if not force_reauth:
        cached = _load_tokens(api_url)
        if cached:
            if not _is_token_expired(cached) and cached.get("access_token"):
                return AuthResult(
                    access_token=cached["access_token"],
                    refresh_token=cached.get("refresh_token"),
                    expires_in=cached.get("expires_in"),
                )

            # Attempt refresh
            refresh = cached.get("refresh_token")
            client_id = cached.get("client_id")
            if refresh and client_id:
                discovery = _discover_oauth_endpoints(api_url)
                new_tokens = _refresh_access_token(
                    discovery, refresh, client_id
                )
                if new_tokens:
                    new_tokens["client_id"] = client_id
                    _save_tokens(api_url, new_tokens)
                    return AuthResult(
                        access_token=new_tokens["access_token"],
                        refresh_token=new_tokens.get("refresh_token"),
                        expires_in=new_tokens.get("expires_in"),
                    )

    # Full device code flow
    discovery = _discover_oauth_endpoints(api_url)
    client_id = _get_or_register_client(api_url, discovery)
    token_data = _device_code_auth(discovery, client_id)
    token_data["client_id"] = client_id
    _save_tokens(api_url, token_data)
    return AuthResult(
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data.get("expires_in"),
    )
