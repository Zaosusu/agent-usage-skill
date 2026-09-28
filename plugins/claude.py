# -*- coding: utf-8 -*-
"""插件：Claude Code —— 直接解析本地会话文件（不依赖 CC Switch）。

数据源：%USERPROFILE%/.claude/projects/<编码路径>/<session-uuid>.jsonl
        以及同目录 <session-uuid>/subagents/agent-*.jsonl

口径（与 CC Switch 对齐，实测 cc 的 request_id 100% 命中本地 message.id）：
  - 只认 message.usage 存在的行（assistant 回复）
  - 按 message.id 去重：同一条回复会因流式/重试在多行重复出现，取首次
    （实测 1467 条 usage 行 → 651 个唯一 id，不去重会虚高 2 倍以上）
  - 一次对话内模型可能切换（step-explore → water18-0910），
    model 取该行的 message.model，按 (session_id, model) 分开计

⚠️ **默认完全脱离 CC Switch**：本插件直接解析本地会话文件，是本项目
   claude 用量的**默认且唯一**来源。
   仅当显式设 `AGENT_USAGE_USE_CCSWITCH=1` **且** `~/.cc-switch/cc-switch.db` 存在时，
   才让位给 plugins/ccswitch.py（避免重复计数）。
"""
import os
import json
import pickle
import time
import collections

from engine.common import (expand, glob_files, claude_daily_files,
                           idle_daily_files, cc_rollup_fallback,
                           cc_source_active)

KEY = 'claude'
NAME = 'Claude Code'
ESTIMATE = False
WATCH_PATHS = ['%USERPROFILE%\\.claude\\projects']

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_path():
    d = os.path.join(_BASE, 'data')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, '.cache_claude.pkl')


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
    if not ts:
        return None
    try:
        import datetime
        s = str(ts).replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _decode_proj(name):
    """E--UserFile-Documents---- -> E:\\UserFile\\Documents\\...（尽量还原）"""
    if len(name) >= 2 and name[1] == '-':
        drive = name[0] + ':'
        rest = name[2:]
        parts = [p for p in rest.split('-') if p != '']
        return drive + os.sep + os.sep.join(parts) if parts else drive
    return name.replace('-', os.sep)


def _parse_file(path):
    """-> [(msg_id, ts_ms, model, in, out, cr, cw)]，已按 message.id 去重。"""
    out = []
    seen = set()
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                m = o.get('message') or {}
                u = m.get('usage')
                if not isinstance(u, dict):
                    continue
                i = u.get('input_tokens') or 0
                ou = u.get('output_tokens') or 0
                cr = u.get('cache_read_input_tokens') or 0
                cw = u.get('cache_creation_input_tokens') or 0
                if not (i or ou or cr or cw):
                    continue
                mid = m.get('id')
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                out.append((mid, o.get('timestamp'), m.get('model'),
                            i, ou, cr, cw))
    except OSError:
        return None
    return out


def scan(full, need, mark):
    # 默认启用（脱离 CC Switch）。仅当显式开启 CC Switch 采集开关、且其库确实存在时让位。
    if cc_source_active():
        return {'sessions': [], 'daily': [], 'daily_files': idle_daily_files(KEY)}

    root = expand(WATCH_PATHS[0])
    if not os.path.isdir(root):
        return {'sessions': [], 'daily': [], 'daily_files': idle_daily_files(KEY)}

    files = glob_files(root, '**/*.jsonl')
    cache = _load_cache()
    newcache = {}
    changed = 0
    recs_all = []
    sid_cwd = {}

    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        fp = '%d:%d' % (st.st_mtime_ns, st.st_size)
        ent = cache.get(p)
        if ent and ent[0] == fp:
            recs = ent[1]
        else:
            got = _parse_file(p)
            if got is None:
                continue
            recs = got
            changed += 1
        newcache[p] = (fp, recs)

        # session_id：主文件用文件名；subagents 用上一级目录名
        b = os.path.basename(p)
        parent = os.path.basename(os.path.dirname(p))
        sid = b[:-6] if b.endswith('.jsonl') else b
        if parent == 'subagents':
            sid = os.path.basename(os.path.dirname(os.path.dirname(p)))
            cwd = _decode_proj(os.path.basename(os.path.dirname(
                os.path.dirname(os.path.dirname(p)))))
        else:
            cwd = _decode_proj(parent)
        if sid:
            sid_cwd[sid] = cwd
        for r in recs:
            if r[0]:
                recs_all.append((sid, r))

    if changed:
        _save_cache(newcache)

    # 全局按 message.id 去重（同一 id 可能跨 subagents 文件重复）
    seen = {}
    for sid, r in recs_all:
        k = r[0] or (sid, r[1], r[3], r[4])
        if k not in seen:
            seen[k] = (sid, r)
    uniq = list(seen.values())

    agg = {}
    by_day = collections.defaultdict(lambda: collections.Counter())
    for sid, r in uniq:
        _, ts, model, i, ou, cr, cw = r
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
                'cwd': sid_cwd.get(sid, ''), 'model': model or '',
                'provider': 'claude',
                'created_at': ms, 'last_activity_at': ms,
                'input_tokens': 0, 'output_tokens': 0,
                'cache_read_tokens': 0, 'cache_write_tokens': 0,
                'total_tokens': 0, 'cost': None, 'est': 0,
                'source_file': os.path.normpath(root),
            }
            agg[k] = a
        a['input_tokens'] += i
        a['output_tokens'] += ou
        a['cache_read_tokens'] += cr
        a['cache_write_tokens'] += cw
        a['total_tokens'] += i + ou + cr + cw
        if ms < a['created_at']:
            a['created_at'] = ms
        if ms > a['last_activity_at']:
            a['last_activity_at'] = ms
        day = time.strftime('%Y-%m-%d', time.localtime(ms / 1000))
        by_day[day]['tokens'] += i + ou + cr + cw

    sessions = [s for s in agg.values() if s['total_tokens'] > 0]
    src = os.path.normpath(root)
    daily_rows = [{'day': d, 'tokens': c['tokens'], 'est': 0,
                   'source_file': src, 'agent': KEY} for d, c in by_day.items()]

    # 历史兜底：本地文件已清掉 / 被裁剪的日期，用 cc-switch 的长期 rollup 补差额。
    # 必须同时补 sessions（否则「总量」偏少而曲线却完整，两边对不上）。
    daily_files = claude_daily_files()
    local_by_day = {d: c['tokens'] for d, c in by_day.items()}
    roll_daily, roll_sess = cc_rollup_fallback(KEY, local_by_day)
    daily_rows += roll_daily
    sessions += roll_sess

    return {'sessions': sessions, 'daily': daily_rows,
            # 与 ccswitch 共用清理清单（见 common.claude_daily_files）
            'daily_files': daily_files}
