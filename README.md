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
| Codex / Claude | 精确 | CC Switch `proxy_request_logs` + `usage_daily_rollups` | 代理层记录每次请求的 input/output/cache tokens。**两表互补必读**：明细表只保留近 30 天，30 天前的历史汇总在续期表，只读前者会丢全部历史 |
| Kimi Code | 精确 | `~/.kimi/sessions/**/wire.jsonl` | 本地 wire 协议含 token_usage |
| WorkBuddy | 精确 | `~/.workbuddy/projects/**/*.jsonl` | 每轮模型调用带 `usage`（prompt/completion/total_tokens），逐轮累加即真实计费量 |
| CodeBuddy | 估算 | `~/.codebuddy/projects/**/*.jsonl` | 文本长度估算 |
| 豆包工作 | 云端校准 | timeline API 百分比 × 固定系数 | 云端应用只给百分比；系数由**消息级对齐**实测（见 [`docs/DOUBAO.md`](docs/DOUBAO.md)） |
| 千问工作 | 估算 | `~/.qwenworkcn/projects/**/*.jsonl` | jsonl 无 usage 字段，文本长度估算 |
| ZCode | 精确 | `~/.zcode/cli/db/db.sqlite` | 本地 SQLite 用量记录 |

> 标"估算"的 Agent 本地没有精确 token 用量，按文本长度或费用折算，仅供参考。

### WorkBuddy 口径说明（踩坑记录）

WorkBuddy 的 jsonl 每轮调用都带真实 `usage`，但有两个坑：

1. **同一行内会出现两个 usage 字典**（原始 API 返回 + 规范化版本，字段分别是
   `prompt_tokens/completion_tokens` 与 `input_tokens/output_tokens`），
   它们描述同一次调用，**只能取一个**，否则总量恰好翻倍（实测 raw/nodedup = 2.00）。
2. `prompt_tokens` 每轮携带完整历史（实测前 60 轮 57 次单调递增），
   所以**逐轮累加 `total_tokens` 就是真实计费量**，无需换算。

不要用 `session_usage.credit_json` 折算 token：那是**费用**字段（元），
与 token 的比值随模型费率浮动（实测 0.31x ~ 12.43x），且约 65% 的会话该字段为 NULL。

### CC Switch 口径说明（踩坑记录）

CC Switch 的源库 `~/.cc-switch/cc-switch.db` 是**双表互补**设计，两张表**都必须读**：

| 表 | 覆盖范围 | 粒度 |
| --- | --- | --- |
| `proxy_request_logs` | **近 30 天**（滚动窗口，约 8/30 起） | 每次请求一行，含 `session_id` |
| `usage_daily_rollups` | **30 天前**（长期保留，可回溯至 5 月） | 每日 × 模型汇总，**无 session_id** |

只读明细表会**丢掉全部 30 天前的历史**（实测本机 codex 少算约 195 亿 token、
模型数从 10 掉到 4，日曲线起点从 5/18 缩到 8/30）。两表时间范围互补、互不重叠，
拼接时仍需按 `(agent, day)` 去重，防止将来窗口调整出现重叠日导致重复计数。

另外：proxy 的 `session_id` 粒度 ≠ Codex 真正的会话粒度——同一个会话可能先后用多个模型
（如 `gpt-5.6-sol` 切到 `gpt-6-sol`）共享同一 `session_id`。**聚合粒度必须到
`(agent, session_id, model)`**，否则只按 `(agent, session_id)` 聚合会让后来的模型覆盖先前的，
早期模型类型（如 `gpt-6-sol`）整个消失。

## 豆包工作插件

豆包是**云端应用**，用量接口只返回**百分比**，看板需要换算成 token。

| | |
|---|---|
| 数据源 | **只有 timeline API 一条**（cookie 认证，拉全部记录累加） |
| 换算系数 | **1% = 50 万 token**（`TOKENS_PER_PCT = 500_000`） |
| 当前实测 | 全时段 499.20% ⇒ **2.50 亿 token** |
| 精度 | `estimate=1`（靠百分比反算，非本地计数） |
| 取不到数据时 | **直接报错**，不静默降级 |

换算就一句：`总量 = timeline 百分比 × 50 万`。这个系数**只对账户总量成立**——
不要用它推算单个任务或某一天的用量（会低估长 agent 任务 3 倍以上），
也**不要与其他 Agent 横向比**（豆包记折后计价量，WorkBuddy 等记原始传输量，量纲不同）。

**这个 50 万 是怎么定出来的 → [`docs/DOUBAO.md`](docs/DOUBAO.md)**
**豆包校准与排障手册（操作向，含已作废错值清单）→ [`docs/SKILL-doubao-calibration.md`](docs/SKILL-doubao-calibration.md)**

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
adapters.py     旧版适配器
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
