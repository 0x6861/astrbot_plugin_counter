# -*- coding: utf-8 -*-
"""
astrbot_plugin_cnt
一个简单易用的“词频计数”Star 插件：
1) /cnt add <counter> [别名1 别名2 ...]  添加计数器（可带多个别名）
2) /cnt del <counter>                       删除计数器（支持用别名指到主名）
3) /cnt list                                列出所有计数器及次数
4) 监听任意消息，若包含任一计数器(或其别名)的文本子串，则为该计数器 +1（默认不回消息）
5) 数据使用 JSON 持久化存储（位于 AstrBot/data 下的插件专属目录）

注意：
- 为避免刷屏，自动 +1 时默认不回复；如需提示，可把类属性 `self.notify_on_increment` 设为 True。
"""

import asyncio
import json
from pathlib import Path
from typing import Dict, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger


PLUGIN_NAME = "astrbot_plugin_counter"
DATA_FILE_NAME = "counters.json"


@register(
    PLUGIN_NAME,
    "hami",
    "计数器（Star）插件：添加/删除/列出计数器；消息命中计数器时自动+1；JSON 持久化。",
    "v0.1.2",
    "https://github.com/0x6861/astrbot_plugin_counter",  # 若上架 GitHub 请替换为真实仓库 URL
)
class CounterStarPlugin(Star):
    """计数器插件主体"""

    # ------------------------- 生命周期与初始化 -------------------------
    def __init__(self, context: Context):
        super().__init__(context)
        # 是否在计数+1时回提示（默认 False，避免刷屏）
        self.notify_on_increment: bool = True

        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)  # 官方推荐的数据目录
        self.data_file: Path = self.data_dir / DATA_FILE_NAME

        # 数据结构：
        # self.data = {
        #   "counters": {
        #       "<主名称>": {
        #           "count": int,
        #           "aliases": ["别名1","别名2",...]
        #       },
        #       ...
        #   }
        # }
        self.data: Dict = {"counters": {}}

        # 运行期索引，便于查重/快速匹配（均为大小写不敏感）
        self._name_index: Dict[str, str] = {}  # 规范名 -> 主名（规范名为 casefold）
        self._alias_index: Dict[str, str] = {}  # 规范别名 -> 主名

        self._lock = asyncio.Lock()
        self._load()

    async def terminate(self):
        """插件卸载/停用时调用：落盘一次"""
        try:
            async with self._lock:
                await self._save()
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] terminate save error: {e}")

    # ------------------------- 工具函数 -------------------------
    @staticmethod
    def _norm(text: str) -> str:
        """统一大小写与空格（用于对比、索引）"""
        return (text or "").strip().casefold()

    def _load(self):
        """同步加载 JSON 数据（启动时调用一次）"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self.data_file.exists():
                self.data = json.loads(self.data_file.read_text("utf-8"))
            else:
                self.data = {"counters": {}}
            self._rebuild_index()
            logger.info(
                f"[{PLUGIN_NAME}] data loaded. counters={len(self.data['counters'])}"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] load error: {e}")
            self.data = {"counters": {}}
            self._rebuild_index()

    async def _save(self):
        """异步落盘，避免阻塞事件循环"""

        def _write():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.data_file.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

        await asyncio.to_thread(_write)

    def _rebuild_index(self):
        """按当前 self.data 重建内存索引"""
        self._name_index.clear()
        self._alias_index.clear()
        for name, meta in self.data.get("counters", {}).items():
            n = self._norm(name)
            self._name_index[n] = name
            for a in meta.get("aliases", []) or []:
                na = self._norm(a)
                # 同名别名忽略
                if not na or na == n:
                    continue
                self._alias_index[na] = name

    @staticmethod
    def _split_parts(msg: str) -> List[str]:
        """将一条消息按空白切分为词元列表"""
        return (msg or "").strip().split()

    def _extract_args_after(self, event: AstrMessageEvent, *route: str) -> List[str]:
        """
        从原始消息中抽取某指令路径后的“剩余参数”。
        例如消息：/cnt add 某词 别名A 别名B
        route=['cnt','add'] -> 返回 ['某词','别名A','别名B']
        """
        parts = self._split_parts(event.message_str)
        if not parts:
            return []
        # 去除前缀'/'（若存在）
        if parts[0].startswith("/"):
            parts[0] = parts[0][1:]

        # 按顺序匹配 route
        i = 0
        for seg in route:
            if i >= len(parts) or self._norm(parts[i]) != self._norm(seg):
                return []
            i += 1
        return parts[i:]

    # ------------------------- 指令：/cnt -------------------------
    @filter.command("cnt")
    async def cnt(self, event: AstrMessageEvent):
        """计数器命令：/cnt add|del|list

        sub(string): 子命令（add|del|list）
        其余参数: 根据子命令不同而不同
        """
        args = self._extract_args_after(event, "cnt")
        if not args:
            yield event.plain_result(
                "用法：/cnt add <计数器名> [别名…]；/cnt del <名称或别名>；/cnt list"
            )
            return

        sub = self._norm(args[0])

        if sub == "add":
            async for r in self._cnt_add(event):
                yield r
        elif sub == "del":
            async for r in self._cnt_del(event):
                yield r
        elif sub == "list":
            async for r in self._cnt_list(event):
                yield r
        else:
            yield event.plain_result("未知子命令。可用：add / del / list")

    async def _cnt_add(self, event: AstrMessageEvent):
        """添加计数器：/cnt add <counter> [别名1 别名2 ...]"""
        args = self._extract_args_after(event, "cnt", "add")
        if len(args) < 1:
            yield event.plain_result(
                "用法：/cnt add <计数器名> [可选：<别名1> <别名2> ...]"
            )
            return

        name = args[0]
        aliases = [a for a in args[1:] if a.strip()]

        n_name = self._norm(name)
        n_aliases = [self._norm(a) for a in aliases]

        async with self._lock:
            # 冲突校验：主名与别名都不能与现有主名/别名重复
            conflicts: List[str] = []
            if n_name in self._name_index or n_name in self._alias_index:
                conflicts.append(f"计数器之「{name}」已存在或被占用")

            for na, a in zip(n_aliases, aliases):
                if not na or na == n_name:
                    conflicts.append(f"别名「{a}」无效（为空或与主名相同）")
                elif na in self._name_index:
                    conflicts.append(f"别名「{a}」与已有主名冲突")
                elif na in self._alias_index:
                    conflicts.append(f"别名「{a}」已被其它计数器占用")

            if conflicts:
                yield event.plain_result("添加失败：\n- " + "\n- ".join(conflicts))
                return

            # 写入数据
            counters = self.data.setdefault("counters", {})
            counters[name] = {
                "count": int(
                    counters.get(name, {}).get("count", 0)
                ),  # 若之前存在则保留次数
                "aliases": aliases,
            }
            self._rebuild_index()
            await self._save()

        alias_info = "无" if not aliases else "、".join(aliases)
        yield event.plain_result(f"✅ 已添加计数器「{name}」有别名：{alias_info}")

    async def _cnt_del(self, event: AstrMessageEvent):
        """删除计数器：/cnt del <counter>（支持用别名指向主名）"""
        args = self._extract_args_after(event, "cnt", "del")
        if len(args) != 1:
            yield event.plain_result("用法：/cnt del <计数器名或其别名>")
            return

        token = args[0]
        n_token = self._norm(token)

        async with self._lock:
            # 允许用别名定位主名
            if n_token in self._alias_index:
                true_name = self._alias_index[n_token]
            elif n_token in self._name_index:
                true_name = self._name_index[n_token]
            else:
                yield event.plain_result(f"未找到计数器「{token}」")
                return

            # 删除并落盘
            self.data["counters"].pop(true_name, None)
            self._rebuild_index()
            await self._save()

        yield event.plain_result(f"🗑️ 已删除计数器「{true_name}」")

    async def _cnt_list(self, event: AstrMessageEvent):
        """列出所有计数器及次数：/cnt list"""
        counters = self.data.get("counters", {})
        if not counters:
            yield event.plain_result(
                "当前没有任何计数器。可用：/cnt add <计数器名> [别名…]"
            )
            return

        # 按次数降序显示，便于查看热度
        items = sorted(
            counters.items(), key=lambda kv: int(kv[1].get("count", 0)), reverse=True
        )
        lines = ["📊 当前计数器列表："]
        for name, meta in items:
            cnt = int(meta.get("count", 0))
            aliases = meta.get("aliases", []) or []
            alias_str = "无" if not aliases else "、".join(aliases)
            lines.append(f"  {name}：{cnt} 次；别名：{alias_str}")
        yield event.plain_result("\n".join(lines))

    # ------------------------- 事件监听：自动计数 +1 -------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """
        监听所有消息，若消息文本包含任一计数器主名或其别名的“子串”，则为该计数器 +1。
        - 忽略机器人自己发的消息
        - 忽略以 /cnt 开头的指令消息，避免误计数
        - 每个计数器每条消息最多 +1 次（同条消息内多次出现也只加 1）
        """
        # 忽略自身与空消息
        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except AttributeError:
            # 某些平台/版本可能无该方法，忽略检查
            pass
        text = (event.message_str or "").strip()
        if not text:
            return

        # 忽略本插件的指令消息
        if text.startswith("/cnt"):
            return

        tnorm = self._norm(text)
        hit_names: List[str] = []

        async with self._lock:
            for name, meta in self.data.get("counters", {}).items():
                # 对每个计数器，任一命中则 +1（不累加多次）
                patterns = [name] + (meta.get("aliases", []) or [])
                for p in patterns:
                    if not p:
                        continue
                    if self._norm(p) and self._norm(p) in tnorm:
                        self.data["counters"][name]["count"] = (
                            int(meta.get("count", 0)) + 1
                        )
                        hit_names.append(name)
                        break  # 该计数器已命中一次，跳到下一个计数器
            if hit_names:
                await self._save()

        if self.notify_on_increment and hit_names:
            # 单个命中：走特殊规则或默认提示；多个命中：逐个展示准确计数
            if len(hit_names) == 1:
                name = hit_names[0]
                count = int(self.data["counters"][name]["count"])
                special = self._get_special_message(name, count)
                if special:
                    yield event.plain_result(special)
                else:
                    yield event.plain_result(f"累计 {name} {count}/114514")
            else:
                parts = [
                    f"{n}({int(self.data['counters'][n]['count'])})" for n in hit_names
                ]
                yield event.plain_result(f"累计 {'、'.join(parts)} /114514")

    # ------------------------- 特殊计数提示规则 -------------------------
    def _get_special_message(self, name: str, count: int) -> str | None:
        """返回某计数值的特殊提示文案；若无匹配返回 None。

        维护性：只需在集合中增减数值或添加分支即可。
        """
        group_map = (
            ({114, 1145, 11451, 114514}, lambda n, c: f"恶臭的计数器就是「{n}」啦~~~"),
            ({1919, 19191, 191919}, lambda n, c: f"就这？————「{n}」"),
            ({520, 1314}, lambda n, c: f"💗💗💗我爱你! 一生一世! ————「{n}」"),
            ({6, 66, 666, 6666}, lambda n, c: f"{n}, 6"),
            ({233, 2333, 23333}, lambda n, c: "23333————"),
            (
                {100, 1000, 10000, 100000},
                lambda n, c: f"🎉🎉🎉恭喜！计数器「{n}」达成 {c} 次！",
            ),
        )
        for nums, fmt in group_map:
            if count in nums:
                return fmt(name, count)
        return None
