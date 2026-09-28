# -*- coding: utf-8 -*-
"""插件：Codex —— 直接解析本地 rollout 会话文件（不依赖 CC Switch）。

数据源：%USERPROFILE%/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl

一个 thread（session_meta.session_id）可能落到多个文件：Codex 每次 resume / fork
都会新开一个文件，但 session_meta.session_id 保持不变。

⚠️ **ordinal 是「文件内」序号，不是 thread 全局序号**（实测：多个文件都从 ord=15
   重新开始）。所以**绝不能按 (session_id, ordinal) 去重**，否则会把不同分支里
   真实发生的消耗误删。

口径（实测单会话与 CC Switch 误差 0.02%）：
  - 只认 type=event_msg 且 payload.type=='token_count' 的行
  - 逐轮取 info.last_token_usage（增量）累加；
    info.total_token_usage 是**累计值**，跨文件会重复，不能直接用
  - 去重键 = (session_id, ordinal, 四个 token 值, total_tokens) 全同才判重复
    —— 只滤掉真正的「重放同一行」，不同分支即使 ordinal 相同也会保留
  - model 取最近一次 turn_context.payload.model
    ⇒ 同一会话切换模型时会拆成多条（gpt-5.6-sol → gpt-6-sol），
      这正是 CC Switch 按 (agent, session_id) 聚合时会丢模型的原因

⚠️ **默认完全脱离 CC Switch**：本插件直接解析本地 rollout 文件，是本项目
   codex 用量的**默认且唯一**来源。
   仅当显式设 `AGENT_USAGE_USE_CCSWITCH=1` **且** `~/.cc-switch/cc-switch.db` 存在时，
   才让位给 plugins/ccswitch.py（避免同一份数据双计）。
"""
import os
import json
import pickle
import time
import collections

from engine.common import (expand, glob_files, codex_daily_files,
                           idle_daily_files, cc_rollup_fallback,
                           cc_source_active)

KEY = 'codex'
NAME = 'Codex'
ESTIMATE = False
WATCH_PATHS = ['%USERPROFILE%\\.codex\\sessions']

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_path():
    d = os.path.join(_BASE, 'data')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, '.cache_codex.pkl')


def _load_cache():
    p = _cache_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return {}


def _save_cache(c):
    try:
        with open(_cache_path(), 'wb') as f:
            pickle.dump(c, f, protocol=4)
    except Exception:
        pass


def _iso_to_ms(ts):
    """ISO8601 -> ms epoch（转本地时区），失败返回 None。"""
    if not ts:
        return None
    try:
        s = str(ts).replace('Z', '+00:00')
        import datetime
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _parse_file(path):
    """解析单个 rollout 文件 -> [(sid, ordinal, ts_ms, model, in, out, cr, cw)]

    只对含关键字的三类行做 json.loads，其余行直接跳过（性能关键）。
    """
    recs = []
    sid = None
    cwd = ''
    model = None
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"session_meta"' in line:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get('type') == 'session_meta':
                        p = o.get('payload') or {}
                        sid = p.get('session_id') or p.get('id') or sid
                        cwd = p.get('cwd') or cwd
                        model = p.get('model') or model
                    continue
                if '"turn_context"' in line:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get('type') == 'turn_context':
                        m = (o.get('payload') or {}).get('model')
                        if m:
                            model = m
                    continue
                if '"token_count"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                p = o.get('payload') or {}
                if p.get('type') != 'token_count':
                    continue
                info = p.get('info') or {}
                lu = info.get('last_token_usage') or {}
                if not lu:
                    continue
                recs.append((
                    sid, o.get('ordinal'), o.get('timestamp'), model,
                    lu.get('input_tokens') or 0,
                    lu.get('output_tokens') or 0,
                    lu.get('cached_input_tokens') or 0,
                    lu.get('cache_write_input_tokens') or 0,
                ))
    except OSError:
        return None
    return recs, cwd


def _scan_cwd_map(root):
    """session_id -> cwd（用于补会话工作目录）。"""
    m = {}
    for p in glob_files(root, '**/*.jsonl'):
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if '"session_meta"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get('type') == 'session_meta':
                        pl = o.get('payload') or {}
                        sid = pl.get('session_id') or pl.get('id')
                        if sid and pl.get('cwd'):
                            m[sid] = pl['cwd']
                    break
        except OSError:
            continue
    return m


def scan(full, need, mark):
    # 默认启用（脱离 CC Switch）。仅当显式开启 CC Switch 采集开关、且其库确实存在时让位。
    if cc_source_active():
        # CC 在岗：本插件空转（排在 ccswitch 之后执行，返回共用清单会误删其刚插入的行）。
        return {'sessions': [], 'daily': [], 'daily_files': idle_daily_files(KEY)}

    root = expand(WATCH_PATHS[0])
    if not os.path.isdir(root):
        # 本地目录不存在也必须上报清理清单：历史上 CC 模式可能留下过本 agent 的
        # 行（source_file 指向 cc-switch.db），否则切换来源后这些残留行无人清理、总量虚高。
        return {'sessions': [], 'daily': [], 'daily_files': idle_daily_files(KEY)}

    files = glob_files(root, '**/*.jsonl')
    cache = _load_cache()
    newcache = {}
    allrec = []
    cwd_map = {}
    changed = 0

    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        fp = '%d:%d' % (st.st_mtime_ns, st.st_size)
        ent = cache.get(p)
        if ent and ent[0] == fp:
            recs, cwd = ent[1], ent[2]
        else:
            got = _parse_file(p)
            if got is None:
                continue
            recs, cwd = got
            changed += 1
        newcache[p] = (fp, recs, cwd)
        if recs:
            allrec.extend(recs)
            for r in recs:
                if r[0] and cwd:
                    cwd_map[r[0]] = cwd

    if changed:
        _save_cache(newcache)

    # ---- 去重：ordinal 是「文件内」序号，会跨文件重用，故必须连同 usage 值一起比对。
    #      只有五元组完全相同的行才判为重放副本；不同分支即使 ordinal 相同也保留。
    seen = set()
    uniq = []
    for r in allrec:
        sid = r[0]
        if not sid:
            continue
        k = (sid, r[1] if r[1] is not None else ('ts', r[2]), r[4], r[5], r[6], r[7])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    # ---- 聚合：(session_id, model) —— 同会话多模型必须拆开 ----
    agg = {}
    by_day = collections.defaultdict(lambda: collections.Counter())
    for r in uniq:
        sid, ordinal, ts, model, i, o_, cr, cw = r
        ms = _iso_to_ms(ts)
        if ms is None:
            continue
        k = (sid, model or '')
        a = agg.get(k)
        if a is None:
            a = {
                'agent': KEY,
                'session_id': ('%s@%s' % (sid, model)) if model else sid,
                'title': (model or '').strip() or ('会话 %s' % sid[:8]),
                'cwd': cwd_map.get(sid, ''), 'model': model or '', 'provider': 'codex',
                'created_at': ms, 'last_activity_at': ms,
                'input_tokens': 0, 'output_tokens': 0,
                'cache_read_tokens': 0, 'cache_write_tokens': 0,
                'total_tokens': 0, 'cost': None, 'est': 0,
                'source_file': os.path.normpath(root),
            }
            agg[k] = a
        a['input_tokens'] += i
        a['output_tokens'] += o_
        a['cache_read_tokens'] += cr
        a['cache_write_tokens'] += cw
        a['total_tokens'] += i + o_ + cr + cw
        if ms < a['created_at']:
            a['created_at'] = ms
        if ms > a['last_activity_at']:
            a['last_activity_at'] = ms
        day = time.strftime('%Y-%m-%d', time.localtime(ms / 1000))
        by_day[day]['tokens'] += i + o_ + cr + cw

    sessions = [s for s in agg.values() if s['total_tokens'] > 0]
    src = os.path.normpath(root)
    daily_rows = [{'day': d, 'tokens': c['tokens'], 'est': 0,
                   'source_file': src, 'agent': KEY} for d, c in by_day.items()]

    # ---- 可选的历史兜底 ----
    # 若本机残留 cc-switch.db，用它的 usage_daily_rollups（长期保留）补本地文件
    # 已清掉 / 被裁剪的日期差额。用独立 source_file（'#rollup'）隔离，绝不与本地行相加。
    # 必须同时补 sessions：看板「总量」取自 sessions 表，只补 daily 会对不上。
    daily_files = codex_daily_files()
    local_by_day = {d: c['tokens'] for d, c in by_day.items()}
    roll_daily, roll_sess = cc_rollup_fallback(KEY, local_by_day)
    daily_rows += roll_daily
    sessions += roll_sess

    return {
        'sessions': sessions,
        'daily': daily_rows,
        # 与 ccswitch 插件共用同一份清理清单（见 common.codex_daily_files）：
        # 无论谁来采，都先把两边的历史行都清掉再插，避免"原生→装CC"切换后重复计数。
        'daily_files': daily_files,
    }
