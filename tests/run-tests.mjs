import { spawnSync } from "node:child_process";

import { selectPythonCommand } from "../scripts/python-selector.mjs";

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    throw result.error;
  }
  if (result.signal) {
    console.error(`Command terminated by ${result.signal}: ${command}`);
    process.kill(process.pid, result.signal);
    throw new Error(`Unable to propagate signal ${result.signal}`);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

const pythonCommand = selectPythonCommand();
run(process.execPath, ["--test", "tests/python-selector.test.mjs", "tests/supervised-process.test.mjs"]);
run(pythonCommand, ["-m", "py_compile", "scripts/unity_docs_db.py"]);
run(pythonCommand, ["tests/package-docsets.test.py"]);
