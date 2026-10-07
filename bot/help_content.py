# -*- coding: utf-8 -*-
"""帮助指令内容定义（分类 + 指令清单）

被 main.py 的 `帮助` / `帮助 <分类>` 指令、帮助卡片按钮共用。
指令名用 <qqbot-cmd-input> 标签渲染成"点击填入输入框"的样式（对齐 CaiBotLite 的做法）。

指令条目：(指令名, 参数占位, 别名, 所需权限, 说明)
  - 所需权限填 permissions 的 PERM_* 常量名；None = 所有人；
    "OWNER" = 仅高级管理员；"BOOTSTRAP" = 本群尚无高级管理员时可用（自助）。
"""

from permissions import (
    MIN_ROLE, PERM_NEED_LABEL, RANK, MEMBER, OWNER,
    PERM_ROLE_MANAGE, PERM_MAP_TOGGLE, PERM_ONLINE_SHOW,
    PERM_ADD_SERVER, PERM_DEL_SERVER, PERM_MAP_FETCH, PERM_SAY_ALL, PERM_EXEC, PERM_BROADCAST,
    PERM_VOTE_MANAGE, PERM_VOTE_PUSH, PERM_RESET, PERM_PROGRESS_NOTIFY, PERM_STATUS_NOTIFY,
    PERM_VOTE_PROPOSAL_DEL, PERM_BACKUP, PERM_WORLD_SETTINGS, PERM_BACKUP_RESTORE, PERM_ECON_ADMIN,
)

# ───────────────────────── 分类与指令 ─────────────────────────
# (cmd, args, aliases, perm, desc)
CATEGORIES = [
    {
        "key": "info", "emoji": "ℹ️", "title": "帮助与信息",
        "commands": [
            ("帮助", "[分类名]", "help", None, "本卡片；带分类名可查看该类指令"),
            ("关于", "", "about", None, "机器人信息与贡献者"),
            ("群信息", "", "群资料", None, "群 ID、联合群、白名单统计"),
            ("权限查询", "", "身份查询 / 查看权限", None, "查看自己的身份与所需权限"),
        ],
    },
    {
        "key": "group", "emoji": "🛡️", "title": "群与权限",
        "commands": [
            ("设置高级管理员", "<玩家名>", "", "BOOTSTRAP", "本群尚无高级管理员时追授（自助）"),
            ("设置身份", "<玩家名> <高级管理员|服主|管理员>", "", PERM_ROLE_MANAGE, "授予身份"),
            ("取消身份", "<玩家名> [角色]", "", PERM_ROLE_MANAGE, "取消身份（至少保留一名高级管理员）"),
            ("允许成员获取地图", "[开|关]", "", PERM_MAP_TOGGLE, "控制普通群员能否获取地图"),
            ("允许查看在线玩家", "[开|关]", "", PERM_ONLINE_SHOW, "关闭后「在线」只显示人数"),
            ("服务器通知", "[开|关]", "", PERM_STATUS_NOTIFY, "服务器上线/掉线时在本群通知（默认开）"),
        ],
    },
    {
        "key": "union", "emoji": "🤝", "title": "多群联合",
        "commands": [
            ("绑定联合群", "<群ID>", "", "OWNER", "发起联合（双方高级管理员确认）"),
            ("解除联合群", "<群ID>", "", "OWNER", "子群脱离总群；总群解散全部子群"),
        ],
    },
    {
        "key": "server", "emoji": "💾", "title": "服务器管理",
        "commands": [
            ("添加服务器", "<ip/域名> <端口> <绑定码>", "", PERM_ADD_SERVER, "绑定码在服务器插件控制台生成"),
            ("删除服务器", "<序号>", "", PERM_DEL_SERVER, "只能删除自己添加的服务器"),
            ("服务器列表", "", "列表 / 服务器", None, "已接入服务器、版本与在线状态"),
            ("在线", "", "服务器在线", None, "各服务器在线玩家与推图进度"),
            ("ping", "<ip/域名> <端口>", "", None, "TCP 连通性测试"),
            ("获取地图", "<序号>", "", PERM_MAP_FETCH, "生成世界地图图片（普通群员需群开关开启）"),
            ("喊话", "<序号> <内容>", "", None, "向指定服务器发全服消息"),
            ("全服喊话", "<内容>", "", PERM_SAY_ALL, "向联合区内所有在线服务器广播"),
            ("远程指令", "<序号|all|*> <指令>", "远程执行", PERM_EXEC, "在服务器执行指令并回传输出"),
            ("广播", "<内容>", "", PERM_BROADCAST, "向联合区所有群发公告卡片"),
            ("插件列表", "<序号>", "", None, "列出该服务器已加载的插件（名称｜作者｜描述｜版本）"),
            ("下载地图", "<序号>", "下载世界文件 / 下载存档", PERM_MAP_FETCH, "以群文件发送当前世界存档 (.wld)"),
            ("下载小地图", "<序号>", "", PERM_MAP_FETCH, "以群文件发送 GenerateMap 小地图 (.map)"),
        ],
    },
    {
        "key": "vote", "emoji": "🗳️", "title": "投票与重置",
        "commands": [
            ("种子投票", "[序号] [候选…]", "", PERM_VOTE_MANAGE, "发起种子投票（候选用 ；分隔、组合用 + 连接）"),
            ("投票", "<编号>", "", None, "对编号投票；再次发送=取消"),
            ("结束投票", "[序号]", "", PERM_VOTE_MANAGE, "提前截止并公布结果"),
            ("推送投票", "[序号]", "", PERM_VOTE_PUSH, "把投票卡推送到联合区所有群"),
            ("查看投票", "<序号>", "", None, "把该服务器的投票卡发到本群"),
            ("种子列表", "[页码]", "", None, "常规 9 + 秘密 37 种世界种子（序号供提案引用）"),
            ("种子提案", "<序号> <种子序号+…>", "", None, "给进行中的投票追加候选，例：种子提案 1 1+3+15"),
            ("撤回提案", "<序号> <投票卡编号>", "", None, "撤回自己提出的提案（票归还给投票人）"),
            ("删除提案", "<序号> <投票卡编号>", "", PERM_VOTE_PROPOSAL_DEL, "管理员删除任意提案（含机器人随机项）"),
            ("世界设置", "<序号> [难度/大小/邪恶 值…]", "", PERM_WORLD_SETTINGS, "查看/修改世界生成参数，重置时生效"),
            ("备份", "[发送] <序号>", "", PERM_BACKUP, "把存档打包备份到服务器（加“发送”同时传到本群）"),
            ("备份列表", "[序号]", "", None, "列出服务器上的备份（每 30 分钟自动备份一次）"),
            ("回退备份", "<序号> <备份编号>", "", PERM_BACKUP_RESTORE, "把备份里的玩家存档导入覆盖（服主+）"),
            ("重置", "[序号]", "", PERM_RESET, "导出存档 → 应用种子 → 重置世界 → 推送存档"),
        ],
    },
    {
        "key": "whitelist", "emoji": "📄", "title": "白名单与设备",
        "commands": [
            ("绑定邮箱", "<邮箱>", "", None, "发送绑定验证码邮件（有频控）"),
            ("添加白名单", "<玩家名> <验证码>", "", None, "校验验证码并绑定玩家名"),
            ("修改白名单", "<新玩家名>", "", None, "改名：不迁移存档，48 小时限一次"),
            ("邮箱改绑", "<新邮箱>", "改绑邮箱", None, "7 天限一次；24 小时内完成否则回滚"),
            ("登录", "[玩家名]", "", None, "批准换设备登录"),
            ("取消", "<玩家名>", "", None, "撤销本人的待批准登录请求"),
            ("清空设备", "[玩家名]", "", None, "清空本人已登录设备，下次进服重新登录"),
            ("玩家查询", "<玩家名>", "", None, "绑定邮箱与进服记录"),
            ("自踢", "", "自提 / 自体", None, "断开自己当前在服务器里的连接"),
        ],
    },
    {
        "key": "query", "emoji": "🔍", "title": "查询",
        "commands": [
            ("查背包", "<序号> [玩家名]", "查看背包 / 查询背包 / 背包", None, "渲染背包图片；省略玩家名=查自己"),
            ("排行", "<序号> <项目> [参数] [页码]", "", None, "排行榜：死亡 / 在线 / 钓鱼 / 金币 / boss（图片卡分页）"),
            ("签到", "[玩家名]", "", None, "每日签到领喵币（需已绑定白名单；15~35 + 连续奖励）"),
            ("我的积分", "[玩家名]", "", None, "喵币余额 / 累计 / 连续签到 / 排名"),
            ("积分排行", "[累计] [页码]", "", None, "本联合体系内已绑定玩家的喵币榜"),
            ("发币 / 扣币", "<玩家名> <数量> [原因]", "", PERM_ECON_ADMIN, "高级管理员发放/扣除喵币"),
            ("重置经济", "确认", "", PERM_ECON_ADMIN, "清零所有人喵币（保留流水）"),
        ],
    },
    {
        "key": "lexicon", "emoji": "📖", "title": "图鉴搜索",
        "commands": [
            ("si", "<物品名|ID>", "搜物品", None, "物品详情：图标、伤害、价值、说明"),
            ("sn", "<生物名|ID>", "搜生物", None, "生物详情：生命、伤害、掉落价值"),
            ("sp", "<弹幕名|ID>", "搜弹幕", None, "弹幕详情：AI 类型、阵营"),
            ("sb", "<增益名|ID>", "搜增益", None, "增益/减益详情与说明"),
            ("sx", "<修饰语|ID>", "搜修饰", None, "修饰语（前缀）详情"),
        ],
    },
    {
        "key": "progress", "emoji": "📈", "title": "进度与提醒",
        "commands": [
            ("进度查询", "<序号>", "", None, "发送该服务器的进度图片卡"),
            ("进度提醒", "<序号> <boss名>", "", PERM_PROGRESS_NOTIFY, "该 boss 首杀时向本群播报"),
            ("进度提醒列表", "", "", PERM_PROGRESS_NOTIFY, "查看本群已设定的进度提醒"),
            ("取消进度提醒", "<序号> <boss名>", "", PERM_PROGRESS_NOTIFY, "取消一条进度提醒"),
            ("进度解锁提醒", "<序号> <boss名> <分钟>", "", PERM_PROGRESS_NOTIFY, "解锁前 N 分钟推送（需 BossLock / ProgressControls）"),
            ("进度解锁提醒列表", "", "", PERM_PROGRESS_NOTIFY, "查看本群已设定的解锁提醒"),
            ("取消进度解锁提醒", "<序号> <boss名>", "", PERM_PROGRESS_NOTIFY, "取消一条解锁提醒"),
        ],
    },
    {
        "key": "github", "emoji": "🐙", "title": "GitHub 动态",
        "commands": [
            ("仓库", "", "repo / github / 状态 / star / 数据", None, "仓库综述卡片"),
            ("pr", "", "/pr / 拉取请求列表 / 拉取 / 更新", None, "最近 PR 动态"),
            ("issue", "", "/issue / 议题列表 / 问题 / 议题", None, "最近 Issue 动态"),
        ],
    },
]

# 键位布局（每行最多 2 个按钮）
KEYBOARD_LAYOUT = [
    ["server", "whitelist"],
    ["query", "lexicon"],
    ["progress", "vote"],
    ["group", "union"],
    ["github", "info"],
]

RANK_LABEL = {4: "高级管理员", 3: "服主", 2: "管理员", 1: "普通群员"}


def category_by_key(key: str):
    for c in CATEGORIES:
        if c["key"] == key:
            return c
    return None


def find_category(arg: str):
    """按 key / 标题 / emoji+标题 / 包含关系匹配分类"""
    if not arg:
        return None
    a = arg.strip().lstrip("/").strip()
    if not a:
        return None
    for c in CATEGORIES:
        if a in (c["key"], c["title"], "%s %s" % (c["emoji"], c["title"]), c["emoji"]):
            return c
    for c in CATEGORIES:
        if c["title"] in a or a in c["title"]:
            return c
    return None


def cmd_tag(text: str, show: str = None) -> str:
    """可点击填入输入框的命令标签（QQ markdown 扩展标签）"""
    if show is None:
        show = text
    return '<qqbot-cmd-input text="%s" show="%s" reference="false" />' % (text, show)


def need_of(perm):
    """返回 (所需最小 rank, 所需身份文案)"""
    if perm is None:
        return RANK[MEMBER], "所有人"
    if perm == "OWNER":
        return RANK[OWNER], "高级管理员"
    if perm == "BOOTSTRAP":
        return RANK[MEMBER], "本群尚无高级管理员时可用"
    return RANK.get(MIN_ROLE.get(perm, OWNER), 4), PERM_NEED_LABEL.get(perm, "管理员及以上")


def _cmd_line(entry) -> str:
    """单条指令展示行（按需求不展示别名；别名仍保留在数据里备用）"""
    cmd, args, _aliases, _perm, desc = entry
    s = cmd_tag(cmd)
    if args:
        s += " " + " ".join("`%s`" % a for a in args.split())
    if desc:
        s += " — %s" % desc
    return s


def render_index(rank: int, title_fn) -> str:
    """帮助主卡：标题 + 引导 + 身份（分类切换全靠下方按钮）"""
    total = sum(len(c["commands"]) for c in CATEGORIES)
    usable = sum(1 for c in CATEGORIES for e in c["commands"] if need_of(e[3])[0] <= rank)
    lines = [
        title_fn("帮助"),
        "",
        "### 🍥 点击下方按钮查看分类",
        "",
        "---",
        "> 您的身份：`%s`｜当前可用 `%d/%d` 条" % (RANK_LABEL.get(rank, "普通群员"), usable, total),
    ]
    return "\n".join(lines)


def render_category(key: str, rank: int, title_fn) -> str:
    """某个分类中「当前身份可使用」的指令清单（无权限的指令整块不展示）"""
    c = category_by_key(key)
    if c is None:
        return render_index(rank, title_fn)
    ok = [e for e in c["commands"] if need_of(e[3])[0] <= rank]
    lines = [title_fn("帮助 · %s %s" % (c["emoji"], c["title"])), ""]
    lines.append("### ✅ 你可以使用")
    if ok:
        lines += [_cmd_line(e) for e in ok]
    else:
        lines.append("> 本分类暂无你可用指令喵")
    lines += ["", "---", "> 您的身份：`%s`" % RANK_LABEL.get(rank, "普通群员")]
    return "\n".join(lines)


def category_usable(cat, rank: int) -> bool:
    """该身份在本分类里是否至少有一条可用指令"""
    return any(need_of(e[3])[0] <= rank for e in cat["commands"])


def keyboard_layout(rank: int = None):
    """返回 [[分类 dict, ...], ...]（每行一组按钮）；给 rank 时过滤掉整类都无权限的按钮"""
    rows = []
    for row in KEYBOARD_LAYOUT:
        cats = [c for c in (category_by_key(k) for k in row) if c]
        if rank is not None:
            cats = [c for c in cats if category_usable(c, rank)]
        if cats:
            rows.append(cats)
    return rows
