#!/usr/bin/env bash
#
# Run the integration's test suite against one navimow-sdk-community release.
#
#   scripts/test-against-sdk.sh [VERSION | --wheel PATH] [-- PYTEST_ARGS...]
#
#   VERSION       a release on TestPyPI, for example 0.2.0a7. With no VERSION
#                 and no --wheel, the newest release on TestPyPI, pre-releases
#                 included.
#   --wheel PATH  install this built wheel instead of downloading from TestPyPI.
#   PYTEST_ARGS   appended to `pytest tests -q -p no:cacheprovider`, for
#                 example `-- -W error::DeprecationWarning`.
#
# What it does, in a temporary directory removed on exit:
#
#   1. a private environment: `uv venv --seed` with Python 3.14.2 or later
#      (what Home Assistant 2026.9.2 requires); every later command runs the
#      environment's own python by absolute path, so neither the shell's
#      python nor the repository's .venv is touched;
#   2. Home Assistant 2026.9.2 and pytest, under a constraints file that pins
#      homeassistant and the two dependency pins pi-main's container applies
#      to the SDK's dependencies (aiohttp 3.14.3, paho-mqtt 2.1.0), so the SDK
#      cannot move them;
#   3. the SDK: the wheel downloaded from TestPyPI alone (no PyPI fallback),
#      or the one given, installed under the same constraints with its
#      dependencies from PyPI; `pip check` then; the installed distribution
#      version and mower_sdk.__version__ must both equal the requested one;
#   4. the tests, from a clean `git archive HEAD` of the repository the
#      script is RUN IN (not the one it lives in), so an older checkout can
#      be tested with a newer script.
#
# The exit status is pytest's: the verdict. Needs uv, git, tar and the
# network; no Docker.

set -euo pipefail

HA_VERSION=2026.9.2
PYTHON_SPEC='>=3.14.2,<3.15'
DIST=navimow-sdk-community
TESTPYPI_SIMPLE=https://test.pypi.org/simple/
TESTPYPI_JSON="https://test.pypi.org/pypi/$DIST/json"

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "test-against-sdk: $*" >&2
    exit 2
}

say() {
    echo
    echo "==> $*"
}

version=""
wheel=""
pytest_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --wheel)
            [ $# -ge 2 ] || die "--wheel needs a path"
            wheel="$2"
            shift 2
            ;;
        --wheel=*)
            wheel="${1#--wheel=}"
            shift
            ;;
        --)
            shift
            pytest_args=("$@")
            break
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            die "unknown option: $1 (see --help)"
            ;;
        *)
            [ -z "$version" ] || die "only one version may be given"
            version="$1"
            shift
            ;;
    esac
done
[ -z "$wheel" ] || [ -z "$version" ] || die "give a version or --wheel, not both"

for tool in uv git tar; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"
done

if [ -n "$wheel" ]; then
    [ -f "$wheel" ] || die "no such wheel: $wheel"
    wheel="$(cd "$(dirname "$wheel")" && pwd)/$(basename "$wheel")"
    # The version is the second field of the wheel's file name.
    version="$(basename "$wheel")"
    version="${version#*-}"
    version="${version%%-*}"
    [ -n "$version" ] || die "cannot read a version from the wheel name"
fi

# The repository under test is the one the script is run in.
repo="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || die "run this from inside the git checkout to test"
tested_commit="$(git -C "$repo" rev-parse HEAD)"
if [ -n "$(git -C "$repo" status --porcelain)" ]; then
    echo "note: the working tree has uncommitted changes; HEAD is what gets tested" >&2
fi

script="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
script_commit="$(git -C "$(dirname "$script")" rev-parse HEAD 2>/dev/null || echo unknown)"

export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONDONTWRITEBYTECODE=1

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PY="$TMP/venv/bin/python"

say "script: $script (commit $script_commit)"
echo "tested repository: $repo"
echo "tested commit: $tested_commit"

say "private environment: Python $PYTHON_SPEC in $TMP/venv"
uv venv --seed --python "$PYTHON_SPEC" "$TMP/venv"
"$PY" --version

say "Home Assistant $HA_VERSION and pytest, pinned like pi-main"
cat > "$TMP/constraints.txt" <<CONSTRAINTS
homeassistant==$HA_VERSION
aiohttp==3.14.3
paho-mqtt==2.1.0
CONSTRAINTS
"$PY" -m pip install -c "$TMP/constraints.txt" "homeassistant==$HA_VERSION" pytest

if [ -z "$version" ]; then
    say "newest $DIST release on TestPyPI"
    version="$("$PY" -I - "$TESTPYPI_JSON" <<'PYTHON'
import json
import sys
import urllib.request

from packaging.version import Version

with urllib.request.urlopen(sys.argv[1], timeout=30) as response:
    releases = json.load(response)["releases"]
available = [
    version
    for version, files in releases.items()
    if files and not all(f.get("yanked") for f in files)
]
if not available:
    sys.exit("no release on TestPyPI")
print(max(available, key=Version))
PYTHON
)"
    echo "$version"
fi

if [ -z "$wheel" ]; then
    say "$DIST $version from TestPyPI"
    mkdir "$TMP/dist"
    "$PY" -m pip download --no-deps --only-binary :all: \
        --index-url "$TESTPYPI_SIMPLE" "$DIST==$version" -d "$TMP/dist"
    set -- "$TMP/dist"/*.whl
    [ $# -eq 1 ] && [ -f "$1" ] || die "expected one wheel in $TMP/dist, found: $*"
    wheel="$1"
fi

say "install $wheel with its dependencies from PyPI"
"$PY" -m pip install -c "$TMP/constraints.txt" "$wheel"
"$PY" -m pip check

say "installed version"
"$PY" -I - "$DIST" "$version" <<'PYTHON'
import importlib.metadata
import sys

from packaging.version import Version

import mower_sdk

dist, wanted = sys.argv[1], Version(sys.argv[2])
dist_version = importlib.metadata.version(dist)
module_version = mower_sdk.__version__
print(f"{dist} {dist_version}; mower_sdk.__version__ {module_version}")
if Version(dist_version) != wanted or Version(module_version) != wanted:
    sys.exit(f"expected {wanted} for both")
PYTHON

say "tests from a clean archive of $tested_commit"
mkdir "$TMP/src"
git -C "$repo" archive HEAD | tar -x -C "$TMP/src"
cd "$TMP/src"
status=0
"$PY" -m pytest tests -q -p no:cacheprovider ${pytest_args[@]+"${pytest_args[@]}"} || status=$?

say "result"
echo "script: $script (commit $script_commit)"
echo "tested commit: $tested_commit"
echo "sdk: $DIST $version"
if [ "$status" -eq 0 ]; then
    echo "tests: passed"
else
    echo "tests: FAILED (pytest exit status $status)"
fi
exit "$status"
