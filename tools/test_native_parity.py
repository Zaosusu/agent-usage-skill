# -*- coding: utf-8 -*-
"""原生采集回归测试总入口（零文件改动，全部在临时目录跑）。

**核心保证：本项目默认完全脱离 CC Switch。**
即使机器上装了 CC Switch、其库里有数据，默认也一律走原生插件
（直接解析本地会话文件），不读 CC 库。

覆盖场景：
  1 默认     —— 装了 CC 但什么都不设：必须走原生（这是"脱离"的核心断言）
  2 开关开   —— AGENT_USAGE_USE_CCSWITCH=1：改用 CC，原生让位
  3 开关空转 —— 开关开着但库不存在：必须自动回退原生，绝不两边都不产出
  4 零接触   —— AGENT_USAGE_NO_CC_BACKFILL=1：连 rollup 兜底也不用
  5 切换     —— 同一个库：原生 ↔ CC 来回切，无残留、不叠加
  6 残留兜底 —— CC 模式留下的残留行，即使本地目录不存在也必须被清掉 ★
  7 无CC纯原生 —— 全新用户机器上压根没装 CC Switch：必须纯原生采到数据、0 CC 行 ★

用法：
    python tools/test_native_parity.py        # 跑全部
    python tools/test_native_parity.py 1      # 只跑场景 1
"""
import os
import sys
import json
import shutil
import sqlite3
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

REAL_CC = os.path.expanduser('~/.cc-switch/cc-switch.db')
FLAGS = ('AGENT_USAGE_USE_CCSWITCH', 'AGENT_USAGE_NO_CC_BACKFILL',
         'AGENT_USAGE_FORCE_NATIVE')


# ---------------------------------------------------------------- 工具

def _cc_base():
    """CC 基准量：明细表 + 长期汇总表（两表互补，必须都读）。"""
    out = {}
    if not os.path.exists(REAL_CC):
        return out
    con = sqlite3.connect('file:' + REAL_CC.replace('\\', '/') + '?mode=ro', uri=True)
    for app in ('codex', 'claude'):
        a = con.execute('select sum(input_tokens+output_tokens+cache_read_tokens+'
                        'cache_creation_tokens) from proxy_request_logs where app_type=?',
                        (app,)).fetchone()[0] or 0
        b = con.execute('select sum(input_tokens+output_tokens+cache_read_tokens+'
                        'cache_creation_tokens) from usage_daily_rollups where app_type=?',
                        (app,)).fetchone()[0] or 0
        out[app] = (a, b, a + b)
    con.close()
    return out


def _boot(work, cc_path, **env):
    """加载干净引擎；所有 CC 相关路径统一指向 cc_path。

    env：要设置的环境变量（其余 CC 开关一律清掉，保证互不干扰）。
    routes：额外路由（如把 codex 原生 root 指到不存在的目录，模拟"从没用过原生 CLI"）。
    """
    routes = env.pop('_routes', None) or {}
    for f in FLAGS:
        os.environ.pop(f, None)
    for k, v in env.items():
        if v is not None:
            os.environ[k] = str(v)
    for m in list(sys.modules):
        if m.startswith(('engine', 'plug_', 'plugins')):
            del sys.modules[m]
    from engine import core
    import engine.common as C
    C.CC_SWITCH_DB = cc_path
    for attr, val in routes.items():
        setattr(C, attr, val)
    core.DATA_DIR = work
    core.DB_PATH = os.path.join(work, 'usage.db')
    core.JSON_PATH = os.path.join(work, 'usage.json')
    os.makedirs(os.path.join(work, 'web'), exist_ok=True)
    core.web_dir = lambda: os.path.join(work, 'web')
    plugs = {p['key']: p for p in core.get_plugins(refresh=True)}
    # 原生插件与 ccswitch 插件都通过 engine.common 的开关/路径判断来源，
    # 故只需 patch common.CC_SWITCH_DB（上面已做）。
    # ⚠️ ccswitch 侧必须走它自己的 _db_path()（内含 normpath），不能直接返回裸路径：
    #    裸路径形如 'C:\\Users\\x/.cc-switch/cc-switch.db'（正斜杠），
    #    与原生插件清理键 _np(CC_SWITCH_DB)（反斜杠）**逐字符不等**，
    #    会造出"CC 残留行清不掉"的**假阳性**（本次又踩一次）。
    if 'ccswitch' in plugs:
        plugs['ccswitch']['module'].WATCH_PATHS = [cc_path]
    # ghost_roots：把某插件的实际扫描路径也指到不存在目录，
    # 才能真正走到「本地目录不存在」的 early-return 分支（只改 common 常量不够）。
    for pkey, gp in (routes.get('_ghost_paths') or {}).items():
        if pkey in plugs:
            plugs[pkey]['module'].WATCH_PATHS = [gp]
    return core


def _tot(d):
    return {a['key']: a['total_tokens'] for a in d['agents']}


def _rel(a, b):
    return abs(a - b) / b if b else 0


# ---------------------------------------------------------- 场景 1：默认脱离

def s1_default_detached():
    print('=' * 68)
    print('场景 1：装了 CC Switch、但什么都不设 —— 必须走原生（脱离的核心断言）')
    work = tempfile.mkdtemp(prefix='au_s1_')
    core = _boot(work, REAL_CC)          # 库存在，但开关不设
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t = _tot(data)
    print('  cc-switch.db 存在?', os.path.exists(REAL_CC), '（故意让它存在）')
    print('  ccswitch 上报行数 =', stats.get('ccswitch', {}).get('upserted', -1),
          '  ← 必须为 0')
    print('  原生 codex 行数 =', stats.get('codex', {}).get('upserted', -1),
          ' / claude =', stats.get('claude', {}).get('upserted', -1))
    base = _cc_base()
    ok = stats.get('ccswitch', {}).get('upserted', -1) == 0
    print()
    print('  %-8s %16s %16s %10s %8s' % ('agent', '看板(原生)', 'CC基准', '比值', '判定'))
    for app in ('codex', 'claude'):
        v, b = t.get(app, 0), base.get(app, (0, 0, 0))[2]
        r = v / b if b else 0
        # 默认走原生：应与「无 CC 时的原生量」一致，即 codex 明显多于 CC（更全）
        good = v > 0
        ok = ok and good
        print('  %-8s %16d %16d %10.3f %8s' % (app, v, b, r, '✅' if good else '❌'))
    print('  判定：CC 库存在也未被读取 ⇒ 已脱离 ✅' if ok else '  ❌ 仍在依赖 CC')
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ------------------------------------------------------ 场景 2：显式启用 CC

def s2_optin_ccswitch():
    print('=' * 68)
    print('场景 2：显式开启 AGENT_USAGE_USE_CCSWITCH=1 —— 改用 CC，原生让位')
    work = tempfile.mkdtemp(prefix='au_s2_')
    core = _boot(work, REAL_CC, AGENT_USAGE_USE_CCSWITCH='1')
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t = _tot(data)
    print('  stats:', json.dumps({k: v.get('upserted') for k, v in stats.items()
                                  if k in ('codex', 'claude', 'ccswitch')},
                                 ensure_ascii=False))
    base = _cc_base()
    ok = stats.get('ccswitch', {}).get('upserted', -1) > 0
    print()
    print('  %-8s %16s %16s %12s %8s' % ('agent', '看板(CC)', 'CC基准', '偏差', '判定'))
    for app in ('codex', 'claude'):
        v, b = t.get(app, 0), base[app][2]
        rel = _rel(v, b)
        # 活库微漂移容差；真实双计量级 +34% 起
        good = rel < 0.005
        ok = ok and good
        print('  %-8s %16d %16d %11.4f%% %8s' % (
            app, v, b, rel * 100, '✅' if good else '❌ 偏差=%+d' % (v - b)))
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ------------------------------------- 场景 3：开关空转（开了但没有库）★

def s3_flag_but_no_db():
    print('=' * 68)
    print('场景 3：开关开着、但 CC 库不存在 —— 必须自动回退原生（绝不双空）★')
    work = tempfile.mkdtemp(prefix='au_s3_')
    ghost = os.path.join(work, 'gone', 'cc-switch.db')
    core = _boot(work, ghost, AGENT_USAGE_USE_CCSWITCH='1',
                 AGENT_USAGE_NO_CC_BACKFILL='1')
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t = _tot(data)
    print('  cc-switch.db 存在?', os.path.exists(ghost))
    print('  ccswitch 行数 =', stats.get('ccswitch', {}).get('upserted', -1),
          '  ← 应为 0')
    print('  原生 codex 行数 =', stats.get('codex', {}).get('upserted', -1),
          ' claude =', stats.get('claude', {}).get('upserted', -1),
          '  ← 必须 > 0（自动回退）')
    ok = (stats.get('codex', {}).get('upserted', 0) > 0
          and stats.get('claude', {}).get('upserted', 0) > 0)
    for app in ('codex', 'claude'):
        print('  %-8s 看板=%d %s' % (app, t.get(app, 0), '✅' if t.get(app, 0) else '❌'))
        ok = ok and t.get(app, 0) > 0
    print('  判定：开关空转时数据未丢失 ⇒ 自动回退生效 ✅' if ok else '  ❌ 数据丢失')
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ------------------------------------------------- 场景 4：零 CC 接触

def s4_zero_touch():
    print('=' * 68)
    print('场景 4：AGENT_USAGE_NO_CC_BACKFILL=1 —— 连 rollup 兜底也不用')
    work = tempfile.mkdtemp(prefix='au_s4_')
    core = _boot(work, REAL_CC, AGENT_USAGE_NO_CC_BACKFILL='1')
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t = _tot(data)
    print('  stats:', json.dumps({k: v.get('upserted') for k, v in stats.items()
                                  if k in ('codex', 'claude', 'ccswitch')},
                                 ensure_ascii=False))
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    roll = con.execute("select count(*) from sessions where source_file like '%#rollup'"
                       ).fetchone()[0]
    con.close()
    print('  库内 #rollup 来源行数 =', roll, '  ← 必须为 0')
    ok = roll == 0 and t.get('codex', 0) > 0
    for app in ('codex', 'claude'):
        print('  %-8s 看板=%d（纯本地）' % (app, t.get(app, 0)))
    print('  判定：零 CC 接触下仍可正常采集 ✅' if ok else '  ❌ 仍读了 CC 库')
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ---------------------------------------------------------- 场景 5：切换

def s5_switch():
    print('=' * 68)
    print('场景 5：同一个库，原生 ↔ CC 来回切，无残留、不叠加')
    work = tempfile.mkdtemp(prefix='au_s5_')
    cc_copy = os.path.join(work, 'cc-switch.db')
    shutil.copy2(REAL_CC, cc_copy)
    ok = True

    # 5-1 默认（原生）
    core = _boot(work, cc_copy)
    d1, s1 = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t1 = _tot(d1)
    print('  5-1 默认(原生):', json.dumps(
        {k: v.get('upserted') for k, v in s1.items()}, ensure_ascii=False))

    # 5-2 切到 CC
    core = _boot(work, cc_copy, AGENT_USAGE_USE_CCSWITCH='1')
    d2, s2 = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t2 = _tot(d2)
    base = _cc_base()
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    left = [r for r in con.execute(
        'select agent, source_file, count(*) from sessions group by agent, source_file')
        if 'codex\\sessions' in r[1] or 'claude\\projects' in r[1]]
    con.close()
    print('  5-2 切CC    :', json.dumps(
        {k: v.get('upserted') for k, v in s2.items()}, ensure_ascii=False))
    print('     本地残留行:', left if left else '无 ✅')
    if left:
        ok = False
    print()
    print('  %-8s %16s %16s %12s %8s' % ('agent', '切CC后', 'CC基准', '偏差', '判定'))
    for app in ('codex', 'claude'):
        v, b = t2.get(app, 0), base[app][2]
        rel = _rel(v, b)
        good = rel < 0.005
        ok = ok and good
        print('  %-8s %16d %16d %11.4f%% %8s' % (
            app, v, b, rel * 100, '✅' if good else '❌ 重复 %+.2f%%' % (rel * 100)))

    # 5-3 切回原生（不删库，只关开关）
    core = _boot(work, cc_copy)
    d3, s3 = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t3 = _tot(d3)
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    cc_left = [r for r in con.execute(
        'select agent, source_file, count(*) from sessions group by agent, source_file')
        if 'cc-switch' in r[1] and not r[1].endswith('#rollup')]
    con.close()
    print('  5-3 切回原生:', json.dumps(
        {k: v.get('upserted') for k, v in s3.items()}, ensure_ascii=False))
    print('     CC 残留行(不含rollup):', cc_left if cc_left else '无 ✅')
    if cc_left:
        ok = False
    print()
    print('  %-8s %16s %16s %12s %8s' % ('agent', '切回原生', '5-1原生', '偏差', '判定'))
    for app in ('codex', 'claude'):
        v, v1 = t3.get(app, 0), t1.get(app, 0)
        rel = _rel(v, v1)
        good = rel < 0.005
        ok = ok and good
        print('  %-8s %16d %16d %11.4f%% %8s' % (
            app, v, v1, rel * 100, '✅' if good else '❌ 残留 %+.2f%%' % (rel * 100)))

    shutil.rmtree(work, ignore_errors=True)
    return ok


# ------------------------------------------------- 场景 6：残留兜底 ★

def s6_idle_cleanup():
    print('=' * 68)
    print('场景 6：CC 残留行必须被清掉 —— 即使本地目录不存在 ★')
    print('  构造：先在 CC 模式扫一次（库里有 CC 明细行），')
    print('        再把本地 root 指向不存在的目录（模拟"从没用过原生 CLI"），')
    print('        切回默认原生 —— CC 残留行必须消失。')
    work = tempfile.mkdtemp(prefix='au_s6_')

    # 6-1 先在 CC 模式扫一次，让库里出现 CC 明细行
    core = _boot(work, REAL_CC, AGENT_USAGE_USE_CCSWITCH='1')
    core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    cc_before = con.execute(
        "select count(*), coalesce(sum(total_tokens),0) from sessions "
        "where source_file like '%cc-switch.db' and source_file not like '%#rollup'"
    ).fetchone()
    con.close()
    print('  6-1 CC 模式扫描后，CC 明细行 = %d 条 / %d tok' % cc_before)

    # 6-2 切回默认原生，且把本地 root 指到不存在的目录
    ghost_c = os.path.join(work, 'no_such_codex_dir')
    ghost_l = os.path.join(work, 'no_such_claude_dir')
    core = _boot(work, REAL_CC, _routes={
        'CODEX_ROOT': ghost_c, 'CLAUDE_ROOT': ghost_l,
        '_ghost_paths': {'codex': ghost_c, 'claude': ghost_l}})
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    cc_after = con.execute(
        "select count(*), coalesce(sum(total_tokens),0) from sessions "
        "where source_file like '%cc-switch.db' and source_file not like '%#rollup'"
    ).fetchone()
    daily_after = con.execute(
        "select count(*) from daily where source_file like '%cc-switch.db'"
    ).fetchone()[0]
    con.close()
    print('  6-2 切回原生（本地目录不存在）后，CC 明细行 = %d 条 / %d tok'
          % cc_after)
    print('      daily 中的 CC 行 =', daily_after, '  ← 必须为 0')
    ok = cc_before[0] > 0 and cc_after[0] == 0 and daily_after == 0
    print('  判定：' + ('CC 残留行已被清干净 ✅' if ok else '❌ 仍有残留'))
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ----------------------------------------- 场景 7：无 CC Switch 纯原生 ★

def s7_clean_no_cc():
    """全新用户视角：机器上压根没装 CC Switch，克隆仓库直接跑。

    这是"别人没有 CC Switch 也能完美拿到数据"的核心断言：
      ① ccswitch 必须 0 行 —— 不读不存在的库、不报错、不抛异常
      ② codex / claude 走原生插件解析本地会话文件，**必须有数据**
      ③ 库内 session 表不得出现任何 CC 明细残留行（非 rollup）—— 防虚高
    临时 DATA_DIR + 临时隐藏 CC 库路径，零风险（不改生产库、不动真实 CC 文件）。
    """
    print('=' * 68)
    print('场景 7：全新用户（无 CC Switch、库不存在）—— 纯原生采集，不依赖 CC ★')
    work = tempfile.mkdtemp(prefix='au_s7_')
    ghost = os.path.join(work, 'gone', 'cc-switch.db')   # 不存在 ⇒ 模拟"没装 CC"
    core = _boot(work, ghost)          # 不设任何 CC 开关
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t = _tot(data)
    cc_up = stats.get('ccswitch', {}).get('upserted', -1)
    cx_up = stats.get('codex', {}).get('upserted', 0)
    cl_up = stats.get('claude', {}).get('upserted', 0)
    print('  cc-switch.db 存在?', os.path.exists(ghost), '（应为 False）')
    print('  ccswitch upserted =', cc_up, '  ← 必须 0（不读不存在的库）')
    print('  原生 codex upserted =', cx_up, ' / claude =', cl_up,
          '  ← 必须 > 0（纯本地会话文件）')
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    cc_detail = con.execute(
        "select count(*) from sessions where source_file like '%cc-switch.db' "
        "and source_file not like '%#rollup'").fetchone()[0]
    con.close()
    print('  库内 CC 明细残留(非rollup) =', cc_detail, '  ← 必须 0（防虚高）')
    ok = (cc_up == 0 and cx_up > 0 and cl_up > 0 and cc_detail == 0)
    print('  判定：' + ('无 CC Switch 也能纯原生完美采集 ✅'
                        if ok else '❌ 无 CC 时数据异常'))
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ---------------------------------------------------------------- 主流程

SCENARIOS = [
    ('1 默认脱离CC', s1_default_detached),
    ('2 显式启用CC', s2_optin_ccswitch),
    ('3 开关空转回退', s3_flag_but_no_db),
    ('4 零CC接触', s4_zero_touch),
    ('5 双向切换', s5_switch),
    ('6 残留兜底', s6_idle_cleanup),
    ('7 无CC纯原生', s7_clean_no_cc),
]


def main():
    sel = sys.argv[1] if len(sys.argv) > 1 else ''
    results = []
    for i, (name, fn) in enumerate(SCENARIOS, 1):
        if sel and str(i) != sel:
            continue
        if i > 1:
            print()
        try:
            results.append((name, fn()))
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print()
    print('=' * 68)
    for name, r in results:
        print('  %-18s %s' % (name, '✅ 通过' if r else '❌ 失败'))
    allok = all(r for _, r in results)
    print()
    print('总判定:', '✅ 全部通过 —— 默认完全脱离 CC Switch，且可正常采集'
          if allok else '❌ 存在失败')
    sys.exit(0 if allok else 1)


if __name__ == '__main__':
    main()
