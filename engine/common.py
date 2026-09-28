# -*- coding: utf-8 -*-
"""engine/common.py — 插件公共工具（各插件 import 用）。"""
import os
import json
import glob
import sqlite3
import datetime


def expand(path):
    """展开 %USERPROFILE% / %USERNAME% / ~ 等占位符。"""
    if not path:
        return path
    p = path.replace('%USERPROFILE%', os.path.expanduser('~'))
    p = p.replace('%USERNAME%', os.environ.get('USERNAME', ''))
    return os.path.expanduser(p)


def glob_files(root, pattern):
    """递归 glob，返回排序后的绝对路径列表。"""
    if not os.path.isdir(root):
        return []
    return sorted(glob.glob(os.path.join(root, pattern), recursive=True))


def ro_connect(path):
    """只读打开 sqlite。"""
    try:
        return sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True)
    except sqlite3.Error:
        return None


def iso_to_ms(ts):
    """ISO8601 -> ms epoch；失败返回 None。"""
    if not ts:
        return None
    try:
        s = str(ts).replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def ts_to_ms(v):
    """兼容 ISO 字符串 / 秒 / 毫秒三种时间戳。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if v > 1e14:
            return int(v / 1e6)
        if v > 1e12:
            return int(v)
        return int(v * 1000)
    return iso_to_ms(v)


def estimate_tokens(text):
    """估算 token：CJK 字符按 1 token/字，其余按 4 字符/token。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            cjk += 1
        elif not ch.isspace():
            other += 1
    return cjk + (other + 3) // 4


def first_text(obj, maxlen=60):
    """递归找第一个较长的字符串作为标题。"""
    if isinstance(obj, dict):
        for v in obj.values():
            t = first_text(v, maxlen)
            if t:
                return t
    elif isinstance(obj, list):
        for v in obj:
            t = first_text(v, maxlen)
            if t:
                return t
    elif isinstance(obj, str):
        s = obj.strip()
        if len(s) >= 2:
            return s[:maxlen]
    return ''


def walk_json_for_usage(obj, acc):
    """递归收集含 input_tokens/output_tokens 的 usage 字典。"""
    if isinstance(obj, dict):
        if 'input_tokens' in obj and 'output_tokens' in obj:
            acc.append(obj)
        for v in obj.values():
            walk_json_for_usage(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            walk_json_for_usage(v, acc)


def extract_usage(u):
    """从 usage 字典提取 (input, output, cache_read, cache_write)，兼容多种字段命名。"""
    if not isinstance(u, dict):
        return (0, 0, 0, 0)
    inp = u.get('input_tokens') or u.get('prompt_tokens') or 0
    out_t = u.get('output_tokens') or u.get('completion_tokens') or 0
    cache_r = u.get('cache_read_input_tokens') or u.get('cache_read_tokens') or u.get('cached_tokens') or 0
    cache_w = u.get('cache_creation_input_tokens') or u.get('cache_creation_tokens') or 0
    return (inp, out_t, cache_r, cache_w)


def collect_strings(obj, out, cap=200000):
    """递归收集所有字符串（估算用）。"""
    if len(out) >= 5000:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            collect_strings(v, out, cap)
    elif isinstance(obj, list):
        for v in obj:
            collect_strings(v, out, cap)
    elif isinstance(obj, str):
        if obj.strip():
            out.append(obj[:cap])


def scan_jsonl_dir(root, pattern, parse_fn, full, need, mark):
    """通用 JSONL 目录扫描：glob 匹配 -> 指纹增量 -> parse_fn(path) -> 会话或 None。"""
    files = glob_files(root, pattern)
    out = []
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        fp = f'{st.st_mtime_ns}:{st.st_size}'
        if not need(full, p, fp):
            continue
        sess = parse_fn(p)
        if sess is not None:
            out.append(sess)
        mark(p, fp)
    return out


# ---------- 数据源开关：默认完全脱离 CC Switch ----------
# 设计原则：
#   **本项目默认不依赖 CC Switch。** codex / claude 由原生插件直接解析本地会话文件
#   （~/.codex/sessions/**、~/.claude/projects/**），无需任何第三方代理。
#   CC Switch 只是**可选**的替代来源，必须显式开启才会使用。
#
#   AGENT_USAGE_USE_CCSWITCH=1     改用 CC Switch 代理库采集 codex/claude
#                                  （原生插件自动让位，避免双计）
#   AGENT_USAGE_NO_CC_BACKFILL=1   连 rollup 历史兜底也不用 ⇒ 零 CC 接触
#
# 三者都不设（默认）⇒ 纯原生采集，行为与「机器上从未装过 CC Switch」完全一致。
CC_SWITCH_DB = os.path.expanduser('~/.cc-switch/cc-switch.db')
CODEX_ROOT = os.path.expanduser('~/.codex/sessions')
CLAUDE_ROOT = os.path.expanduser('~/.claude/projects')

_TRUTHY = ('1', 'true', 'yes', 'on')


def _env_flag(name):
    return os.environ.get(name, '').strip().lower() in _TRUTHY


def use_ccswitch():
    """是否用 CC Switch 作为 codex/claude 的采集源。**默认 False（脱离 CC Switch）**。"""
    return _env_flag('AGENT_USAGE_USE_CCSWITCH')


def cc_source_active():
    """CC Switch 是否真的在当班（开启开关 **且** 库确实存在）。

    ⚠️ 必须同时判「库存在」：若只判开关，用户开了 `AGENT_USAGE_USE_CCSWITCH=1`
    但库里没有数据（如已卸载、或路径不对），原生插件会静默让位、ccswitch 又空转
    ⇒ **两边都不产出，数据全丢**。加上存在性判断后，这种情况自动回退到原生。
    """
    return use_ccswitch() and os.path.exists(CC_SWITCH_DB)


def allow_cc_backfill():
    """是否允许读 cc-switch 的 rollup 表做**历史差额兜底**。默认允许。

    这是纯可选的增益：仅在 cc-switch.db 存在、且本地文件缺该日期时补差额；
    文件不存在时无任何副作用。要求零 CC 接触时设 `AGENT_USAGE_NO_CC_BACKFILL=1`。
    """
    return not _env_flag('AGENT_USAGE_NO_CC_BACKFILL')


def _np(p):
    return os.path.normpath(p)


def codex_daily_files():
    """codex 这个 agent 名下「所有可能出现 daily 行」的 source_file 全集。

    ⚠️ 必须让 ccswitch 插件与原生 codex 插件共用这份清单：
    否则「先无 CC（原生写行）→ 后装 CC（ccswitch 写行）」时，
    另一侧的历史行不会被清掉 ⇒ daily 曲线重复计数。
    先把两边都清掉再插入，无论谁运行，结果都唯一。
    """
    return [_np(CODEX_ROOT), _np(CC_SWITCH_DB), _np(CC_SWITCH_DB) + '#rollup']


def claude_daily_files():
    """claude 这个 agent 名下所有可能 source_file 的全集（同上）。"""
    return [_np(CLAUDE_ROOT), _np(CC_SWITCH_DB), _np(CC_SWITCH_DB) + '#rollup']


# ---------- 清理清单的「在岗 / 空转」双档语义 ----------
# 插件执行有固定先后（按 key 排序：ccswitch < claude < codex），core 对每个返回的
# daily_files 都执行「先删后插」（删除范围限定 agent 属于该插件自己的 key）。
#
#   在岗（本轮真的产出了数据）→ 返回 codex_daily_files()/claude_daily_files()
#       「共用清单」：既清自己的本地行，也清对方（CC）名下的历史行，
#       因为跨来源记录过同一份数据时必须双向清理，否则叠加。
#
#   空转（本轮不产出，如未开开关、或本地目录不存在）→ 用 idle_daily_files()
#       ① CC 在岗时 → 返回 []：本插件排在 ccswitch **之后**执行，
#          若此时返回共用清单，会把 ccswitch 刚插入的行顺手删光（先删后插所致）。
#       ② CC 不在岗时 → 返回该 agent 的共用清单：清掉「CC 部落」的历史残留。
#          这是必要的兜底 —— 例如从未用过 codex 原生 CLI（目录不存在），
#          但 CC 模式留下过 codex 的 CC 明细行；若不清理，切换后
#          codex 的 CC 残留行会一直虚高（没有任何插件会去删它）。
def idle_daily_files(agent):
    """空转插件该上报的清理清单（见上方双档语义）。"""
    if cc_source_active():
        return []
    if agent == 'codex':
        return codex_daily_files()
    if agent == 'claude':
        return claude_daily_files()
    return []


def cc_rollup_rows(app_type):
    """读 cc-switch 的 usage_daily_rollups，返回 [(day, model, in, out, cr, cw)]。

    cc-switch 不存在 / 表不存在 / 无法读取时一律返回 []（绝不抛异常）。
    """
    import sqlite3
    if not os.path.exists(CC_SWITCH_DB):
        return []
    try:
        con = sqlite3.connect(
            'file:' + CC_SWITCH_DB.replace('\\', '/') + '?mode=ro', uri=True)
    except sqlite3.Error:
        return []
    rows = []
    try:
        rows = con.execute(
            'select date, model, input_tokens, output_tokens, '
            'cache_read_tokens, cache_creation_tokens '
            'from usage_daily_rollups where app_type=?', (app_type,)).fetchall()
    except Exception:
        rows = []
    finally:
        try:
            con.close()
        except Exception:
            pass
    return [(r[0], r[1] or '', r[2] or 0, r[3] or 0, r[4] or 0, r[5] or 0)
            for r in rows if r[0]]


def cc_rollup_fallback(agent, local_by_day):
    """CC rollup 兜底：用 cc-switch 长期汇总表补齐本地文件缺失的用量。

    返回 (daily_rows, session_rows)，粒度与 plugins/ccswitch.py **完全对齐**：
      - daily  ：按 (agent, day)
      - session：按 (agent, day, model) 合成伪会话，session_id = 'rollup:<day>@<model>'

    ⚠️ 必须**同时**产出 sessions，不能只补 daily：
    看板「总 token / agent 总量 / 模型分布」都取自 sessions 表，
    若只补 daily 曲线，会出现「曲线面积 > 卡片总量」的自相矛盾
    （只补 daily 会漏掉 sessions 侧的量，使总量偏少）。

    local_by_day：{day: tokens} 本地文件已采到的每日量。分三种情况：
      1. 本地完全没有该日（lt == 0）→ 整日用 rollup 补
      2. 本地有该日、但 rollup 显著更大（>0.5%）→ 判定本地文件被**裁剪**，
         只补差额（按当日各 model 占比分摊），避免与本地重复计数
      3. 其余情况（含两边相等的多数日）→ 本地为准，跳过

    依据：多个重叠日两边用量几乎完全相等（同源同算法），
    因此「不相等」只可能来自本地文件不完整，而非口径差异。
    """
    import collections
    import time as _t
    if not allow_cc_backfill():
        return [], []
    rollup = cc_rollup_rows(agent)
    if not rollup:
        return [], []
    rsrc = _np(CC_SWITCH_DB) + '#rollup'
    by_day_model = collections.defaultdict(dict)
    by_day_tot = collections.Counter()
    for (day, model, i, o_, cr, cw) in rollup:
        tok = (i or 0) + (o_ or 0) + (cr or 0) + (cw or 0)
        if tok <= 0:
            continue
        mk = model or ''
        by_day_model[day][mk] = by_day_model[day].get(mk, 0) + tok
        by_day_tot[day] += tok

    local_by_day = local_by_day or {}
    daily_rows = []
    agg = {}
    for day, rt in by_day_tot.items():
        lt = local_by_day.get(day, 0)
        if lt == 0:
            gap = rt
        elif rt > lt * 1.005:
            gap = rt - lt          # ← 本地被裁剪，只补差额
        else:
            continue               # ← 本地完整（含两边相等），不重复计
        if gap <= 0:
            continue
        try:
            ts_ms = int(_t.mktime(_t.strptime(day, '%Y-%m-%d'))) * 1000
        except Exception:
            continue
        daily_rows.append({'day': day, 'tokens': gap, 'est': 0,
                           'source_file': rsrc, 'agent': agent})
        # 差额按当日各 model 占比分摊（末条吸收舍入余量，确保合计 == gap）
        models = list(by_day_model[day].items())
        assigned = 0
        for idx, (mk, mt) in enumerate(models):
            if idx == len(models) - 1:
                share = gap - assigned
            else:
                share = int(round(gap * mt / rt))
                assigned += share
            if share <= 0:
                continue
            k = (day, mk)
            s = agg.get(k)
            if s is None:
                s = {
                    'agent': agent,
                    'session_id': 'rollup:%s@%s' % (day, mk),
                    'title': mk.strip() or '历史汇总',
                    'cwd': '', 'model': mk, 'provider': agent,
                    'created_at': ts_ms, 'last_activity_at': ts_ms,
                    'input_tokens': 0, 'output_tokens': 0,
                    'cache_read_tokens': 0, 'cache_write_tokens': 0,
                    'total_tokens': 0, 'cost': None, 'est': 0,
                    'source_file': rsrc,
                }
                agg[k] = s
            s['total_tokens'] += share
    return daily_rows, list(agg.values())



def parse_claude_like_file(agent, path, root=None):
    """Claude CLI / CodeBuddy 类 JSONL 会话解析。root 用于生成相对 session_id。"""
    sid = os.path.splitext(os.path.basename(path))[0]
    first_ts = last_ts = None
    title = ''
    cwd = ''
    model = ''
    inp = out_t = cache_r = cache_w = 0
    first_user = True
    try:
        fh = open(path, 'r', encoding='utf-8', errors='replace')
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts = ts_to_ms(obj.get('timestamp'))
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            msg = obj.get('message')
            msg = msg if isinstance(msg, dict) else {}
            role = msg.get('role') or obj.get('role')
            if role == 'user' and first_user and not title:
                t = first_text(msg.get('content') or obj.get('content'), 60)
                if t:
                    title = t
                    first_user = False
            u = msg.get('usage')
            if not isinstance(u, dict):
                found = []
                walk_json_for_usage(obj, found)
                if found:
                    u = found[0]
            if isinstance(u, dict):
                i, o, cr, cw = extract_usage(u)
                inp += i
                out_t += o
                cache_r += cr
                cache_w += cw
            if not model:
                pd = obj.get('providerData')
                if isinstance(pd, dict):
                    model = pd.get('model') or pd.get('requestModelName') or ''
                if not model:
                    model = msg.get('model') or ''
            if obj.get('cwd') and not cwd:
                cwd = obj['cwd']
            if obj.get('type') == 'user' and first_user and not title:
                t = first_text(obj.get('message') or obj.get('content'), 60)
                if t:
                    title = t
                    first_user = False
    total = inp + out_t + cache_r + cache_w
    if total == 0 and first_ts is None:
        return None
    return {
        'agent': agent, 'session_id': sid, 'title': title or '未命名会话',
        'cwd': cwd, 'model': model, 'provider': '',
        'created_at': first_ts or 0, 'last_activity_at': last_ts or 0,
        'input_tokens': inp, 'output_tokens': out_t,
        'cache_read_tokens': cache_r, 'cache_write_tokens': cache_w,
        'total_tokens': total, 'cost': None, 'est': 0, 'source_file': path,
    }


def parse_estimate_file(agent, path, root):
    """估算型会话解析（豆包/千问等无 usage 字段的数据源）。"""
    tokens = 0
    first_ts = last_ts = None
    title = ''
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        fh = open(path, 'r', encoding='utf-8', errors='replace')
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                tokens += estimate_tokens(line)
                continue
            ts = iso_to_ms(obj.get('timestamp'))
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            if not title:
                title = first_text(obj, 60)
            texts = []
            collect_strings(obj, texts, 200000)
            tokens += estimate_tokens('\n'.join(texts))
    if tokens == 0 and first_ts is None:
        return None
    rel = os.path.relpath(path, root)
    sid = rel.replace(os.sep, '/')
    return {
        'agent': agent, 'session_id': sid, 'title': title or '会话 ' + sid[:20],
        'cwd': '', 'model': '', 'provider': '',
        'created_at': first_ts or int(st.st_mtime * 1000),
        'last_activity_at': last_ts or int(st.st_mtime * 1000),
        'input_tokens': 0, 'output_tokens': 0,
        'cache_read_tokens': 0, 'cache_write_tokens': 0,
        'total_tokens': tokens, 'cost': None, 'est': 1, 'source_file': path,
    }

