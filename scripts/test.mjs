// Load a built wheel into Pyodide under Node.js and check it really is
// running the expected SQLite version.
//
//   node scripts/test.mjs wheels/pysqlite3-....whl 3.54.1
//
// Needs the "pyodide" npm package, matching the Pyodide the wheel was built for.
import { loadPyodide } from "pyodide";
import fs from "fs";
import path from "path";

const [wheelPath, expected] = process.argv.slice(2);
if (!wheelPath || !expected) {
  console.error("usage: node test.mjs WHEEL SQLITE_VERSION");
  process.exit(2);
}
const wheel = path.basename(wheelPath);
const pyodide = await loadPyodide();
pyodide.FS.writeFile("/tmp/" + wheel, fs.readFileSync(wheelPath));
await pyodide.loadPackage("micropip");
const result = JSON.parse(
  await pyodide.runPythonAsync(`
import json, micropip, sqlite3
await micropip.install("emfs:/tmp/${wheel}")
import pysqlite3

db = pysqlite3.connect(":memory:")
db.execute("create table t (id integer primary key, body text)")
db.execute("insert into t (body) values ('hello')")
json.dumps({
    "pyodide_builtin_sqlite": sqlite3.sqlite_version,
    "pysqlite3": pysqlite3.sqlite_version,
    "sqlite_version()": db.execute("select sqlite_version()").fetchone()[0],
    "rows": db.execute("select * from t").fetchall(),
})
`)
);
console.log(JSON.stringify(result));
const ok =
  result.pysqlite3 === expected &&
  result["sqlite_version()"] === expected &&
  JSON.stringify(result.rows) === '[[1,"hello"]]';
if (!ok) {
  console.error(`FAILED: expected SQLite ${expected}`);
  process.exit(1);
}
console.log(`OK: SQLite ${expected}`);
