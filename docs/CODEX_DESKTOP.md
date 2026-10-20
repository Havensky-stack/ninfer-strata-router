# Codex Desktop 本地模型工具缺失

使用本地模型(例如 `coder390`)时,常见现象是演示文稿、文档或表格技能要求调用
`load_workspace_dependencies`,但会话的工具列表里只有 `exec_command`、`write_stdin`
和 `read_mcp_resource`,没有该工具。模型随后可能搜索整块磁盘,或反复尝试
`read_mcp_resource`,最终无法开始制作文件。

这是 Codex 客户端的动态工具注入问题,不是模型推理服务或本路由的协议问题。相关公开报告:

- [openai/codex#37718](https://github.com/openai/codex/issues/37718):自定义 provider
  新线程出现 `dynamic_tool_count=0`,本地工具被裁掉。
- [openai/codex#37075](https://github.com/openai/codex/issues/37075):自定义
  Responses provider 的 `codex_app` 工具被放进 deferred 组,模型没有 `tool_search`
  时无法发现。
- [openai/codex#39225](https://github.com/openai/codex/issues/39225):
  `load_workspace_dependencies` 在部分新线程中不可用。

另外,`codex_app` MCP 服务只实现了 `tools`,没有实现 `resources`。因此对
`codex_app` 调用 `read_mcp_resource` 或 `list_mcp_resources` 返回
`Mcp error: -32601: Method not found` 是预期结果,应当直接使用工具,不要读资源。

## 推荐兜底:全局 AGENTS.md + 路径脚本

这个办法不依赖 Codex 是否成功注入动态工具,已在本机 `coder390` 上验证:

1. 把 [workspace-deps-mcp.py](../examples/codex/workspace-deps-mcp.py) 放到
   `C:\Users\<you>\.codex\tools\workspace-deps-mcp.py`。
2. 把 [AGENTS.md](../examples/codex/AGENTS.md) 的内容加入
   `C:\Users\<you>\.codex\AGENTS.md`,并把 `<CODEX_PRIMARY_RUNTIME>` 和
   `<CODEX_HOME>` 换成实际路径。
3. 新建本地模型线程。模型发现 `load_workspace_dependencies` 不在函数列表中时,会运行:

```powershell
C:\Users\<you>\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe C:\Users\<you>\.codex\tools\workspace-deps-mcp.py --print
```

脚本输出 Node.js、`node_modules`、Python 和 Git 的路径,并检查
`@oai/artifact-tool` 是否存在。把它当作
`load_workspace_dependencies` 的返回值使用即可。

`~/.codex/AGENTS.md` 是 Codex 的全局指令文件,详见
[官方 AGENTS.md 文档](https://developers.openai.com/codex/guides/agents-md)。

## 可选:把脚本注册成 MCP 服务

同一份脚本也可以作为 stdio MCP 服务运行:

```toml
[mcp_servers.workspace_deps]
command = 'C:\Users\<you>\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
args = ['C:\Users\<you>\.codex\tools\workspace-deps-mcp.py']
startup_timeout_sec = 10
tool_timeout_sec = 30
```

在能正常加载 MCP 工具的线程里,它会提供
`mcp__workspace_deps__load_workspace_dependencies`。当前部分自定义 provider 版本仍会
把 MCP 工具整体隐藏,所以推荐同时保留上面的 AGENTS.md 兜底。

## 可选:给自定义模型补元数据

Codex 对目录中没有登记的模型 slug 会用 fallback ModelInfo,可能连带改变 shell 类型、
推理档位和工具模式。用 `codex debug models` 可以查看当前解析结果。给 `coder390`
在 `model_catalog_json` 中登记一项(可参考同一目录的模型条目,至少设置 `slug`、
`display_name`、`context_window`、`shell_type`、`supported_reasoning_levels`、
`tool_mode`、`use_responses_lite`)可以避免 fallback 元数据。

模型目录由 cc-switch 维护时,切换 provider 可能覆盖手写条目。把稳定版本放在独立文件,
并在 `config.toml` 的 `model_catalog_json` 指向它,更容易保留。

## 验证

让本地模型执行:

```text
Follow the workspace dependency fallback in AGENTS.md. Run the fallback command and
reply with exactly the Node.js packages path it printed.
```

预期返回类似:

```text
C:\Users\<you>\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules
```
