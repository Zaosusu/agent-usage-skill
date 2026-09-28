# -*- coding: utf-8 -*-
"""豆包 token 系数校准器（一键复用，不要再手工推）。

背景：豆包 timeline API 只返回「占 7 天额度」的百分比（如 "0.15%"），
**本地与 API 都没有任何“绝对 token 数”字段**，无法做 1:1 硬锚点反推。
所以系数只能靠「本地会话轨迹重建 ÷ 同批 timeline 百分比」来测。

本脚本采用**消息级对齐法**（取代了此前两个被证伪的方法）：

  1. timeline 的每条记录 = 一条 user 消息（display_name 就是消息原文）
  2. 本地重建按**同一批消息**累加 token（一次 assistant 消息 = 一次模型调用，
     消耗 = 累积上下文 + 本条输出）
  3. 分子分母来自同一批消息 ⇒ 口径天然一致，不受「漏算了哪类用量」影响

  ⇒ agent 内容密度 = Σtok(命中) / Σpct(命中)
  ⇒ 账户下限     = Σtok(全量) / Σpct(全量)   （假设非 agent 内容零消耗）

  真值介于两者之间。配套做**双向匹配率**验证（正向 timeline→本地、
  反向本地→timeline），匹配率低就说明对齐是假的。

用法：
    python tools/calibrate_doubao.py                 # 只算，不改代码
    python tools/calibrate_doubao.py --apply         # 算出后直接改写 plugins/doubao.py
    python tools/calibrate_doubao.py --anchor 1.5e8  # 有硬锚点：一个 7 天窗口的绝对 token 数
    python tools/calibrate_doubao.py --root "D:/xxx/DoubaoWork/User Data/Default"

已作废的方法（不要再用）：
- **IndexedDB 时间窗硬锚点（旧算法 G）**：把 IndexedDB 里的真实 token 与
  「±5min 时间窗」内的 timeline 百分比相除。**已证伪**：窗口稍一放宽，系数就
  漂移数十倍 ⇒ 分子分母不是同一批事件；且该库里 `quota_source_code` 从未出现，
  「同额度池」无法证明；记录时间集中在少数几分钟内，是批量落盘时刻而非调用时刻。
- **tool schema 敏感性扫描**：tool schema 本地不落盘，K 值只是拍脑袋的
  假设值，会给结论引入假精度。现在改用消息级对齐，不再需要这个假设。
"""
import os
import re
import sys
import json
import time
import glob
import math
import urllib.request
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.common import estimate_tokens          # noqa: E402
from plugins.doubao import _extract_cookie, _fetch_timeline  # noqa: E402

# 模型上下文窗口（判决「物理不可能」的基准）。
# doubao-seed-2-1-turbo / -pro 均为 256K（火山引擎官方套餐文档），
# 本地配置里出现过的模型也只有这两个 256K 系列 + doubao-seed-character，
# **没有任何 1M 窗口模型** ⇒ agent 模式每次调用都在 256K 内。
# ⚠️ 必须按 256K 判决：agent 模式 max 上下文约 240K，本身即超 128K。
WINDOW_TOKENS = 256 * 1024


def default_root():
    """豆包工作数据根目录（通用路径，不含个人信息）。"""
    return os.path.expandvars(
        r'%USERPROFILE%\AppData\Local\DoubaoWork\User Data\Default'
    )


def sessions_dir(root):
    return os.path.join(root, '.doubaowork', 'agent_mode', 'workspace', '.sessions')


# ---------------------------------------------------------------- 本地重建

def _msg_tokens(o):
    return estimate_tokens(json.dumps(o, ensure_ascii=False))


def _user_text(o):
    c = o.get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                parts.append(str(b.get('text') or b.get('content') or ''))
            else:
                parts.append(str(b))
        return '\n'.join(parts)
    return str(c or '')


def _norm(s):
    return re.sub(r'\s+', '', s or '')


def rebuild_sessions(root, verbose=True):
    """逐会话重建 agent 循环，返回统计量、逐 turn 明细、按消息 key 的 token 汇总。

    模型（关键，别再简化）：
      - **一次 assistant 消息 = 一次模型调用**：消耗 = 该次请求的 input + output
        input = 此刻累积上下文（历史消息已含 tool result）+ system prompt
        output = 本条 assistant 内容
      - ⚠️ **不要在 tool 消息处再额外加一次"上下文重放"**：工具结果已经进了 ctx，
        下一次 assistant 调用的 input（ctx_before）里本就包含它。额外加一次等于
        把同一份 input 算两遍，会系统性高估约 2 倍（据此曾误得 113.6 万，
        又派生出错误的 120 万）。
      - system prompt 由调用方作为独立分量叠加（每次调用都要重发）。

    返回的 `by_key` 是消息级对齐法的分子：
      key = 归一化后的 user 消息前 40 字符（与 timeline display_name 对齐），
      value = 该消息引发的全部模型调用消耗 + system prompt。
    """
    sdir = sessions_dir(root)
    files = glob.glob(os.path.join(sdir, '**', 'trajectory.jsonl'), recursive=True)
    if not files:
        raise SystemExit('[!] 未找到 trajectory.jsonl，请检查 --root：' + sdir)

    sys_avg, n_sys = system_prompt_avg(root)

    stats = dict(sessions=0, turns=0, assistant=0, model_calls=0, tool_calls=0)
    call_cost = 0          # 模型调用成本：Σ(input=累积上下文 + output=本条)
    call_cost_sys = 0      # 同上，另加 system prompt
    turn_items = []        # (day, user_text, turn_token)
    by_key = {}            # 消息 key -> token（含 system prompt）
    by_key_ns = {}         # 消息 key -> token（不含）
    by_key_calls = {}      # 消息 key -> 摊到的模型调用次数（用于分型系数）
    ctx_list = []          # 每次调用时的上下文长度（用于物理合理性检验）
    output_cost = 0        # 输出侧合计（assistant 本条），用于推理 token 影响估算

    for fp in files:
        try:
            lines = open(fp, encoding='utf-8', errors='replace').read().splitlines()
        except Exception:
            continue
        stats['sessions'] += 1
        ctx = 0
        cur_day = time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(fp)))
        cur_text = ''
        cur_tok = 0
        # ⚠️ 两个必须遵守的建模要求：
        #  1) **连续 user 消息算一个「工作单元」**：用户会连发短消息（"你看看"→"还是这个啊"），
        #     模型只回一次，而 timeline 给每条都记一个 pct。若把整轮的消耗全算给最后一条，
        #     前面的 key 拿到 pct 却 token=0 ⇒ 把系数系统性拽低约 2~4 倍。
        #     现在：单元内所有 key 均摊随后 assistant 调用的消耗，Σ(单元内各 key) = 单元总消耗。
        #  2) **user 消息本身也是输入**，必须计入 ctx。
        open_keys = []
        burst_replied = False
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            role = o.get('role')
            t = _msg_tokens(o)
            if role == 'user':
                if cur_text:
                    turn_items.append((cur_day, cur_text, cur_tok))
                if burst_replied:          # 上一条已得到回复 ⇒ 开启新工作单元
                    open_keys = []
                cur_text = _user_text(o)
                k = _norm(cur_text)[:40] or None
                if k and k not in open_keys:
                    open_keys.append(k)
                cur_tok = 0
                burst_replied = False
                stats['turns'] += 1
                ctx += t
            elif role == 'assistant':
                stats['assistant'] += 1
                stats['model_calls'] += 1
                call_cost += ctx + t
                call_cost_sys += ctx + t + sys_avg
                cur_tok += ctx + t
                if open_keys:
                    share = (ctx + t + sys_avg) / len(open_keys)
                    share_ns = (ctx + t) / len(open_keys)
                    for k in open_keys:
                        by_key[k] = by_key.get(k, 0.0) + share
                        by_key_ns[k] = by_key_ns.get(k, 0.0) + share_ns
                        by_key_calls[k] = by_key_calls.get(k, 0.0) + 1.0 / len(open_keys)
                ctx_list.append(ctx)
                output_cost += t
                burst_replied = True
                ctx += t
            elif role == 'tool':
                # 只进上下文，不另计消耗（它的成本体现在下一次调用的 input 里）
                stats['tool_calls'] += 1
                ctx += t
            else:
                ctx += t
        if cur_text:
            turn_items.append((cur_day, cur_text, cur_tok))

    stats['call_cost'] = call_cost
    stats['call_cost_sys'] = call_cost_sys
    stats['turn_items'] = turn_items
    stats['by_key'] = by_key
    stats['by_key_ns'] = by_key_ns
    stats['by_key_calls'] = by_key_calls
    stats['ctx_list'] = ctx_list
    stats['output_cost'] = output_cost
    stats['sys_avg'] = sys_avg
    stats['n_sys'] = n_sys
    if verbose:
        print(f'[本地] 会话 {stats["sessions"]} / turn {stats["turns"]} / '
              f'assistant {stats["assistant"]} / 工具调用 {stats["tool_calls"]} / '
              f'模型调用 {stats["model_calls"]}')
        print(f'[本地] system prompt 均值 {sys_avg:,.0f} tok（{n_sys} 份）')
    return stats


def system_prompt_avg(root):
    """system prompt（assignment.md）平均 token —— 每次模型调用都要重发。"""
    files = glob.glob(os.path.join(sessions_dir(root), '**', 'system', 'assignment.md'),
                      recursive=True)
    if not files:
        return 0, 0
    vals = [estimate_tokens(open(f, encoding='utf-8', errors='replace').read()) for f in files]
    return sum(vals) / len(vals), len(vals)


# ---------------------------------------------------------------- 核心算法

def message_alignment(stats, timeline_entries):
    """消息级对齐法（现行主算法）。

    timeline 每条 = 一条 user 消息（display_name 就是消息原文）。
    把本地重建的 token 按同一批消息 key 汇总，再除以这批消息的 Σpct。

    返回 dict：
      hit_forward   : timeline 条目能在本地找到的比例（按条数 / 按 pct）
      hit_backward  : 本地消息能在 timeline 找到的比例
      by_key        : 命中的 key 数
      density       : 命中批的 Σtok/Σpct（agent 内容密度，万/1%）
      floor         : 全量 Σtok/全量 Σpct（账户下限，万/1%）
    """
    tl_pct = {}          # key -> pct
    for ts_ms, pct, name, code in timeline_entries:
        k = _norm(name)[:40]
        if k:
            tl_pct[k] = tl_pct.get(k, 0.0) + pct
    tl_total = sum(x[1] for x in timeline_entries)

    by_key = stats.get('by_key', {})
    by_key_ns = stats.get('by_key_ns', {})

    hit = set(by_key) & set(tl_pct)
    hit_tok = sum(by_key[k] for k in hit)
    hit_tok_ns = sum(by_key_ns[k] for k in hit)
    hit_pct = sum(tl_pct[k] for k in hit)

    # 正向：timeline 条目里有多少能对上本地
    fwd_n = sum(1 for ts_ms, pct, name, code in timeline_entries
                if _norm(name)[:40] in by_key)
    fwd_pct = sum(pct for ts_ms, pct, name, code in timeline_entries
                  if _norm(name)[:40] in by_key)
    # 反向：本地消息里有多少能对上 timeline
    bwd_n = sum(1 for k in by_key if k in tl_pct)

    return dict(
        by_key=len(hit),
        hit_tok=hit_tok, hit_tok_ns=hit_tok_ns, hit_pct=hit_pct,
        tl_total=tl_total,
        fwd_n=fwd_n, fwd_total=len(timeline_entries), fwd_pct=fwd_pct,
        bwd_n=bwd_n, bwd_total=len(by_key),
        density=hit_tok / hit_pct if hit_pct else 0,
        density_ns=hit_tok_ns / hit_pct if hit_pct else 0,
        floor=stats['call_cost_sys'] / tl_total if tl_total else 0,
        floor_ns=stats['call_cost'] / tl_total if tl_total else 0,
    )


def workload_split(stats, timeline_entries):
    """按任务长度分型给出各档系数 —— 证明「1% ≠ 固定 token 数」。

    机制：agent 长任务里绝大部分 input 是**重复的累积上下文**，云端按缓存折扣计费，
    所以「同样 1%，长任务代表的原始 token 远多于短对话」。账户级系数只是混合平均，
    **不能当作「1% = X 万 token」的定律用**（单任务/单日推算是常见误用）。

    返回 dict(buckets=[(label,n,Σpct,Σtok,coef)], r=双对数相关系数)。
    """
    tl_pct = {}
    for ts_ms, pct, name, code in timeline_entries:
        k = _norm(name)[:40]
        if k:
            tl_pct[k] = tl_pct.get(k, 0.0) + pct
    by_key = stats.get('by_key', {})
    calls = stats.get('by_key_calls', {})

    buckets = [('短对话    (<3 次调用)', 0, 3),
               ('中等      (3~10 次)', 3, 10),
               ('长 agent  (≥10 次)', 10, float('inf'))]
    out = []
    for label, lo, hi in buckets:
        ks = [k for k in by_key if k in tl_pct and lo <= calls.get(k, 0) < hi]
        sp = sum(tl_pct[k] for k in ks)
        st = sum(by_key[k] for k in ks)
        out.append((label, len(ks), sp, st, (st / sp if sp else 0.0)))

    # 双对数相关系数（对齐若为真应接近 1）
    pts = [(calls.get(k, 0), by_key[k], tl_pct[k]) for k in by_key if k in tl_pct]
    pts = [(c, t, p) for c, t, p in pts if t > 0 and p > 0]
    r = 0.0
    if len(pts) > 2:
        lx = [math.log(t) for _, t, _ in pts]
        ly = [math.log(p) for _, _, p in pts]
        mx = sum(lx) / len(lx)
        my = sum(ly) / len(ly)
        num = sum((a - mx) * (b - my) for a, b in zip(lx, ly))
        den = math.sqrt(sum((a - mx) ** 2 for a in lx) * sum((b - my) ** 2 for b in ly))
        r = num / den if den else 0.0
    return dict(buckets=out, r=r, n=len(pts))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='豆包 token 系数校准器（消息级对齐法）')
    ap.add_argument('--root', default=default_root(), help='豆包工作 Default 目录')
    ap.add_argument('--apply', action='store_true', help='把结果写回 plugins/doubao.py')
    ap.add_argument('--anchor', type=float, default=0,
                    help='硬锚点：一个 7 天窗口(100%%)的绝对 token 数，如 1.5e8')
    ap.add_argument('--round-to', type=int, default=100_000, help='取整粒度，默认 10 万')
    args = ap.parse_args()

    print('=' * 72)
    print('豆包 token 系数校准器（消息级对齐法）')
    print('=' * 72)

    # 1) timeline 百分比（唯一权威分母）
    cookie = _extract_cookie()
    api_pct, api_count, api_daily = _fetch_timeline(cookie)
    if not api_pct:
        raise SystemExit('[!] 拿不到 timeline 百分比：cookie 缺失或失效 '
                         '(~/.doubao-usage/config.json)')
    print(f'[API] timeline 总百分比 = {api_pct:.2f}%  ({api_count} 条, {len(api_daily)} 天)')
    print(f'[本地] root = {args.root}')

    # 2) 真硬锚点优先（用户能提供绝对配额时）
    if args.anchor > 0:
        exact = args.anchor / 100.0
        print()
        print(f'[硬锚点] 一个 7 天窗口 = {args.anchor:,.0f} token')
        print(f'  ⇒ 1% = {exact:,.0f} token（精确，其余算法作废）')
        if args.apply:
            _apply(exact, reason=f'硬锚点：7天窗口 {args.anchor:,.0f} token')
        return

    # 3) 本地重建
    stats = rebuild_sessions(args.root)

    # 4) 消息级对齐
    entries = _timeline_entries(cookie)
    if not entries:
        raise SystemExit('[!] 拿不到 timeline 逐条明细，无法做消息级对齐')

    m = message_alignment(stats, entries)

    print()
    print('-' * 72)
    print('① 双向匹配验证（匹配率低 ⇒ 对齐是假的，结论作废）')
    print(f'   正向 timeline→本地 : {m["fwd_n"]}/{m["fwd_total"]} 条 '
          f'({m["fwd_n"]/max(m["fwd_total"],1)*100:.1f}%)，'
          f'占 Σpct {m["fwd_pct"]/m["tl_total"]*100:.1f}%')
    print(f'   反向 本地→timeline : {m["bwd_n"]}/{m["bwd_total"]} 条 '
          f'({m["bwd_n"]/max(m["bwd_total"],1)*100:.1f}%)')
    print(f'   命中 key 数         : {m["by_key"]}')

    ws = workload_split(stats, entries)
    print()
    print('①·补 分型系数（同一个 1% 不等于同样的 token 数 —— 这是账户级系数的适用边界）')
    print(f'   {"任务类型":<24}{"key数":>7}{"Σpct":>10}{"Σtok":>12}{"隐含系数":>12}')
    print('   ' + '-' * 64)
    for label, n_, sp, st, coef in ws['buckets']:
        print(f'   {label:<24}{n_:>7}{sp:>9.1f}%{st/1e8:>10.3f}亿{coef/1e4:>10.1f}万')
    print(f'   双对数相关系数 r = {ws["r"]:.3f}（对齐若为真应接近 1；弱相关 ⇒ % 与原始 token 非严格正比）')
    print('   ⚠️ 长任务每 1% 代表的原始 token 远多于短对话：长 agent 循环里绝大部分 input 是')
    print('      重复的累积上下文，云端按**缓存折扣**计费。所以账户系数只是混合平均，')
    print('      不能用「1% = X 万」去推算单个任务/单天的实际 token 量（会低估长任务 3 倍以上）。')

    print()
    print('② 两个口径（真值介于两者之间）')
    print(f'   agent 内容密度 = Σtok(命中) / Σpct(命中)')
    print(f'      含 system prompt : {m["hit_tok"]:,.0f} / {m["hit_pct"]:.3f}% '
          f'= {m["density"]/1e4:.1f} 万/1%')
    print(f'      不含             : {m["hit_tok_ns"]:,.0f} / {m["hit_pct"]:.3f}% '
          f'= {m["density_ns"]/1e4:.1f} 万/1%')
    print(f'   账户下限 = Σtok(全量) / Σpct(全量)（假设非 agent 内容零消耗）')
    print(f'      含 system prompt : {stats["call_cost_sys"]:,.0f} / {m["tl_total"]:.3f}% '
          f'= {m["floor"]/1e4:.1f} 万/1%')
    print(f'      不含             : {stats["call_cost"]:,.0f} / {m["tl_total"]:.3f}% '
          f'= {m["floor_ns"]/1e4:.1f} 万/1%')

    # 账户级系数 = Σtok(全量) / Σpct(全量)，定义上等价于「raw 总量 ÷ 百分比总量」。
    # ⚠️ 不能用「命中密度 density」当总量系数：命中的 key 偏长任务，密度天然高于账户混合平均
    #    （见 ①·补 分型：长 agent 161.9 万 vs 短对话 21.1 万）。density 只用于交叉验证对齐为真。
    rec = int(round(m['floor'] / args.round_to) * args.round_to)

    print()
    print(f'   命中密度 : {m["density"]/1e4:.1f} 万/1%（只覆盖命中批，偏长任务 —— 不是总量系数）')
    print(f'   账户系数 : {m["floor"]/1e4:.1f} 万/1%（Σtok全量 / Σpct全量 ⇒ 用于全时段总量换算）')
    print(f'   推荐     : {rec/1e4:.0f} 万/1%（取整粒度 {args.round_to//10000} 万）')
    print(f'   ⇒ 全时段 {m["tl_total"]:.1f}% = {m["tl_total"]*rec/1e8:.2f} 亿 token（**原始** token，含重复上下文）')
    print(f'   ⚠️ 该值只保证「账户总量」对；单个任务/单天的实际量随任务长度浮动 7~8 倍（见 ①·补）。')

    # ③ 物理合理性检验：单次 API 调用不可能超过模型上下文窗口
    print()
    print('-' * 72)
    print(f'③ 物理合理性检验（独立于匹配率，只看 {WINDOW_TOKENS//1024}K 上下文窗口约束）')
    ctxs = sorted(stats.get('ctx_list') or [])
    if ctxs:
        n = len(ctxs)
        avg_ctx = sum(ctxs) / n
        sys_avg = stats.get('sys_avg') or 0
        vis_mean = stats['call_cost_sys'] / n      # 可见单次总计（含 sys）
        k = m['tl_total'] / n                       # 单次均值 = 系数 × k

        print(f'   agent 模式单次「调用时上下文」统计：'
              f'中位 {ctxs[n//2]:,} / p90 {ctxs[9*n//10]:,} / max {ctxs[-1]:,} / 均值 {avg_ctx:,.0f} tok')
        print(f'   可见单次总计（含 system prompt {sys_avg:,.0f}）: {vis_mean:,.0f} tok')
        print(f'   这是下限（未含 tool schema / 推理 token / 图片 / 非 agent 用量）')
        print()

        # 用 max 那次调用反推：全部漏算项每次能占多少预算
        leak_budget = WINDOW_TOKENS - ctxs[-1] - sys_avg
        coef_hi = (vis_mean + leak_budget) / k
        print(f'   ★ max 那次调用 {ctxs[-1]:,} + system {sys_avg:,.0f} = {ctxs[-1]+sys_avg:,.0f}，'
              f'距 {WINDOW_TOKENS:,} 窗口仅剩 {leak_budget:,.0f} tok')
        print(f'     这 {leak_budget:,.0f} 就是全部「看不见的消耗」每次能占的总预算')
        print(f'   ⇒ 系数上界 = ({vis_mean:,.0f} + {leak_budget:,.0f}) / k = '
              f'{coef_hi:,.0f}  ({coef_hi/1e4:.1f} 万/1%)')
        print(f'   ⇒ 这一条只给上界（能否定 120 万/230 万），定不了真值；真值取定义式')
        print()

        print(f'   {"系数":<10}{"全时段总量":>11}{"账户均值":>11}{"漏算/次":>10}'
              f'{"max那次真实":>13}  判决')
        print('   ' + '-' * 70)
        for nm, c in [('47.8 万', 47.8e4), ('50 万', 50e4), ('55 万', 55e4),
                      ('58 万', 58e4), ('59 万', 59e4), ('120 万', 120e4),
                      ('150 万', 150e4), ('230 万', 230e4)]:
            per = c * k
            leak = per - vis_mean
            max_real = ctxs[-1] + sys_avg + leak
            if max_real > WINDOW_TOKENS:
                note = '❌ 该次调用突破窗口'
            elif c > coef_hi:
                note = '❌ 超上界'
            elif c < 46.7e4:
                note = '⚠ 低于可见量，偏小'
            else:
                note = '✅ 在窗口内'
            mark = '  ← 现行（定义式）' if abs(c - 50e4) < 1 else ''
            print(f'   {nm:<10}{c*m["tl_total"]/1e8:>9.2f} 亿{per:>11,.0f}'
                  f'{leak:>10,.0f}{max_real:>13,.0f}  {note}{mark}')
        print()
        print(f'   读法：下界 = 可见消耗的密度（漏算只会让真值更大，不可能更小）；')
        print(f'         上界 = max 调用占满窗口后，留给漏算的预算只有 {leak_budget:,.0f} tok/次。')
        hi_wan = int(coef_hi / 1e3) / 10          # 向下取，避免显示成"59 万"（实际已超窗）
        print(f'   ⇒ 系数上界 {hi_wan} 万/1%（能否定 120 万 / 230 万，但定不了真值）')
        print(f'   ⚠️ 上界前提：本地统计到的调用数 = 账户全部消耗；若有未落盘的调用，上界相应放宽。')
        print(f'   ⚠️ 若将来发现 agent 会用 1M 窗口模型，此上界需放宽（当前本地无此证据）。')

    # ④ 已知偏差：推理 token 不可见（影响有界，需说明）
    print()
    print('-' * 72)
    print('④ 已知偏差：推理(thinking) token 本地不可见')
    in_side = stats['call_cost']
    out_side = stats.get('output_cost', 0)
    if out_side and in_side:
        tot = in_side + out_side
        share = out_side / tot
        print(f'   豆包 planner 用 doubao-seed-2-1-turbo（支持深度思考），'
              f'但 trajectory 只记 user/assistant/tool，无 reasoning 字段')
        print(f'   ⇒ 推理 token 消耗了但本地看不见，重建值天然偏低')
        print(f'   拆解：输入侧 {in_side:,.0f}（{in_side/tot*100:.1f}%）/ '
              f'输出侧 {out_side:,.0f}（{out_side/tot*100:.1f}%）')
        print(f'   推理只加在输出侧 ⇒ 总消耗增幅 = {share*100:.1f}% × (R-1)')
        print()
        print(f'   {"推理倍数 R":<14}{"总消耗增幅":>12}{"修正后系数":>14}')
        print('   ' + '-' * 40)
        for R in (1, 3, 10, 20):
            add = share * (R - 1)
            print(f'   R = {R:<11}{add*100:>11.1f}%{rec*(1+add)/1e4:>12.1f} 万')
        need = 230e4 / rec - 1
        print()
        print(f'   ⇒ 要到 230 万需总消耗 +{need*100:.0f}%，'
              f'即推理量须达可见输出的 {need/share+1:.0f} 倍 —— 不现实。')
        print(f'   ⇒ 推理缺失最多让系数 +7.5%（R=20），**不足以改变量级**。相比之下')
        print(f'      「任务长度」对「每 1% 值多少 token」的影响是 7.7 倍（见 ①·补），那才是主因。')

    cur = _current_coef()
    if cur:
        print()
        print(f'当前代码 TOKENS_PER_PCT = {cur:,}（{cur/1e4:.0f} 万/1%）')
        print(f'   换算：timeline {api_pct:.2f}% → {api_pct*cur/1e8:.2f} 亿')
        if abs(cur - rec) > args.round_to:
            print(f'   ⚠ 与推荐值差 {cur/rec:.2f} 倍，建议 --apply')

    if args.apply:
        reason = (f'消息级对齐：命中 Σtok {m["hit_tok"]:,.0f} ÷ Σpct {m["hit_pct"]:.3f}% '
                  f'= {m["density"]/1e4:.1f} 万；账户下限 {m["floor"]/1e4:.1f} 万；取中')
        _apply(rec, reason=reason)


def timeline_with_ts(cookie):
    """拉 timeline 逐条 [(ts_ms, pct, name, quota_code)]。

    display_name 就是用户消息原文，这是消息级对齐的锚。
    """
    from plugins.doubao import _TIMELINE_URL, _timeline_headers
    out = []
    cursor = None
    try:
        for _ in range(200):
            body = json.dumps({"cursor": cursor} if cursor else {}).encode()
            req = urllib.request.Request(_TIMELINE_URL, data=body,
                                         headers=_timeline_headers(cookie), method="POST")
            d = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("data", {})
            es = d.get("entries", [])
            if not es:
                break
            for e in es:
                u = e.get("usage", {})
                ps = u.get("quota_source", {}).get("display_text", "0%")
                p = 0.005 if "<" in ps else float(ps.replace("%", "") or 0)
                out.append((u.get("occurred_at_ms", 0), p,
                            u.get("display_name") or "",
                            (u.get("quota_source") or {}).get("quota_source_code", "")))
            cursor = d.get("next_cursor")
            if not d.get("has_more") or not cursor:
                break
    except Exception:
        return out
    return [x for x in out if x[0] > 0]


def _current_coef():
    m = re.search(r'^TOKENS_PER_PCT = ([\d_]+)',
                  open(os.path.join(ROOT, 'plugins', 'doubao.py'), encoding='utf-8').read(),
                  re.M)
    return int(m.group(1).replace('_', '')) if m else None


def _timeline_entries(cookie):
    """拉 timeline 逐条 [(ts_ms, pct, name, quota_code)]，供消息级对齐。失败返回 []。"""
    if not cookie:
        return []
    return timeline_with_ts(cookie)


def _apply(value, reason):
    path = os.path.join(ROOT, 'plugins', 'doubao.py')
    txt = open(path, encoding='utf-8').read()
    # 注意：必须匹配 [\d_]+，正则写 \d+ 只会吃掉 "1_200_000" 里的 "1"，
    # 替换后会变成 "5100000_200_000" 这种垃圾（已踩过）。
    pretty = f'{int(value):_}'
    new = re.sub(r'^TOKENS_PER_PCT = [\d_]+', f'TOKENS_PER_PCT = {pretty}',
                 txt, count=1, flags=re.M)
    if new == txt:
        print(f'[apply] 未匹配到 TOKENS_PER_PCT 赋值行，请手工改 {path}')
        return
    open(path, 'w', encoding='utf-8').write(new)
    print(f'[apply] 已写入 plugins/doubao.py : TOKENS_PER_PCT = {int(value)}')
    print(f'        依据：{reason}')
    print('[!] 记得重启 serve：常驻进程已加载旧模块，不重启的话 watcher 会用旧系数写回。')
    print('    python monitor.py scan --agents doubao --full')


if __name__ == '__main__':
    main()
