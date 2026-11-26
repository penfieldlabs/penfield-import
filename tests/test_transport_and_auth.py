"""Destination binding, durable credential storage and readback at the API boundary."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import penfield_auth as auth
import penfield_import as imp
import penfield_io as io


def client():
    return imp.PenfieldClient("https://api.example", access_token="test-token")


def test_environment_host_precedence(monkeypatch):
    monkeypatch.setenv("PENFIELD_BASE_URL", "https://dev.example")
    parser = imp.build_parser()
    assert parser.parse_args([]).base_url == "https://dev.example"
    assert parser.parse_args(["--base-url", "https://other.example"]).base_url == "https://other.example"


def test_refresh_identity_change_stops_subsequent_writes():
    api = client()
    verify = lambda tenant: {"data": {"valid": True, "tenant_id": tenant}}
    api._raw_request = Mock(side_effect=[verify("first"), imp.APIError(401, "", ""), verify("second")])
    api._refresh_access_token = Mock()
    api.destination_identity()
    with pytest.raises(imp.APIError):
        api.request("POST", "/memories", {"content": "test"})
    with pytest.raises(imp.APIError):
        api.request("POST", "/memories", {"content": "test"})
    assert [call.args[0] for call in api._raw_request.call_args_list] == ["GET", "POST", "GET"]


def test_final_rate_limit_preserves_status_and_charges_each_transmission(monkeypatch):
    api = client()
    api._raw_request = Mock(side_effect=imp.APIError(429, "limit", "/", "2"))
    api._rate_limiter.wait = Mock()
    sleep = Mock()
    monkeypatch.setattr(imp.time, "sleep", sleep)
    with pytest.raises(imp.APIError) as err:
        api.request("POST", "/memories", {})
    assert err.value.status_code == 429
    assert api._rate_limiter.wait.call_count == 4
    assert [c.args for c in sleep.call_args_list] == [(2,), (2,), (2,)]


def test_ambiguous_create_is_not_replayed():
    api = client()
    api._raw_request = Mock(side_effect=imp.APIError(503, "private response", "/memories"))
    with pytest.raises(imp.APIError) as err:
        api.create_memory("test")
    assert "private response" not in str(err.value)
    assert api._raw_request.call_count == 1


@pytest.mark.parametrize("old,expected", [("new", True), ("different", False)])
def test_artifact_conflict_requires_equal_bytes(old, expected):
    api = client()
    api.request = Mock(side_effect=[imp.APIError(409, "", "/artifacts"),
                                   {"data": {"path": "/x", "content": old}}])
    if expected:
        assert api.create_artifact("/x", "new")["existing_identical"]
    else:
        with pytest.raises(imp.APIError) as err:
            api.create_artifact("/x", "new")
        assert err.value.status_code == 412


def test_bulk_readback_matches_source_order():
    api = client()
    edge = {"from_id": "a", "to_id": "b", "relationship_type": "supports"}
    other = dict(edge, to_id="c")
    api.request = Mock(side_effect=[
        {"data": {"created": [{"id": "2"}, {"id": "1"}]}},
        {"data": dict(other, id="2")}, {"data": dict(edge, id="1")},
    ])
    assert api.create_relationships_bulk([edge, other])["verified_ids"] == ["1", "2"]


def test_cache_fresh_login_and_stale_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_TOKEN_CACHE_DIR", tmp_path)
    monkeypatch.setattr(auth, "_TOKEN_CACHE_FILE", tmp_path / "tokens.json")
    auth._save_tokens("server", {"access_token": "one", "refresh_token": "old"}, expected=None)
    first = auth._load_tokens("server")
    auth._save_tokens("server", {"access_token": "two"}, expected=first, fresh=True)
    assert "refresh_token" not in auth._load_tokens("server")
    with pytest.raises(auth.AuthError):
        auth._save_tokens("server", {"access_token": "late"}, expected=first)
    assert auth._TOKEN_CACHE_FILE.stat().st_mode & 0o777 == 0o600
    second = auth._load_tokens("server")
    assert auth.clear_tokens("server")
    with pytest.raises(auth.AuthError):
        auth._save_tokens("server", {"access_token": "late"}, expected=second)


def test_cached_token_uses_remaining_lifetime(monkeypatch):
    start = datetime.now(timezone.utc) - timedelta(seconds=1800)
    monkeypatch.setattr(auth, "_load_tokens", lambda _: {
        "access_token": "test", "expires_in": 3600, "saved_at": start.isoformat(),
    })
    assert 1795 < auth.get_valid_token().expires_in <= 1800


def test_redirect_origin_and_discovery(monkeypatch):
    redirect = io._SameOriginRedirect()
    with pytest.raises(HTTPError):
        redirect.redirect_request(Request("https://example.test/path"), None, 302, "", {},
                                  "https://different.example/path")
    monkeypatch.setattr(auth, "_http_get_json", lambda *_: {
        "issuer": "https://auth.penfield.app", "token_endpoint": "https://unrelated.example/token",
    })
    with pytest.raises(auth.AuthError):
        auth._discover_oauth_endpoints("https://api.penfield.app")


def test_lock_contention_and_reacquisition(tmp_path):
    target = tmp_path / "state"
    with io.checkpoint_lock(target):
        with pytest.raises(ValueError):
            with io.checkpoint_lock(target):
                pytest.fail("second writer admitted")
    with io.checkpoint_lock(target):
        io.write_json_atomic(target, {"state": "ok"})
    assert target.read_text() == '{"state":"ok"}'


def test_multipart_uses_wire_bytes_and_independent_download(monkeypatch):
    import json
    from email import policy
    from email.parser import BytesParser
    from io import BytesIO

    api = client()
    blob = bytes(range(256))
    metadata = {"batch": "example"}
    paths = []

    def serve(req, timeout):
        path = req.full_url.split("/api/v2")[1]
        paths.append((req.method if hasattr(req, "method") else req.get_method(), path))
        assert req.get_header("Authorization") == "Bearer test-token"
        if path == "/documents/upload":
            raw = b"Content-Type: " + req.get_header("Content-type").encode() + b"\r\n\r\n" + req.data
            parts = list(BytesParser(policy=policy.default).parsebytes(raw).iter_parts())
            assert parts[0].get_filename() == "original.bin"
            assert parts[0].get_payload(decode=True) == blob
            assert json.loads(parts[1].get_payload(decode=True)) == metadata
            return BytesIO(b'{"data":{"document_id":"doc-1"}}')
        if path == "/documents/doc-1":
            return BytesIO(json.dumps({"data": {
                "id": "doc-1", "filename": "original.bin", "metadata": metadata,
                "processing_status": "pending",
            }}).encode())
        assert path == "/documents/doc-1/download"
        assert req.get_header("User-agent") == f"penfield-import/{imp.__version__}"
        return BytesIO(blob)

    monkeypatch.setattr(imp, "open_request", serve)
    receipt = api.upload_document_bytes("original.bin", blob, metadata=metadata)
    assert receipt["id"] == "doc-1"
    assert receipt["processing_status"] == "pending"
    assert [method for method, _ in paths] == ["POST", "GET", "GET"]


def test_wrong_document_bytes_leave_unknown_outcome(monkeypatch):
    from io import BytesIO
    api = client()
    replies = [
        b'{"data":{"id":"doc-1"}}',
        b'{"data":{"id":"doc-1","filename":"original.txt","metadata":{}}}',
        b"old",
    ]
    monkeypatch.setattr(imp, "open_request", lambda *args, **kwargs: BytesIO(replies.pop(0)))
    with pytest.raises(imp.APIError) as exc:
        api.upload_document_bytes("original.txt", b"new")
    assert exc.value.status_code == 0


def test_null_valued_document_metadata_key_must_survive(monkeypatch):
    from io import BytesIO
    api = client()
    replies = [b'{"data":{"id":"d"}}', b'{"data":{"id":"d","filename":"file.txt","metadata":{}}}']
    monkeypatch.setattr(imp, "open_request", lambda *args, **kwargs: BytesIO(replies.pop(0)))
    with pytest.raises(imp.APIError) as exc:
        api.upload_document_bytes("file.txt", b"data", metadata={"key": None})
    assert exc.value.body == "Document metadata readback mismatch"
    assert not replies


def test_bad_device_grant_preserves_existing_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_TOKEN_CACHE_DIR", tmp_path)
    monkeypatch.setattr(auth, "_TOKEN_CACHE_FILE", tmp_path / "tokens.json")
    auth._save_tokens("server", {"access_token": "prior", "client_id": "client"}, expected=None)
    before = auth._TOKEN_CACHE_FILE.read_bytes()
    monkeypatch.setattr(auth, "_discover_oauth_endpoints", lambda url: {})
    monkeypatch.setattr(auth, "_device_code_auth", lambda *_: {})
    with pytest.raises(auth.AuthError):
        auth.get_valid_token("server", force_reauth=True)
    assert auth._TOKEN_CACHE_FILE.read_bytes() == before


def test_verify_malformed_account_is_handled():
    api = client()
    api._raw_request = Mock(return_value={"data": None})
    with pytest.raises(imp.APIError) as exc:
        api.destination_identity()
    assert exc.value.status_code == 401


def test_unrelated_extra_edge_receipt_stops_restore():
    api = client()
    edge = {"from_id": "a", "to_id": "b", "relationship_type": "supports"}
    api.request = Mock(side_effect=[
        {"data": {"created": [{"id": "x"}, {"id": "y"}]}},
        {"data": dict(edge, id="x")}, {"data": dict(edge, id="y", from_id="other")},
    ])
    with pytest.raises(imp.APIError) as exc:
        api.create_relationships_bulk([edge])
    assert exc.value.status_code == 0


@pytest.mark.parametrize("host", ["https://api-dev.penfield.app", "https://api.penfield.app"])
def test_api_key_exchange_and_verification_stay_on_selected_host(host, monkeypatch):
    from io import BytesIO
    sent = []

    def serve(req, timeout):
        sent.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        if req.full_url.endswith("/auth/token"):
            return BytesIO(b'{"data":{"access_token":"synthetic-access","expires_in":3600}}')
        return BytesIO(b'{"data":{"valid":true,"tenant_id":"synthetic-tenant"}}')

    monkeypatch.setattr(imp, "open_request", serve)
    api = imp.PenfieldClient(host, api_key="synthetic-key")
    api.destination_identity()
    assert sent == [
        ("POST", host + "/api/v2/auth/token", "Bearer synthetic-key"),
        ("GET", host + "/api/v2/auth/verify", "Bearer synthetic-access"),
    ]
