"""WorkBuddy 用量解析（真实 token 版）。

数据源：`~/.workbuddy/projects/*/*.jsonl`
每个会话的 jsonl 里，每轮模型调用都带 `usage` 字段：
  {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M,
   "prompt_cache_hit_tokens": ..., "prompt_cache_miss_tokens": ...}
以及规范化版本 {"input_tokens": N, "output_tokens": M, "total_tokens": N+M, ...}

口径说明：
1. **同一行内会出现两个 usage 字典**（原始 API 返回 + 规范化版本），
   它们描述同一次调用，**只能取一个**，否则总量翻倍。
2. `prompt_tokens` 每轮携带完整历史（前 60 轮中 57 次单调递增），
   所以**逐轮累加 total_tokens 就是真实计费量**，不需要额外换算。
3. 因此本插件产出的是**真实 token（est=0）**，不是估算。

不再使用 `session_usage.credit_json`：那是**费用**字段（元），
与 token 的比值随模型费率浮动（0.31x~12.43x），无法作为 token 计量。
"""
from __future__ import annotations
import json, os, glob, time
from engine.common import ro_connect

KEY = 'workbuddy'
NAME = 'WorkBuddy'
DBP = os.path.expanduser('~/.workbuddy/workbuddy.db')
PROJECTS_ROOT = os.path.expanduser('~/.workbuddy/projects')
WATCH_PATHS = [DBP, PROJECTS_ROOT]


def _pick_usage(acc):
    """同一行可能有多个 usage 字典（同一次调用的不同表示），取字段最全/总量最大的一个。"""
    if not acc:
        return None
    return max(acc, key=lambda a: (int(a.get('total_tokens') or 0),
                                   len(a)))


def _walk_usage(obj, acc):
    """递归收集含 usage 特征的字典。"""
    if isinstance(obj, dict):
        if ('total_tokens' in obj
                or ('input_tokens' in obj and 'output_tokens' in obj)
                or ('prompt_tokens' in obj and 'completion_tokens' in obj)):
            acc.append(obj)
        for v in obj.values():
            _walk_usage(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk_usage(v, acc)


def _parse_jsonl_file(path):
    """解析一个会话 jsonl。

    返回 (daily, title, totals)：
      daily  : {day: {'total':int,'input':int,'output':int,'cache_read':int,'calls':int}}
      title  : AI 生成的标题
      totals : 全会话汇总 dict
    """
    daily = {}
    title = os.path.basename(path).replace('.jsonl', '')
    seen = set()          # 去重键，防止同一次调用跨行重复计入
    tot = {'total': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'calls': 0}

    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if 'total_tokens' not in line and 'input_tokens' not in line \
                    and 'prompt_tokens' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue

            ts = d.get('timestamp')
            if ts and isinstance(ts, (int, float)):
                day = time.strftime('%Y-%m-%d', time.localtime(ts / 1000))
            else:
                day = None

            if d.get('type') == 'ai-title':
                title = d.get('aiTitle') or title

            acc = []
            _walk_usage(d, acc)
            u = _pick_usage(acc)
            if not u:
                continue

            total = int(u.get('total_tokens') or 0)
            inp = int(u.get('input_tokens') or u.get('prompt_tokens') or 0)
            out = int(u.get('output_tokens') or u.get('completion_tokens') or 0)
            if total == 0:
                total = inp + out
            if total == 0:
                continue
            cache_r = int(u.get('cache_read_input_tokens')
                          or u.get('prompt_cache_hit_tokens') or 0)

            # 去重：优先 providerData.messageId（同一次调用唯一），否则退回行 id+总量
            pd = d.get('providerData') or {}
            key = pd.get('messageId') or pd.get('requestId') or (d.get('id'), total)
            if key in seen:
                continue
            seen.add(key)

            tot['total'] += total
            tot['input'] += inp
            tot['output'] += out
            tot['cache_read'] += cache_r
            tot['calls'] += 1

            if day:
                dd = daily.setdefault(day, {'total': 0, 'input': 0, 'output': 0,
                                            'cache_read': 0, 'calls': 0})
                dd['total'] += total
                dd['input'] += inp
                dd['output'] += out
                dd['cache_read'] += cache_r
                dd['calls'] += 1

    return daily, title, tot


def _session_meta():
    """从 workbuddy.db 读会话元信息（标题/模型/时间/工作目录）。只读，失败则返回空。"""
    meta = {}
    if not os.path.exists(DBP):
        return meta
    con = ro_connect(DBP)
    if not con:
        return meta
    try:
        for sid, title, model, created, last, cwd in con.execute(
                'select id, title, model, created_at, last_activity_at, cwd from sessions'):
            meta[sid] = {'title': title, 'model': model, 'created_at': created,
                         'last_activity_at': last, 'cwd': cwd}
    except Exception:
        pass
    con.close()
    return meta


def scan(full, need, mark):
    out = []
    daily_rows = []
    daily_files = []

    if not os.path.isdir(PROJECTS_ROOT):
        return {'sessions': out, 'daily': daily_rows, 'daily_files': daily_files}

    meta = _session_meta()
    files = glob.glob(os.path.join(PROJECTS_ROOT, '*', '*.jsonl'))

    for p in files:
        sid = os.path.basename(p).replace('.jsonl', '')
        try:
            st = os.stat(p)
        except OSError:
            continue
        fp = f'{st.st_mtime_ns}:{st.st_size}'
        if not need(full, p, fp):
            continue

        daily, title, tot = _parse_jsonl_file(p)
        if tot['total'] <= 0:
            mark(p, fp)
            continue

        m = meta.get(sid) or {}
        sess_title = (m.get('title') or title or '未命名会话')
        created = m.get('created_at') or 0
        last = m.get('last_activity_at') or 0

        # 会话级汇总行：真实 token（est=0）
        out.append({
            'agent': KEY, 'session_id': sid,
            'title': str(sess_title)[:60],
            'cwd': m.get('cwd') or '', 'model': m.get('model') or '',
            'provider': 'workbuddy',
            'created_at': created, 'last_activity_at': last,
            'input_tokens': tot['input'], 'output_tokens': tot['output'],
            'cache_read_tokens': tot['cache_read'], 'cache_write_tokens': 0,
            'total_tokens': tot['total'], 'cost': None, 'est': 0,
            'source_file': p,
        })

        # 按天行：source_file 用 会话级唯一路径（jsonl 本身天然唯一）
        for day, dd in daily.items():
            if dd['total'] <= 0:
                continue
            daily_rows.append({
                'agent': KEY, 'day': day, 'source_file': p,
                'tokens': dd['total'], 'est': 0,
            })

        daily_files.append(p)
        mark(p, fp)

    return {'sessions': out, 'daily': daily_rows, 'daily_files': daily_files}
