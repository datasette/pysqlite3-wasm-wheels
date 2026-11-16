#!/usr/bin/env bash
# Build a pysqlite3 Pyodide wheel against one tagged version of SQLite.
#
#   scripts/build.sh 3.54.1
#
# The SQLite source comes from the matching "version-X.Y.Z" tag of the GitHub
# mirror. The finished wheel is written to wheels/X.Y.Z/.
#
# Requires: uv, git, make, a C compiler and tclsh (older SQLite releases need
# tclsh to generate the amalgamation).
set -euo pipefail

SQLITE_VERSION="${1:?usage: build.sh SQLITE_VERSION}"
SQLITE_REPO="${SQLITE_REPO:-https://github.com/sqlite/sqlite.git}"
PYODIDE_VERSION="${PYODIDE_VERSION:-314.0.8}"   # must match the Pyodide that loads the wheel
PYTHON_VERSION="${PYTHON_VERSION:-3.14}"        # must match that Pyodide's Python
PYSQLITE3_VERSION="${PYSQLITE3_VERSION:-0.6.2}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${BUILD_DIR:-$ROOT/build}"
mkdir -p "$BUILD"
cd "$BUILD"

# 1. Build tools, shared by every build
if [ ! -x venv/bin/pyodide ]; then
  uv venv --python "$PYTHON_VERSION" venv
  uv pip install --python venv/bin/python pyodide-build pip
fi
# shellcheck disable=SC1091
source venv/bin/activate
if [ ! -f ".xbuildenv-$PYODIDE_VERSION" ]; then
  pyodide xbuildenv install "$PYODIDE_VERSION"
  pyodide xbuildenv install-emscripten
  touch ".xbuildenv-$PYODIDE_VERSION"
fi

# 2. SQLite amalgamation, generated from the tagged source with the native
#    compiler (so this has to happen before the Emscripten environment is loaded)
SRC="$BUILD/sqlite-$SQLITE_VERSION"
rm -rf "$SRC"
git -c advice.detachedHead=false clone --quiet --depth 1 \
  --branch "version-$SQLITE_VERSION" "$SQLITE_REPO" "$SRC"
mkdir "$SRC/bld"
(
  cd "$SRC/bld"
  ../configure >configure.log 2>&1 || { tail -n 40 configure.log; exit 1; }
  make sqlite3.c >make.log 2>&1 || { tail -n 40 make.log; exit 1; }
)

# 3. pysqlite3, with the amalgamation dropped into its root. setup.py compiles
#    sqlite3.c into the extension when it finds it there
if [ ! -f "pysqlite3-$PYSQLITE3_VERSION.tar.gz" ]; then
  pip download "pysqlite3==$PYSQLITE3_VERSION" --no-deps --no-binary :all:
fi
PKG="$BUILD/pysqlite3-$PYSQLITE3_VERSION-sqlite-$SQLITE_VERSION"
rm -rf "$PKG"
mkdir "$PKG"
tar xzf "pysqlite3-$PYSQLITE3_VERSION.tar.gz" -C "$PKG" --strip-components 1
cp "$SRC/bld/sqlite3.c" "$SRC/bld/sqlite3.h" "$PKG/"

# 4. Build. No mmap in WASM. -fvisibility=hidden stops the wheel's SQLite
#    symbols resolving against the SQLite that is statically linked into Pyodide
cd "$PKG"
grep -v SQLITE_MAX_MMAP_SIZE setup.py >setup.py.new && mv setup.py.new setup.py
# shellcheck disable=SC1091
source "$(pyodide config get emsdk_dir)/emsdk_env.sh" >/dev/null 2>&1
CFLAGS="-fvisibility=hidden -DSQLITE_THREADSAFE=0" pyodide build .

# 5. Every build produces the same filename, so put the SQLite version in the
#    wheel filename's optional build tag slot, straight after the pysqlite3
#    version: pysqlite3-0.6.2-3.54.1-cp314-cp314-pyemscripten_2026_0_wasm32.whl
#    The rest of the name is the Python / Pyodide ABI the wheel is compatible with.
mkdir -p "$ROOT/wheels"
for whl in dist/*.whl; do
  name="$(basename "$whl")"
  prefix="pysqlite3-$PYSQLITE3_VERSION-"
  out="$ROOT/wheels/${prefix}${SQLITE_VERSION}-${name#"$prefix"}"
  cp "$whl" "$out"
  echo "Built $out"
done
