# -*- coding: utf-8 -*-
"""插件：豆包工作。

【核心】豆包云端只返回**百分比**，没有绝对 token 字段。所以只需一条固定换算：

    TOKENS_PER_PCT = 500_000        # 1% = 50 万 token

    总量 = timeline 百分比 × 50 万

其余一切都是为论证这个系数服务的，推导与证据链见 docs/DOUBAO.md
（可一键复现：python tools/calibrate_doubao.py）。

【数据来源】只有一条：timeline API 百分比（Cookie 认证，拉全部记录累加）。
拿不到就**直接抛错**，绝不静默降级 —— 理由见下。

【本地存档】高水位与完整时序都落在引擎的 `data/usage.db`：
- `agent_high_water`    —— 累计量高水位（单行/agent），接口回落时据此兜底
- `agent_usage_history` —— 每 agent 每天的 pct/tokens 时序，只增不减
两者都**只增不减**（写回时取 max），保证曲线不会被接口抖动或历史窗口收缩打回去。

【为什么不做降级】本地任何途径都拿不到与 timeline 同一额度池的数字：
- Local Storage 的 usedThisPeriod/monthlyLimit 是**另一个额度池**
  （plan:premium / billingMode:metered，周期约 4 个月），与 timeline 的账户累计量
  量级差**两个数量级**（同一时刻 2.8% vs 499%，相差约 176 倍）。
  若用它做降级，cookie 失效时会**静默暴跌约 176 倍**；故该分支已移除，
  改为直接报错。
- trajectory.jsonl 只有 5 个顶层字段（role/content/tool_call_id/tool_calls/
  image_link_list），**没有任何 usage 字段**；按文本估算与真实用量无稳定关系
  （同一个 1% 随任务长度浮动 7.7 倍）。
- IndexedDB / .alaudalog 里的 inputTokens 零散，且无法证明同额度池。
给一个会被误读成"真实用量"的数，比明确失败更糟。

【两条边界，读数前必读】
- **只对账户总量成立**：1% 不是固定 token 数，按任务长度浮动 7.7 倍
  （短对话约 21 万 / 中等约 47 万 / 长 agent 约 162 万）。拿 50 万 推算单个任务
  或某一天，会低估长任务 3~8 倍。
  ⚠️ 但**按天的百分比拆分本身是精确的**（Σ每日 pct = 总 pct），
  所以 `agent_usage_history` 里的 pct 时序可信；失真的只是 pct→token 的换算系数。
- **不能与其他 Agent 横向比**：豆包记的是折后计价量，WorkBuddy 等记原始传输量。

【系数怎么来的（三行版）】
- 消息级对齐：timeline 每条 = 一条 user 消息，本地重建按同一批消息累加。
  双向匹配：正向约 83% / 反向约 95% ⇒ 对齐成立。
- 定义式：Σtok(全量) ÷ Σpct(全量) = 约 49.3 万/1% ⇒ 取整 **50 万**。
- 独立交叉验证：256K 窗口下最紧的那次单调用上下文已逼近窗口，
  只剩少量余量 ⇒ 系数上界约 58.9 万（足以否定 120 万/230 万，但定不了真值）。
  50 万 落在区间内。
- 推导全文与逐项数据见 docs/DOUBAO.md。

【钉死精确值】拿到「一个 7 天窗口 = 多少 token」的绝对数即可一击锁死：
    python tools/calibrate_doubao.py --anchor 1.5e8 --apply
"""
import os, sys, json, time, urllib.request, urllib.error

KEY = 'doubao'
NAME = '豆包工作'
ESTIMATE = True
WATCH_PATHS = ['%USERPROFILE%\\AppData\\Local\\DoubaoWork\\User Data\\Default']

# 固定系数：1% = 50 万 token（仅对「账户总量」成立，边界见模块 docstring）
TOKENS_PER_PCT = 500_000

_CONFIG_PATH = os.path.expanduser("~/.doubao-usage/config.json")

# 存档落在引擎的 usage.db（与 sessions/daily/meta 同库）：
#   agent_high_water       —— 每 agent 一行的累计量高水位
#   agent_usage_history    —— 每 agent 每天的完整时序
# 表结构由 engine/core.py 的 _conn() 统一创建（这里只兜底，见 _db()）。
if getattr(sys, 'frozen', False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_BASE, 'data', 'usage.db')

# 早期版本把高水位存在独立 JSON 里；已废弃，仅保留用于一次性迁移
_HISTORY_PATH = os.path.join(_BASE, 'data', 'doubao_history.json')

_DEFAULT_ROOT = os.path.expanduser(WATCH_PATHS[0].replace('%USERPROFILE%', os.path.expanduser('~')))


_TIMELINE_QS = ("version_code=20800&language=zh&device_platform=web"
                "&aid=497858&real_aid=497858&pkg_type=release_version"
                "&device_id=7667391337516697151&pc_version=3.37.5"
                "&doubao_pc_version=3.37.5&web_id=7505737990292227620"
                "&tea_uuid=7505737990292227620&region=CN&sys_region=CN"
                "&samantha_web=1&web_platform=browser&use-olympus-account=1")
_TIMELINE_URL = f"https://www.doubao.com/alice/commerce/usage/timeline/?{_TIMELINE_QS}"


def _timeline_headers(cookie_str):
    return {
        "Cookie": cookie_str,
        "Referer": "https://www.doubao.com/chat",
        "User-Agent": "Mozilla/5.0",
        "agw-js-conv": "str",
        "accept": "application/json",
        "content-type": "application/json",
    }


# 最近一次拉取失败的原因，供 scan() 生成**准确**的报错文案。
#   ('net',  ...) —— 网络/TLS 层失败，与 cookie 无关（如 UNEXPECTED_EOF_WHILE_READING 抖动）
#   ('auth', ...) —— 接口可达但认证失败/无数据，多半 cookie 过期
_last_error = None


def _request_page(cookie_str, cursor, timeout=20, retries=3):
    """拉 timeline 单页，带指数退避重试。

    为什么必须重试：整轮要翻 80+ 页、耗时约 2 分钟，而 SSL 层存在偶发抖动
    （`UNEXPECTED_EOF_WHILE_READING`）。单页抖动就让整轮白费太亏，故重试。
    4xx 是确定性错误（认证/参数），重试无意义，直接上抛。
    """
    headers = _timeline_headers(cookie_str)
    body = json.dumps({"cursor": cursor} if cursor else {}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(_TIMELINE_URL, data=body,
                                         headers=headers, method="POST")
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:      # 认证/参数类，重试无用
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < retries - 1:
            time.sleep(0.8 * (2 ** attempt))   # 0.8s / 1.6s
    raise last


def _fetch_timeline(cookie_str):
    """拉 timeline API，返回 (总百分比, 唯一条目数, {日期: 当日百分比})

    【两个必须同时做的防护：页间延迟 + item_id 去重】
    根因是**分页限流**：连续快速翻页时，API 会开始重复返回已经给过的
    条目（游标每次都变、v3./v4. 交替，但内容按 ~4 页周期循环），而
    has_more **始终为 True**（200/200 页永不终止）。

    无限流的两个后果（同一 cookie 对照）：
      - 唯一条目数远小于累计页数（大量页在重复同一批条目）；
      - 累加 pct 被同一批条目重复计入 ⇒ 总量虚高约 1.7 倍（甚至更高，
        取决于限流强度），且**每日拆分同样被污染**。

    证据：同一 item_id 每次出现 pct 恒定（440/440 全部一致）⇒ 去重合法无损。

    因此：① 页间 sleep 让分页正常回溯；② 按 item_id 去重兜底；
    ③ 终止条件不能只信 has_more，叠加「连续 N 页无新条目」判定。
    """
    if not cookie_str:
        return None, None, {}
    global _last_error
    seen = set()          # item_id 去重
    all_pct = 0.0
    daily = {}
    cursor = None
    stale = 0             # 连续无新条目的页数
    _STALE_LIMIT = 40     # 到顶后 40 页内不会有新条目，留足余量
    _PAGE_DELAY = 0.25    # 关键：不加延迟会触发限流，分页开始重复返回
    try:
        for _ in range(200):
            r = _request_page(cookie_str, cursor)
            d = r.get("data", {})
            entries = d.get("entries", [])
            if not entries:
                break
            new_in_page = 0
            for e in entries:
                u = e.get("usage", {})
                pct_str = u.get("quota_source", {}).get("display_text", "0%")
                ts = u.get("occurred_at_ms", 0) / 1000 if u.get("occurred_at_ms") else 0
                # 去重键：优先 item_id，缺失时退化为 (时间, 百分比) 组合
                iid = u.get("item_id", "") or f"{int(ts)}|{pct_str}"
                if iid in seen:
                    continue
                seen.add(iid)
                new_in_page += 1
                if "<" in pct_str:
                    pct = 0.005
                else:
                    try:
                        pct = float(pct_str.replace("%", ""))
                    except ValueError:
                        pct = 0
                all_pct += pct
                if ts > 0:
                    day = time.strftime('%Y-%m-%d', time.localtime(ts))
                    daily[day] = daily.get(day, 0) + pct
            # 游标会循环 ⇒ 连续无新条目即判定到顶
            if new_in_page == 0:
                stale += 1
                if stale >= _STALE_LIMIT:
                    break
            else:
                stale = 0
            cursor = d.get("next_cursor")
            if not d.get("has_more") or not cursor:
                break
            time.sleep(_PAGE_DELAY)   # 限流保护，必须在翻页前
    except Exception as e:
        # 区分「网络层抖动」与「认证失败」——两者处置完全不同：
        #   网络失败 ⇒ 重试/稍后再扫，cookie 没坏，别去重新登录
        #   HTTP 4xx ⇒ 多半 cookie 过期，需补 cookie
        if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500:
            _last_error = ('auth', f'HTTP {e.code}')
        else:
            _last_error = ('net', f'{type(e).__name__}: {str(e)[:120]}')
        return None, None, {}
    _last_error = None
    return all_pct, len(seen), daily


def _extract_cookie():
    if not os.path.isfile(_CONFIG_PATH):
        return None
    try:
        with open(_CONFIG_PATH, 'r') as f:
            config = json.load(f)
        cookie = config.get("doubao_cookie", "")
        return cookie if cookie else None
    except Exception:
        return None


def _cookie_hint():
    """报错文案：按**真实失败原因**给出下一步，别一律怪 cookie。

    SSL 层存在偶发抖动（`UNEXPECTED_EOF_WHILE_READING`），若文案一律写
    「cookie 多半已过期」，会让人白去重新登录复制 cookie。
    """
    if not os.path.isfile(_CONFIG_PATH):
        return (f"未配置 cookie：请把浏览器里 doubao.com 的 Cookie 写入 {_CONFIG_PATH}\n"
                f'    格式：{{"doubao_cookie": "你的cookie字符串"}}')
    if _last_error and _last_error[0] == 'net':
        return (f"网络/TLS 请求失败（与 cookie 无关，cookie 仍是好的）：{_last_error[1]}\n"
                f"    已自动重试 3 次仍失败，稍后重扫即可，无需重新登录")
    return (f"cookie 已配置（{_CONFIG_PATH}）但接口认证失败"
            f"{'（' + _last_error[1] + '）' if _last_error else ''}，多半已过期："
            f"请重新登录 doubao.com 复制 Cookie 覆盖该文件")


# ---------------------------------------------------------------- 防衰减高水位
# 背景：本接口返回的是**账户累计消耗**，理论上只增不减。但豆包用量页有
# 「只展示最近半年的历史记录」——若该窗口也作用于本接口，旧条目会陆续掉出，
# 导致累计值**随时间回落**。用户明确要求：一旦出现明显衰减，必须用本地记录的
# 历史数据，不要让看板数字跟着掉。
#
# 存档位置：引擎的 data/usage.db（早期版本存独立 JSON，已迁移，见 _migrate_json）
#   agent_high_water      —— 累计量高水位（单行）
#   agent_usage_history   —— 每 agent 每天的完整时序（只增不减）
_DECAY_TOL = 0.005   # 相对容差 0.5%：低于此幅度视为抖动，不触发保护

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_high_water(
    agent TEXT PRIMARY KEY,
    high_water_pct REAL DEFAULT 0,
    high_water_tokens INTEGER DEFAULT 0,
    high_water_count INTEGER DEFAULT 0,
    updated_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_usage_history(
    agent TEXT NOT NULL,
    day TEXT NOT NULL,
    pct REAL DEFAULT 0,
    tokens INTEGER DEFAULT 0,
    updated_at INTEGER DEFAULT 0,
    PRIMARY KEY(agent, day)
);
'''


def _db():
    """连 usage.db 并兜底建表（表结构权威定义在 engine/core.py）。

    兜底建表是为了让插件能脱离引擎单独跑（如校准脚本、手工排障）。
    """
    import sqlite3
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.executescript(_SCHEMA)
    return con


def _migrate_json(con):
    """一次性把早期独立 JSON 的高水位迁进 DB（迁完即删，避免双份真相）。"""
    if not os.path.isfile(_HISTORY_PATH):
        return
    try:
        with open(_HISTORY_PATH, 'r', encoding='utf-8') as f:
            h = json.load(f)
        if not isinstance(h, dict):
            raise ValueError('bad json')
        # 旧旧格式用 cumulated_pct / last_pct_this_period 命名
        pct = h.get('high_water_pct') or h.get('last_pct_this_period') or h.get('cumulated_pct') or 0
        count = int(h.get('high_water_count') or h.get('last_count') or 0)
        pct = float(pct)
        if pct > 0:
            con.execute(
                'insert into agent_high_water(agent, high_water_pct, high_water_tokens,'
                ' high_water_count, updated_at) values(?,?,?,?,?) '
                'on conflict(agent) do nothing',
                (KEY, pct, int(pct * TOKENS_PER_PCT), count, int(time.time())))
            for day, p in (h.get('daily') or {}).items():
                con.execute(
                    'insert into agent_usage_history(agent, day, pct, tokens, updated_at)'
                    ' values(?,?,?,?,?) on conflict(agent, day) do nothing',
                    (KEY, day, float(p), int(float(p) * TOKENS_PER_PCT), int(time.time())))
            con.commit()
        os.remove(_HISTORY_PATH)   # 迁移完成，删掉孤儿文件（避免双份真相）
    except Exception:
        pass   # 迁移失败不影响主流程：DB 里没有就按首次运行处理


def _load_high_water(con):
    row = con.execute('select high_water_pct, high_water_count from agent_high_water'
                      ' where agent=?', (KEY,)).fetchone()
    if not row:
        return 0.0, 0
    return float(row[0] or 0), int(row[1] or 0)


def _load_history_daily(con):
    """读完整时序，返回 {day: pct}。"""
    rows = con.execute('select day, pct from agent_usage_history where agent=?', (KEY,)).fetchall()
    return {d: float(p or 0) for d, p in rows}


def _write_archive(con, pct, count, daily):
    """落盘高水位 + 按天时序（都只增不减）。"""
    now = int(time.time())
    con.execute(
        'insert into agent_high_water(agent, high_water_pct, high_water_tokens,'
        ' high_water_count, updated_at) values(?,?,?,?,?) '
        'on conflict(agent) do update set'
        ' high_water_pct=excluded.high_water_pct,'
        ' high_water_tokens=excluded.high_water_tokens,'
        ' high_water_count=excluded.high_water_count,'
        ' updated_at=excluded.updated_at',
        (KEY, round(float(pct), 4), int(float(pct) * TOKENS_PER_PCT), int(count), now))
    for day, p in (daily or {}).items():
        # 只增不减：该天已有更大值就保留（防接口抖动把历史天改小）
        con.execute(
            'insert into agent_usage_history(agent, day, pct, tokens, updated_at)'
            ' values(?,?,?,?,?) '
            'on conflict(agent, day) do update set'
            ' pct=max(agent_usage_history.pct, excluded.pct),'
            ' tokens=max(agent_usage_history.tokens, excluded.tokens),'
            ' updated_at=excluded.updated_at',
            (KEY, day, round(float(p), 4), int(float(p) * TOKENS_PER_PCT), now))
    con.commit()


def _apply_decay_guard(api_pct, api_count, api_daily):
    """高水位保护 + 时序存档，返回 (pct, count, daily, note)。

    采用的累计量 = max(历史高水位, 本次接口总量, 逐日并集之和)，即**只增不减**。
    返回的 daily 是「本地时序 ∪ 本次接口按天」的逐日取大结果，再整体缩放一次，
    使其 Σ == 返回的 pct —— 保证 sessions 汇总恒等于高水位，总量与按天不会打架
    （正常情况两者本就相等，缩放系数为 1，不改动任何数字）。

    note 非空表示触发了衰减保护（接口值明显低于历史最高，已沿用本地高水位）。
    """
    con = _db()
    try:
        _migrate_json(con)
        hw, hw_count = _load_high_water(con)
        merged = _load_history_daily(con)          # 本地已存的历史时序
        for day, p in (api_daily or {}).items():   # 并入本次接口拿到的天（逐日取大）
            merged[day] = max(merged.get(day, 0), p)

        s = sum(merged.values())
        final_pct = max(hw, api_pct, s)            # 只增不减
        if s > 0 and abs(s - final_pct) > 1e-6:    # 逐日并集与总量不一致时对齐
            k = final_pct / s
            merged = {d: v * k for d, v in merged.items()}
        final_count = max(api_count or 0, hw_count or 0)   # 条目数同样只增不减

        _write_archive(con, final_pct, final_count, merged)

        note = ''
        if hw > 0 and api_pct < hw * (1 - _DECAY_TOL):
            drop = (hw - api_pct) / hw * 100
            note = (f'衰减保护：接口 {api_pct:.2f}% 低于历史最高 {hw:.2f}%'
                    f'（-{drop:.1f}%），已保留本地存档（含完整时序），总量未回落')
        return final_pct, final_count, merged, note
    finally:
        con.close()


def _wrap_result(sessions, daily_map, source):
    """按引擎新协议返回 {sessions, daily, daily_files}，让豆包进入按天曲线。

    之前只返回 sessions 列表（旧协议），引擎拿不到 daily，导致豆包的每日用量
    只进 sessions（总量 KPI）而按天曲线始终为空——今天/按天视图看不到豆包。
    注意：daily 主键是 (agent, day, source_file)，故 source_file 必须按天唯一，
    否则同一天多行互相覆盖（历史踩过的坑）。
    """
    daily_rows = [{'agent': KEY, 'day': d, 'source_file': f'doubao-{d}',
                   'tokens': int(t), 'est': 1}
                  for d, t in daily_map.items() if t >= 1000]
    # daily_files 带上旧的共享 source，用于清理历史遗留行
    daily_files = [f'doubao-{d}' for d in daily_map] + [source]
    return {'sessions': sessions, 'daily': daily_rows, 'daily_files': daily_files}


def scan(full, need, mark):
    """只认 timeline API。拿不到就抛错，不降级、不估算。"""
    cookie = _extract_cookie()
    if not cookie:
        raise RuntimeError(f'豆包用量取不到：{_cookie_hint()}')

    api_pct, api_count, api_daily = _fetch_timeline(cookie)
    if not api_pct or api_pct <= 0:
        raise RuntimeError(f'豆包用量取不到：{_cookie_hint()}')

    # 防衰减：累计量理论上只增不减，若接口值回落则沿用本地高水位
    pct, count, daily, guard_note = _apply_decay_guard(api_pct, api_count, api_daily)

    source = f'timeline-api ({count}条, {pct:.1f}%)'
    if guard_note:
        source += f' [{guard_note}]'
    daily_map = {}   # day -> tokens，供 daily 表使用
    sessions = []
    today_str = time.strftime('%Y-%m-%d')
    for day, p in sorted((daily or {}).items()):
        if day > today_str:
            continue
        day_tokens = int(p * TOKENS_PER_PCT)
        if day_tokens < 1000:
            continue
        daily_map[day] = day_tokens
        ts_ms = int(time.mktime(time.strptime(day, '%Y-%m-%d')) * 1000)
        sessions.append({
            'agent': KEY,
            'session_id': f'doubao-{day}',
            'title': f'豆包工作 {day}',
            'cwd': '',
            'model': 'doubao-seed',
            'provider': 'bytedance',
            'created_at': ts_ms,
            'last_activity_at': ts_ms,
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_read_tokens': 0,
            'cache_write_tokens': 0,
            'total_tokens': day_tokens,
            'cost': None,
            'est': 1,
            'source_file': source,
        })

    if not sessions:
        # 有百分比但没有任何一天 ≥1000 token ⇒ 数据异常，宁可报错也不写脏数据
        raise RuntimeError(f'豆包 timeline 返回 {api_pct:.2f}%（{api_count} 条）'
                           f'但按日拆不出任何用量，数据异常，已跳过')

    return _wrap_result(sessions, daily_map, source)
