# 算力资源管理

> 算力 HR · Token 劳务派遣 —— 本地多 AI Agent Token 用量统一监控 Skill

监控你电脑上所有 AI Agent 的 token 用量，统一看板展示。**算力资源管理局**（花名：算力 HR · Token 劳务派遣）持续营业，替你看好每个 Agent 的 token 劳务合同。**插件化架构**——装了新 Agent，丢一个插件文件就能接入；**实时监控**——数据源一变，看板自动刷新；**Skill 接口**——写了 SKILL.md，任何 AI 都能自动发现并调用。

## 功能

- 总用量 KPI、每日趋势堆叠柱（含区间平均日用量）、各 Agent 对比、模型 TOP15、会话明细
- 区间平均日用量：今天 / 近 3 天 / 7 天 / 14 天 / 30 天 / 90 天 / 全部，自动算对应区间日均；
  默认**近 3 天**，并记住你的上次选择（localStorage）
- 实时推送（SSE），数据源变化即自动刷新
- 单文件 exe，无需安装 Python
- 本地运行，数据不上传
- **Agent API**：结构化 JSON 返回，AI 直接调用，不用解析复杂数据

## 快速开始

1. 下载 `dist/agent-usage-skill.exe`
2. 双击运行，自动打开 http://127.0.0.1:8765/
3. 看数

命令行参数：

```
agent-usage-skill.exe [--port 8765] [--no-open] [--interval 5] [--full]
```

## 内置 Agent 支持

| Agent | 精确度 | 数据源 | 说明 |
| --- | --- | --- | --- |
| Codex | 精确 | **本地 rollout 文件（默认）** / CC Switch（可选） | **默认脱离 CC Switch**，见下方「完全脱离 CC Switch」 |
| Claude Code | 精确 | **本地 projects jsonl（默认）** / CC Switch（可选） | 同上 |
| Kimi Code | 精确 | `~/.kimi/sessions/**/wire.jsonl` | 本地 wire 协议含 token_usage |
| WorkBuddy | 精确 | `~/.workbuddy/projects/**/*.jsonl` | 每轮模型调用带 `usage`（prompt/completion/total_tokens），逐轮累加即真实计费量 |
| CodeBuddy | 估算 | `~/.codebuddy/projects/**/*.jsonl` | 文本长度估算 |
| 豆包工作 | 云端校准 | timeline API 百分比 × 固定系数 | 云端应用只给百分比；系数由**消息级对齐**校准（见 [`docs/DOUBAO.md`](docs/DOUBAO.md)） |
| 千问工作 | 估算 | `~/.qwenworkcn/projects/**/*.jsonl` | jsonl 无 usage 字段，文本长度估算 |
| ZCode | 精确 | `~/.zcode/cli/db/db.sqlite` | 本地 SQLite 用量记录 |

> 标"估算"的 Agent 本地没有精确 token 用量，按文本长度或费用折算，仅供参考。

### WorkBuddy 口径说明

WorkBuddy 的 jsonl 每轮调用都带真实 `usage`，但有两个注意点：

1. **同一行内会出现两个 usage 字典**（原始 API 返回 + 规范化版本，字段分别是
   `prompt_tokens/completion_tokens` 与 `input_tokens/output_tokens`），
   它们描述同一次调用，**只能取一个**，否则总量恰好翻倍。
2. `prompt_tokens` 每轮携带完整历史，
   所以**逐轮累加 `total_tokens` 就是真实计费量**，无需换算。

不要用 `session_usage.credit_json` 折算 token：那是**费用**字段（元），
与 token 的比值随模型费率大幅浮动，且多数会话该字段为 NULL。

### 完全脱离 CC Switch（默认行为）

**本项目默认不依赖 CC Switch。** codex / claude 的用量由原生插件直接解析本地会话文件采集，
不需要任何第三方代理，也不需要任何配置。**即使机器上装了 CC Switch，默认也不读它的库。**

| 环境变量 | 作用 |
| --- | --- |
| *（都不设，默认）* | **纯原生采集** —— 直接读 `~/.codex/sessions/**`、`~/.claude/projects/**` |
| `AGENT_USAGE_USE_CCSWITCH=1` | 可选：改用 CC Switch 代理库采集（原生插件自动让位，避免双计） |
| `AGENT_USAGE_NO_CC_BACKFILL=1` | 连 rollup 历史兜底也不用 ⇒ **零 CC 接触** |

**为什么能替代**：CC Switch 本身就是「解析本地会话文件」的 —— 它的
`proxy_request_logs.data_source` 字段值就是 `codex_session` / `session_log`，
`request_id` 形如 `codex_session:<sid>:<seq>`。也就是说它不产生数据，只是转发本地数据，
因此本项目可以 1:1 复刻，且**覆盖更全**（CC 只收录经过它代理的会话，
而本地 rollout 文件保留了全部会话）。

**原生采集口径**：

- **Codex**：`~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl`，
  逐轮累加 `event_msg.payload.info.last_token_usage`（**增量**；
  `total_token_usage` 是累计值，跨文件会重复，不能用）。
  ⚠️ `ordinal` 是**文件内**序号（多个文件都会从头重新计数），**绝不能单独作去重键**；
  判重必须 `(session_id, ordinal, 四 token 值)` 全同。
- **Claude Code**：`~/.claude/projects/<编码路径>/<uuid>.jsonl`（含 `subagents/agent-*.jsonl`），
  累加 `message.usage`，**必须按 `message.id` 去重**（同一条回复会因流式/重试落盘多行，
  不去重会虚高 1 倍以上）。

**一个安全兜底**：若开了 `AGENT_USAGE_USE_CCSWITCH=1` 但 `cc-switch.db` 实际不存在
（已卸载、或路径不对），原生插件会**自动接管**，不会出现「两边都不产出、数据全丢」。
判据是「开关 **且** 库存在」，两者同时成立才让位。

> **rollup 历史兜底**：若曾安装过 `cc-switch.db`（哪怕后来卸载），其
> `usage_daily_rollups` 长期汇总表仍可用来补本地文件已清掉的历史差额
> （独立 `source_file` 前缀 `#rollup` 隔离，绝不与本地行相加）。
> 要完全零 CC 接触就设 `AGENT_USAGE_NO_CC_BACKFILL=1`。
> 全新机器没有这份兜底，只能采本地文件现有的范围——这是正常且正确的行为。

**切换来源时的残留清理**（重要，否则总量虚高）：

插件的清理清单分「在岗 / 空转」两档，判据见 `engine/common.py` 的 `idle_daily_files()`：

| 插件状态 | 返回的清理清单 | 原因 |
| --- | --- | --- |
| 在岗（产出了数据） | 该 agent 的**共用清单** `codex_daily_files()`/`claude_daily_files()` | 跨来源切换必须双向清理，否则原生行与 CC 行叠加 |
| 空转 + **CC 不在岗** | 同样返回共用清单 | 清掉历史「CC 部落」残留行；即使本地目录不存在也要清 |
| 空转 + **CC 在岗** | `[]` | 本插件排在 `ccswitch` **之后**执行，返回共用清单会误删它刚插入的行 |

> 为什么空转也要清理：`core.scan` 对每个上报的 `daily_files` 都执行「先删后插」，
> 而删除范围按 agent 限定。若从「CC 模式」切回「默认原生」，CC 明细行
> （`source_file` 指向 `cc-switch.db`）只有原生插件会去清；若原生插件因目录不存在
> 而早退且不上报清单，这些残留行会**永久躺着**、与本地行叠加虚高。

**回归测试**：`python tools/test_native_parity.py`
覆盖 6 个场景：① 默认脱离（**CC 库存在也不读**）② 显式启用 CC ③ 开关空转自动回退
④ 零 CC 接触 ⑤ 双向切换无残留 ⑥ CC 残留行兜底清理（本地目录不存在也要清）。

### CC Switch 口径说明

CC Switch 的源库 `~/.cc-switch/cc-switch.db` 是**双表互补**设计，两张表**都必须读**：

| 表 | 覆盖范围 | 粒度 |
| --- | --- | --- |
| `proxy_request_logs` | **近 30 天**（滚动窗口） | 每次请求一行，含 `session_id` |
| `usage_daily_rollups` | **30 天前**（长期保留） | 每日 × 模型汇总，**无 session_id** |

只读明细表会**丢掉全部 30 天前的历史**（模型种类与日曲线起点都会大幅缩水）。
两表时间范围互补、互不重叠，拼接时仍需按 `(agent, day)` 去重，
防止将来窗口调整出现重叠日导致重复计数。

另外：proxy 的 `session_id` 粒度 ≠ Codex 真正的会话粒度——同一个会话可能先后用多个模型
（如 `gpt-5.6-sol` 切到 `gpt-6-sol`）共享同一 `session_id`。**聚合粒度必须到
`(agent, session_id, model)`**，否则只按 `(agent, session_id)` 聚合会让后来的模型覆盖先前的，
早期模型类型（如 `gpt-6-sol`）整个消失。**原生插件与 ccswitch 插件同此口径。**

**跨来源清理一致性**：`engine/common.py` 的 `codex_daily_files()` / `claude_daily_files()`
返回「该 agent 名下所有可能的 `source_file` 全集」（本地 root、CC 库、CC 库 `#rollup`），
原生插件与 ccswitch 插件**共用同一份清单**。这样无论从「无 CC」切到「装 CC」还是反向卸载，
引擎都会先把两侧的历史行全部清掉再写，不会出现两条曲线叠加的重复计数。

## 豆包工作插件

豆包是**云端应用**，用量接口只返回**百分比**，看板需要换算成 token。

| | |
|---|---|
| 数据源 | **只有 timeline API 一条**（cookie 认证，拉全部记录累加） |
| 换算系数 | **1% = 50 万 token**（`TOKENS_PER_PCT = 500_000`） |
| 精度 | `estimate=1`（靠百分比反算，非本地计数） |
| 取不到数据时 | **直接报错**，不静默降级 |

换算就一句：`总量 = timeline 百分比 × 50 万`。这个系数**只对账户总量成立**——
不要用它推算单个任务或某一天的用量（会低估长 agent 任务 3 倍以上），
也**不要与其他 Agent 横向比**（豆包记折后计价量，WorkBuddy 等记原始传输量，量纲不同）。

**这个 50 万是怎么定出来的 → [`docs/DOUBAO.md`](docs/DOUBAO.md)**

需要配置 cookie 拿精确百分比时：`~/.doubao-usage/config.json`
```json
{"doubao_cookie": "sessionid=xxx; passport_csrf_token=xxx; ..."}
```

## Agent API（给 AI 调用）

所有接口返回结构化 JSON，AI 直接调用，不用解析复杂数据。

### 接口列表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/plugins` | 已接入的插件列表 |
| GET | `/api/data` | 完整聚合数据 |
| GET | `/api/refresh` | 触发一次增量扫描 |
| GET | `/api/stream` | SSE 实时推送 |
| **GET** | **`/api/agent/summary`** | **精简用量摘要（推荐）** |
| **GET** | **`/api/agent/agents/<key>/usage`** | **单个 agent 详细用量** |
| **GET** | **`/api/agent/config`** | **查看本地配置** |
| POST | `/api/agents` | 添加新 agent |
| DELETE | `/api/agents/<key>` | 删除 agent |

### 示例

```bash
# 拿摘要（AI 最常用）
curl http://127.0.0.1:8765/api/agent/summary

# 拿单个 agent 详情
curl http://127.0.0.1:8765/api/agent/agents/doubao/usage

# 检查豆包 cookie 是否已配置
curl http://127.0.0.1:8765/api/agent/config

# 触发扫描
curl http://127.0.0.1:8765/api/refresh
```

## 接入新 Agent

### 方式一：插件文件

在 `plugins/` 新建 `<name>.py`：

```python
KEY = 'myagent'
NAME = '我的 Agent'
ESTIMATE = False
WATCH_PATHS = ['%USERPROFILE%\\.myagent\\data']

def scan(full, need, mark):
    return [...]
```

重启即可。

> **插件返回 `daily` 时的硬性约定**：`daily` 表主键是 `(agent, day, source_file)`。
> 若你的插件在 `daily` 里用**同一个 `source_file`** 上报多条同一天的数据，它们会互相覆盖，
> 导致「总用量正确、但按天曲线偏低」。凡是**一个数据源内含多个会话**（如读一张 DB 表、
> 一个目录下的多个会话）的插件，`source_file` 必须**按会话唯一**（例如 `f'{DBP}#{session_id}'`）。
> 会话天然分散在不同文件的插件（多数 jsonl 类）不受影响。

### 方式二：API 自助接入

```bash
curl -X POST http://127.0.0.1:8765/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "MyAgent", "data_path": "C:/Users/you/.myagent/sessions/a.jsonl"}'
```

找不到数据时会返回结构化提示，告诉你去哪找。

## 架构

```
app.py          入口
serve.py        HTTP + SSE 服务 + Agent API
monitor.py      命令行扫描
engine/
  core.py       扫描调度、聚合
  registry.py   插件发现/加载
  watcher.py    实时监控
  onboard.py    新 agent 自助接入
  common.py     公共工具
plugins/        各 Agent 适配器
web/            ECharts 看板
```

## 兜底方案

| 场景 | 行为 |
|---|---|
| 某个插件崩了 | 其他插件正常扫描，崩的那个标 `status=error` 并打印原因 |
| **豆包 cookie 缺失/失效** | **明确报错**（不降级），该 agent 本次不更新，旧数据保留 |
| 服务挂了 | 数据还在 JSON 文件里，重启自动加载 |

豆包为什么不降级：本地任何途径都拿不到与 timeline 同一额度池的数字，
给一个会被误读成真实用量的数，比明确失败更糟——报错文案会直接告诉你补 cookie 的路径。

## 开发

```bash
python app.py          # 源码运行
python monitor.py scan # 命令行扫描
build.bat              # 打包 exe
```

Python 3.8+，无第三方依赖。

## License

MIT
