"""Dev maintenance helper tests. All service requests are in-memory."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

import penfield_auth as auth
from penfield_import import APIError
from tools import reset_dev_content as reset


class DevAPI:
    def __init__(self):
        self.identity = [reset.DEV_ORIGIN, "test-tenant", "test-user"]
        self.records = {
            "relationships": {"rel1"}, "memories": {"m1"}, "documents": {"doc1"},
            "artifacts": {"/top.txt", "/old/nested/note.txt"},
        }
        self.calls = []
        self.fail = {}
        self.unchanged = set()

    def destination_identity(self):
        return json.dumps(self.identity)

    def request(self, method, route):
        self.calls.append((method, route))
        if (method, route) in self.fail:
            raise APIError(self.fail[method, route], "stub", route)
        u = urlsplit(route)
        params = parse_qs(u.query)
        section = u.path.split("/")[1]
        if method == "DELETE":
            key = params["path"][0] if section == "artifacts" else u.path.split("/")[2]
            if (section, key) not in self.unchanged:
                self.records[section].remove(key)
            return {}
        assert method == "GET"
        if "path" in params or (u.path != "/artifacts/list" and u.path.count("/") == 2):
            key = params["path"][0] if section == "artifacts" else u.path.split("/")[2]
            if key not in self.records[section]:
                raise APIError(404, "missing", route)
            return {"data": {"id": key}}
        if section == "artifacts":
            prefix = params["prefix"][0]
            folders, files = [], []
            for path in self.records["artifacts"]:
                if path.startswith(prefix):
                    child = path[len(prefix):]
                    if "/" in child:
                        folders.extend([child.split("/", 1)[0]] * 2)
                    else:
                        files.append({"name": child})
            return {"data": {"folders": folders, "files": files, "path": prefix}}
        if section == "memories":
            assert params["include_inactive"] == ["true"]
        page = int(params["page"][0])
        records = sorted(self.records[section])
        return {"data": {"items": [{"id": k} for k in records[(page-1)*100:page*100]],
                         "pagination": {"total": len(records), "has_next": page*100 < len(records), "page": page}}}


def test_preview_walks_pages_and_duplicate_deep_folders_once():
    api = DevAPI()
    api.records["memories"] = {f"m{i}" for i in range(108)}
    api.records["artifacts"].add("/" + "folder/" * 16 + "deep.txt")
    result = reset.reset_dev_content(api, "test-tenant")
    assert result["before"] == {"relationships": 1, "memories": 108, "documents": 1, "artifacts": 3}
    assert all(method == "GET" for method, _ in api.calls)
    assert len(api.calls) == len(set(api.calls))
    assert any("page=2" in path for _, path in api.calls)


@pytest.mark.parametrize("identity", [
    ["https://api.penfield.app", "test-tenant", "test-user"],
    [reset.DEV_ORIGIN, "other-tenant", "test-user"],
])
def test_wrong_destination_stops_before_inventory(identity):
    api = DevAPI()
    api.identity = identity
    with pytest.raises(ValueError, match="does not match"):
        reset.reset_dev_content(api, "test-tenant", apply=True)
    assert api.calls == []


def test_apply_rechecks_every_inventory():
    api = DevAPI()
    result = reset.reset_dev_content(api, "test-tenant", apply=True)
    assert result["verified_empty"] and all(v == 0 for v in result["remaining"].values())
    assert result["confirmed_404"] == result["before"]
    assert sum(method == "DELETE" for method, _ in api.calls) == 5
    assert all(not entries for entries in api.records.values())


def test_delete_error_and_unacknowledged_remaining_record():
    api = DevAPI()
    api.fail["DELETE", "/documents/doc1"] = 500
    api.unchanged.add(("memories", "m1"))
    result = reset.reset_dev_content(api, "test-tenant", apply=True)
    assert not result["verified_empty"]
    assert result["remaining"]["documents"] == result["remaining"]["memories"] == 1
    assert {"operation": "delete", "section": "documents", "key": "doc1", "status": 500} in result["errors"]
    assert result["confirmed_404"] == {"relationships": 1, "documents": 0, "artifacts": 2, "memories": 0}


def test_malformed_folder_response_cannot_trigger_deletion(monkeypatch):
    api = DevAPI()
    original = api.request

    def request(method, route):
        response = original(method, route)
        if route.startswith("/artifacts/list"):
            response["data"]["path"] = "/other"
        return response

    monkeypatch.setattr(api, "request", request)
    with pytest.raises(ValueError, match="Unexpected folder"):
        reset.reset_dev_content(api, "test-tenant", apply=True)
    assert all(method == "GET" for method, _ in api.calls)


def test_permission_failure_stops_deletes_and_reports_remaining():
    api = DevAPI()
    api.fail["DELETE", "/relationships/rel1"] = 403
    result = reset.reset_dev_content(api, "test-tenant", apply=True)
    assert result["remaining"] == result["before"] and not result["verified_empty"]
    assert sum(method == "DELETE" for method, _ in api.calls) == 1


def test_oauth_profile_separates_registration_scope_and_cache(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setattr(auth, "_TOKEN_CACHE_FILE", tmp_path / "import" / "tokens.json")
    monkeypatch.setattr(auth, "_http_post_json", lambda url, data, _: requests.append(data) or {"client_id": "dev"})
    default = auth._profile()
    with auth.oauth_profile("penfield-dev-content-reset", tmp_path / "dev", "read delete offline_access"):
        client_id = auth._get_or_register_client(reset.DEV_ORIGIN, {"registration_endpoint": "https://example.test/reg"})
        assert client_id == "dev"
        assert requests[0]["scope"] == "read delete offline_access"
        assert requests[0]["client_name"] == "penfield-dev-content-reset"
        assert auth._load_tokens(reset.DEV_ORIGIN)["client_id"] == "dev"
    assert auth._profile() == default
    assert auth._load_tokens(reset.DEV_ORIGIN) is None


@pytest.mark.parametrize("route,section", [("/documents/doc1", "documents"), ("/memories/m1", "memories"),
                                          ("/relationships/rel1", "relationships"),
                                          ("/artifacts?path=%2Ftop.txt", "artifacts")])
def test_unlisted_but_retrievable_object_fails_reset(monkeypatch, route, section):
    api = DevAPI()
    original = api.request

    def request(method, path):
        if method == "GET" and path == route:
            return {"data": {"still_present": True}}
        return original(method, path)

    monkeypatch.setattr(api, "request", request)
    result = reset.reset_dev_content(api, "test-tenant", apply=True)
    assert not result["verified_empty"]
    assert all(count == 0 for count in result["remaining"].values())
    assert result["confirmed_404"][section] == result["before"][section] - 1
    assert result["errors"][0]["operation"] == "readback"
    assert result["errors"][0]["status"] == "still retrievable"


def test_readback_server_error_cannot_count_as_absent():
    api = DevAPI()
    api.fail["GET", "/documents/doc1"] = 503
    result = reset.reset_dev_content(api, "test-tenant", apply=True)
    assert not result["verified_empty"] and result["confirmed_404"]["documents"] == 0
    assert result["errors"] == [{"operation": "readback", "section": "documents", "key": "doc1", "status": 503}]
