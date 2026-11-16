#!/usr/bin/env python3
"""
Find tagged SQLite versions that do not yet have a wheel in this repo, and
build them.

    scripts/build_missing.py list            # show versions still to build
    scripts/build_missing.py build --max 10  # build up to 10, newest first
    scripts/build_missing.py index           # rebuild wheels.json from wheels/

Standard library only. Building needs everything scripts/build.sh needs, plus
Node.js with the "pyodide" npm package installed (npm ci) for the test.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys

SQLITE_REPO = "https://github.com/sqlite/sqlite.git"
PYODIDE_VERSION = "314.0.8"
PYTHON_VERSION = "3.14"
PYSQLITE3_VERSION = "0.6.2"

ROOT = pathlib.Path(__file__).resolve().parent.parent
WHEELS_DIR = ROOT / "wheels"
INDEX = ROOT / "wheels.json"
FAILURES = ROOT / "failures.json"

# Only SQLite 3 tags: "version-3.54.1", "version-3.7.16.2"
TAG_RE = re.compile(r"^refs/tags/version-(3(?:\.\d+){1,3})$")
# pysqlite3-0.6.2-3.54.1-cp314-cp314-pyemscripten_2026_0_wasm32.whl
WHEEL_RE = re.compile(
    r"^pysqlite3-(?P<pysqlite3_version>[^-]+)-(?P<sqlite_version>\d[^-]*)"
    r"-(?P<python_tag>[^-]+)-(?P<abi_tag>[^-]+)-(?P<platform_tag>[^-]+)\.whl$"
)


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def tagged_versions():
    "All SQLite 3 versions tagged in the mirror, newest first"
    output = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", SQLITE_REPO],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    versions = set()
    for line in output.splitlines():
        match = TAG_RE.match(line.split("\t")[1])
        if match:
            versions.add(match.group(1))
    return sorted(versions, key=version_key, reverse=True)


def read_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_index():
    "Rewrite wheels.json to describe every wheel in wheels/, newest SQLite first"
    previous = {w["filename"]: w for w in read_json(INDEX, {}).get("wheels", [])}
    wheels = []
    for path in WHEELS_DIR.glob("*.whl"):
        match = WHEEL_RE.match(path.name)
        if not match:
            print(f"Skipping unrecognized wheel {path.name}", file=sys.stderr)
            continue
        content = path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        old = previous.get(path.name, {})
        unchanged = old.get("sha256") == sha256
        wheels.append(
            {
                "sqlite_version": match["sqlite_version"],
                "filename": path.name,
                "path": f"wheels/{path.name}",
                "pysqlite3_version": match["pysqlite3_version"],
                "pyodide_version": old["pyodide_version"] if unchanged else PYODIDE_VERSION,
                "python_tag": match["python_tag"],
                "abi_tag": match["abi_tag"],
                "platform_tag": match["platform_tag"],
                "size": len(content),
                "sha256": sha256,
                "built": old["built"] if unchanged else now(),
            }
        )
    wheels.sort(key=lambda w: version_key(w["sqlite_version"]), reverse=True)
    write_json(INDEX, {"wheels": wheels})
    return wheels


def missing_versions():
    built = {w["sqlite_version"] for w in read_json(INDEX, {}).get("wheels", [])}
    # A failure is only skipped while the versions that produced it are unchanged
    failed = {
        f["sqlite_version"]
        for f in read_json(FAILURES, {}).get("failures", [])
        if f.get("pysqlite3_version") == PYSQLITE3_VERSION
        and f.get("pyodide_version") == PYODIDE_VERSION
    }
    return [v for v in tagged_versions() if v not in built and v not in failed]


def run(*args, **kwargs):
    if "stdout" not in kwargs:
        print("+", " ".join(str(a) for a in args), flush=True)
    return subprocess.run(args, cwd=ROOT, **kwargs)


def build_one(version, log=None):
    """
    Build and test one version. Returns (wheel path or None, error or None).
    Output goes to the log file if there is one, which parallel builds need.
    """
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as fp:
            return _build_one(version, stdout=fp, stderr=subprocess.STDOUT)
    return _build_one(version)


def _build_one(version, **output):
    env = dict(
        os.environ,
        SQLITE_REPO=SQLITE_REPO,
        PYODIDE_VERSION=PYODIDE_VERSION,
        PYTHON_VERSION=PYTHON_VERSION,
        PYSQLITE3_VERSION=PYSQLITE3_VERSION,
    )
    for stale in WHEELS_DIR.glob(f"pysqlite3-*-{version}-*.whl"):
        stale.unlink()
    if run("scripts/build.sh", version, env=env, **output).returncode != 0:
        return None, "build failed"
    built = list(WHEELS_DIR.glob(f"pysqlite3-*-{version}-*.whl"))
    if len(built) != 1:
        return None, f"expected one wheel, found {len(built)}"
    if run("node", "scripts/test.mjs", built[0], version, **output).returncode != 0:
        built[0].unlink()
        return None, "test failed"
    return built[0], None


def record_failure(version, error):
    data = read_json(FAILURES, {"failures": []})
    data["failures"] = [f for f in data["failures"] if f["sqlite_version"] != version]
    data["failures"].append(
        {
            "sqlite_version": version,
            "error": error,
            "pysqlite3_version": PYSQLITE3_VERSION,
            "pyodide_version": PYODIDE_VERSION,
            "failed": now(),
        }
    )
    data["failures"].sort(key=lambda f: version_key(f["sqlite_version"]), reverse=True)
    write_json(FAILURES, data)


def commit(message, paths, push):
    run("git", "add", "--", *paths, check=True)
    run("git", "commit", "-m", message, check=True)
    if push:
        run("git", "pull", "--rebase", check=True)
        run("git", "push", check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="List versions with no wheel yet")
    list_parser.add_argument("--count", action="store_true", help="Just the number")
    build_parser = sub.add_parser("build", help="Build missing versions")
    build_parser.add_argument("versions", nargs="*", help="Default: all missing")
    build_parser.add_argument("--max", type=int, default=10)
    build_parser.add_argument("--jobs", type=int, default=1, help="Parallel builds")
    build_parser.add_argument("--commit", action="store_true", help="Commit each result")
    build_parser.add_argument("--push", action="store_true", help="Push each commit")
    sub.add_parser("index", help="Rebuild wheels.json")
    args = parser.parse_args()

    if args.command == "index":
        print(f"{len(update_index())} wheels in {INDEX.name}")
    elif args.command == "list":
        missing = missing_versions()
        print(len(missing) if args.count else "\n".join(missing))
    elif args.command == "build":
        versions = (args.versions or missing_versions())[: args.max]
        failed = []
        if args.jobs > 1:
            # The first build runs alone, since it may have to install the tools
            logs = ROOT / "build" / "logs"
            first = [(versions[0], build_one(versions[0], logs / f"{versions[0]}.log"))]
            pool = concurrent.futures.ThreadPoolExecutor(args.jobs)
            futures = {
                pool.submit(build_one, v, logs / f"{v}.log"): v for v in versions[1:]
            }
            results = itertools.chain(
                first,
                ((futures[f], f.result()) for f in concurrent.futures.as_completed(futures)),
            )
        else:
            results = ((v, build_one(v)) for v in versions)
        for done, (version, (wheel, error)) in enumerate(results, 1):
            print(f"\n=== SQLite {version}: {error or 'ok'} ({done}/{len(versions)}) ===", flush=True)
            if wheel:
                update_index()
                message, paths = f"Wheel for SQLite {version}", [wheel, INDEX]
            else:
                failed.append(version)
                record_failure(version, error)
                message, paths = f"SQLite {version} failed: {error}", [FAILURES]
            if args.commit or args.push:
                commit(message, paths, args.push)
        print(f"\nBuilt {len(versions) - len(failed)}, failed {len(failed)}: {failed}")


if __name__ == "__main__":
    main()
