import contextlib
import io
import logging
import os
import re
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


BASE_ENV = {"PANGOLIN_API_URL": "http://x/v1", "PANGOLIN_ORG_ID": "o", "PANGOLIN_API_KEY": "k"}
RULES = "team:*=Squadre,env:prod=Produzione,env:staging=Staging"


class TestLabelGroupsConfig(unittest.TestCase):
    def rules(self, raw, **extra):
        return pgs.Config.from_env({**BASE_ENV, "GATUS_LABEL_GROUPS": raw, **extra}).label_groups

    def test_not_set_means_no_rules(self):
        self.assertEqual(pgs.Config.from_env(BASE_ENV).label_groups, [])
        self.assertEqual(self.rules(" , ,"), [])

    def test_entries_keep_their_order(self):
        self.assertEqual(self.rules("prod = Produzione, staging=Staging ,env:*=Ambienti"),
                         [("prod", "Produzione"), ("staging", "Staging"), ("env:*", "Ambienti")])

    def test_group_spelling_is_unified_because_gatus_keys_ignore_case(self):
        rules = self.rules("a=Prod,b=prod,c=PANGOLIN", GATUS_GROUP="Pangolin")
        self.assertEqual([group for _, group in rules], ["Prod", "Prod", "Pangolin"])

    def test_malformed_entries_are_rejected(self):
        for raw in ("prod", "=Group", "prod=", " = ", "ok=Fine,broken"):
            with self.assertRaises(pgs.ConfigError, msg=raw):
                self.rules(raw)


class TestGroupFor(unittest.TestCase):
    config = pgs.Config(api_url="http://x/v1", org_id="o", api_key="k",
                        label_groups=[("critical", "Critici"), ("env:*", "Ambienti"), ("PROD", "Produzione")])

    @staticmethod
    def labelled(*names):
        return {"labels": [{"name": name} for name in names]}

    def test_first_rule_wins_not_first_label(self):
        # The resource carries env:prod first, but the "critical" rule is listed before "env:*".
        self.assertEqual(pgs.group_for(self.labelled("env:prod", "critical"), self.config), "Critici")
        self.assertEqual(pgs.group_for(self.labelled("critical", "env:prod"), self.config), "Critici")

    def test_wildcard_and_case_insensitive_matching(self):
        self.assertEqual(pgs.group_for(self.labelled("env:staging"), self.config), "Ambienti")
        self.assertEqual(pgs.group_for(self.labelled("ENV:Dev"), self.config), "Ambienti")
        self.assertEqual(pgs.group_for(self.labelled("prod"), self.config), "Produzione")

    def test_pattern_must_match_the_whole_label(self):
        self.assertEqual(pgs.group_for(self.labelled("myenv:prod"), self.config), "Pangolin")
        self.assertEqual(pgs.group_for(self.labelled("production"), self.config), "Pangolin")

    def test_unlabelled_or_malformed_resources_use_the_default_group(self):
        for resource_ in ({}, {"labels": None}, {"labels": []}, {"labels": "prod"},
                          {"labels": ["prod", {"labelId": 1}, {"name": ""}]}):
            self.assertEqual(pgs.group_for(resource_, self.config), "Pangolin", msg=resource_)

    def test_no_rules_always_default_group(self):
        config = pgs.Config(api_url="http://x/v1", org_id="o", api_key="k", group="Mine")
        self.assertEqual(pgs.group_for(self.labelled("env:prod"), config), "Mine")


class TestLabelGroupsSync(SyncTestCase):
    def endpoints(self, **env):
        text, endpoints = self.generated(**env)
        if endpoints is None:
            self.skipTest("PyYAML not installed")
        return text, {e["name"]: e["group"] for e in endpoints}

    def test_endpoints_are_assigned_to_groups(self):
        _, groups = self.endpoints(GATUS_LABEL_GROUPS=RULES)
        self.assertEqual(groups, {
            "Pangolin API": "Pangolin",        # the API check stays in the default group
            "Nextcloud": "Produzione",
            "Grafana": "Squadre",              # env:prod and team:infra: the team:* rule is listed first
            "Starting up": "Staging",
            "Cost $$avings": "Pangolin",       # no label: default group
        })

    def test_default_group_name_is_configurable(self):
        _, groups = self.endpoints(GATUS_LABEL_GROUPS=RULES, GATUS_GROUP="Altro")
        self.assertEqual(groups["Cost $$avings"], "Altro")
        self.assertEqual(groups["Pangolin API"], "Altro")

    def test_group_names_are_escaped_for_gatus(self):
        text, _ = self.generated(GATUS_LABEL_GROUPS="env:prod=Cash $ Flow")
        self.assertIn('group: "Cash $$ Flow"', text)

    def test_output_is_identical_to_before_when_no_rules_are_set(self):
        # Pangolin reports labels, but without GATUS_LABEL_GROUPS they must not change anything.
        with_labels, _ = self.generated()
        self.mock.no_labels = True
        other = Path(self.tmp.name) / "without-labels.yaml"
        self.assertTrue(pgs.sync_once(self.config(OUTPUT_FILE=str(other))))
        self.assertEqual(other.read_text(encoding="utf-8"), with_labels)
        self.assertEqual(with_labels.count('group: "Pangolin"'), 5)

    def test_pangolin_without_labels_falls_back_and_warns_once(self):
        self.mock.no_labels = True
        cfg = self.config(GATUS_LABEL_GROUPS=RULES)
        self.assertTrue(pgs.sync_once(cfg))
        self.assertEqual(cfg.warned, {"labels"})
        first = self.output.read_text(encoding="utf-8")
        self.assertEqual(first.count('group: "Pangolin"'), 5)
        self.assertTrue(pgs.sync_once(cfg))
        self.assertEqual(cfg.warned, {"labels"})

    def test_show_labels_lists_labels_and_resulting_groups(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(pgs.show_labels(self.config(GATUS_LABEL_GROUPS=RULES)))
        out = buffer.getvalue()
        self.assertRegex(out, r"\n\s+2\s+env:prod\n")
        self.assertRegex(out, r"\n\s+1\s+team:infra\n")
        self.assertRegex(out, r"\n\s+3\s+\(no label\)\n")
        for group in ("Squadre", "Produzione", "Staging", "Pangolin"):
            self.assertRegex(out, rf"\n\s+1\s+{group}\n")

    def test_show_labels_without_label_support(self):
        self.mock.no_labels = True
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(pgs.show_labels(self.config()))
        self.assertIn("does not report labels", buffer.getvalue())


class TestSameNameAcrossGroups(SyncTestCase):
    resources = [
        resource(1, "web", "a.example.com", labels=["prod"]),
        resource(2, "web", "b.example.com", labels=["staging"]),
        resource(3, "api", "c.example.com", labels=["prod"]),
        resource(4, "api", "d.example.com", labels=["prod"]),
    ]

    def test_only_collisions_inside_a_group_are_renamed(self):
        _, endpoints = self.generated(GATUS_LABEL_GROUPS="prod=Produzione,staging=Staging")
        if endpoints is None:
            self.skipTest("PyYAML not installed")
        pairs = {(e["group"], e["name"]) for e in endpoints}
        self.assertIn(("Produzione", "web"), pairs)
        self.assertIn(("Staging", "web"), pairs)
        self.assertIn(("Produzione", "api (c.example.com)"), pairs)
        self.assertIn(("Produzione", "api (d.example.com)"), pairs)
        self.assertEqual(len(pairs), len(endpoints))


class TestTouchForGatus(SyncTestCase):
    def test_touch_is_requested_only_when_the_file_changed(self):
        cfg = self.config()
        self.assertTrue(pgs.sync_once(cfg))
        self.assertTrue(cfg.touch_pending)              # file created: Gatus may have missed it
        cfg.touch_pending = False
        self.assertTrue(pgs.sync_once(cfg))
        self.assertFalse(cfg.touch_pending)             # unchanged: nothing to nudge

    def test_touch_output_bumps_the_modification_time(self):
        cfg = self.config()
        self.assertTrue(pgs.sync_once(cfg))
        old = time.time() - 3600
        os.utime(self.output, (old, old))
        before = self.output.read_text(encoding="utf-8")
        pgs.touch_output(cfg)
        self.assertGreater(self.output.stat().st_mtime, old + 3000)
        self.assertEqual(self.output.read_text(encoding="utf-8"), before)

    def test_touch_output_survives_a_missing_file(self):
        pgs.touch_output(self.config())                 # no file yet: logs a warning, must not raise


if __name__ == "__main__":
    unittest.main()
