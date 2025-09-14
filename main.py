from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

def _any_text_decorator():
    """返回一个尽可能广泛匹配文本消息的装饰器，兼容不同 AstrBot 版本。

    优先顺序：on_message()/message() -> on_content(r".+") -> on_text() -> no-op
    """
    # 1. on_message 或 message（文档推荐全消息监听）
    for name in ("on_message", "message"):
        deco = getattr(filter, name, None)
        if callable(deco):
            try:
                return deco()
            except Exception:
                pass

    # 2. on_content 正则全匹配
    deco = getattr(filter, "on_content", None)
    if callable(deco):
        try:
            return deco(r".+")
        except Exception:
            pass

    # 3. 直接 on_text()
    deco = getattr(filter, "on_text", None)
    if callable(deco):
        try:
            return deco()
        except Exception:
            pass

    # 4. 兜底：返回一个 no-op 装饰器，避免导入时报错
    def _noop(fn):
        return fn

    return _noop


ANY_TEXT = _any_text_decorator()


@register("counter", "astrbot_plugin_counter", "基于关键字的计数器插件", "0.1.0")
class CounterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._data_path = self._resolve_data_path()
        self._counters: Dict[str, Dict[str, object]] = {}
        # 运行期快速映射：别名/主名 -> 主名
        self._alias_map: Dict[str, str] = {}

    async def initialize(self):
        """加载数据文件。"""
        self._load()

    async def terminate(self):
        """保存数据文件。"""
        self._save()

    # =====================
    # 指令：/cnt ...
    # =====================
    @filter.command("cnt")
    async def cnt(self, event: AstrMessageEvent):
        """计数器管理：/cnt add <名称> [别名...]；/cnt del <名称|别名>；/cnt list"""
        raw = (event.message_str or "").strip()
        tokens = raw.split()
        # 兼容以 /cnt 开头或仅携带参数的情况
        if tokens and tokens[0].lstrip("/").lower() == "cnt":
            tokens = tokens[1:]

        if not tokens:
            yield event.plain_result(self._usage())
            return

        sub = tokens[0].lower()
        if sub == "add":
            if len(tokens) < 2:
                yield event.plain_result("用法：/cnt add <名称> [别名1 别名2 ...]")
                return
            name = tokens[1]
            aliases = tokens[2:]
            ok, msg = self._add_counter(name, aliases)
            if ok:
                alias_text = "、".join(aliases) if aliases else "无"
                yield event.plain_result(f"已添加计数器“{name}”。别名：{alias_text}。当前计数：0")
            else:
                yield event.plain_result(msg)
            return

        if sub == "del":
            if len(tokens) < 2:
                yield event.plain_result("用法：/cnt del <名称或别名>")
                return
            # 仅群聊中限制为管理员可删除；私聊则默认允许
            try:
                if self._is_group_message(event) and not self._is_group_admin(event):
                    yield event.plain_result("权限不足：仅群管理员可删除计数器。")
                    return
            except Exception:
                # 万一检测异常，出于安全考虑仍然阻止删除
                yield event.plain_result("权限检测失败：仅群管理员可删除计数器。")
                return
            key = tokens[1]
            ok, msg = self._delete_counter(key)
            yield event.plain_result(msg)
            return

        if sub == "list":
            text = self._list_counters()
            yield event.plain_result(text)
            return

        # 未知子命令
        yield event.plain_result(self._usage())

    # =====================
    # 文本检测：任意消息中包含计数器名或别名则 +1
    # =====================
    @ANY_TEXT
    async def on_any_text(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return
        # 避免对管理指令本身计数
        if text.startswith("/cnt"):
            return

        # 找出本条消息触发的所有主计数器名（同一主计数器仅+1一次）
        triggered: Set[str] = set()
        for main, meta in self._counters.items():
            names: List[str] = [main] + list(meta.get("aliases", []))  # type: ignore[arg-type]
            for n in names:
                if n and n in text:
                    triggered.add(main)
                    break

        if not triggered:
            return

        # 统一加一并持久化
        for main in triggered:
            self._counters[main]["count"] = int(self._counters[main].get("count", 0)) + 1
        self._save()

        # 回复简要统计
        parts = [f"{name}({self._counters[name]['count']})" for name in sorted(triggered)]
        yield event.plain_result("计数 +1 → " + "，".join(parts))

    # =====================
    # 内部方法
    # =====================
    def _usage(self) -> str:
        return (
            "用法：\n"
            "- /cnt add <名称> [别名1 别名2 ...]\n"
            "- /cnt del <名称或别名>\n"
            "- /cnt list"
        )

    def _rebuild_alias_map(self):
        self._alias_map.clear()
        for main, meta in self._counters.items():
            self._alias_map[main] = main
            for a in meta.get("aliases", []):
                self._alias_map[str(a)] = main

    def _add_counter(self, name: str, aliases: List[str]) -> Tuple[bool, str]:
        name = name.strip()
        aliases = [a.strip() for a in aliases if a.strip()]
        if not name:
            return False, "名称不能为空。"

        # 冲突检测：名称或任一别名与现有主名/别名冲突
        for item in [name] + aliases:
            if item in self._alias_map:
                exist_main = self._alias_map[item]
                return False, f"“{item}”已存在（归属计数器“{exist_main}”）。"

        if name not in self._counters:
            self._counters[name] = {"count": 0, "aliases": []}

        exists_aliases = set(self._counters[name].get("aliases", []))
        exists_aliases.update(aliases)
        self._counters[name]["aliases"] = sorted(list(exists_aliases))
        self._rebuild_alias_map()
        self._save()
        return True, "OK"

    def _delete_counter(self, key: str) -> Tuple[bool, str]:
        key = key.strip()
        if not key:
            return False, "名称不能为空。"
        if key not in self._alias_map:
            return False, f"未找到计数器或别名：“{key}”。"
        main = self._alias_map[key]
        # 删除主计数器
        if main in self._counters:
            del self._counters[main]
        self._rebuild_alias_map()
        self._save()
        return True, f"已删除计数器“{main}”。"

    def _list_counters(self) -> str:
        if not self._counters:
            return "暂无计数器。使用 /cnt add <名称> 添加。"
        lines: List[str] = []
        for main in sorted(self._counters.keys()):
            meta = self._counters[main]
            count = int(meta.get("count", 0))
            aliases = list(meta.get("aliases", []))
            if aliases:
                lines.append(f"{main}：{count}（别名：{', '.join(aliases)}）")
            else:
                lines.append(f"{main}：{count}")
        return "\n".join(lines)

    def _load(self):
        try:
            if self._data_path.exists():
                data = json.loads(self._data_path.read_text(encoding="utf-8"))
                self._counters = data.get("counters", {})
            else:
                self._counters = {}
        except Exception as e:
            logger.error(f"读取数据失败：{e}")
            self._counters = {}
        self._rebuild_alias_map()

    def _save(self):
        try:
            payload = {"counters": self._counters}
            # 确保父目录存在
            self._data_path.parent.mkdir(parents=True, exist_ok=True)
            self._data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"保存数据失败：{e}")

    # =====================
    # 权限与上下文检测（适配不同适配器的可能接口）
    # =====================
    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        candidates = [
            "is_group",
            "is_group_message",
        ]
        for name in candidates:
            attr = getattr(event, name, None)
            if isinstance(attr, bool):
                return attr
            if callable(attr):
                try:
                    res = attr()
                    if isinstance(res, bool):
                        return res
                except Exception:
                    pass
        # 如果存在 group_id 之类的字段，也认为是群聊
        if getattr(event, "group_id", None) is not None:
            return True
        return False

    def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        # 直接布尔接口
        bool_candidates = [
            "is_group_admin",
            "sender_is_admin",
        ]
        for name in bool_candidates:
            attr = getattr(event, name, None)
            if isinstance(attr, bool):
                return attr
            if callable(attr):
                try:
                    res = attr()
                    if isinstance(res, bool):
                        return res
                except Exception:
                    pass

        # 角色字符串接口
        role_candidates = [
            "get_sender_role",
            "get_sender_permission",
            "get_member_role",
            "sender_role",
        ]
        for name in role_candidates:
            attr = getattr(event, name, None)
            role = None
            if isinstance(attr, str):
                role = attr
            elif callable(attr):
                try:
                    role = attr()
                except Exception:
                    role = None
            if isinstance(role, str):
                r = role.lower()
                if any(k in r for k in ["admin", "administrator", "owner", "manager", "群主", "管理员"]):
                    return True
                return False

        # 默认非管理员
        return False

    # =====================
    # 数据目录解析（遵循 AstrBot data 目录规范）
    # =====================
    def _resolve_data_path(self) -> Path:
        base: Path | None = None
        ctx = getattr(self, "context", None)

        # 优先使用上下文提供的数据目录（尽量兼容不同 API 命名）
        candidate_attrs = [
            "get_data_dir",
            "get_data_path",
            "get_plugin_data_dir",
            "data_dir",
            "data_path",
        ]
        for name in candidate_attrs:
            attr = getattr(ctx, name, None)
            try:
                if callable(attr):
                    p = attr()  # 期望返回字符串或 Path
                else:
                    p = attr
                if p:
                    base = Path(str(p))
                    break
            except Exception:
                continue

        # 退化：使用工作目录下 data/counter
        if base is None:
            base = Path.cwd() / "data"

        data_dir = base / "counter"
        return data_dir / "data.json"
