# -*- coding: utf-8 -*-
"""原生采集回归测试总入口（零文件改动，全部在临时目录跑）。

覆盖三个场景，全部断言「不重复计数、口径与 CC 对齐」：

  A) 并存     —— 装了 CC Switch：ccswitch 采集，原生插件静默，总量 == CC 基准
  B) 无 CC    —— 朋友机器（从未装 CC）：原生插件自足采集，且不比 CC 差
  C) 切换     —— 同一个库：无 CC → 装 CC（以及反向卸载），历史行被正确清理、不叠加

用法：
    python tools/_test_native.py          # 跑全部
    python tools/_test_native.py A        # 只跑场景 A
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


def _boot(work, cc_path, force_native=False):
    """加载干净引擎；所有插件的 CC 路径统一指向 cc_path（便于模拟存在/不存在）。"""
    if force_native:
        os.environ['AGENT_USAGE_FORCE_NATIVE'] = '1'
    else:
        os.environ.pop('AGENT_USAGE_FORCE_NATIVE', None)
    for m in list(sys.modules):
        if m.startswith(('engine', 'plug_', 'plugins')):
            del sys.modules[m]
    from engine import core
    import engine.common as C
    C.CC_SWITCH_DB = cc_path
    core.DATA_DIR = work
    core.DB_PATH = os.path.join(work, 'usage.db')
    core.JSON_PATH = os.path.join(work, 'usage.json')
    os.makedirs(os.path.join(work, 'web'), exist_ok=True)
    core.web_dir = lambda: os.path.join(work, 'web')
    plugs = {p['key']: p for p in core.get_plugins(refresh=True)}
    for k in ('codex', 'claude'):
        if k in plugs:
            plugs[k]['module']._CC_DB = cc_path
    if 'ccswitch' in plugs:
        plugs['ccswitch']['module']._db_path = lambda: cc_path
    return core


def _tot(d):
    return {a['key']: a['total_tokens'] for a in d['agents']}


# ---------------------------------------------------------------- 场景 A：并存

def scenario_A():
    print('=' * 66)
    print('场景 A：装了 CC Switch —— ccswitch 采集，原生插件应静默')
    work = tempfile.mkdtemp(prefix='au_A_')
    core = _boot(work, REAL_CC)
    data, stats = core.scan(full=True)
    t = _tot(data)
    print('  stats:', json.dumps(stats, ensure_ascii=False))
    print('  原生插件上报行数应为 0：claude=%d, codex=%d' % (
        stats.get('claude', {}).get('upserted', -1),
        stats.get('codex', {}).get('upserted', -1)))
    base = _cc_base()
    print()
    print('  %-8s %16s %16s %12s %8s' % ('agent', '看板', 'CC基准', '偏差', '判定'))
    ok = True
    for app in ('codex', 'claude'):
        v, b = t.get(app, 0), base[app][2]
        d = v - b
        rel = abs(d) / b if b else 0
        # ⚠️ 源库是**活库**：扫描期间用户可能仍在用 codex/claude，
        #    基准（扫描后读取）与实际采集时刻必然有微小漂移。
        #    真实缺陷（双计）量级是 +34%~+138%，0.5% 容差绝不会掩盖它。
        good = rel < 0.005
        ok = ok and good
        print('  %-8s %16d %16d %11.4f%% %8s' % (
            app, v, b, rel * 100, '✅' if good else '❌ 偏差=%+d' % d))
    shutil.rmtree(work, ignore_errors=True)
    return ok


# ---------------------------------------------------------------- 场景 B：无 CC

def scenario_B(cc_base):
    print('=' * 66)
    print('场景 B：从未装 CC Switch（朋友机器）—— 原生插件自足采集')
    work = tempfile.mkdtemp(prefix='au_B_')
    ghost = os.path.join(work, 'never', 'cc-switch.db')
    core = _boot(work, ghost)
    data, stats = core.scan(full=True, only=['codex', 'claude', 'ccswitch'])
    t = _tot(data)
    print('  cc-switch.db 存在?', os.path.exists(ghost))
    print('  stats:', json.dumps(stats, ensure_ascii=False))
    print()
    print('  %-8s %16s %16s %16s %8s' % ('agent', '原生', 'CC基准', '比值', '判定'))
    ok = True
    for app in ('codex', 'claude'):
        v = t.get(app, 0)
        b = cc_base.get(app, (0, 0, 0))[2]
        r = v / b if b else 0
        # 无 CC 时不许比 CC 差太多（>0.5 视为可用；本机 codex 反而更全）
        good = v > 0 and (r >= 0.5 or b == 0)
        ok = ok and good
        print('  %-8s %16d %16d %16.3f %8s' % (app, v, b, r, '✅' if good else '❌'))
    shutil.rmtree(work, ignore_errors=True)
    return ok, t


# ---------------------------------------------------------------- 场景 C：切换

def scenario_C():
    print('=' * 66)
    print('场景 C：切换 —— 同一个库，无 CC → 装 CC → 卸载')
    work = tempfile.mkdtemp(prefix='au_C_')
    cc_copy = os.path.join(work, 'cc-switch.db')
    ok = True

    # C1 无 CC → 原生写库
    core = _boot(work, os.path.join(work, 'ghost.db'), force_native=True)
    d1, s1 = core.scan(full=True, only=['codex', 'claude'])
    t1 = _tot(d1)
    print('  C1 无CC(原生):', json.dumps(s1, ensure_ascii=False))

    # C2 装 CC（用副本，路径与真实一致）→ 原生行应被清掉
    shutil.copy2(REAL_CC, cc_copy)
    core = _boot(work, cc_copy)
    d2, s2 = core.scan(full=True, only=['ccswitch'])
    t2 = _tot(d2)
    base = _cc_base()
    print('  C2 装CC:', json.dumps(s2, ensure_ascii=False))
    con = sqlite3.connect(os.path.join(work, 'usage.db'))
    left = con.execute(
        "select count(*) from sessions where source_file like '%codex%s' "
        "or source_file like '%claude%s'" % (os.sep + 'sessions', os.sep + 'projects')
    ).fetchone()[0] if False else 0
    # 更可靠：直接看有没有本地 root 来源的行
    rows = con.execute('select agent, source_file, count(*) from sessions '
                       'group by agent, source_file').fetchall()
    local_left = [r for r in rows if 'codex\\sessions' in r[1] or
                  'claude\\projects' in r[1]]
    con.close()
    print('  本地残留行:', local_left if local_left else '无 ✅')
    if local_left:
        ok = False
    print()
    print('  %-8s %16s %16s %16s %10s' % ('agent', 'C1原生', 'C2装CC', 'CC基准', '判定'))
    for app in ('codex', 'claude'):
        v1, v2, b = t1.get(app, 0), t2.get(app, 0), base[app][2]
        rel = abs(v2 - b) / b if b else 0
        good = rel < 0.005   # 同场景 A：活库微漂移容差（真实双计量级 +34% 起）
        ok = ok and good
        print('  %-8s %16d %16d %16d %10s' % (
            app, v1, v2, b, '✅' if good else '❌ 重复 %+.2f%%' % (rel * 100)))

    # C3 卸载（文件消失、但**路径不变**）→ 原生接管，CC 行应被清掉
    os.remove(cc_copy)
    core = _boot(work, cc_copy)
    d3, s3 = core.scan(full=True, only=['ccswitch', 'codex', 'claude'])
    t3 = _tot(d3)
    print('  C3 卸CC:', json.dumps(s3, ensure_ascii=False))
    print()
    print('  %-8s %16s %16s %10s' % ('agent', 'C3卸载后', 'C1原生', '判定'))
    for app in ('codex', 'claude'):
        v3, v1 = t3.get(app, 0), t1.get(app, 0)
        r = (v3 - v1) / v1 if v1 else 1
        # 全新机器无 rollup，卸载后应回到「裸原生」量级（差异仅来自测试间新增用量）
        good = abs(r) < 0.01
        ok = ok and good
        print('  %-8s %16d %16d %10s' % (
            app, v3, v1, '✅' if good else '❌ 残留 %.1f%%' % (r * 100)))

    shutil.rmtree(work, ignore_errors=True)
    return ok


# ---------------------------------------------------------------- 主流程

def main():
    only = sys.argv[1].upper() if len(sys.argv) > 1 else ''
    base = _cc_base()
    if not base:
        print('⚠️ 未找到 ~/.cc-switch/cc-switch.db；场景 A/C2 的基准比对将跳过')

    results = []
    if not only or only == 'A':
        results.append(('A 并存(CC在岗)', scenario_A()))
    if not only or only == 'B':
        print()
        r, _ = scenario_B(base)
        results.append(('B 无CC(朋友机器)', r))
    if not only or only == 'C':
        print()
        results.append(('C 切换/卸载', scenario_C()))

    print()
    print('=' * 66)
    for name, r in results:
        print('  %-22s %s' % (name, '✅ 通过' if r else '❌ 失败'))
    allok = all(r for _, r in results)
    print()
    print('总判定:', '✅ 全部通过 —— codex/claude 无需 CC Switch 亦可正确采集'
          if allok else '❌ 存在失败')
    sys.exit(0 if allok else 1)


if __name__ == '__main__':
    main()
