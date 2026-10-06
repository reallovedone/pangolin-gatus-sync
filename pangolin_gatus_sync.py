#!/usr/bin/env python3
"""pangolin-gatus-sync: a Gatus sidecar that turns Pangolin resources into Gatus endpoints.

Every SYNC_INTERVAL seconds it lists the resources of a Pangolin organization through the
Integration API and writes one Gatus endpoint per resource that has an active health check.
Gatus then polls the Pangolin API for each resource and alerts when it turns unhealthy.

Standard library only. Run with --help for the command line options.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

__version__ = "0.2.0"

log = logging.getLogger("pangolin-gatus-sync")

HEALTHY, UNHEALTHY, UNKNOWN = "healthy", "unhealthy", "unknown"
PAGE_SIZE = 100
# Gatus compares file times in whole seconds against the moment it loaded its config, so a file written
# while Gatus is starting can be missed for good. Touching it once, a few seconds later, fixes that.
NUDGE_DELAY = 10
MAX_PAGES = 1000


class ConfigError(Exception):
    """Invalid or missing configuration."""


class ApiError(Exception):
    """The Pangolin API could not be reached or returned an unexpected answer."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- configuration


def _env_bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _env_int(env: dict, name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_list(env: dict, name: str) -> list[str]:
    return [item.strip() for item in env.get(name, "").split(",") if item.strip()]


def _parse_label_groups(raw: str, default_group: str) -> list[tuple[str, str]]:
    """Parse GATUS_LABEL_GROUPS ("label=Group,other-*=Group 2") into ordered (pattern, group) rules."""
    canonical = {default_group.lower(): default_group}  # Gatus keys ignore case: keep one spelling
    rules = []
    for entry in (item.strip() for item in raw.split(",")):
        if not entry:
            continue
        pattern, separator, group = (part.strip() for part in entry.partition("="))
        if not separator or not pattern or not group:
            raise ConfigError(f"GATUS_LABEL_GROUPS entries must look like label=Group, got {entry!r}")
        rules.append((pattern, canonical.setdefault(group.lower(), group)))
    return rules


@dataclass
class Config:
    api_url: str
    org_id: str
    api_key: str
    output_file: Path = Path("/config/pangolin.yaml")
    state_file: Path = Path("/tmp/pangolin-gatus-sync.ok")
    group: str = "Pangolin"
    label_groups: list[tuple[str, str]] = field(default_factory=list)
    sync_interval: int = 300
    check_interval: str = "60s"
    alert_types: list[str] = field(default_factory=list)
    alert_failure_threshold: int = 2
    alert_success_threshold: int = 1
    fail_on_unknown: bool = False
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    tls_verify: bool = True
    api_endpoint: bool = True
    api_key_env: str = "PANGOLIN_API_KEY"
    timeout: int = 15
    warned: set = field(default_factory=set, repr=False, compare=False)  # one-time warnings
    touch_pending: bool = field(default=False, repr=False, compare=False)  # file changed, see NUDGE_DELAY

    @classmethod
    def from_env(cls, env: dict | None = None, require_key: bool = True) -> "Config":
        env = dict(os.environ if env is None else env)
        missing = [n for n in ("PANGOLIN_API_URL", "PANGOLIN_ORG_ID") if not env.get(n, "").strip()]
        if require_key and not env.get("PANGOLIN_API_KEY", "").strip():
            missing.append("PANGOLIN_API_KEY")
        if missing:
            raise ConfigError("missing required environment variables: " + ", ".join(missing))

        check_interval = env.get("CHECK_INTERVAL", "60s").strip() or "60s"
        if not re.fullmatch(r"\d+(ms|s|m|h)", check_interval):
            raise ConfigError(f"CHECK_INTERVAL must look like 30s, 1m or 1h, got {check_interval!r}")

        group = env.get("GATUS_GROUP", "").strip() or "Pangolin"
        return cls(
            api_url=env["PANGOLIN_API_URL"].strip().rstrip("/"),
            org_id=env["PANGOLIN_ORG_ID"].strip(),
            api_key=env.get("PANGOLIN_API_KEY", "").strip(),
            output_file=Path(env.get("OUTPUT_FILE", "").strip() or "/config/pangolin.yaml"),
            state_file=Path(env.get("STATE_FILE", "").strip() or "/tmp/pangolin-gatus-sync.ok"),
            group=group,
            label_groups=_parse_label_groups(env.get("GATUS_LABEL_GROUPS", ""), group),
            sync_interval=_env_int(env, "SYNC_INTERVAL", 300, minimum=10),
            check_interval=check_interval,
            alert_types=_env_list(env, "GATUS_ALERT_TYPES"),
            alert_failure_threshold=_env_int(env, "ALERT_FAILURE_THRESHOLD", 2),
            alert_success_threshold=_env_int(env, "ALERT_SUCCESS_THRESHOLD", 1),
            fail_on_unknown=_env_bool(env, "FAIL_ON_UNKNOWN", False),
            include=_env_list(env, "RESOURCE_INCLUDE"),
            exclude=_env_list(env, "RESOURCE_EXCLUDE"),
            tls_verify=_env_bool(env, "PANGOLIN_TLS_VERIFY", True),
            api_endpoint=_env_bool(env, "GATUS_API_ENDPOINT", True),
            timeout=_env_int(env, "HTTP_TIMEOUT", 15),
        )


# --------------------------------------------------------------------------- Pangolin API


class PangolinClient:
    """Minimal read-only client for the Pangolin Integration API."""

    def __init__(self, config: Config):
        self.config = config
        self.base = config.api_url
        self.ssl_context = ssl.create_default_context()
        if not config.tls_verify:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        # Set by list_resources(): newer Pangolin versions use /public-resource, older ones /resource.
        self.detail_path = "/public-resource/{id}"
        self.list_path = f"/org/{quote(config.org_id, safe='')}/public-resources"

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self.base + path + ("?" + urlencode(params) if params else "")
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
                "User-Agent": f"pangolin-gatus-sync/{__version__}",
            },
        )
        try:
            with urlopen(request, timeout=self.config.timeout, context=self.ssl_context) as response:
                body = json.load(response)
        except HTTPError as exc:
            raise ApiError(f"GET {path} returned HTTP {exc.code}", status=exc.code) from exc
        except URLError as exc:
            raise ApiError(f"GET {path} failed: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise ApiError(f"GET {path} failed: {exc}") from exc
        if not isinstance(body, dict):
            raise ApiError(f"GET {path} returned an unexpected payload")
        return body

    def list_resources(self) -> list[dict]:
        org = quote(self.config.org_id, safe="")
        try:
            resources = self._paginate(f"/org/{org}/public-resources")
        except ApiError as exc:
            if exc.status != 404:
                raise
            log.info("/public-resources not found, falling back to the legacy /resources API")
            resources = self._paginate(f"/org/{org}/resources")
            self.detail_path = "/resource/{id}"
            self.list_path = f"/org/{org}/resources"
        return resources

    def _paginate(self, path: str) -> list[dict]:
        seen: dict = {}
        for page in range(1, MAX_PAGES + 1):
            data = self.get(path, {"page": page, "pageSize": PAGE_SIZE}).get("data")
            items = data.get("resources") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ApiError(f"GET {path}: data.resources missing from the response")
            new = [r for r in items if isinstance(r, dict) and r.get("resourceId") not in seen]
            for resource in new:
                seen[resource.get("resourceId")] = resource
            pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
            total = pagination.get("total")
            if not new:
                break
            if isinstance(total, int):
                if len(seen) >= total:
                    break
            elif len(items) < PAGE_SIZE:
                break
        return list(seen.values())

    def targets(self, resource_id) -> list[dict]:
        data = self.get(f"/resource/{resource_id}/targets").get("data")
        if isinstance(data, dict):
            data = data.get("targets")
        if not isinstance(data, list):
            raise ApiError(f"GET /resource/{resource_id}/targets: unexpected payload")
        return [t for t in data if isinstance(t, dict)]


# --------------------------------------------------------------------------- selection


def _matches(resource: dict, patterns: list[str]) -> bool:
    values = [str(resource.get("name") or ""), str(resource.get("fullDomain") or "")]
    return any(fnmatch.fnmatchcase(v.lower(), p.lower()) for p in patterns for v in values if v)


def has_health_check(resource: dict, client: PangolinClient) -> bool:
    """True when at least one target of the resource has an active health check."""
    if resource.get("health") in (HEALTHY, UNHEALTHY):
        return True  # Pangolin only computes a health state when a check is running.
    targets = resource.get("targets")
    if isinstance(targets, list) and targets and all(
        isinstance(t, dict) and "hcEnabled" in t for t in targets
    ):
        return any(t.get("hcEnabled") for t in targets)
    try:
        targets = client.targets(resource.get("resourceId"))
    except ApiError as exc:
        log.warning("cannot read targets of resource %s: %s", resource.get("resourceId"), exc)
        return False
    return any(t.get("hcEnabled") for t in targets)


def select_resources(resources: list[dict], config: Config, client: PangolinClient) -> list[dict]:
    selected = []
    for resource in resources:
        if resource.get("resourceId") is None or not resource.get("enabled", True):
            continue
        if config.include and not _matches(resource, config.include):
            continue
        if config.exclude and _matches(resource, config.exclude):
            continue
        if has_health_check(resource, client):
            selected.append(resource)
    return sorted(selected, key=lambda r: (str(r.get("name") or "").lower(), str(r.get("resourceId"))))


def label_names(resource: dict) -> list[str]:
    """Names of the labels attached to a resource (empty when Pangolin does not report any)."""
    labels = resource.get("labels")
    if not isinstance(labels, list):
        return []
    return [str(label["name"]) for label in labels if isinstance(label, dict) and label.get("name")]


def group_for(resource: dict, config: Config) -> str:
    """Gatus group of a resource: the first GATUS_LABEL_GROUPS rule matching one of its labels wins."""
    names = [name.lower() for name in label_names(resource)]
    for pattern, group in config.label_groups:
        if any(fnmatch.fnmatchcase(name, pattern.lower()) for name in names):
            return group
    return config.group


def group_counts(resources: list[dict], config: Config) -> dict[str, int]:
    counts: dict[str, int] = {}
    for resource in resources:
        group = group_for(resource, config)
        counts[group] = counts.get(group, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0].lower()))


def warn_if_labels_missing(resources: list[dict], config: Config) -> None:
    """Label groups are configured but Pangolin never sends labels: tell the user once."""
    if config.label_groups and "labels" not in config.warned and not any("labels" in r for r in resources):
        config.warned.add("labels")
        log.warning("GATUS_LABEL_GROUPS is set but Pangolin does not report labels "
                    "(older version?): every resource goes to the %r group", config.group)


# --------------------------------------------------------------------------- Gatus config


def gatus_escape(value: str) -> str:
    """Gatus expands ${VAR} in its config: a literal $ must be written as $$."""
    return value.replace("$", "$$")


def gatus_key(group: str, name: str) -> str:
    """Approximation of the key Gatus derives from group and name, used to avoid collisions."""
    return re.sub(r"[ /_,.#+&]", "-", f"{group}_{name}".lower())


def endpoint_names(resources: list[dict], groups: dict) -> dict:
    """Readable endpoint names, disambiguated when two resources of a group would collide in Gatus.

    groups maps resourceId to its Gatus group: the same name in two different groups is not a collision.
    """
    base = {r["resourceId"]: str(r.get("name") or r.get("fullDomain") or f"resource {r['resourceId']}")
            for r in resources}
    counts: dict = {}
    for rid, name in base.items():
        key = gatus_key(groups[rid], name)
        counts[key] = counts.get(key, 0) + 1
    names, used = {}, set()
    for resource in resources:
        rid = resource["resourceId"]
        group = groups[rid]
        name = base[rid]
        if counts[gatus_key(group, name)] > 1:
            name = f"{name} ({resource.get('fullDomain') or rid})"
            if counts.get(gatus_key(group, name)) or gatus_key(group, name) in used:
                name = f"{base[rid]} (#{rid})"
        used.add(gatus_key(group, name))
        names[rid] = name
    return names


def build_gatus_config(resources: list[dict], config: Config, client: PangolinClient) -> dict:
    auth = {"Authorization": "Bearer ${%s}" % config.api_key_env}
    base_url = gatus_escape(config.api_url)
    if config.fail_on_unknown:
        health_condition = f"[BODY].data.health == {HEALTHY}"
    else:
        # any() fails when the field is missing, unlike != which would pass on a malformed body.
        health_condition = f"[BODY].data.health == any({HEALTHY}, {UNKNOWN})"

    def common(name: str, group: str, url: str, conditions: list[str], description: str) -> dict:
        endpoint = {
            "name": gatus_escape(name),
            "group": gatus_escape(group),
            "url": url,
            "interval": config.check_interval,
            "headers": dict(auth),
            "conditions": conditions,
            "ui": {"hide-hostname": True, "hide-url": True, "hide-port": True},
        }
        if not config.tls_verify:
            endpoint["client"] = {"insecure": True}
        if config.alert_types:
            endpoint["alerts"] = [
                {
                    "type": alert_type,
                    "failure-threshold": config.alert_failure_threshold,
                    "success-threshold": config.alert_success_threshold,
                    "send-on-resolved": True,
                    "description": gatus_escape(description),
                }
                for alert_type in config.alert_types
            ]
        return endpoint

    endpoints = []
    if config.api_endpoint:
        endpoints.append(common(
            "Pangolin API",
            config.group,
            f"{base_url}{gatus_escape(client.list_path)}?page=1&pageSize=1",
            ["[STATUS] == 200"],
            "Pangolin Integration API unreachable: every Pangolin check depends on it",
        ))
    groups = {r["resourceId"]: group_for(r, config) for r in resources}
    names = endpoint_names(resources, groups)
    # Stable sort: resources stay ordered by name inside each group.
    for resource in sorted(resources, key=lambda r: groups[r["resourceId"]].lower()):
        rid = resource["resourceId"]
        domain = str(resource.get("fullDomain") or "")
        endpoints.append(common(
            names[rid],
            groups[rid],
            f"{base_url}{client.detail_path.format(id=quote(str(rid), safe=''))}",
            ["[STATUS] == 200", health_condition],
            f"Pangolin resource {domain or names[rid]} is unhealthy",
        ))
    return {"endpoints": endpoints}


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    # A JSON string is a valid YAML double-quoted scalar: safe for any character.
    return json.dumps(str(value), ensure_ascii=False)


def _yaml_lines(obj, indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.extend(_yaml_lines(value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
    else:
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                sub = _yaml_lines(item, indent + 2)
                sub[0] = f"{pad}- {sub[0][indent + 2:]}"
                lines.extend(sub)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return lines


def render_yaml(gatus_config: dict, config: Config) -> str:
    header = [
        "# Generated by pangolin-gatus-sync - do not edit, changes are overwritten.",
        f"# Source: {config.api_url} (organization {config.org_id})",
        "# https://github.com/reallovedone/pangolin-gatus-sync",
    ]
    return "\n".join(header + _yaml_lines(gatus_config, 0)) + "\n"


def write_if_changed(path: Path, content: str) -> bool:
    """Atomically replace path with content. Returns False when the file is already up to date."""
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_name(path.name + ".tmp")  # Gatus only loads .yaml/.yml files.
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


# --------------------------------------------------------------------------- runtime


def sync_once(config: Config, dry_run: bool = False) -> bool:
    """One full sync. Returns True on success; on failure the existing file is left untouched."""
    client = PangolinClient(config)
    try:
        resources = client.list_resources()
        if not resources:
            log.error("Pangolin returned no resources: keeping the current %s", config.output_file)
            return False
        selected = select_resources(resources, config, client)
    except ApiError as exc:
        log.error("sync failed, keeping the current %s: %s", config.output_file, exc)
        return False

    warn_if_labels_missing(resources, config)
    content = render_yaml(build_gatus_config(selected, config, client), config)
    summary = f"{len(resources)} resources, {len(selected)} with an active health check"
    if config.label_groups:
        summary += " (" + ", ".join(f"{g}: {n}" for g, n in group_counts(selected, config).items()) + ")"
    if dry_run:
        sys.stdout.write(content)
        log.info("dry run: %s", summary)
        return True
    try:
        changed = write_if_changed(config.output_file, content)
        config.touch_pending = config.touch_pending or changed
        config.state_file.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        log.error("cannot write %s: %s", config.output_file, exc)
        return False
    log.info("%s, %s %s", summary, "updated" if changed else "unchanged", config.output_file)
    return True


def show_labels(config: Config) -> bool:
    """Print the labels Pangolin reports and, when GATUS_LABEL_GROUPS is set, where resources would go."""
    client = PangolinClient(config)
    try:
        resources = client.list_resources()
        selected = select_resources(resources, config, client) if config.label_groups else []
    except ApiError as exc:
        log.error("cannot read resources: %s", exc)
        return False

    counts: dict[str, int] = {}
    for resource in resources:
        for name in set(label_names(resource)):
            counts[name] = counts.get(name, 0) + 1
    unlabeled = sum(1 for r in resources if not label_names(r))
    out = sys.stdout
    if not counts and not any("labels" in r for r in resources):
        out.write("Pangolin does not report labels for these resources (older version?).\n")
        return True
    out.write(f"Labels found on {len(resources)} resources:\n")
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())):
        out.write(f"  {count:4d}  {name}\n")
    out.write(f"  {unlabeled:4d}  (no label)\n")
    if config.label_groups:
        out.write("\nWith the current GATUS_LABEL_GROUPS, monitored resources go to:\n")
        for group, count in group_counts(selected, config).items():
            out.write(f"  {count:4d}  {group}\n")
    return True


def touch_output(config: Config) -> None:
    """Bump the modification time of the generated file so Gatus notices it (see NUDGE_DELAY)."""
    try:
        os.utime(config.output_file, None)
    except OSError as exc:
        log.warning("cannot touch %s: %s", config.output_file, exc)
        return
    log.debug("touched %s", config.output_file)


def healthcheck(config: Config) -> bool:
    """Container health: the last successful sync is not older than three intervals."""
    try:
        age = time.time() - config.state_file.stat().st_mtime
    except OSError:
        return False
    return age < config.sync_interval * 3


def run_forever(config: Config) -> None:
    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("received signal %s, exiting", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("pangolin-gatus-sync %s: syncing %s every %ss into %s",
             __version__, config.org_id, config.sync_interval, config.output_file)
    first = True
    while not stop.is_set():
        started = time.monotonic()
        sync_once(config)
        if first and config.touch_pending:
            if not stop.wait(NUDGE_DELAY):
                touch_output(config)
            config.touch_pending = False
        first = False
        stop.wait(max(0.0, config.sync_interval - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gatus sidecar: publish Pangolin resource health checks as Gatus endpoints.")
    parser.add_argument("--once", action="store_true", help="run a single sync and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the generated Gatus config to stdout without writing it")
    parser.add_argument("--show-labels", action="store_true",
                        help="list the Pangolin labels in use and the groups GATUS_LABEL_GROUPS would create")
    parser.add_argument("--healthcheck", action="store_true",
                        help="exit 0 if the last successful sync is recent (container health)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        config = Config.from_env(require_key=not args.healthcheck)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.healthcheck:
        return 0 if healthcheck(config) else 1
    if args.show_labels:
        return 0 if show_labels(config) else 1
    if args.once or args.dry_run:
        return 0 if sync_once(config, dry_run=args.dry_run) else 1
    run_forever(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
