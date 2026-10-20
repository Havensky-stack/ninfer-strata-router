# Local Codex guidance

## Workspace dependency fallback

When a skill asks you to call `load_workspace_dependencies` and that tool is not present
in your function list, do not search the disk and do not stop. Run this command:

```text
<CODEX_PRIMARY_RUNTIME>\dependencies\python\python.exe <CODEX_HOME>\tools\workspace-deps-mcp.py --print
```

For example, on Windows the two paths are usually:

```text
C:\Users\<you>\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
C:\Users\<you>\.codex\tools\workspace-deps-mcp.py
```

Use the Node.js executable and Node.js packages path it prints as the supported runtime.
`@oai/artifact-tool` is installed in that package directory. This output is equivalent to
the result of `load_workspace_dependencies`.
