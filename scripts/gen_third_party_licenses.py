"""Generate THIRD_PARTY_LICENSES.md + a CycloneDX SBOM from REAL package metadata.

Python deps: read from the runtime venv via importlib.metadata (the actual
installed .dist-info). npm deps: read the redistributed (non-dev) set from
frontend/package-lock.json, falling back to each package's own package.json
license field. No guessing — every license/version comes from on-disk metadata.

Usage: python gen_licenses.py <repo_root> <product_name> <product_version> <date_iso> <requirements.txt>

Python deps are restricted to the CLOSURE of the shipped requirements file
(top-level names + their transitive Requires-Dist, intersected with what is
installed) — so stray venv-only packages from local experiments are excluded
and the list reflects what actually ships.
"""
import importlib.metadata as im
import json
import os
import re
import sys
from urllib.parse import quote

repo, product, version, date_iso = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
req_file = sys.argv[5] if len(sys.argv) > 5 else ""


def _req_name(token: str) -> str:
    """Extract the bare distribution name from a requirement/Requires-Dist token."""
    token = token.strip()
    # Strip environment markers and version specs; take the leading name.
    m = re.match(r"^([A-Za-z0-9_.\-]+)", token)
    return (m.group(1).lower().replace("_", "-")) if m else ""


def shipped_closure(requirements_path: str) -> set[str]:
    """Top-level names in the requirements file + transitive Requires-Dist,
    normalised (lower, - for _). Empty set = no filtering (list everything)."""
    if not requirements_path or not os.path.isfile(requirements_path):
        return set()
    roots: list[str] = []
    with open(requirements_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "-r", "-e", "--")):
                continue
            line = line.split("#", 1)[0].split(";", 1)[0]
            # Drop [extras]
            line = re.sub(r"\[.*?\]", "", line)
            name = _req_name(line)
            if name:
                roots.append(name)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = im.metadata(name).get_all("Requires-Dist") or []
        except Exception:
            continue
        for r in reqs:
            dep = _req_name(re.sub(r"\[.*?\]", "", r))
            if dep and dep not in seen:
                stack.append(dep)
    return seen


_CLOSURE = shipped_closure(req_file)


# Packages whose dist metadata carries no License field/classifier, but whose
# bundled LICENSE file was read and verified by hand (name.lower() -> SPDX).
_VERIFIED_OVERRIDES = {
    "clr_loader": "MIT",  # verified 2026-07-03 from its dist-info/licenses/LICENSE
}


def py_license(meta) -> str:
    name = (meta.get("Name") or "").lower()
    if name in _VERIFIED_OVERRIDES:
        return _VERIFIED_OVERRIDES[name]
    lic = (meta.get("License-Expression") or "").strip()
    if lic:
        return lic
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    if classifiers:
        return "; ".join(sorted({c.split("::")[-1].strip() for c in classifiers}))
    raw = (meta.get("License") or "").strip()
    if raw:
        first = raw.splitlines()[0].strip()
        return first[:80] if first else "UNKNOWN"
    return "UNKNOWN"


def py_url(meta) -> str:
    url = (meta.get("Home-page") or "").strip()
    if url:
        return url
    for pu in (meta.get_all("Project-URL") or []):
        if "://" in pu:
            return pu.split(",")[-1].strip()
    return ""


# ── Python ────────────────────────────────────────────────────────────────
py = {}
for d in im.distributions():
    try:
        meta = d.metadata
        name = meta["Name"]
        if not name:
            continue
        key = name.lower()
        if key in py:
            continue
        # Restrict to the shipped requirements closure when one was provided.
        if _CLOSURE and name.lower().replace("_", "-") not in _CLOSURE:
            continue
        py[key] = {
            "name": name, "version": d.version or "",
            "license": py_license(meta), "url": py_url(meta), "eco": "pypi",
        }
    except Exception:
        continue
py_list = sorted(py.values(), key=lambda x: x["name"].lower())

# ── npm (redistributed = non-dev) ───────────────────────────────────────────
npm = {}
lock = os.path.join(repo, "frontend", "package-lock.json")
if os.path.isfile(lock):
    with open(lock, encoding="utf-8") as f:
        data = json.load(f)
    for path, info in (data.get("packages") or {}).items():
        if not path.startswith("node_modules/"):
            continue
        if info.get("dev"):
            continue  # dev-only build tooling is not shipped in the built app
        name = path.split("node_modules/")[-1]
        ver = info.get("version", "")
        lic = info.get("license", "")
        if not lic:
            pj = os.path.join(repo, "frontend", path, "package.json")
            if os.path.isfile(pj):
                try:
                    with open(pj, encoding="utf-8") as f:
                        pd = json.load(f)
                    lv = pd.get("license") or pd.get("licenses")
                    if isinstance(lv, str):
                        lic = lv
                    elif isinstance(lv, dict):
                        lic = lv.get("type", "")
                    elif isinstance(lv, list) and lv:
                        lic = lv[0].get("type", "") if isinstance(lv[0], dict) else str(lv[0])
                except Exception:
                    pass
        key = (name.lower(), ver)
        if key in npm:
            continue
        npm[key] = {
            "name": name, "version": ver, "license": lic or "UNKNOWN",
            "url": f"https://www.npmjs.com/package/{name}", "eco": "npm",
        }
npm_list = sorted(npm.values(), key=lambda x: x["name"].lower())

# ── THIRD_PARTY_LICENSES.md ─────────────────────────────────────────────────
lines = [
    f"# Third-Party Licenses - {product}",
    "",
    f"`{product}` v{version} bundles the open-source components listed below. "
    "Each remains under its own license; this file is provided for attribution "
    "and license compliance.",
    "",
    f"Generated {date_iso} from installed package metadata "
    "(Python: `importlib.metadata`; npm: `package-lock.json`, redistributed / "
    "non-dev dependencies only). Regenerate with `scripts/gen_third_party_licenses.py`.",
    "",
    f"- Python packages: **{len(py_list)}**",
    f"- npm packages (bundled): **{len(npm_list)}**",
    "",
    "---",
    "",
    "## Python dependencies",
    "",
    "| Package | Version | License |",
    "|---|---|---|",
]
for p in py_list:
    nm = f"[{p['name']}]({p['url']})" if p["url"] else p["name"]
    lines.append(f"| {nm} | {p['version']} | {p['license']} |")
lines += [
    "",
    "## Frontend (npm) dependencies — bundled",
    "",
    "| Package | Version | License |",
    "|---|---|---|",
]
for p in npm_list:
    lines.append(f"| [{p['name']}]({p['url']}) | {p['version']} | {p['license']} |")
lines.append("")

with open(os.path.join(repo, "THIRD_PARTY_LICENSES.md"), "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines))

# ── CycloneDX 1.5 SBOM ──────────────────────────────────────────────────────
components = []
for p in py_list:
    comp = {
        "type": "library", "name": p["name"], "version": p["version"],
        "purl": f"pkg:pypi/{quote(p['name'].lower())}@{quote(p['version'])}",
    }
    if p["license"] and p["license"] != "UNKNOWN":
        comp["licenses"] = [{"license": {"name": p["license"]}}]
    components.append(comp)
for p in npm_list:
    nm = p["name"]
    purl_name = ("%40" + nm[1:].replace("/", "/", 1)) if nm.startswith("@") else nm
    comp = {
        "type": "library", "name": nm, "version": p["version"],
        "purl": f"pkg:npm/{purl_name}@{quote(p['version'])}",
    }
    if p["license"] and p["license"] != "UNKNOWN":
        comp["licenses"] = [{"license": {"name": p["license"]}}]
    components.append(comp)

sbom = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "version": 1,
    "metadata": {
        "timestamp": date_iso,
        "component": {"type": "application", "name": product, "version": version},
        "tools": [{"name": "gen_third_party_licenses.py", "vendor": "Hybridyn"}],
    },
    "components": components,
}
with open(os.path.join(repo, "sbom.cdx.json"), "w", encoding="utf-8", newline="\n") as f:
    json.dump(sbom, f, indent=2)
    f.write("\n")

print(f"OK {product}: {len(py_list)} python + {len(npm_list)} npm components")
# Surface any UNKNOWN licenses so they can be resolved by hand (honesty).
unknown = [p["name"] for p in py_list + npm_list if p["license"] == "UNKNOWN"]
if unknown:
    print("UNKNOWN license (verify manually):", ", ".join(sorted(unknown)))
