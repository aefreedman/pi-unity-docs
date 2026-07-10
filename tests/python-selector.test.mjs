import assert from "node:assert/strict";
import test from "node:test";

import { selectPythonCommand } from "../scripts/python-selector.mjs";

test("selects platform defaults when the override is unset", () => {
  assert.equal(selectPythonCommand({}, "win32"), "python");
  assert.equal(selectPythonCommand({}, "darwin"), "python3");
  assert.equal(selectPythonCommand({}, "linux"), "python3");
});

test("selects platform defaults when the override is empty", () => {
  assert.equal(selectPythonCommand({ PI_UNITY_DOCS_PYTHON: "" }, "win32"), "python");
  assert.equal(selectPythonCommand({ PI_UNITY_DOCS_PYTHON: "   " }, "darwin"), "python3");
});

test("preserves explicit interpreter paths, including spaces", () => {
  const pythonPath = "C:\\Program Files\\Python 3\\python.exe";
  assert.equal(selectPythonCommand({ PI_UNITY_DOCS_PYTHON: pythonPath }, "linux"), pythonPath);
});

test("the environment override has priority on every platform", () => {
  for (const platform of ["win32", "darwin", "linux"]) {
    assert.equal(selectPythonCommand({ PI_UNITY_DOCS_PYTHON: "/custom/python" }, platform), "/custom/python");
  }
});
