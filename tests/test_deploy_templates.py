# SPDX-License-Identifier: Apache-2.0
"""The Coolify template and the plain compose files (#41, docs/security.md T15).

The files are rendered here without Docker: parsed, the Coolify magic variables and the
operator's settings substituted as Compose does (`${VAR}`, `:-`, `-`, `:?`, `?`, `$$`), and,
for Coolify, the routers Coolify itself generates for the domain added the way its
`fqdnLabelsForTraefik` does (coollabsio/coolify `bootstrap/helpers/docker.php`): an `https`
and an `http` router per domain, `Host() && PathPrefix(/)`, with every middleware the service
defines by label. Requests are then routed with Traefik's rule: the matching router of
highest priority (an explicit `priority`, else the length of the rule) wins, after the entry
point's `http.sanitizePath` (on by default in Traefik v3, which Coolify's proxy runs) cleaned
the path: percent-decoded, dot segments and repeated slashes removed (Go's `path.Clean`), a
trailing slash kept.

The threat-model criterion: only `/act/` is exempt from the SSO middleware; `/runs`,
`/healthz` and everything else require SSO; no router reaches Horae without one of the two.
CI also runs `docker compose config` on these files (`.github/workflows/ci.yml`, job
`deploy`).
"""

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COOLIFY = ROOT / "deploy" / "coolify" / "docker-compose.yaml"
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
TRAEFIK = ROOT / "deploy" / "compose" / "traefik.yaml"
ENV_EXAMPLE = ROOT / "deploy" / "compose" / ".env.example"

FQDN = "horae.example.test"
SSO_URL = "http://authentik-server:9000/outpost.goauthentik.io/auth/traefik"
SSO = "horae-sso"
ACT = "horae-act"
COOLIFY_UUID = "abcd1234efgh5678ijkl9012"

COOLIFY_REQUIRED = {
    "HORAE_SSO_FORWARD_AUTH_URL",
    "HORAE_ANTHROPIC_API_KEY",
    "HORAE_MONITOR_DAILY_BRIEF_URL",
    "HORAE_MONITOR_REMIND_URL",
}
COOLIFY_ENV = {
    # Set by Coolify from the domain of the `horae` service (SERVICE_URL_HORAE_8080).
    "SERVICE_FQDN_HORAE": FQDN,
    "SERVICE_URL_HORAE": f"https://{FQDN}",
    "HORAE_SSO_FORWARD_AUTH_URL": SSO_URL,
    "HORAE_ANTHROPIC_API_KEY": "synthetic-anthropic-key-for-tests",
    "HORAE_MONITOR_DAILY_BRIEF_URL": "https://kuma.example.test/api/push/synthetic1",
    "HORAE_MONITOR_REMIND_URL": "https://kuma.example.test/api/push/synthetic2",
}

# Paths that must go through SSO. `/act` (no slash) and look-alikes are not `/act/`; the
# traversals are cleaned by Traefik's `sanitizePath` before routing (to `/runs`).
TRAVERSALS = ("/act/../runs", "/act/%2e%2e/runs", "/act/..%2Fruns", "//act/../runs")
SSO_PATHS = (
    "/",
    "/runs",
    "/runs/abc",
    "/runs?limit=5",
    "/healthz",
    "/act",
    "/actx",
    "/ACT/x",
    *TRAVERSALS,
)
ACT_PATHS = ("/act/", "/act/token123")


class MissingVariable(Exception):
    """A required variable (`:?` or `?`) is unset (or empty, for `:?`), as Compose refuses."""


_VAR = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-|:\?|\?)([^}]*))?\}")


def interpolate(value: str, env: Mapping[str, str]) -> str:
    def one(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name, operator, rest = match.group(1), match.group(2), match.group(3)
        current = env.get(name)
        if operator == ":-":
            return current if current else rest
        if operator == "-":
            return rest if current is None else current
        if operator == ":?" and not current:
            raise MissingVariable(name)
        if operator == "?" and current is None:
            raise MissingVariable(name)
        return current or ""

    return _VAR.sub(one, value)


def render(node: Any, env: Mapping[str, str]) -> Any:
    if isinstance(node, str):
        return interpolate(node, env)
    if isinstance(node, list):
        return [render(item, env) for item in node]
    if isinstance(node, dict):
        return {key: render(item, env) for key, item in node.items()}
    return node


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def labels_of(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("labels", [])
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    out: dict[str, str] = {}
    for item in raw:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


def env_of(service: Mapping[str, Any]) -> dict[str, str | None]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {str(key): None if value is None else str(value) for key, value in raw.items()}
    out: dict[str, str | None] = {}
    for item in raw:
        key, sep, value = str(item).partition("=")
        out[key] = value if sep else None
    return out


def label_middlewares(labels: Mapping[str, str]) -> list[str]:
    """The middleware names Coolify adds to its generated routers: every name a
    `traefik.http.middlewares.<name>.*` label defines, and `coolify.traefik.middlewares`."""
    names: list[str] = []
    for key, value in labels.items():
        match = re.match(r"traefik\.http\.middlewares\.(.*?)(\.|$)", key)
        if match and match.group(1) not in names:
            names.append(match.group(1))
        if key == "coolify.traefik.middlewares":
            names.extend(name for name in value.split(",") if name not in names)
    return names


def coolify_generated_labels(
    service_labels: Mapping[str, str],
    *,
    force_https: bool,
    service: str = "horae",
    port: int,
    fqdn: str = FQDN,
    uuid: str = COOLIFY_UUID,
) -> dict[str, str]:
    """The labels Coolify adds for an `https://<fqdn>` domain with no path (gzip on)."""
    https = f"https-0-{uuid}-{service}"
    http = f"http-0-{uuid}-{service}"
    rule = f"Host(`{fqdn}`) && PathPrefix(`/`)"
    middlewares = ["gzip-sse", *label_middlewares(service_labels)]
    labels = {
        "traefik.enable": "true",
        "traefik.http.middlewares.gzip-sse.compress.excludedcontenttypes": "text/event-stream",
        "traefik.http.middlewares.redirect-to-https.redirectscheme.scheme": "https",
        f"traefik.http.routers.{https}.rule": rule,
        f"traefik.http.routers.{https}.entryPoints": "https",
        f"traefik.http.routers.{https}.service": https,
        f"traefik.http.services.{https}.loadbalancer.server.port": str(port),
        f"traefik.http.routers.{https}.middlewares": ",".join(middlewares),
        f"traefik.http.routers.{https}.tls": "true",
        f"traefik.http.routers.{https}.tls.certresolver": "letsencrypt",
        f"traefik.http.routers.{http}.rule": rule,
        f"traefik.http.routers.{http}.entryPoints": "http",
        f"traefik.http.services.{http}.loadbalancer.server.port": str(port),
        f"traefik.http.routers.{http}.service": http,
        f"traefik.http.routers.{http}.middlewares": (
            "redirect-to-https" if force_https else ",".join(middlewares)
        ),
    }
    return labels


@dataclass(frozen=True)
class Router:
    name: str
    rule: str
    entrypoints: tuple[str, ...]
    middlewares: tuple[str, ...]
    priority: int
    service: str | None


def routers_of(labels: Mapping[str, str]) -> dict[str, Router]:
    fields: dict[str, dict[str, str]] = {}
    for key, value in labels.items():
        match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(.+)", key)
        if match:
            fields.setdefault(match.group(1), {})[match.group(2).lower()] = value
    routers = {}
    for name, field in fields.items():
        rule = field["rule"]
        routers[name] = Router(
            name=name,
            rule=rule,
            entrypoints=tuple(e for e in field.get("entrypoints", "").split(",") if e),
            middlewares=tuple(m for m in field.get("middlewares", "").split(",") if m),
            priority=int(field["priority"]) if "priority" in field else len(rule),
            service=field.get("service"),
        )
    return routers


_MATCHER = re.compile(r"(Host|PathPrefix)\(`([^`]*)`\)")


def rule_matches(rule: str, host: str, path: str) -> bool:
    """Traefik rule matching, for the matchers these files use (`Host`, `PathPrefix`, `&&`)."""
    path = path.split("?", 1)[0]
    for part in rule.split(" && "):
        match = _MATCHER.fullmatch(part.strip())
        assert match, f"unexpected matcher in rule {rule!r}"
        kind, arg = match.groups()
        if kind == "Host" and host.lower() != arg.lower():
            return False
        if kind == "PathPrefix" and not path.startswith(arg):
            return False
    return True


def sanitize_path(path: str) -> str:
    """Traefik's entry point `sanitizePath` (default on): the decoded path, cleaned as Go's
    `path.Clean` does (dot segments, repeated slashes), a trailing slash kept."""
    path, _, query = path.partition("?")
    decoded = unquote(path)
    cleaned = posixpath.normpath(decoded)
    cleaned = "/" + cleaned.lstrip("/")  # normpath keeps a leading `//`; Go's Clean does not
    if decoded.endswith("/") and cleaned != "/":
        cleaned += "/"
    return f"{cleaned}?{query}" if query else cleaned


def test_sanitize_path_models_traefik() -> None:
    assert sanitize_path("/act/../runs") == "/runs"
    assert sanitize_path("/act/%2e%2e/runs") == "/runs"
    assert sanitize_path("/act/..%2Fruns") == "/runs"
    assert sanitize_path("//act/../runs") == "/runs"
    assert sanitize_path("/act/./x") == "/act/x"
    assert sanitize_path("/act/") == "/act/"
    assert sanitize_path("/runs?limit=5") == "/runs?limit=5"


def route(routers: Mapping[str, Router], entrypoint: str, host: str, path: str) -> Router | None:
    path = sanitize_path(path)
    candidates = [
        r
        for r in routers.values()
        if entrypoint in r.entrypoints and rule_matches(r.rule, host, path)
    ]
    if not candidates:
        return None
    top = max(r.priority for r in candidates)
    winners = [r for r in candidates if r.priority == top]
    assert len(winners) == 1, f"ambiguous routers for {path}: {winners}"
    return winners[0]


def coolify_rendered(
    env: Mapping[str, str] = COOLIFY_ENV,
    *,
    escape_labels: bool = False,
    force_https: bool = True,
    uuid: str = COOLIFY_UUID,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The Coolify file as deployed: the generated labels merged in, then interpolated.

    `escape_labels` is Coolify's "Escape special characters in labels" (on by default): every
    `$` of the service's own labels becomes `$$`, so their variables stay literal."""
    data = load(COOLIFY)
    service = data["services"]["horae"]
    own = labels_of(service)
    if escape_labels:
        own = {key: value.replace("$", "$$") for key, value in own.items()}
    generated = coolify_generated_labels(
        own, force_https=force_https, port=8080, fqdn=env.get("SERVICE_FQDN_HORAE", FQDN), uuid=uuid
    )
    merged = {**own, **generated}
    service["labels"] = [f"{key}={value}" for key, value in merged.items()]
    rendered = render(data, env)
    return rendered, labels_of(rendered["services"]["horae"])


def compose_rendered(*, traefik: bool, env: Mapping[str, str]) -> dict[str, Any]:
    """The plain compose file, with `traefik.yaml` merged in as Compose does for these keys."""
    data = load(COMPOSE)
    if traefik:
        override = load(TRAEFIK)
        horae = data["services"]["horae"]
        extra = override["services"]["horae"]
        horae["environment"] = {**horae["environment"], **extra["environment"]}
        horae["labels"] = extra["labels"]
        horae["networks"] = extra["networks"]
        data["networks"] = override["networks"]
    rendered = render(data, env)
    assert isinstance(rendered, dict)
    return rendered


def env_example() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def assert_only_act_bypasses_sso(
    labels: Mapping[str, str], https_entrypoint: str, http_entrypoints: tuple[str, ...] = ()
) -> None:
    routers = routers_of(labels)
    assert labels[f"traefik.http.middlewares.{SSO}.forwardauth.address"] == SSO_URL
    # Structure: the /act/ router matches only `/act/` on the public host, with no middleware;
    # every other router has the SSO middleware or only redirects to https.
    act = routers[ACT]
    assert act.rule == f"Host(`{FQDN}`) && PathPrefix(`/act/`)"
    assert act.middlewares == ()
    for router in routers.values():
        if router.name == ACT:
            continue
        assert SSO in router.middlewares or router.middlewares == ("redirect-to-https",), router
    # Behaviour: what each request reaches.
    for path in SSO_PATHS:
        reached = route(routers, https_entrypoint, FQDN, path)
        assert reached is not None, path
        assert SSO in reached.middlewares, (path, reached)
    for path in ACT_PATHS:
        reached = route(routers, https_entrypoint, FQDN, path)
        assert reached is not None, path
        assert reached.name == ACT, (path, reached)
    for path in TRAVERSALS:  # out of `/act/` once cleaned: the SSO router, never `horae-act`
        reached = route(routers, https_entrypoint, FQDN, path)
        assert reached is not None, path
        assert reached.name != ACT, (path, reached)
    # The SSO provider decides on what Traefik received, never on X-Forwarded-* from a client.
    assert labels[f"traefik.http.middlewares.{SSO}.forwardauth.trustForwardHeader"] == "false"
    for entrypoint in http_entrypoints:
        for path in SSO_PATHS + ACT_PATHS:
            reached = route(routers, entrypoint, FQDN, path)
            assert (
                reached is None
                or SSO in reached.middlewares
                or (reached.middlewares == ("redirect-to-https",))
            ), (entrypoint, path, reached)


# --- Coolify ---------------------------------------------------------------------------------


@pytest.mark.parametrize("force_https", [True, False])
def test_coolify_only_act_is_exempt_from_sso(force_https: bool) -> None:
    _, labels = coolify_rendered(force_https=force_https)
    assert_only_act_bypasses_sso(labels, "https", ("http",))


def test_coolify_the_template_defines_no_other_middleware() -> None:
    # Coolify adds every label-defined middleware to the routers it generates: only SSO here.
    assert label_middlewares(labels_of(load(COOLIFY)["services"]["horae"])) == [SSO]


def test_coolify_with_escaped_labels_everything_fails_closed() -> None:
    # Coolify's default escapes `$` in labels: the /act/ router's host stays literal and never
    # matches, so /act/ falls back to the SSO router (whose forwardAuth address is literal too:
    # Traefik refuses the requests). Nothing is exposed.
    _, labels = coolify_rendered(escape_labels=True)
    routers = routers_of(labels)
    assert "${SERVICE_FQDN_HORAE}" in routers[ACT].rule
    assert labels[f"traefik.http.middlewares.{SSO}.forwardauth.address"].startswith("${")
    for path in SSO_PATHS + ACT_PATHS:
        reached = route(routers, "https", FQDN, path)
        assert reached is not None, path
        assert SSO in reached.middlewares, path


def test_coolify_required_variables_use_the_bare_required_form() -> None:
    # Coolify stores the text after `:?` as the variable's value (serviceParser), which would
    # unblock the deployment with that text as the secret: required means `${VAR:?}`, bare.
    text = COOLIFY.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    required = set(re.findall(r"\$\{([A-Z0-9_]+)(?::\?|\?)", body))
    assert required == COOLIFY_REQUIRED
    for name in required:
        assert f"${{{name}:?}}" in body
    assert not re.search(r"\$\{[A-Z0-9_]+:?\?[^}]", body)


@pytest.mark.parametrize("name", sorted(COOLIFY_REQUIRED))
@pytest.mark.parametrize("unset", [True, False])
def test_coolify_a_missing_required_variable_blocks_the_deployment(name: str, unset: bool) -> None:
    env = dict(COOLIFY_ENV)
    if unset:
        del env[name]
    else:
        env[name] = ""
    with pytest.raises(MissingVariable, match=name):
        coolify_rendered(env)


def test_coolify_allowed_hosts_come_from_the_domain() -> None:
    raw = env_of(load(COOLIFY)["services"]["horae"])
    assert "SERVICE_URL_HORAE_8080" in raw  # the domain routes to port 8080
    assert raw["HORAE_ALLOWED_HOSTS"] == "${SERVICE_FQDN_HORAE}"
    rendered, _ = coolify_rendered()
    env = env_of(rendered["services"]["horae"])
    assert env["HORAE_ALLOWED_HOSTS"] == FQDN
    assert env["HORAE_SCHEDULER"] == "external"


def test_coolify_publishes_no_port_and_stays_on_its_own_network() -> None:
    data = load(COOLIFY)
    assert "networks" not in data
    for name, service in data["services"].items():
        assert "ports" not in service, name
        assert "networks" not in service, name
        assert "network_mode" not in service, name


def test_coolify_act_router_leaves_the_certificate_to_coolify() -> None:
    # TLS on, no resolver named: the certificate is the domain's, obtained by the router Coolify
    # generates for it with the server's resolver (Traefik picks certificates by SNI).
    labels = labels_of(load(COOLIFY)["services"]["horae"])
    assert labels[f"traefik.http.routers.{ACT}.tls"] == "true"
    assert not any(key.endswith(".certresolver") for key in labels)


def _traefik_names(labels: Mapping[str, str]) -> set[tuple[str, str]]:
    names = set()
    for key in labels:
        match = re.match(r"traefik\.http\.(routers|middlewares|services)\.([^.]+)\.", key)
        if match:
            names.add((match.group(1), match.group(2)))
    return names


def test_coolify_template_names_are_fixed_one_horae_per_server() -> None:
    # Coolify substitutes no per-resource value into labels (COOLIFY_RESOURCE_UUID is only
    # in the containers' environment, not in the `.env` that Compose interpolates; the
    # SERVICE_NAME_* values are the compose service names): the template's own names are
    # fixed, so docs/operations/coolify.md says one Horae per Coolify server. If this set
    # changes, update the doc and the two-instance test below.
    labels = labels_of(load(COOLIFY)["services"]["horae"])
    assert _traefik_names(labels) == {("middlewares", SSO), ("routers", ACT), ("services", ACT)}


def _merge_like_traefik(*containers: Mapping[str, str]) -> dict[str, str]:
    """Traefik's Docker provider across containers: an element (router, middleware, service)
    defined by several containers with different configurations is dropped ("defined multiple
    times with different configurations"); a router whose middleware is missing is not
    served."""
    by_name: dict[tuple[str, str], list[dict[str, str]]] = {}
    for labels in containers:
        for kind, name in _traefik_names(labels):
            prefix = f"traefik.http.{kind}.{name}."
            config = {k: v for k, v in labels.items() if k.startswith(prefix)}
            by_name.setdefault((kind, name), []).append(config)
    merged: dict[str, str] = {}
    for configs in by_name.values():
        if all(config == configs[0] for config in configs):
            merged.update(configs[0])
    middlewares = {name for kind, name in _traefik_names(merged) if kind == "middlewares"}
    for name, router in routers_of(merged).items():
        if not set(router.middlewares) <= middlewares:
            for key in [k for k in merged if k.startswith(f"traefik.http.routers.{name}.")]:
                del merged[key]
    return merged


@pytest.mark.parametrize("same_sso", [True, False])
def test_coolify_a_second_horae_on_the_server_fails_closed(same_sso: bool) -> None:
    second_fqdn = "second.example.test"
    _, first = coolify_rendered()
    env = {
        **COOLIFY_ENV,
        "SERVICE_FQDN_HORAE": second_fqdn,
        "SERVICE_URL_HORAE": f"https://{second_fqdn}",
        "HORAE_SSO_FORWARD_AUTH_URL": SSO_URL if same_sso else SSO_URL + "?other",
    }
    # The second resource has its own UUID: its generated routers do not clash.
    _, second = coolify_rendered(env, uuid="zyxw9876vuts5432rqpo1098")
    routers = routers_of(_merge_like_traefik(first, second))
    assert ACT not in routers  # dropped: the two `horae-act` routers differ (their hosts)
    for host in (FQDN, second_fqdn):
        for path in SSO_PATHS + ACT_PATHS:
            reached = route(routers, "https", host, path)
            # Nothing at all (the SSO middleware clashed), or the SSO router: never Horae
            # without SSO.
            assert reached is None or SSO in reached.middlewares, (host, path, reached)


def test_the_image_default_is_written_once_per_file_and_agrees() -> None:
    defaults = set()
    for path in (COOLIFY, COMPOSE):
        text = path.read_text(encoding="utf-8")
        assert text.count("ghcr.io/horae-dev/horae:") == 1, path
        data = load(path)
        image = data["x-horae-image"]
        assert all(service["image"] == image for service in data["services"].values()), path
        match = re.fullmatch(
            r"ghcr\.io/horae-dev/horae:\$\{HORAE_IMAGE_TAG:-(sha-[0-9a-f]{7})\}", image
        )
        assert match, image
        defaults.add(match.group(1))
    assert len(defaults) == 1, defaults
    assert env_example()["HORAE_IMAGE_TAG"] == ""  # empty: the compose file's default


def test_coolify_header_names_the_port() -> None:
    header = COOLIFY.read_text(encoding="utf-8").split("services:", 1)[0]
    assert "# port: 8080" in header
    assert "# documentation: " in header


# --- both files ------------------------------------------------------------------------------


def _duration_s(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", value)
    assert value, value
    assert match, value
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def _rendered_services() -> list[tuple[str, dict[str, Any]]]:
    coolify, _ = coolify_rendered()
    plain = compose_rendered(traefik=True, env=env_example())
    return [("coolify", coolify), ("compose", plain)]


@pytest.mark.parametrize(("kind", "data"), _rendered_services())
def test_every_service_is_hardened(kind: str, data: dict[str, Any]) -> None:
    for name, service in data["services"].items():
        assert service["read_only"] is True, (kind, name)
        # The tmpfs mount, not a temp file (S108): bounded, nothing executed from it.
        tmpfs = dict(str(entry).partition(":")[::2] for entry in service["tmpfs"])
        assert set(tmpfs) == {"/tmp"}, (kind, name)  # noqa: S108
        options = set(tmpfs["/tmp"].split(","))  # noqa: S108
        assert {"size=64m", "noexec", "nosuid", "nodev"} <= options, (kind, name)
        assert service["pids_limit"] == 256, (kind, name)
        user = str(service["user"])
        assert user.split(":", 1)[0] not in {"0", "root", ""}, (kind, name)
        assert service["cap_drop"] == ["ALL"], (kind, name)
        assert "no-new-privileges:true" in service["security_opt"], (kind, name)
        assert not service.get("privileged"), (kind, name)
        assert not service.get("cap_add"), (kind, name)
        assert "horae-instance:/instance" in service["volumes"], (kind, name)
        assert not any("docker.sock" in str(v) for v in service["volumes"]), (kind, name)
        pinned = r"ghcr\.io/horae-dev/horae:[\w.-]+(@sha256:[0-9a-f]{64})?"
        assert re.fullmatch(pinned, service["image"]), (kind, name)
        assert service["image"].split(":", 1)[1] not in {"edge", "latest"}, (kind, name)


@pytest.mark.parametrize(("kind", "data"), _rendered_services())
def test_horae_keeps_the_image_contract(kind: str, data: dict[str, Any]) -> None:
    horae = data["services"]["horae"]
    assert _duration_s(horae["stop_grace_period"]) >= 20, kind
    # The image's command, entrypoint (tini) and HEALTHCHECK (/healthz) are kept.
    for key in ("command", "entrypoint", "healthcheck", "init"):
        assert key not in horae, (kind, key)
    assert horae["depends_on"] == {"horae-init": {"condition": "service_completed_successfully"}}
    init = data["services"]["horae-init"]
    assert init["restart"] == "no"
    assert init["command"] == ["test -e /instance/horae.yaml || exec horae init /instance"]
    assert "labels" not in init
    assert "ports" not in init


# --- plain compose ---------------------------------------------------------------------------


def test_compose_binds_only_to_loopback() -> None:
    for traefik in (False, True):
        data = compose_rendered(traefik=traefik, env=env_example())
        for name, service in data["services"].items():
            for port in service.get("ports", []):
                assert str(port).startswith("127.0.0.1:"), (name, port)
    base = compose_rendered(traefik=False, env=env_example())["services"]["horae"]
    assert "labels" not in base  # no proxy route without traefik.yaml
    assert env_of(base)["HORAE_SCHEDULER"] == "builtin"


def test_compose_with_traefik_only_act_is_exempt_from_sso() -> None:
    data = compose_rendered(traefik=True, env=env_example())
    horae = data["services"]["horae"]
    labels = labels_of(horae)
    assert_only_act_bypasses_sso(labels, "websecure")
    assert label_middlewares(labels) == [SSO]
    assert env_of(horae)["HORAE_ALLOWED_HOSTS"] == FQDN
    # A network of its own with the proxy, which Traefik uses to reach Horae.
    assert horae["networks"] == ["default", "proxy"]
    assert data["networks"]["proxy"] == {"external": True, "name": "horae-proxy"}
    assert labels["traefik.docker.network"] == "horae-proxy"


@pytest.mark.parametrize(
    ("traefik", "name"),
    [
        (False, "HORAE_SECRETS_DIR"),
        (True, "HORAE_SECRETS_DIR"),
        (True, "HORAE_PUBLIC_HOST"),
        (True, "HORAE_SSO_FORWARD_AUTH_URL"),
    ],
)
def test_compose_a_missing_required_variable_fails(traefik: bool, name: str) -> None:
    env = env_example()
    del env[name]
    with pytest.raises(MissingVariable, match=name):
        compose_rendered(traefik=traefik, env=env)


def test_env_example_lists_every_variable_the_compose_files_use() -> None:
    used = set()
    for path in (COMPOSE, TRAEFIK):
        body = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        used |= set(re.findall(r"\$\{([A-Z0-9_]+)", body))
    assert used == set(env_example())
