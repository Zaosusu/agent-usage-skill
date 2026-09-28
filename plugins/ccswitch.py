# -*- coding: utf-8 -*-
"""插件：CC Switch（权威用量源）。

CC Switch 是本机的 API 代理，所有 Codex / Claude Code 的请求都经过它，
proxy_request_logs 表记录了每一次请求的精确 token 数（含缓存命中），
这比逆向本地 rollout 文件准确得多。

数据源：~/.cc-switch/cc-switch.db
  - proxy_request_logs: created_at(秒), app_type(codex/claude), model,
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
    total_cost_usd, session_id
"""
import os
import re
import json
import glob
import time
import collections
from engine.common import ro_connect

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
    # 'C:\\Users\\xxx/.cc-switch/cc-switch.db'（反斜杠+正斜杠混用），
    # 会导致按 source_file 清理时匹配不到旧行（或匹配不一致），
    # 且 source_file 字段入库后跨机器/跨时间格式不一致。
    return os.path.normpath(os.path.expanduser(
        WATCH_PATHS[0].replace('%USERPROFILE%', os.path.expanduser('~'))))


def scan(full, need, mark):
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
    try:
        rows = con.execute(
            'select created_at, app_type, model, input_tokens, output_tokens, '
            'cache_read_tokens, cache_creation_tokens, total_cost_usd, session_id '
            'from proxy_request_logs'
        ).fetchall()
    except Exception:
        con.close()
        return {'sessions': [], 'daily': [], 'daily_files': []}
    con.close()

    # 按 (agent, day) 聚合
    by_day = collections.defaultdict(lambda: collections.Counter())
    # 按 session 聚合
    sess = {}
    for r in rows:
        (ts, app, model, inp, out, cr, cc, cost, sid) = r
        agent = APP_TO_AGENT.get(app)
        if not agent or not ts:
            continue
        tok = (inp or 0) + (out or 0) + (cr or 0) + (cc or 0)
        day = time.strftime('%Y-%m-%d', time.localtime(ts))
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

    daily_rows = []
    for (agent, day), c in by_day.items():
        daily_rows.append({
            'day': day, 'tokens': c['tokens'], 'est': 0,
            'source_file': dbp, 'agent': agent,
        })

    sessions = []
    cwd_map = _build_sid_cwd_map()
    for s in sess.values():
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
        'daily_files': [dbp],
    }
