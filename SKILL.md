---
# name 是机器标识符（与 GitHub 仓库名 Zaosusu/agent-usage-skill 对应），勿改；
# 对外花名：算力资源管理局 / 算力资源管理 / 算力 HR · Token 劳务派遣
name: agent-usage-skill
description: 监控你电脑上所有 AI Agent（Codex、Claude Code、Kimi、WorkBuddy、豆包工作等）的 token 用量。当用户问"我用了多少 token"、"我的额度怎么消耗的"、"哪个 AI 用最多"、"我的 AI 用量统计"时使用。
---

# 算力资源管理 · 算力 HR · Token 劳务派遣

本地多 AI Agent Token 用量统一监控 Skill（算力资源管理局出品）。插件化架构，任何 AI 编程工具都能接入。

## 什么时候用

用户问以下问题时触发：
- "我用了多少 token / AI 用量 / token 消耗"
- "我的额度怎么用的 / 哪个 Agent 用最多"
- "帮我统计一下 AI 编程工具的用量"
- "看看我这个月花了多少 token"

## 怎么用

### 1. 先扫描（第一次或定期跑）

```bash
python monitor.py scan
```

输出各 Agent 扫描状态和总用量。

### 2. 拿精简摘要（最常用）

```bash
python monitor.py summary
```

输出结构化 JSON（下为真实样例，已省略部分 agent）：

```json
{
  "ok": true,
  "summary": {
    "total_tokens": 18213307134,
    "real_tokens": 17963630443,
    "est_tokens": 249676691,
    "agent_count": 7,
    "agents": [
      { "key": "codex",     "name": "codex",     "total_tokens": 8567256657, "sessions": 33, "estimate": 0 },
      { "key": "kimi",      "name": "Kimi",      "total_tokens": 5152300033, "sessions": 138, "estimate": 0 },
      { "key": "workbuddy", "name": "WorkBuddy", "total_tokens": 3823534605, "sessions": 156, "estimate": 0 },
      { "key": "claude",    "name": "claude",    "total_tokens": 403153985,  "sessions": 2,   "estimate": 0 },
      { "key": "doubao",    "name": "豆包工作",   "total_tokens": 249599994,  "sessions": 13,  "estimate": 1 },
      { "key": "codebuddy", "name": "CodeBuddy", "total_tokens": 17385163,   "sessions": 6,   "estimate": 0 }
    ]
  }
}
```

直接解析 JSON 回答用户即可。`estimate=1` 的条目（豆包、千问）是换算值，
其余是本地真实计数。

### 3. 启动 Web 看板（可选）

```bash
python monitor.py serve
```

启动 http://127.0.0.1:8765/，有可视化图表。

⚠️ **铁律：启动时绝不要接 `| head`**（如 `python serve.py | head -5`）——
管道读满就关闭 ⇒ 服务被 SIGPIPE 干掉或卡死。症状极具迷惑性：
**端口仍在 LISTENING，但 curl 返回 HTTP 000**，看着像活着实际完全不响应。
正确姿势：后台启动 + 输出重定向到日志文件，然后**必须探活**：

```bash
curl -s -o /dev/null -w "HTTP %{http_code}" --max-time 8 "http://127.0.0.1:8765/"
```

必须拿到 `HTTP 200`；不是 200 就 `taskkill` 重来。详细排障见
`doubao-token-calibration` skill 第四节。

⚠️ **姊妹坑：`nohup ... &` 也活不过工具调用**。在 Agent 环境里用
`(nohup python serve.py --port 8765 &)` 启动，Bash 调用一结束子进程就被回收，
下次 `curl` 立刻 `ERR_CONNECTION_REFUSED`（同样"看着启动了、其实没了"）。
**正确做法：用工具的 `run_in_background: true` 启动**（拿到 task_id 常驻），
不要靠 `nohup`/`&` 兜底。判定标准始终只有一条：**实际 curl 拿到 200**。

## 已支持的 Agent

| Agent | key | 精确度 |
|---|---|---|
| Codex | `codex` | 精确（**本地 rollout 文件**，默认；可选 CC Switch） |
| Claude Code | `claude` | 精确（**本地 projects jsonl**，默认；可选 CC Switch） |
| Kimi | `kimi` | 精确（本地 wire 协议） |
| WorkBuddy | `workbuddy` | 精确（jsonl 真实 `usage`，去重后 est=0） |
| CodeBuddy | `codebuddy` | 精确（jsonl `message.usage`） |
| ZCode | `zcode` | 精确（本地 SQLite；当前表 0 行，尚未实际使用） |
| 豆包工作 | `doubao` | **估算**（云端只给百分比 × 50 万系数） |
| 千问工作 | `qwenworkcn` | 估算（文本长度） |

> **`codex` / `claude` 默认完全脱离 CC Switch**：由 `plugins/codex.py` +
> `plugins/claude.py` **直接解析本地会话文件**（`~/.codex/sessions/**`、
> `~/.claude/projects/**`），无需任何第三方代理、无需任何配置。
> **即使机器上装了 CC Switch，默认也不读它的库。**
>
> | 环境变量 | 作用 |
> |---|---|
> | *(都不设，默认)* | 纯原生采集（脱离 CC Switch） |
> | `AGENT_USAGE_USE_CCSWITCH=1` | 可选：改用 CC Switch 代理库（原生插件让位） |
> | `AGENT_USAGE_NO_CC_BACKFILL=1` | 连 rollup 兜底也不用 ⇒ 零 CC 接触 |
>
> 实测脱离 CC 时 Codex 反而更全（本地 358.8 亿 vs CC 266.2 亿，CC 漏采 84% 会话），
> claude 与 CC 比值 1.000。
> 安全兜底：开关开着但 `cc-switch.db` 不存在时会自动回退原生，不会两边都不产出。
> 切换来源会按「在岗 / 空转」双档清理历史残留行（`common.idle_daily_files()`），
> 避免 CC 明细行与本地行叠加虚高。
> 回归测试：`python tools/test_native_parity.py`（6 场景，含"CC 残留行兜底清理"）。
>
> ⚠️ **改完插件必须重启 `serve.py`**：它是常驻进程 + 内置 Watcher（每 5s 自动扫描入库），
> Python 不热加载已 import 的模块。用旧代码长跑的老进程会持续回写旧口径数据，
> 让命令行扫描结果看起来「改了没生效」甚至「数值还在涨」。排查前先重启服务。

标"估算"的 Agent 本地没有精确 token 用量，按文本长度或费用折算，仅供参考。
**豆包**是唯一需要系数校准的（云端只给百分比）；换算直接用 **1% = 50 万**，
这个系数怎么来的见 [`docs/DOUBAO.md`](docs/DOUBAO.md)，操作与排障见独立 skill
`doubao-token-calibration`（已固化，勿重推）。

## 注意事项

- 所有数据本地存储，不上传
- 标 `estimate=1` 的是估算值，不是精确 token 数
- ⚠️ **跨 agent 的 token 数不可横向比较**：WorkBuddy / Codex / Kimi 记的是**原始传输量**
  （每轮把整段上下文重发一遍的累计，input 占 99%+、output 不足 1%），
  豆包 / 千问记的是**折后计价量换算**（缓存命中的重复上下文近乎免费）。
  同一段工作实际在两边能差 5~8 倍。回答「哪个 AI 用最多」时，
  只能表述为**同一口径内的排序**，必须一并提示量纲差异，否则会得出误导结论。
  实测样本见 `docs/DOUBAO.md`「怎么用 · 边界二」。
- 豆包工作插件**必须**配 cookie（`~/.doubao-usage/config.json`，键 `doubao_cookie`）：
  配了才拉得到 timeline API 的**真实百分比**；没配或已过期会**直接报错**
  （不做本地估算兜底，报错文案会直接给出补 cookie 的路径与格式）。
  但**注意**：即使配了 cookie，API 也只返回**百分比**（如 `0.15%`），
  绝对 token 仍需乘系数换算。
- 新增 Agent 只需在 `plugins/` 丢一个 Python 文件（照 `_template.py` 改，
  记得设 `KEY` / `NAME` / `ESTIMATE` / `WATCH_PATHS`）
