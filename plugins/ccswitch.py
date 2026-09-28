# -*- coding: utf-8 -*-
"""插件：CC Switch（权威用量源）。

CC Switch 是 Codex / Claude Code 的 API 代理，所有请求都经过它，
token 数是精确计数（含缓存命中），比逆向本地 rollout 文件准确得多。

数据源：~/.cc-switch/cc-switch.db，**两张互补的表**，必须都读：
  - proxy_request_logs  = 近 30 天**明细**（每次请求一行），
      created_at(秒), app_type(codex/claude), model, input/output/cache_*,
      total_cost_usd, session_id
  - usage_daily_rollups = 30 天前滚出明细窗口的**每日×模型汇总**（长期保留），
      date, app_type, model, request_count, input/output/cache_* tokens, total_cost_usd

⚠️ 只读 proxy_request_logs 会丢掉全部 30 天前的历史（含该时段内所有模型）。
   两表时间范围互补、互不重叠：明细 ≥ 切分日，rollup < 切分日。
"""
import os
import re
import json
import glob
import time
import collections
from engine.common import ro_connect, cc_source_active

KEY = 'ccswitch'
NAME = 'CC Switch'
ESTIMATE = False
WATCH_PATHS = ['%USERPROFILE%\\.cc-switch\\cc-switch.db']

# app_type -> 上报用的 agent key（与内置 codex/claude 对齐）
APP_TO_AGENT = {
    'codex': 'codex',
    'claude': 'claude',
}

_CODEX_ROLLOUT_ROOT = os.path.expanduser('~/.codex/sessions')
_CLAUDE_PROJECTS_ROOT = os.path.expanduser('~/.claude/projects')
# 与 engine.common.codex_daily_files()/claude_daily_files() 返回的本地 root 完全同格式，
# 供本插件的 daily_files 清理清单共用（见 scan() 末尾注释）。
_CODEX_ROOT_NP = os.path.normpath(_CODEX_ROLLOUT_ROOT)
_CLAUDE_ROOT_NP = os.path.normpath(_CLAUDE_PROJECTS_ROOT)
_sid_cwd_cache = None


def _build_sid_cwd_map():
    """从 Codex rollout 文件和 Claude 项目目录提取 session_id -> cwd 映射。"""
    global _sid_cwd_cache
    if _sid_cwd_cache is not None:
        return _sid_cwd_cache
    m = {}
    # Codex: rollout-<ts>-<uuid>.jsonl
    if os.path.isdir(_CODEX_ROLLOUT_ROOT):
        for fp in glob.glob(os.path.join(_CODEX_ROLLOUT_ROOT, '**', '*.jsonl'), recursive=True):
            fname = os.path.basename(fp)
            mm = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', fname)
            if not mm:
                continue
            sid = mm.group(1)
            try:
                with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        try:
                            obj = json.loads(line.strip())
                            if obj.get('type') == 'session_meta':
                                cwd = obj.get('payload', {}).get('cwd', '')
                                if cwd:
                                    m[sid] = cwd
                                break
                        except:
                            continue
            except:
                pass
    # Claude: projects/<encoded_path>/<uuid>.jsonl
    if os.path.isdir(_CLAUDE_PROJECTS_ROOT):
        for proj_dir in os.listdir(_CLAUDE_PROJECTS_ROOT):
            proj_path = os.path.join(_CLAUDE_PROJECTS_ROOT, proj_dir)
            if not os.path.isdir(proj_path):
                continue
            decoded = proj_dir.replace('--', ':\\').replace('-', '\\')
            for jf in glob.glob(os.path.join(proj_path, '*.jsonl')):
                sid = os.path.basename(jf).replace('.jsonl', '')
                m[sid] = decoded
    _sid_cwd_cache = m
    return m


def _db_path():
    # 必须 normpath：Windows 下 ~/.cc-switch/cc-switch.db 展开会变成
    # 'C:\Users\xxx/.cc-switch/cc-switch.db'（反斜杠+正斜杠混用），
    # 会导致按 source_file 清理时匹配不到旧行，
    # 且 source_file 字段入库后格式不一致（跨机器/跨时间无法稳定比对）。
    return os.path.normpath(os.path.expanduser(
        WATCH_PATHS[0].replace('%USERPROFILE%', os.path.expanduser('~'))))


def scan(full, need, mark):
    # ⚠️ 本项目**默认脱离 CC Switch**：本插件默认不产出任何数据，
    #    codex/claude 由原生插件（plugins/codex.py、plugins/claude.py）
    #    直接解析本地会话文件采集。
    #    仅当显式设 AGENT_USAGE_USE_CCSWITCH=1 时，才启用本插件作为替代来源。
    #
    # 说明：空转时**不必**在此清理原本属于自己的历史行 ——
    # 本插件上报的 agent 是 codex/claude（子 agent），而引擎的删除范围
    # 按「插件 key」限定（见 core.scan 的 agents_in_rows），故此处返回任何
    # 清单都删不到 codex/claude 名下由本插件写过的历史行。
    # 真正负责清理这些残留的是：原生 codex/claude 插件空转时返回的
    # idle_daily_files()（见 engine/common.py 与 plugins/codex.py）。
    if not cc_source_active():
        return {'sessions': [], 'daily': [], 'daily_files': []}

    dbp = _db_path()
    if not os.path.exists(dbp):
        return {'sessions': [], 'daily': [], 'daily_files': []}

    st = os.stat(dbp)
    fp = f'{st.st_mtime_ns}:{st.st_size}'
    if not need(full, dbp, fp):
        return {'sessions': [], 'daily': [], 'daily_files': []}
    mark(dbp, fp)

    con = ro_connect(dbp)
    if con is None:
        return {'sessions': [], 'daily': [], 'daily_files': []}
    detail_rows = []
    rollup_rows = []
    try:
        detail_rows = con.execute(
            'select created_at, app_type, model, input_tokens, output_tokens, '
            'cache_read_tokens, cache_creation_tokens, total_cost_usd, session_id '
            'from proxy_request_logs'
        ).fetchall()
    except Exception:
        detail_rows = []
    try:
        # 30 天前滚出明细窗口的历史汇总（长期保留）
        rollup_rows = con.execute(
            'select date, app_type, model, request_count, input_tokens, output_tokens, '
            'cache_read_tokens, cache_creation_tokens, total_cost_usd '
            'from usage_daily_rollups'
        ).fetchall()
    except Exception:
        rollup_rows = []
    con.close()

    # 按 (agent, day) 聚合
    by_day = collections.defaultdict(lambda: collections.Counter())
    # 按 session 聚合
    sess = {}
    # 明细已覆盖的 (agent, day)，用于 rollup 去重
    detail_days = set()
    for r in detail_rows:
        (ts, app, model, inp, out, cr, cc, cost, sid) = r
        agent = APP_TO_AGENT.get(app)
        if not agent or not ts:
            continue
        tok = (inp or 0) + (out or 0) + (cr or 0) + (cc or 0)
        day = time.strftime('%Y-%m-%d', time.localtime(ts))
        detail_days.add((agent, day))
        by_day[(agent, day)]['tokens'] += tok
        by_day[(agent, day)]['cost'] += float(cost or 0)

        # 聚合粒度必须到 (agent, sid, model)：
        # proxy 的 session_id 粒度 ≠ codex 真正会话粒度——同一个 codex 会话在 proxy 层
        # 可能先后用多个模型（如 gpt-5.6-sol 切到 gpt-6-sol），共享同一个 session_id。
        # 若只按 (agent, sid) 聚合，后处理的 model 会覆盖前者，导致早期模型类型
        # （如 gpt-6-sol）作为独立维度彻底丢失、token 被错算进最后一个 model。
        key = (agent, sid or '', model or '')
        if key not in sess:
            sess[key] = {
                'agent': agent, 'session_id': '%s@%s' % (sid or '', model or ''),
                'title': (model or '').strip() or '未命名会话',
                'cwd': '', 'model': model or '', 'provider': 'ccswitch',
                'created_at': ts * 1000, 'last_activity_at': ts * 1000,
                'input_tokens': 0, 'output_tokens': 0,
                'cache_read_tokens': 0, 'cache_write_tokens': 0,
                'total_tokens': 0, 'cost': 0.0, 'est': 0,
                'source_file': dbp, '_raw_sid': sid or '',
            }
        s = sess[key]
        s['input_tokens'] += inp or 0
        s['output_tokens'] += out or 0
        s['cache_read_tokens'] += cr or 0
        s['cache_write_tokens'] += cc or 0
        s['total_tokens'] += tok
        s['cost'] += float(cost or 0)
        s['last_activity_at'] = max(s['last_activity_at'], ts * 1000)

    # 历史汇总表：只有「每日 × 模型」粒度，无 session_id / provider 明细，
    # 无法还原到具体会话，统一按 (agent, day, model) 聚合为一条伪会话。
    # 用独立的 source_file 前缀 '#rollup'，与明细的清理互不干扰。
    rollup_agg = {}
    for r in rollup_rows:
        (d, app, model, reqs, inp, out, cr, cc, cost) = r
        agent = APP_TO_AGENT.get(app)
        if not agent or not d:
            continue
        # 去重保护：明细已覆盖的日期不再累加（防止两表未来出现重叠日重复计数）
        if (agent, d) in detail_days:
            continue
        tok = (inp or 0) + (out or 0) + (cr or 0) + (cc or 0)
        if tok <= 0:
            continue
        k = (agent, d, model or '')
        if k not in rollup_agg:
            ts_ms = int(time.mktime(time.strptime(d, '%Y-%m-%d'))) * 1000
            rollup_agg[k] = {
                'agent': agent,
                'session_id': 'rollup:%s@%s' % (d, model or ''),
                'title': (model or '').strip() or '历史汇总',
                'cwd': '', 'model': model or '', 'provider': 'ccswitch',
                'created_at': ts_ms, 'last_activity_at': ts_ms,
                'input_tokens': 0, 'output_tokens': 0,
                'cache_read_tokens': 0, 'cache_write_tokens': 0,
                'total_tokens': 0, 'cost': 0.0, 'est': 0,
                'source_file': dbp + '#rollup', '_raw_sid': '',
            }
        s = rollup_agg[k]
        s['input_tokens'] += inp or 0
        s['output_tokens'] += out or 0
        s['cache_read_tokens'] += cr or 0
        s['cache_write_tokens'] += cc or 0
        s['total_tokens'] += tok
        s['cost'] += float(cost or 0)

    daily_rows = []
    for (agent, day), c in by_day.items():
        daily_rows.append({
            'day': day, 'tokens': c['tokens'], 'est': 0,
            'source_file': dbp, 'agent': agent,
        })
    # rollup 的按天行单独聚合（同样跳过与明细重叠的日期）
    rollup_by_day = collections.defaultdict(lambda: collections.Counter())
    for (agent, d, model), s in rollup_agg.items():
        rollup_by_day[(agent, d)]['tokens'] += s['total_tokens']
    for (agent, day), c in rollup_by_day.items():
        daily_rows.append({
            'day': day, 'tokens': c['tokens'], 'est': 0,
            'source_file': dbp + '#rollup', 'agent': agent,
        })

    sessions = []
    cwd_map = _build_sid_cwd_map()
    for s in list(sess.values()) + list(rollup_agg.values()):
        s['cost'] = round(s['cost'], 4)
        # 从 rollout 文件补 cwd
        if not s['cwd'] and s.get('_raw_sid') in cwd_map:
            s['cwd'] = cwd_map[s['_raw_sid']]
        s.pop('_raw_sid', None)
        if s['total_tokens'] > 0:
            sessions.append(s)

    return {
        'sessions': sessions,
        'daily': daily_rows,
        # 明细与汇总分属不同 source_file：引擎按 source_file 先删后插，各自独立清理。
        # ⚠️ 这里返回的是**与原生 codex/claude 插件共用**的清理清单
        #    （common.codex_daily_files / claude_daily_files 的同名内容）：
        #    从「无 CC（走原生插件）」切到「装 CC（走本插件）」时，
        #    原生插件留下的 daily 行必须由本插件一并清掉，否则两条曲线叠加、重复计数。
        'daily_files': [dbp, dbp + '#rollup', _CODEX_ROOT_NP, _CLAUDE_ROOT_NP],
    }
