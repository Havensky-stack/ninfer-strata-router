# 协议兼容层

这个路由转发的是 **HTTP 请求体和响应**,不是"翻译 API"。它能同时提供 Chat Completions、
Responses、Anthropic Messages,是因为后端自己实现了这三种协议,路由只做透传(含 SSE)。

真正需要额外处理的是**同一份协议在不同实现之间的差异**。下面五层都是真实踩过的坑,每一层都附上
触发它的原始错误,便于以后遇到新差异时对照着加。

这些处理都是**按模型**开关的:`effort_map`、`strip_rejected` 只对配置了它的后端生效;没有配置的
后端保持严格透传,真正的客户端错误仍然会被如实报告。

---

## 一、路径前缀:`/v1` 该不该有

**差异**:Anthropic 客户端会把 `/v1/messages` 追加到 base URL;OpenAI Responses 客户端会把
`/responses` 追加到 base URL。于是同一台机器上的两个客户端需要**相反**的 base URL 写法。

| 客户端 | base URL | 实际请求 |
| --- | --- | --- |
| Claude Code | `http://host:8090` | `/v1/messages` ✅ |
| Claude Code(配成带 /v1) | `http://host:8090/v1` | `/v1/v1/messages` ❌ 404 |
| Codex | `http://host:8090/v1` | `/v1/responses` ✅ |
| Codex(配成不带 /v1) | `http://host:8090` | `/responses` ❌ 404 |

**处理**:转发前统一纠正路径——

```
缺 /v1 的      /responses        -> /v1/responses
              /messages         -> /v1/messages
              /chat/completions -> /v1/chat/completions
重复 /v1 的    /v1/v1/messages   -> /v1/messages
写对的        /v1/messages       -> 原样
```

路由自身的 `/router/*`、`/health`、`/models` 与单独的 `/v1` 不受影响。
纠正时会在日志里留一行,便于看出客户端写错了。

---

## 二、推理档位名

**差异**:OpenAI 给推理强度定义的名字是 `minimal` / `low` / `medium` / `high`,而不少本地
后端的聊天模板只暴露其中一部分。NInfer 的模板只有 `low` / `medium` / `xhigh`,并且**明确拒绝**
`minimal`、`high`、`max`:

```json
HTTP 400
{"error":{"code":"reasoning_effort_not_supported",
          "param":"output_config.effort",
          "message":"reasoning effort 'high' is not supported by the loaded chat template"}}
```

**档位可能出现在三个位置**,不同协议各不相同:

| 协议 | 字段 |
| --- | --- |
| Chat Completions | `"reasoning_effort": "high"` |
| Responses | `"reasoning": {"effort": "high"}` |
| Anthropic Messages | `"output_config": {"effort": "high"}` |

**处理**:按模型的 `effort_map` 在这三个位置改写:

```json
"effort_map": { "minimal": "low", "high": "xhigh", "max": "xhigh" }
```

选择 `high → xhigh` 是因为它是该模板的最高档,语义上对得上;如果你更在意速度而不是思考深度,
把 `high` 映射到 `medium` 或 `low` 也是合理的。

---

## 三、后端不认识的字段

**差异**:较严格的实现会对不认识的顶层字段直接 400,而客户端往往按"更完整"的 API 发送。

实测 NInfer 的相关错误(注意 `param` 都指向字段名):

```json
{"error":{"code":"unknown_parameter",            "param":"client_metadata",
          "message":"unknown parameter: client_metadata"}}
{"error":{"code":"parameter_not_supported",      "param":"prompt_cache_key",
          "message":"prompt_cache_key is not supported"}}
{"error":{"code":"include_not_supported",        "param":"include",
          "message":"additional response fields are not supported"}}
{"error":{"code":"parallel_tool_calls_not_supported","param":"parallel_tool_calls",
          "message":"parallel_tool_calls=false cannot be enforced"}}
{"error":{"code":"text_option_not_supported",    "param":"text",
          "message":"text.verbosity is not supported"}}
{"error":{"code":"reasoning_option_not_supported","param":"reasoning",
          "message":"reasoning.summary is not supported"}}
```

**处理**:只对两类错误动手——`unknown_parameter` 和以 `_not_supported` 结尾的错误码:

1. 从 `message` 里取点号路径(如 `text.verbosity`),否则用 `param`。
2. 删掉该字段(嵌套的只删子键,父级被删空时连父级一起删),重试。
3. 每次删除记一行日志,最多 8 次。

**取值错误不会被处理**:例如 `{"code":"invalid_value","param":"max_output_tokens"}` 会原样透传,
不会把参数悄悄删掉。这一条很重要——否则真正的客户端错误会被掩盖成"行为变了但没人报错"。

---

## 四、工具定义(`tools`)

**差异**:Responses API 的工具列表允许多种类型,而严格的实现通常只支持 function 工具,还要求
`strict: false`:

```json
{"error":{"code":"tool_type_not_supported",   "param":"tools",
          "message":"only function tools are supported"}}          // custom / hosted 工具
{"error":{"code":"strict_tools_not_supported","param":"tools",
          "message":"strict function schema enforcement is not supported"}}
```

**为什么不能像其它字段一样直接删**:整组 `tools` 被删掉后,模型仍然从系统提示里知道"有 Bash 这个
工具",于是把调用**当成普通文本**写出来(类似
`<tool_call><function=Bash><parameter=command>…`),客户端拿到的不是结构化调用,请求本身却是 200。
这种失败没有任何报错,极难排查。

**处理**:`tools` 只过滤、不删除——

- 只保留 `"type": "function"` 的条目;
- 把保留下来的条目里 `strict` 统一改成 `false`;
- 无可改的时候把后端的 400 原样透传,不做静默降级。

副作用:自定义自由格式工具(例如某些客户端的补丁工具)在只支持 function 工具的后端上不可用,
客户端会退回用普通命令完成同样的工作。

---

## 五、工具结果的形状

**差异**:Responses API 允许 `function_call_output.output` 是内容数组,而严格的实现只接受字符串:

```json
{"error":{"code":null,"param":"input",
          "message":"function_call_output output must be a string"}}
```

注意这里的 `param` 是 `input`、`code` 为空——它属于**值的类型错误**,不是"字段不认识",所以不归
第三层管。单独处理:

```json
"output": [{"type":"input_text","text":"file1\nfile2"}]
        ↓
"output": "file1\nfile2"
```

多个内容块按换行拼接;已经是字符串的原样保留。

---

## 适用的开关

| 层 | 生效条件 |
| --- | --- |
| 一、路径前缀 | 始终生效(对所有后端) |
| 二、档位映射 | 该模型配置了 `effort_map` |
| 三、字段摘除 | 该模型 `strip_rejected: true` |
| 四、tools 过滤 | 该模型 `strip_rejected: true` |
| 五、工具结果拼接 | 该模型 `strip_rejected: true` |

`strip_rejected` 的语义是"这个后端比较严格,允许为了兼容而裁剪请求"。对能力完整的后端不要开它,
这样它的报错仍然会如实返回给客户端。

---

## 怎么再加一层

遇到新的形状不一致时,按同样的思路:

1. **先复现**:用最小请求体打后端,拿到原始 400,看清 `code` / `param` / `message`。
2. **判断类型**:是"字段不认识"(第三层),"值类型不对"(第五层),还是"语义不同"(第二层)?
3. **尽量无损**:能转换就别删除;必须删除时只删最小的那一块。
4. **别掩盖真错**:只处理明确的兼容类错误码,取值错误一律透传。
5. **记日志**:每次改写留一行,便于事后解释"为什么客户端要的字段不见了"。
6. **只对该后端生效**:加配置开关,不要让所有后端都被改写。

---

## 兼容矩阵(实测)

在双 Tesla V100、Strata 与 NInfer-DFlash 上的实测结果:

| | Chat Completions | Anthropic Messages | Responses |
| --- | --- | --- | --- |
| Strata | ✅ | ✅(含 SSE) | ✅(含 SSE) |
| NInfer | ✅ | ✅(含 SSE) | ✅(含 SSE,流式工具调用) |

| 客户端 | 对接方式 | 备注 |
| --- | --- | --- |
| Claude Code | Anthropic Messages | base URL 不带 `/v1`;模型名用路由认得的别名 |
| Codex CLI | Responses | base URL 带 `/v1`;`wire_api = "responses"` |
| Open WebUI | OpenAI 兼容 | 连接地址 `.../v1`,模型下拉会出现全部模型 |
| 任意 OpenAI SDK | Chat / Responses | `reasoning_effort` 会被按后端映射 |
