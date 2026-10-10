#!/usr/bin/env python3
"""Verify every externally-sourced pin exists upstream.

Reads playbooks/group_vars/all/versions.yml (and requirements.yml) and checks
each pin against its source, printing one line per pin:

    OK    <name>  <what was confirmed>
    FAIL  <name>  <why>
    SKIP  <name>  <why it cannot be checked>

Exits non-zero if any pin FAILs. Read-only HTTPS GETs/HEADs to public
registries; it never touches a host. Run with `make check-pins` or
`python3 tests/check_pins.py`. Deliberately not part of `make check`, which
stays offline.
"""

import gzip
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("check_pins: PyYAML is required (`import yaml` failed). It ships "
             "with ansible; run this with the python that has ansible, or "
             "`pip install pyyaml`.")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS = os.path.join(ROOT, "playbooks/group_vars/all/versions.yml")
REQUIREMENTS = os.path.join(ROOT, "requirements.yml")
BORG_DEFAULTS = os.path.join(ROOT, "roles/backup_server/defaults/main.yml")

TIMEOUT = 20
UA = "provisioner-check-pins/1"

DOCKER_PACKAGES = ("https://download.docker.com/linux/ubuntu/dists/resolute/"
                   "stable/binary-amd64/Packages.gz")
PGDG_PACKAGES = ("https://apt.postgresql.org/pub/repos/apt/dists/resolute-pgdg/"
                 "main/binary-amd64/Packages.gz")
LAUNCHPAD_ARCHIVE = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
LAUNCHPAD_RESOLUTE_AMD64 = "https://api.launchpad.net/1.0/ubuntu/resolute/amd64"
ACME_TARBALL = "https://github.com/acmesh-official/acme.sh/archive/refs/tags/{}.tar.gz"
KEYSERVER = "https://keys.openpgp.org/vks/v1/by-fingerprint/{}"
GALAXY = "https://galaxy.ansible.com"

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])


class CheckError(Exception):
    """A check that could not confirm its pin; the message is the reason."""


# --- HTTP helpers ------------------------------------------------------------

def http(url, method="GET", headers=None):
    """Return (status, body bytes, response headers). Errors raise CheckError."""
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        raise CheckError(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise CheckError(f"network error fetching {url}: {reason}") from None


def get_json(url, headers=None):
    return json.loads(http(url, headers=headers)[1])


_cache = {}


def apt_packages(url):
    """Parse an apt Packages(.gz) index into {package name: set(versions)}."""
    if url not in _cache:
        body = http(url)[1]
        if url.endswith(".gz"):
            body = gzip.decompress(body)
        index = {}
        for stanza in body.decode("utf-8", "replace").split("\n\n"):
            fields = dict(line.split(": ", 1) for line in stanza.splitlines()
                          if ": " in line and not line.startswith(" "))
            if "Package" in fields and "Version" in fields:
                index.setdefault(fields["Package"], set()).add(fields["Version"])
        _cache[url] = index
    return _cache[url]


def apt_has(url, package, version):
    versions = apt_packages(url).get(package)
    if not versions:
        raise CheckError(f"no package {package} in {url}")
    if version not in versions:
        newest = max(versions, key=natural)
        raise CheckError(f"{package}={version} not in {url} "
                         f"({len(versions)} versions listed, newest {newest})")
    return f"{package}={version} listed"


def natural(version):
    """Sort key that orders 17.11 after 17.9 (not apt-exact, display only)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", version)]


def sha256_of(url):
    return hashlib.sha256(http(url)[1]).hexdigest()


# --- one function per source -------------------------------------------------

def check_cs_agent(v):
    url = v["cs_agent_apt_repo"].rstrip("/") + "/dists/stable/main/binary-amd64/Packages"
    return apt_has(url, "cs-agent", v["cs_agent_apt_version"])


def check_cs_agent_key(v):
    # Not checksum-pinned in versions.yml, so reachability is all there is.
    http(v["cs_agent_apt_key_url"])
    return "key URL reachable (not checksum-pinned)"


def check_docker_package(entry):
    name, sep, version = entry.partition("=")
    if not sep:
        raise CheckError(f"'{entry}' carries no =version")
    return apt_has(DOCKER_PACKAGES, name, version)


def check_node_exporter(v):
    version = str(v["node_exporter_apt_version"])
    if not version:
        raise CheckError("empty (floats to whatever 26.04 ships) -- not a pin")
    # Launchpad getPublishedBinaries, not getPublishedSources: the pin is the
    # BINARY package version apt installs, which can differ from the source
    # version (e.g. a no-change rebuild). Restricted to resolute/amd64.
    query = urllib.parse.urlencode({
        "ws.op": "getPublishedBinaries",
        "binary_name": "prometheus-node-exporter",
        "distro_arch_series": LAUNCHPAD_RESOLUTE_AMD64,
        "exact_match": "true",
        "status": "Published",
    })
    entries = get_json(f"{LAUNCHPAD_ARCHIVE}?{query}")["entries"]
    found = {(e["binary_package_version"], e["pocket"]) for e in entries}
    pockets = sorted(p for ver, p in found if ver == version)
    if not pockets:
        raise CheckError(f"prometheus-node-exporter {version} not published for "
                         f"resolute/amd64 (published: {sorted(found)})")
    return f"prometheus-node-exporter {version} published in resolute ({', '.join(pockets)})"


def check_postgresql(v):
    package = f"postgresql-{v['postgresql_version']}"
    versions = apt_packages(PGDG_PACKAGES).get(package)
    if not versions:
        raise CheckError(f"no {package} in resolute-pgdg")
    return f"{package} in resolute-pgdg (newest {max(versions, key=natural)})"


def check_checksum(url, expected):
    actual = sha256_of(url)
    if actual != str(expected).lower():
        raise CheckError(f"sha256 of {url} is {actual}, pinned {expected}")
    return f"sha256 matches {url}"


def check_pgdg_key(v):
    return check_checksum(v["pgdg_key_url"], v["pgdg_key_checksum"])


def check_acme_sh(v):
    return check_checksum(ACME_TARBALL.format(v["acme_sh_version"]),
                          v["acme_sh_tarball_sha256"])


def check_borg_release(v):
    # Same URL roles/backup_server downloads: its defaults name the asset and
    # the release base URL (a "{{ borg_version }}" template).
    with open(BORG_DEFAULTS) as f:
        defaults = yaml.safe_load(f)
    base = defaults["backup_server_borg_release_url"].replace(
        "{{ borg_version }}", str(v["borg_version"]))
    if "{{" in base:
        raise CheckError(f"cannot render backup_server_borg_release_url: {base}")
    asset = defaults["backup_server_borg_asset"]
    for name in (asset, asset + ".asc"):
        http(f"{base}/{name}", method="HEAD")
    return f"{base}/{asset} (+ .asc) exists"


def check_borg_key(v):
    fpr = v["borg_release_gpg_fingerprint"]
    http(KEYSERVER.format(fpr), method="HEAD")
    return f"key {fpr} on keys.openpgp.org"


def parse_image(ref):
    """Split an image reference into (registry, repository, tag)."""
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag:
        raise CheckError(f"'{ref}' has no tag")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first, rest, tag
    return "docker.io", name if "/" in name else f"library/{name}", tag


def check_image(ref):
    registry, repo, tag = parse_image(ref)
    if registry == "ghcr.io":
        token_url = f"https://ghcr.io/token?scope=repository:{repo}:pull"
        manifest = f"https://ghcr.io/v2/{repo}/manifests/{tag}"
    elif registry == "docker.io":
        token_url = ("https://auth.docker.io/token?service=registry.docker.io"
                     f"&scope=repository:{repo}:pull")
        manifest = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
    else:
        return None, f"registry {registry} not supported (may need credentials)"
    token = get_json(token_url).get("token")
    if not token:
        raise CheckError(f"no anonymous pull token for {registry}/{repo}")
    # HEAD does not count against Docker Hub's pull rate limit.
    _, _, headers = http(manifest, method="HEAD", headers={
        "Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT})
    digest = headers.get("Docker-Content-Digest", "no digest returned")
    return True, f"{registry}/{repo}:{tag} exists ({digest})"


def check_galaxy_collection(name, version):
    ns, _, coll = name.partition(".")
    http(f"{GALAXY}/api/v3/plugin/ansible/content/published/collections/index/"
         f"{ns}/{coll}/versions/{version}/")
    return f"galaxy collection {name} {version} exists"


def check_galaxy_role(name, version):
    owner, _, role = name.partition(".")
    query = urllib.parse.urlencode({"owner__username": owner, "name": role})
    results = get_json(f"{GALAXY}/api/v1/roles/?{query}")["results"]
    if not results:
        raise CheckError(f"no galaxy role {name}")
    url = f"{GALAXY}/api/v1/roles/{results[0]['id']}/versions/?page_size=100"
    versions = set()
    while url:
        page = get_json(url)
        versions |= {r["name"] for r in page["results"]}
        url = page.get("next") and urllib.parse.urljoin(GALAXY, page["next"])
    if version not in versions:
        raise CheckError(f"galaxy role {name} has no version {version}")
    return f"galaxy role {name} {version} exists"


# --- driver ------------------------------------------------------------------

# Keys that are in versions.yml but are not externally-sourced pins.
NOT_PINS = {
    "cs_agent_apt_repo": "repository URL; exercised by cs_agent_apt_version",
    "controller_image_repo": "checked with controller_image_tag",
    "controller_image_allow_floating_tag": "policy flag, not a pin",
    "borg_binary_path": "local install path, not a pin",
    "pgdg_key_url": "checked with pgdg_key_checksum",
    "acme_sh_version": "checked with acme_sh_tarball_sha256",
}

CHECKS = {
    "cs_agent_apt_version": check_cs_agent,
    "cs_agent_apt_key_url": check_cs_agent_key,
    "node_exporter_apt_version": check_node_exporter,
    "postgresql_version": check_postgresql,
    "pgdg_key_checksum": check_pgdg_key,
    "acme_sh_tarball_sha256": check_acme_sh,
    "borg_version": check_borg_release,
    "borg_release_gpg_fingerprint": check_borg_key,
}


class Report:
    def __init__(self):
        self.failed = 0

    def line(self, status, name, reason):
        if status == "FAIL":
            self.failed += 1
        print(f"{status:<5} {name:<40} {reason}")

    def run(self, name, fn, *args):
        try:
            result = fn(*args)
        except CheckError as e:
            self.line("FAIL", name, str(e))
            return
        except Exception as e:  # a layout surprise upstream, not a crash
            self.line("FAIL", name, f"{type(e).__name__}: {e}")
            return
        if isinstance(result, tuple):
            ok, reason = result
            self.line("OK" if ok else "SKIP", name, reason)
        else:
            self.line("OK", name, result)


def find_verify(value, path):
    """Yield the paths of any value that still carries a VERIFY marker."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from find_verify(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from find_verify(v, f"{path}[{i}]")
    elif isinstance(value, str) and "VERIFY" in value:
        yield path


def main():
    with open(VERSIONS) as f:
        versions = yaml.safe_load(f)
    report = Report()

    for key, value in versions.items():
        for path in find_verify(value, key):
            report.line("FAIL", path, "value still carries a VERIFY marker")

    for key, value in versions.items():
        if key in CHECKS:
            report.run(key, CHECKS[key], versions)
        elif key == "controller_image_tag":
            ref = f"{versions['controller_image_repo']}:{value}"
            report.run(key, check_image, ref)
        elif key.endswith("_image"):
            report.run(key, check_image, value)
        elif key == "docker_apt_packages":
            for entry in value:
                report.run(f"{key}[{entry.partition('=')[0]}]",
                           check_docker_package, entry)
        elif key in NOT_PINS:
            report.line("SKIP", key, NOT_PINS[key])
        else:
            report.line("SKIP", key, "no checker for this key -- add one to tests/check_pins.py")

    if os.path.exists(REQUIREMENTS):
        with open(REQUIREMENTS) as f:
            reqs = yaml.safe_load(f) or {}
        for c in reqs.get("collections", []):
            version = str(c.get("version", "")).lstrip("=")
            report.run(f"requirements:{c['name']}", check_galaxy_collection,
                       c["name"], version)
        for r in reqs.get("roles", []):
            report.run(f"requirements:{r['name']}", check_galaxy_role,
                       r["name"], str(r.get("version", "")))

    if report.failed:
        print(f"\ncheck-pins: {report.failed} pin(s) FAILED")
        return 1
    print("\ncheck-pins: every pin resolved upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
