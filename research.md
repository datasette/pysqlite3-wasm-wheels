# Build a pysqlite3 WASM wheel for Pyodide with a chosen SQLite version

Requires: uv, Node.js, curl, unzip. Run everything in one empty directory.

## Build

```bash
SQLITE_VERSION=3.54.1
SQLITE_YEAR=2026          # the year in the sqlite.org download URL
SQLITE_NUM=3540100        # 3.54.1 as XYYZZPP
PYODIDE_VERSION=314.0.8   # must match the Pyodide that will load the wheel

# 1. Build tools. The host Python must match Pyodide's (3.14 for Pyodide 314)
uv venv --python 3.14 venv
source venv/bin/activate
uv pip install pyodide-build pip
pyodide xbuildenv install $PYODIDE_VERSION
pyodide xbuildenv install-emscripten
source "$(pyodide config get emsdk_dir)/emsdk_env.sh"

# 2. Sources: pysqlite3, plus the SQLite amalgamation dropped into its root.
#    setup.py compiles sqlite3.c into the extension when it finds it there
pip download pysqlite3==0.6.2 --no-deps --no-binary :all:
tar xzf pysqlite3-0.6.2.tar.gz
curl -sSO https://sqlite.org/$SQLITE_YEAR/sqlite-amalgamation-$SQLITE_NUM.zip
unzip -j sqlite-amalgamation-$SQLITE_NUM.zip '*/sqlite3.c' '*/sqlite3.h' -d pysqlite3-0.6.2

# 3. Build
cd pysqlite3-0.6.2
sed -i '/SQLITE_MAX_MMAP_SIZE/d' setup.py     # no mmap in WASM
CFLAGS="-fvisibility=hidden -DSQLITE_THREADSAFE=0" pyodide build .
ls -la dist/
cd ..
```

Result: `pysqlite3-0.6.2/dist/pysqlite3-0.6.2-cp314-cp314-pyemscripten_2026_0_wasm32.whl` (about 420KB).

`-fvisibility=hidden` is essential. Pyodide has SQLite 3.39.0 statically linked
into its main binary. Without the flag the wheel's SQLite symbols resolve
against that copy and the two versions get mixed: new SQL functions work but
`sqlite_version()` still returns 3.39.0.

## Test

Save as `test.mjs` in the same directory:

```javascript
import { loadPyodide } from "pyodide";
import fs from "fs";

const wheel = "pysqlite3-0.6.2-cp314-cp314-pyemscripten_2026_0_wasm32.whl";
const pyodide = await loadPyodide();
pyodide.FS.writeFile("/tmp/" + wheel, fs.readFileSync("pysqlite3-0.6.2/dist/" + wheel));
await pyodide.loadPackage(["micropip", "setuptools"]);
console.log(await pyodide.runPythonAsync(`
import json, micropip, sqlite3
await micropip.install("emfs:/tmp/${wheel}")
await micropip.install("datasette", pre=True)

import pysqlite3
from datasette.app import Datasette

db = pysqlite3.connect("/tmp/test.db")
db.execute("create table t (id integer primary key, body text)")
db.execute("insert into t (body) values ('hello')")
db.commit()
db.close()

ds = Datasette(["/tmp/test.db"], settings={"num_sql_threads": 0})
versions = (await ds.client.get("/-/versions.json")).json()
# unistr() was added in SQLite 3.50, so this fails on Pyodide's own 3.39.0
query = await ds.client.get(
    "/test/-/query.json?_shape=array&sql="
    "select+sqlite_version()+as+version,+unistr('caf%5Cu00e9')+as+unistr"
)
json.dumps({
    "pyodide_builtin_sqlite": sqlite3.sqlite_version,
    "pysqlite3": pysqlite3.sqlite_version,
    "datasette_versions": versions["sqlite"]["version"],
    "query": query.json(),
    "table_page_status": (await ds.client.get("/test/t")).status_code,
}, indent=2)
`));
```

Run it:

```bash
npm install pyodide@314.0.8
node test.mjs
```

Expected output (after Pyodide's package loading messages):

```json
{
  "pyodide_builtin_sqlite": "3.39.0",
  "pysqlite3": "3.54.1",
  "datasette_versions": "3.54.1",
  "query": [
    {
      "version": "3.54.1",
      "unistr": "caf\u00e9"
    }
  ],
  "table_page_status": 200
}
```

Both `pysqlite3` and `datasette_versions` must show the version you built. If
either shows 3.39.0, the symbol visibility flag was not applied.
