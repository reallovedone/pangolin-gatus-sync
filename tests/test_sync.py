import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pangolin_gatus_sync as pgs  # noqa: E402
from mock_pangolin import API_KEY, DEMO, ORG, MockPangolin, resource  # noqa: E402

try:
    import yaml
except ImportError:  # PyYAML is optional: only used to double check the generated YAML.
    yaml = None

logging.disable(logging.CRITICAL)


class SyncTestCase(unittest.TestCase):
    resources = DEMO
    mock_options: dict = {}

    def setUp(self):
        self.mock = MockPangolin(self.resources, **self.mock_options).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "pangolin.yaml"

    def tearDown(self):
        self.mock.stop()
        self.tmp.cleanup()

    def config(self, **env):
        base = {
            "PANGOLIN_API_URL": self.mock.url,
            "PANGOLIN_ORG_ID": ORG,
            "PANGOLIN_API_KEY": API_KEY,
            "OUTPUT_FILE": str(self.output),
            "STATE_FILE": str(Path(self.tmp.name) / "state"),
        }
        base.update(env)
        return pgs.Config.from_env(base)

    def generated(self, **env):
        self.assertTrue(pgs.sync_once(self.config(**env)))
        text = self.output.read_text(encoding="utf-8")
        if yaml is not None:
            return text, yaml.safe_load(text)["endpoints"]
        return text, None


class TestSelection(SyncTestCase):
    def test_selects_only_resources_with_active_health_check(self):
        cfg = self.config()
        client = pgs.PangolinClient(cfg)
        selected = pgs.select_resources(client.list_resources(), cfg, client)
        # 4: health check off, 5: disabled. 6 lacks hcEnabled in the list: resolved via /targets.
        self.assertEqual(sorted(r["resourceId"] for r in selected), [1, 2, 3, 6])
        self.assertIn("/v1/resource/6/targets", self.mock.requests)
        self.assertNotIn("/v1/resource/1/targets", self.mock.requests)

    def test_include_and_exclude_match_name_or_domain(self):
        cfg = self.config(RESOURCE_INCLUDE="*.example.com", RESOURCE_EXCLUDE="grafana*,Cost*")
        client = pgs.PangolinClient(cfg)
        selected = pgs.select_resources(client.list_resources(), cfg, client)
        self.assertEqual(sorted(r["resourceId"] for r in selected), [1, 3])


class TestGeneratedConfig(SyncTestCase):
    def test_endpoints_and_conditions(self):
        text, endpoints = self.generated(GATUS_ALERT_TYPES="telegram, email")
        self.assertIn('"Bearer ${PANGOLIN_API_KEY}"', text)
        self.assertNotIn(API_KEY, text)
        self.assertIn('"Cost $$avings"', text)
        self.assertIn('"[BODY].data.health == any(healthy, unknown)"', text)
        self.assertIn(f'"{self.mock.url}/public-resource/1"', text)
        if endpoints is not None:
            self.assertEqual([e["name"] for e in endpoints],
                             ["Pangolin API", "Cost $$avings", "Grafana", "Nextcloud", "Starting up"])
            self.assertEqual([a["type"] for a in endpoints[1]["alerts"]], ["telegram", "email"])
            self.assertTrue(all(e["ui"]["hide-url"] for e in endpoints))

    def test_fail_on_unknown_and_no_api_endpoint(self):
        text, endpoints = self.generated(FAIL_ON_UNKNOWN="true", GATUS_API_ENDPOINT="false",
                                         PANGOLIN_TLS_VERIFY="false")
        self.assertIn('"[BODY].data.health == healthy"', text)
        self.assertNotIn("Pangolin API", text)
        self.assertNotIn("alerts", text)
        if endpoints is not None:
            self.assertTrue(all(e["client"]["insecure"] for e in endpoints))

    def test_idempotent_write(self):
        self.assertTrue(pgs.sync_once(self.config()))
        first = self.output.stat().st_mtime_ns
        time.sleep(0.05)
        self.assertTrue(pgs.sync_once(self.config()))
        self.assertEqual(self.output.stat().st_mtime_ns, first)
        self.assertFalse((self.output.parent / "pangolin.yaml.tmp").exists())

    def test_api_errors_keep_existing_file(self):
        self.output.write_text("previous", encoding="utf-8")
        self.assertFalse(pgs.sync_once(self.config(PANGOLIN_API_KEY="wrong")))
        self.mock.status_override = 500
        self.assertFalse(pgs.sync_once(self.config()))
        self.assertFalse(pgs.sync_once(self.config(PANGOLIN_API_URL="http://127.0.0.1:1/v1")))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous")

    def test_healthcheck_follows_last_success(self):
        cfg = self.config()
        self.assertFalse(pgs.healthcheck(cfg))
        self.assertTrue(pgs.sync_once(cfg))
        self.assertTrue(pgs.healthcheck(cfg))
        old = time.time() - cfg.sync_interval * 4
        os.utime(cfg.state_file, (old, old))
        self.assertFalse(pgs.healthcheck(cfg))


class TestEmptyOrganization(SyncTestCase):
    resources = []

    def test_empty_list_keeps_existing_file(self):
        self.output.write_text("previous", encoding="utf-8")
        self.assertFalse(pgs.sync_once(self.config()))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous")


class TestPagination(SyncTestCase):
    resources = [resource(i, f"svc-{i:03d}", f"svc{i}.example.com") for i in range(1, 137)]
    mock_options = {"page_cap": 50}  # The server returns fewer items than requested.

    def test_reads_every_page(self):
        cfg = self.config()
        self.assertEqual(len(pgs.PangolinClient(cfg).list_resources()), 136)


class TestLegacyApi(SyncTestCase):
    mock_options = {"legacy_only": True}

    def test_falls_back_to_legacy_paths(self):
        text, _ = self.generated()
        self.assertIn(f'"{self.mock.url}/resource/1"', text)
        self.assertIn(f"/org/{ORG}/resources?page=1&pageSize=1", text)


class TestDuplicateNames(SyncTestCase):
    resources = [
        resource(1, "web", "a.example.com"),
        resource(2, "web", "b.example.com"),
        resource(3, "Web", "c.example.com"),  # Same Gatus key as "web" once lowercased.
        resource(4, "app", "same.example.com"),
        resource(5, "app", "same.example.com"),
    ]

    def test_names_are_unique(self):
        _, endpoints = self.generated()
        if endpoints is None:
            self.skipTest("PyYAML not installed")
        names = [e["name"] for e in endpoints]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("web (a.example.com)", names)
        self.assertIn("app (#5)", names)


class TestConfig(unittest.TestCase):
    def test_missing_variables(self):
        with self.assertRaises(pgs.ConfigError):
            pgs.Config.from_env({"PANGOLIN_API_URL": "http://x/v1"})

    def test_invalid_values(self):
        base = {"PANGOLIN_API_URL": "http://x/v1/", "PANGOLIN_ORG_ID": "o", "PANGOLIN_API_KEY": "k"}
        for name, value in (("SYNC_INTERVAL", "abc"), ("CHECK_INTERVAL", "5"), ("FAIL_ON_UNKNOWN", "maybe")):
            with self.assertRaises(pgs.ConfigError, msg=name):
                pgs.Config.from_env({**base, name: value})
        self.assertEqual(pgs.Config.from_env(base).api_url, "http://x/v1")


if __name__ == "__main__":
    unittest.main()
