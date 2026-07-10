export function selectPythonCommand(env = process.env, platform = process.platform) {
  const override = env.PI_UNITY_DOCS_PYTHON;
  if (typeof override === "string" && override.trim() !== "") {
    return override;
  }
  return platform === "win32" ? "python" : "python3";
}
