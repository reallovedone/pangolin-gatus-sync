"""Fake Pangolin Integration API used by the unit tests and the end-to-end test.

Implements the subset of endpoints pangolin-gatus-sync relies on:
  GET /v1/org/{org}/public-resources?page=&pageSize=   (or the legacy /resources)
  GET /v1/public-resource/{id}                         (or the legacy /resource/{id})
  GET /v1/resource/{id}/targets

Run standalone with: python mock_pangolin.py [port]  (serves the DEMO dataset, key "test-key").
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = "test-key"
ORG = "demo-org"


def resource(rid, name, domain, health="healthy", enabled=True, hc=True, hc_in_list=True, labels=None):
    target = {"targetId": rid * 10, "ip": "10.0.0.%d" % (rid % 250), "port": 80, "siteId": 1, "hcEnabled": hc}
    listed = dict(target) if hc_in_list else {k: v for k, v in target.items() if k != "hcEnabled"}
    data = {
        "resourceId": rid,
        "name": name,
        "fullDomain": domain,
        "enabled": enabled,
        "health": health,
        "targets": [listed],
        "_targets": [target],
    }
    if labels is not None:  # newer Pangolin versions report labels, older ones omit the field
        data["labels"] = [{"labelId": 100 + i, "name": label, "color": "#16a34a"} for i, label in enumerate(labels)]
    return data


DEMO = [
    resource(1, "Nextcloud", "cloud.example.com", labels=["env:prod"]),
    resource(2, "Grafana", "grafana.example.com", health="unhealthy", labels=["env:prod", "team:infra"]),
    resource(3, "Starting up", "boot.example.com", health="unknown", labels=["env:staging"]),
    resource(4, "No health check", "api.prj.example.com", health="unknown", hc=False),
    resource(5, "Disabled", "off.example.com", enabled=False),
    resource(6, "Cost $avings", "money.example.com", health="unknown", hc_in_list=False),
]


class MockPangolin:
    def __init__(self, resources, page_cap=None, legacy_only=False, api_key=API_KEY, no_labels=False):
        self.resources = resources
        self.no_labels = no_labels
        self.page_cap = page_cap
        self.legacy_only = legacy_only
        self.api_key = api_key
        self.status_override = None
        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def public(self, r):
        hidden = ("labels",) if self.no_labels else ()
        return {k: v for k, v in r.items() if not k.startswith("_") and k not in hidden}

    def handle(self, method, raw_path, headers):
        url = urlparse(raw_path)
        parts = url.path.strip("/").split("/")
        self.requests.append(url.path)
        if self.status_override:
            return self.status_override, {"error": True, "message": "forced"}
        if headers.get("Authorization") != "Bearer " + self.api_key:
            return 401, {"error": True, "message": "unauthorized"}
        if method != "GET" or parts[:1] != ["v1"]:
            return 404, {"error": True}
        parts = parts[1:]
        by_id = {str(r["resourceId"]): r for r in self.resources}
        list_names = ("resources",) if self.legacy_only else ("public-resources", "resources")
        detail_names = ("resource",) if self.legacy_only else ("public-resource", "resource")

        if len(parts) == 3 and parts[0] == "org" and parts[2] in list_names:
            query = parse_qs(url.query)
            page = int(query.get("page", ["1"])[0])
            size = int(query.get("pageSize", ["20"])[0])
            if self.page_cap:
                size = min(size, self.page_cap)
            chunk = self.resources[(page - 1) * size: page * size]
            return 200, {"data": {
                "resources": [self.public(r) for r in chunk],
                "pagination": {"total": len(self.resources), "page": page, "pageSize": size},
            }, "success": True}
        if len(parts) == 2 and parts[0] in detail_names and parts[1] in by_id:
            return 200, {"data": self.public(by_id[parts[1]]), "success": True}
        if len(parts) == 3 and parts[0] == "resource" and parts[2] == "targets" and parts[1] in by_id:
            return 200, {"data": {"targets": by_id[parts[1]]["_targets"]}, "success": True}
        return 404, {"error": True, "message": "not found"}

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = mock.handle("GET", self.path, self.headers)
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                pass

        return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3003
    mock = MockPangolin(DEMO)
    mock.server.server_close()
    mock.server = ThreadingHTTPServer(("0.0.0.0", port), mock._handler())
    sys.stderr.write("mock Pangolin listening on :%d (org %s)\n" % (port, ORG))
    mock.server.serve_forever()
