# 配置参考

配置文件默认是 `router.json`,与 `router.py` 同目录。启动时读取一次,改完要重启路由。
样例见 [../router.example.json](../router.example.json)。

---

## 顶层

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `listen.host` | string | 必填 | 监听地址。建议保持 `127.0.0.1`,公网访问靠隧道。 |
| `listen.port` | int | 必填 | 监听端口,例如 `8090`。 |
| `default_model` | string | 第一个模型 | 请求没写 `model`、或写了不认识的模型名时的兜底。 |
| `auth` | object | `{}` | 鉴权,见下。留空表示**不鉴权**,只在完全隔离的环境里可以这样。 |
| `limits` | object | `{}` | 体积、并发与限速,见下。 |
| `switch` | object | `{}` | 模型切换的等待窗口,见下。 |
| `models` | object | 必填 | 每个后端一项,见下。 |

---

## `auth`

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `keys` | string[] | `[]` | 所有合法的 API key。空数组表示关闭鉴权。 |
| `admin_keys` | string[] | `[]` | 管理员 key,应当是 `keys` 的子集。 |
| `admin_only_paths` | string[] | `["/router/load","/router/unload"]` | 只对管理员开放的路径。样例配置把 `/router/metrics` 也加进去了。 |
| `exempt_paths` | string[] | `["/health","/router/health"]` | 免鉴权路径,只回状态,不含内部信息。 |

客户端提交 key 的两种方式都支持:

```
Authorization: Bearer <key>
x-api-key: <key>
```

**管理员 key 的额外能力**

- 不参与按 key 限速(永远不会 429)。
- 不占 `max_inflight` 名额(不会因为并发满是 503)。
- 切换模型时使用更长的排空窗口(`switch.admin_drain_seconds`),到点后**强制接管**。
- 管理员请求等待期间,目标模型对新来的普通请求熔断,返回 `503`(`code: admin_priority`),
  这样管理员不会排在一条不断变长的队尾。

注意:没有"本机免鉴权"的旁路。隧道把公网流量从 `127.0.0.1` 转进来,任何本机旁路都等于没有鉴权。

---

## `limits`

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_body_bytes` | int | `33554432`(32 MiB) | 请求体上限,超出返回 `413`。 |
| `max_inflight` | int | `16` | 同时在处理的请求上限,超出返回 `503`(管理员不占名额)。 |
| `rate_limit.requests_per_minute` | number | `0`(关闭) | 每个 key 每分钟的请求数。 |
| `rate_limit.burst` | number | `10` | 令牌桶容量,即瞬时允许的突发请求数。 |

限速采用令牌桶:以 `requests_per_minute / 60` 每秒补充,最多攒到 `burst` 个。
例如 60/10 表示"瞬时最多 10 个,之后约 1 个/秒"。超出返回 `429`,带 `Retry-After`。

限速只统计真正转发给模型的请求;`/v1/models`、`/router/*`、`/health` 不计入,所以监控不会被限。

---

## `switch`

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `drain_seconds` | number | `45` | 普通请求要切换模型时,等待对方正在写的回答结束的时间;等不到则返回 `503 + Retry-After`,**不掐断**对方。 |
| `admin_drain_seconds` | number | 同 `drain_seconds` | 管理员请求的等待窗口;到点后仍然强制切换,被打断的调用方会收到一个明确的 `503`。 |

等待发生在模型仍可继续服务的时候,不占 GPU 之外的资源;同一时刻只会有一个加载/卸载在进行。

---

## `models.<名称>`

名称是内部标识,同时也是一个别名。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base` | string | 必填 | 后端地址,例如 `http://127.0.0.1:8082`。 |
| `start` | string[] | 无 | 启动后端的命令数组,例如 `["cmd","/c","examples\\strata-8082.bat"]`。没有它就不能按需启动。 |
| `cwd` | string | 路由自己所在目录 | 启动命令的工作目录。 |
| `aliases` | string[] | `[]` | 客户端可用的其它模型名(大小写不敏感)。 |
| `send_model` | string | 模型名称 | 转发时写进请求体的模型 id。后端需要的名字与客户端常用的名字不同时用它。 |
| `ready_path` | string | `/health` | 就绪探测路径。 |
| `ready_json` | object | `null` | 就绪响应必须包含的键值,例如 `{"loaded": true}`。为 `null` 时只要 HTTP 200 即视为就绪。 |
| `start_timeout_s` | number | `900` | 启动后等待就绪的最长时间。加载大模型要留足。 |
| `stop_timeout_s` | number | `120` | 停止后等待端口释放的最长时间。 |
| `stragglers` | string[] | `[]` | 后端引擎可能比"占端口的进程"活得更久(例如 Strata 的 server 会把引擎脱离出去)。这里列出它的可执行文件名,停止时和路由启动时会按 GPU 上的进程把残留清掉。 |
| `effort_map` | object | `{}` | 推理档位名的映射,例如 `{"minimal":"low","high":"xhigh","max":"xhigh"}`。只对配置了它的模型生效。 |
| `strip_rejected` | bool | `false` | 打开"后端拒绝哪个字段就处理哪个"的兼容层,详见 [PROTOCOLS.md](PROTOCOLS.md)。 |

### 一个后端项的完整例子

```json
"coder390": {
  "aliases": ["coder", "ninfer", "dflash"],
  "send_model": "coder390",
  "base": "http://127.0.0.1:8081",
  "ready_path": "/health",
  "ready_json": null,
  "effort_map": { "minimal": "low", "high": "xhigh", "max": "xhigh" },
  "strip_rejected": true,
  "start": ["cmd", "/c", "examples\\ninfer-DFlash.cmd"],
  "start_timeout_s": 900,
  "stop_timeout_s": 120,
  "stragglers": ["ninfer-serve.exe"]
}
```

### 就绪探测怎么选

- 后端在"端口开始 accept 时引擎已经加载完"(例如 NInfer 先绑端口、加载引擎、再 accept),
  那么 `/health` 返回 200 就代表可用,`ready_json` 留 `null` 即可。
- 后端是"先起服务、后加载模型"(例如 Strata 的 `/health` 会回 `loaded` 字段),就该用
  `"ready_json": {"loaded": true}`,否则路由会在模型还没加载完时就开始转发。

---

## 环境上的依赖

路由用到的外部命令:

| 命令 | 用途 |
| --- | --- |
| `netstat -ano -p TCP` | 找出占用某个端口的 PID(用于停止后端)。 |
| `taskkill /PID <pid> /T /F` | 结束后端进程树。 |
| `nvidia-smi --query-gpu=memory.used` | 停止后等显存真正释放。 |
| `nvidia-smi --query-compute-apps=pid,process_name` | 找出 GPU 上的残留进程(`stragglers`)。 |

这些都随 Windows 提供,不需要额外安装。

---

## 端口建议

同一台机器上跑多个 Web UI 或推理服务时,端口很容易撞车。常见冲突:

| 服务 | 常占用 |
| --- | --- |
| Open WebUI | `8080` |
| Strata 默认 | `8080`(建议改到 `8082`) |
| NInfer 默认 | `8081` |
| 本路由 | `8090` |

路由自身不占用后端端口,只按 `base` 去连它们;把后端放到哪由你决定,改 `base` 与启动脚本保持一致即可。
