import json
import time
import random
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig


def _parse_id_list(value) -> set:
    """解析 list 或逗号分隔的群号字符串为集合（兼容旧版字符串格式）。"""
    if isinstance(value, list):
        return set(str(item) for item in value if str(item).strip())
    if not isinstance(value, str):
        return set()
    return set(item.strip() for item in value.split(",") if item.strip())


def _serialize_id_list(id_set: set) -> list:
    """序列化群号集合为列表，与 _conf_schema.json 的 list 类型对应。"""
    return sorted(id_set)


def _parse_group_templates(value) -> dict:
    """解析群欢迎语模板 JSON。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception as e:
        logger.error(f"[group_welcome] 解析群模板失败: {e}")
        return {}


def _serialize_group_templates(templates: dict) -> str:
    """序列化群欢迎语模板。"""
    return json.dumps(templates, ensure_ascii=False)


def _parse_template_list(value) -> list:
    """解析模板库配置（template_list 类型）。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.append(item)
    return result


class GroupWelcomePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 锁初始化 (Python 3.10+ 安全)
        self._lock = asyncio.Lock()

        # 运行状态标记
        self._is_running = True

        # 机器人自身 ID 缓存（用于跳过"机器人入群欢迎自己"）
        # v2.6.0: 改为按 client 分开缓存（多 bot 各查各的）
        self._self_id_cache = {}  # id(client) -> str(QQ号)
        self._self_id_map = {}    # str(QQ号) -> client（事件 self_id 路由用）
        self._qq_cache_ts = 0.0   # self_id 缓存时间戳（600s 过期）

        # v2.6.0: 多 bot 欢迎白名单（留空=全部 aiocqhttp 实例）
        self._welcome_bots = [str(x).strip()
                              for x in (config.get("welcome_bots") or [])
                              if str(x).strip()]

        # 【Fix #2】改为实例变量，避免热重载时状态残留
        self._global_cooldown = {}
        self._last_cleanup_time = 0

        # 配置加载
        self._enable_member_count: bool = config.get("enable_member_count", True)
        self._enable_private_rules: bool = config.get("enable_private_rules", False)
        self._enable_ai_welcome: bool = config.get("enable_ai_welcome", False)
        self._ai_retry_count: int = config.get("ai_retry_count", 0)
        self._welcome_image_url: str | list = config.get("welcome_image_url", "")

        self._whitelist: set = _parse_id_list(config.get("group_whitelist", []))
        self._blacklist: set = _parse_id_list(config.get("group_blacklist", []))

        # 迁移旧版字符串格式 → list，避免 UI 把字符串逐字符展开
        self._migrate_id_lists()

        # 迁移旧版单张图片字符串 → list
        self._migrate_image_list()

        self.cooldown_file = StarTools.get_data_dir() / "cooldowns.json"

        # 加载持久化的冷却数据
        self._load_cooldowns()

        # 【Fix #1】保存 Task 引用，避免 GC 回收 & 支持 terminate() 主动取消
        self._register_task = asyncio.create_task(self._safe_register_handler())

    # ──────────────────────────────────────────
    # 生命周期管理
    # ──────────────────────────────────────────

    async def _safe_register_handler(self):
        """稳健的事件监听注册逻辑（v2.6.0 多 bot 版）。

        遍历当前全部可用 aiocqhttp 客户端（多个 QQ 号 = 多个实例），
        为每个 bot 分别注册 group_increase 监听；事件到达后按 self_id
        （OneBot 事件自带的 bot QQ 号）路由到对应 client，避免串号。
        """
        max_retries = 15
        for _ in range(max_retries):
            if not self._is_running:
                return

            clients = self._all_clients()
            if clients:
                try:
                    for client in clients:
                        if not hasattr(client, "on_notice"):
                            continue
                        # 闭包绑定当前 client
                        bound_client = client

                        @bound_client.on_notice("group_increase")
                        async def _group_increase_handler(event):
                            if not self._is_running:
                                return
                            await self._on_notice(event, bound_client)

                    logger.info(
                        "[group_welcome] OneBot 11 入群事件监听已注册："
                        f"共 {len(clients)} 个 bot（{self._describe_bots(clients)}）。"
                    )
                    return
                except Exception as e:
                    logger.error(f"[group_welcome] 注册监听失败: {e}")

            await asyncio.sleep(5)

        logger.warning("[group_welcome] 超时未找到 OneBot 适配器，插件功能可能受限。")

    def _all_clients(self) -> list:
        """返回当前所有可用 aiocqhttp 客户端（call_action 形态，鸭子类型）。

        兼容旧版 platform_manager.get_insts() 与新版 platform_insts 属性。
        """
        try:
            mgr = getattr(self.context, "platform_manager", None)
            insts = []
            if mgr is not None:
                insts = list(getattr(mgr, "platform_insts", None) or [])
                if not insts and hasattr(mgr, "get_insts"):
                    try:
                        insts = list(mgr.get_insts())
                    except Exception:
                        insts = []
        except Exception:
            insts = []
        out = []
        for adapter in insts or []:
            try:
                if (hasattr(adapter, "bot") and adapter.bot
                        and hasattr(adapter.bot, "call_action")):
                    out.append(adapter.bot)
            except Exception:
                continue
        return out

    def _get_client(self):
        """获取第一个可用客户端（多 bot 兼容的兜底/旧接口）。

        事件链路请用 _client_for_event(event) 按 self_id 精确路由；
        此方法仅在无法从事件判断 bot 时回退使用。
        """
        try:
            clients = self._all_clients()
            if clients:
                return clients[0]
        except Exception as e:
            logger.debug(f"[group_welcome] _get_client 遍历适配器异常: {e}")
        return None

    def _describe_bots(self, clients: list) -> str:
        """日志用：尽量显示 bot 的 QQ 号（从缓存读取，未探测到则显示序号）。"""
        parts = []
        for idx, client in enumerate(clients, 1):
            qq = self._self_id_cache.get(id(client), "")
            parts.append(qq or f"#{idx}")
        return ", ".join(parts)

    async def _bot_self_id(self, client) -> str:
        """获取某 client 的登录 QQ 号（get_login_info），结果缓存 600s。"""
        import time as _t
        now = _t.time()
        if now - self._qq_cache_ts > 600:
            self._qq_cache_ts = now
        cid = id(client)
        if cid in self._self_id_cache:
            return self._self_id_cache[cid]
        qq = ""
        try:
            res = await client.call_action("get_login_info")
            qq = str(res.get("user_id", ""))
        except Exception as e:
            logger.debug(f"[group_welcome] 获取登录信息失败: {e}")
        self._self_id_cache[cid] = qq
        if qq:
            self._self_id_map[qq] = client
        return qq

    async def _client_for_event(self, event) -> object:
        """按事件 self_id（bot 的 QQ 号）路由到对应 client。

        事件带 self_id 且匹配成功 → 返回对应 client；
        否则返回第一个可用 client（兼容无 self_id 的旧事件源）。
        """
        clients = self._all_clients()
        if not clients:
            return None
        try:
            sid = str(event.get("self_id") or "")
        except Exception:
            sid = ""
        if sid:
            # 优先查路由缓存；未命中则逐个探测并回填
            if sid in self._self_id_map:
                return self._self_id_map[sid]
            for client in clients:
                qq = await self._bot_self_id(client)
                if qq == sid:
                    return client
        return clients[0]

    def _bot_enabled(self, qq: str) -> bool:
        """welcome_bots 白名单过滤：留空=全部 bot 生效。"""
        if not self._welcome_bots:
            return True
        return bool(qq) and qq in self._welcome_bots

    async def terminate(self):
        """插件卸载回调。"""
        self._is_running = False
        # 【Fix #1】主动取消 Task，避免插件卸载后幽灵任务残留
        if self._register_task and not self._register_task.done():
            self._register_task.cancel()
            try:
                await self._register_task
            except asyncio.CancelledError:
                pass
        self._save_cooldowns()
        logger.info("[group_welcome] 插件已卸载，冷却数据已保存。")

    # ──────────────────────────────────────────
    # 冷却数据持久化
    # ──────────────────────────────────────────

    def _load_cooldowns(self):
        """从文件加载冷却数据。"""
        if not self.cooldown_file.exists():
            return
        try:
            with open(self.cooldown_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                now = time.time()
                count = 0
                for k, v in data.items():
                    if now - v < 86400:
                        self._global_cooldown[k] = v
                        count += 1
                logger.debug(f"[group_welcome] 已加载 {count} 条有效冷却记录。")
        except Exception as e:
            logger.warning(f"[group_welcome] 加载冷却文件失败: {e}")

    def _save_cooldowns(self):
        """保存冷却数据到文件。"""
        try:
            with open(self.cooldown_file, "w", encoding="utf-8") as f:
                json.dump(self._global_cooldown, f)
        except Exception as e:
            logger.warning(f"[group_welcome] 保存冷却数据失败: {e}")

    # ──────────────────────────────────────────
    # 核心逻辑
    # ──────────────────────────────────────────

    async def _is_self_user(self, client, user_id: str) -> bool:
        """判断 user_id 是否为该 client 自身（OneBot get_login_info）。结果按 client 缓存。"""
        try:
            qq = await self._bot_self_id(client)
            return bool(qq) and user_id == qq
        except Exception as e:
            logger.debug(f"[group_welcome] 获取机器人自身信息失败: {e}")
            return False

    def _clean_expired_cooldowns(self):
        now = time.time()
        if now - self._last_cleanup_time < 3600:
            return

        expired = [k for k, ts in self._global_cooldown.items() if now - ts > 86400]
        for key in expired:
            del self._global_cooldown[key]

        self._last_cleanup_time = now
        self._save_cooldowns()

    async def _on_notice(self, event, client=None):
        """群成员增加事件处理（v2.6.0 多 bot）。

        client：触发该事件的 bot 客户端（监听闭包绑定传入）；
        为空时按事件 self_id 自动路由到对应 bot。
        """
        try:
            notice_type = event.get("notice_type")
            group_id = str(event.get("group_id", ""))
            user_id = str(event.get("user_id", ""))
        except Exception:
            return

        if notice_type != "group_increase" or not group_id or not user_id:
            return

        if not self._check_group_allowed(group_id):
            return

        # v2.6.0: 多 bot 路由 + welcome_bots 白名单
        if client is None:
            client = await self._client_for_event(event)
        if client is None:
            return
        qq = await self._bot_self_id(client)
        if not self._bot_enabled(qq):
            logger.debug(f"[group_welcome] bot[{qq or '?'}] 不在 welcome_bots"
                         f" 中，群 {group_id} 欢迎跳过")
            return
        # v2.6.0: 群已被其他 bot 的专属模板认领（exclusive）→ 本 bot 跳过
        if self._group_claimed_by_other(group_id, qq):
            logger.info(f"[group_welcome] 群 {group_id} 由其他 bot 的专属模板"
                        f"负责，bot[{qq or '?'}] 跳过")
            return

        self._clean_expired_cooldowns()

        key = f"{group_id}:{user_id}"
        cooldown = self._get_resolved_config(
            group_id, "cooldown_seconds",
            self.config.get("cooldown_seconds", 300), qq)

        async with self._lock:
            now = time.time()
            if now - self._global_cooldown.get(key, 0) < cooldown:
                return
            self._global_cooldown[key] = now

        # 【Fix #3】机器人自己入群时不欢迎自己
        if await self._is_self_user(client, user_id):
            logger.info(f"[group_welcome] 检测到机器人自身 ({user_id}) 入群，跳过欢迎。")
            return

        name = await self._get_member_name(client, group_id, user_id)

        count_text = ""
        if self._get_resolved_config(group_id, "enable_member_count",
                                     self._enable_member_count, qq):
            count = await self._get_group_member_count(client, group_id)
            if count:
                count_text = f"\n你是当前群里第 {count} 位成员！"

        # 生成欢迎语
        template = self._get_welcome_template(group_id, qq)

        try:
            welcome_text = template.format(name=name, count_text=count_text)
        except Exception as e:
            logger.warning(f"[group_welcome] 群 {group_id} 欢迎语模板格式错误: {e}")
            welcome_text = f"🎉 欢迎 {name} 加入本群！{count_text}"

        if self._get_resolved_config(group_id, "enable_ai_welcome",
                                     self._enable_ai_welcome, qq):
            ai_text = await self._gen_ai_welcome(group_id, name, qq)
            if ai_text:
                welcome_text += f"\n\n✨ {ai_text}"

        await self._send_group_welcome(client, group_id, user_id, welcome_text)

        if self._get_resolved_config(group_id, "enable_private_rules",
                                     self._enable_private_rules, qq):
            rules = self._get_resolved_config(
                group_id, "group_rules",
                self.config.get("group_rules", "📋 请遵守群规，友善交流！"), qq)
            await self._send_private_rules(client, user_id, rules)

    def _check_group_allowed(self, group_id: str) -> bool:
        if self._whitelist:
            return group_id in self._whitelist
        return group_id not in self._blacklist

    def _get_welcome_template(self, group_id: str, self_id: str = "") -> str:
        # 优先级1：模板库中匹配当前群 + 当前 bot 的模板欢迎语
        tpl = self._get_template_for_group(group_id, self_id)
        if tpl:
            tpl_text = tpl.get("template_text")
            if isinstance(tpl_text, str) and tpl_text.strip():
                return tpl_text
        # 优先级2：旧版群专属欢迎语（/welcome set 配置）
        templates = self._load_group_templates()
        default = "🎉 欢迎 {name} 加入本群！很高兴认识你～{count_text}"
        # 优先级3：全局默认欢迎语
        return templates.get(group_id, self.config.get("welcome_template", default))

    def _load_template_list(self) -> list:
        """加载模板库配置。"""
        return _parse_template_list(self.config.get("group_template_list", []))

    def _template_matches_bot(self, tpl: dict, self_id: str) -> bool:
        """v2.6.0: 模板的 bot_ids 匹配。留空 = 所有 bot 适用；
        非空 = 仅名单内 bot（按 QQ 号）适用。"""
        bot_ids = tpl.get("bot_ids") or []
        if not bot_ids:
            return True
        if not self_id:
            return False  # 未知来源不匹配专属模板
        return self_id in {str(b) for b in bot_ids}

    def _group_claimed_by_other(self, group_id: str, self_id: str) -> bool:
        """v2.6.0: 群是否存在"绑定其他 bot 且 exclusive"的专属模板。

        存在 → 说明该群由其他 bot 专门负责，当前 bot（self_id）
        不应再欢迎此群（避免同群多 bot 重复欢迎）。
        """
        try:
            for tpl in self._load_template_list():
                ids = tpl.get("group_ids") or []
                if group_id not in {str(i) for i in ids}:
                    continue
                bot_ids = tpl.get("bot_ids") or []
                if not bot_ids:
                    continue
                if not bool(tpl.get("exclusive")):
                    continue
                if self_id and self_id in {str(b) for b in bot_ids}:
                    continue  # 自己就是负责人
                return True
        except Exception:
            pass
        return False

    def _get_template_for_group(self, group_id: str,
                                self_id: str = "") -> dict | None:
        """查找 group_ids 包含当前群号且 bot 匹配的模板。

        v2.6.0 返回优先级：bot 专属（bot_ids 含 self_id）→ 通用
        （bot_ids 留空）。若群已被其他 bot 的 exclusive 专属模板认领，
        通用模板同样不可用（返回 None）。
        """
        matched_any = None
        for tpl in self._load_template_list():
            ids = tpl.get("group_ids") or []
            if group_id not in {str(i) for i in ids}:
                continue
            if not self._template_matches_bot(tpl, self_id):
                continue
            if tpl.get("bot_ids"):
                return tpl  # bot 专属命中优先返回
            if matched_any is None:
                matched_any = tpl
        if matched_any is None:
            return None
        if self._group_claimed_by_other(group_id, self_id):
            return None  # 群已被其他 bot 专属认领 → 通用模板不生效
        return matched_any

    def _get_resolved_config(self, group_id: str, key: str,
                             global_value, self_id: str = ""):
        """三级 fallback：模板字段(非空) → 全局配置 → 全局默认。

        v2.6.0 增加 self_id（bot QQ 号）维度：模板按 群+bot 匹配；
        模板里的 bool/int 字段始终以模板为准（模板条目的 default 已由
        WebUI 填充），字符串/list 字段为空时回退到全局配置。
        """
        tpl = self._get_template_for_group(group_id, self_id)
        if tpl is None:
            return global_value
        value = tpl.get(key)
        if value is None:
            return global_value
        if isinstance(value, (str, list)) and not value:
            return global_value
        return value

    async def _get_member_name(self, client, group_id: str, user_id: str) -> str:
        try:
            if not group_id.isdigit() or not user_id.isdigit():
                return user_id
            res = await client.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            return res.get("card") or res.get("nickname") or user_id
        except Exception as e:
            logger.debug(f"[group_welcome] 获取成员信息失败: {e}")
            return user_id

    async def _get_group_member_count(self, client, group_id: str):
        try:
            if not group_id.isdigit():
                return None
            res = await client.call_action(
                "get_group_info", group_id=int(group_id), no_cache=True
            )
            return res.get("member_count")
        except Exception:
            return None

    async def _send_group_welcome(self, client, group_id: str, user_id: str, text: str):
        try:
            if not group_id.isdigit() or not user_id.isdigit():
                return
            message = [
                {"type": "at", "data": {"qq": user_id}},
                {"type": "text", "data": {"text": f" {text}"}},
            ]
            image_urls = self._get_resolved_image_urls(group_id)
            if image_urls:
                image_url = random.choice(image_urls)
                message.append({"type": "image", "data": {"file": image_url}})
                logger.debug(f"[group_welcome] 已随机附加欢迎图片: {image_url}")
            await client.call_action(
                "send_group_msg", group_id=int(group_id), message=message
            )
        except Exception as e:
            logger.error(f"[group_welcome] 发送欢迎语异常: {e}")

    def _get_resolved_image_urls(self, group_id: str) -> list:
        """获取当前群欢迎图片池。模板优先，否则用全局配置。兼容旧版字符串配置。"""
        urls = self._get_resolved_config(group_id, "welcome_image_url", self._welcome_image_url)
        if isinstance(urls, str):
            urls = [urls] if urls.strip() else []
        elif not isinstance(urls, list):
            urls = []
        urls = [u for u in urls if isinstance(u, str) and u.strip()]
        for image_url in urls:
            self._warn_invalid_image_url(image_url)
        return urls

    @staticmethod
    def _warn_invalid_image_url(image_url: str):
        """对格式可疑的图片 URL 记录警告，便于排查。"""
        try:
            if not image_url.lower().startswith(("http://", "https://", "file:///")):
                logger.warning(
                    f"[group_welcome] 欢迎图片 URL 格式可能无效: {image_url}"
                )
                return
            image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
            non_image_exts = (".mp4", ".avi", ".zip", ".exe", ".pdf", ".txt", ".svg")
            base = image_url.lower().split("?")[0]
            if not base.endswith(image_exts) and base.endswith(non_image_exts):
                logger.warning(
                    f"[group_welcome] 欢迎图片后缀不是常见图片格式: {image_url}"
                )
        except Exception:
            pass

    async def _send_private_rules(self, client, user_id: str, rules: str):
        await asyncio.sleep(2)
        try:
            if not user_id.isdigit():
                return
            await client.call_action(
                "send_private_msg", user_id=int(user_id), message=rules
            )
        except Exception as e:
            logger.warning(f"[group_welcome] 私聊发送群规失败: {e}")

    async def _gen_ai_welcome(self, group_id: str, name: str,
                              self_id: str = "") -> str:
        """
        使用指定的 LLM Provider 生成欢迎语，支持配置重试次数。
        模板配置优先（群+bot 维度），空值回退全局配置。
        """
        try:
            provider_id = self._get_resolved_config(
                group_id, "llm_provider",
                self.config.get("llm_provider", ""), self_id)
            provider = None

            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    logger.warning(
                        f"[group_welcome] 未找到指定的 LLM ({provider_id})，回退到默认模型。"
                    )
                    provider = self.context.get_using_provider()
            else:
                provider = self.context.get_using_provider()

            if not provider:
                return ""

            prompt_fmt = self._get_resolved_config(
                group_id,
                "ai_welcome_prompt",
                self.config.get(
                    "ai_welcome_prompt",
                    "请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语：{name}",
                ),
                self_id,
            )

            final_prompt = prompt_fmt.replace("{name}", name)
            if not final_prompt.strip():
                final_prompt = (
                    f"请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语：{name}"
                )

            retry_count = self._get_resolved_config(
                group_id, "ai_retry_count", self._ai_retry_count, self_id)
            last_error = None
            for attempt in range(retry_count + 1):
                try:
                    resp = await provider.text_chat(
                        prompt=final_prompt, session_id=f"gw_{name}"
                    )
                    return resp.completion_text.strip()
                except Exception as e:
                    last_error = e
                    logger.debug(
                        f"[group_welcome] AI 生成第 {attempt + 1}/{retry_count + 1} 次尝试失败: {e}"
                    )
            logger.warning(
                f"[group_welcome] AI 生成 {retry_count + 1} 次全部失败: {last_error}"
            )
            return ""
        except Exception as e:
            logger.warning(f"[group_welcome] AI 生成失败: {e}")
            return ""

    # ──────────────────────────────────────────
    # 配置辅助
    # ──────────────────────────────────────────
    def _migrate_id_lists(self) -> None:
        """若白/黑名单配置仍是旧版字符串，一次性转换为 list 并写回，防止 UI 逐字符展开。"""
        changed = False
        for key, id_set in [
            ("group_whitelist", self._whitelist),
            ("group_blacklist", self._blacklist),
        ]:
            if isinstance(self.config.get(key), str):
                self.config[key] = _serialize_id_list(id_set)
                logger.info(
                    f"[group_welcome] 已将 {key} 从旧版字符串格式迁移为列表格式。"
                )
                changed = True
        if changed:
            self.config.save_config()

    def _migrate_image_list(self) -> None:
        """迁移旧版单张图片字符串 → list，避免 UI 把字符串逐字符展开。"""
        value = self.config.get("welcome_image_url")
        if isinstance(value, str):
            self.config["welcome_image_url"] = (
                [value] if value.strip() else []
            )
            self._welcome_image_url = self.config["welcome_image_url"]
            self.config.save_config()
            logger.info("[group_welcome] 已将 welcome_image_url 从旧版字符串格式迁移为列表格式。")

    def _save_switches(self):
        self.config["enable_member_count"] = self._enable_member_count
        self.config["enable_private_rules"] = self._enable_private_rules
        self.config["enable_ai_welcome"] = self._enable_ai_welcome
        self.config.save_config()

    def _save_lists(self):
        self.config["group_whitelist"] = _serialize_id_list(self._whitelist)
        self.config["group_blacklist"] = _serialize_id_list(self._blacklist)
        self.config.save_config()

    def _load_group_templates(self) -> dict:
        return _parse_group_templates(self.config.get("group_templates", "{}"))

    def _save_group_template(self, group_id: str, template: str):
        templates = self._load_group_templates()
        templates[group_id] = template
        self.config["group_templates"] = _serialize_group_templates(templates)
        self.config.save_config()

    def _del_group_template(self, group_id: str):
        templates = self._load_group_templates()
        if templates.pop(group_id, None):
            self.config["group_templates"] = _serialize_group_templates(templates)
            self.config.save_config()

    # ──────────────────────────────────────────
    # 指令
    # ──────────────────────────────────────────

    @filter.command_group("welcome")
    async def welcome(self, event: AstrMessageEvent):
        """指令入口。"""
        pass

    @welcome.command("count")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_count(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_member_count = True
            self._save_switches()
            yield event.plain_result("✅ 群人数统计已开启")
        elif action == "off":
            self._enable_member_count = False
            self._save_switches()
            yield event.plain_result("🔕 群人数统计已关闭")
        else:
            status = "开启" if self._enable_member_count else "关闭"
            yield event.plain_result(
                f"当前群人数统计：{status}\n用法：/welcome count on|off"
            )

    @welcome.command("rules")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_rules(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_private_rules = True
            self._save_switches()
            yield event.plain_result("✅ 私聊群规已开启")
        elif action == "off":
            self._enable_private_rules = False
            self._save_switches()
            yield event.plain_result("🔕 私聊群规已关闭")
        else:
            status = "开启" if self._enable_private_rules else "关闭"
            yield event.plain_result(
                f"当前私聊群规：{status}\n用法：/welcome rules on|off"
            )

    @welcome.command("ai")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_ai(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_ai_welcome = True
            self._save_switches()
            yield event.plain_result("✅ AI 个性化欢迎语已开启")
        elif action == "off":
            self._enable_ai_welcome = False
            self._save_switches()
            yield event.plain_result("🔕 AI 个性化欢迎语已关闭")
        else:
            status = "开启" if self._enable_ai_welcome else "关闭"
            yield event.plain_result(
                f"当前 AI 欢迎语：{status}\n用法：/welcome ai on|off"
            )

    @welcome.command("set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group_template(self, event: AstrMessageEvent):
        """设置欢迎语。"""
        raw_msg = event.message_obj.message_str.strip()
        parts = raw_msg.split(maxsplit=2)
        text_content = parts[2].strip() if len(parts) > 2 else ""
        text = text_content.replace("｛", "{").replace("｝", "}")

        current_group_id = (
            str(event.message_obj.group_id) if event.message_obj.group_id else ""
        )

        target_group_id = current_group_id
        final_content = text
        op_type = "set"

        if not current_group_id:
            sub_parts = text.split(maxsplit=1)
            first_word = sub_parts[0] if sub_parts else ""

            if first_word.isdigit():
                target_group_id = first_word
                remaining = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                if remaining in ["reset", "show"]:
                    op_type = remaining
                else:
                    final_content = remaining
            elif first_word in ["reset", "show"]:
                op_type = first_word
                if len(sub_parts) < 2:
                    yield event.plain_result(
                        f"❌ 私聊请指定群号，例如：/welcome set {first_word} 123456"
                    )
                    return
                target_group_id = sub_parts[1].strip()
                if not target_group_id.isdigit():
                    yield event.plain_result(f"❌ 群号格式错误：{target_group_id}")
                    return
            else:
                yield event.plain_result("❌ 私聊模式请先写群号或操作(reset/show)。")
                return
        else:
            if text in ["reset", "show"]:
                op_type = text

        if op_type == "reset":
            self._del_group_template(target_group_id)
            yield event.plain_result(f"✅ 群 {target_group_id} 已恢复默认。")
        elif op_type == "show":
            sid = str(getattr(event.message_obj, "self_id", "") or "")
            tmpl = self._get_welcome_template(target_group_id, sid)
            yield event.plain_result(f"📋 群 {target_group_id} 当前欢迎语：\n{tmpl}")
        elif op_type == "set":
            if not final_content:
                yield event.plain_result("❌ 内容不能为空。")
                return
            self._save_group_template(target_group_id, final_content)
            yield event.plain_result(
                f"✅ 群 {target_group_id} 欢迎语已设置：\n{final_content}"
            )

    @welcome.command("wl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_whitelist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ):
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            content = "、".join(sorted(self._whitelist)) if self._whitelist else "空"
            yield event.plain_result(f"📋 白名单：{content}")
        elif action == "add" and group_id:
            self._whitelist.add(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已加入白名单 {group_id}")
        elif action == "del" and group_id:
            self._whitelist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已移除白名单 {group_id}")
        else:
            yield event.plain_result(
                "用法：\n/welcome wl add <群号>\n/welcome wl del <群号>\n/welcome wl list"
            )

    @welcome.command("bl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_blacklist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ):
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            content = "、".join(sorted(self._blacklist)) if self._blacklist else "空"
            yield event.plain_result(f"🚫 黑名单：{content}")
        elif action == "add" and group_id:
            self._blacklist.add(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已加入黑名单 {group_id}")
        elif action == "del" and group_id:
            self._blacklist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已移除黑名单 {group_id}")
        else:
            yield event.plain_result(
                "用法：\n/welcome bl add <群号>\n/welcome bl del <群号>\n/welcome bl list"
            )

    @welcome.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent, target_group: str = ""):
        target_group = target_group.strip()
        curr_gid = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        query_gid = target_group if target_group else curr_gid

        templates = self._load_group_templates()
        template_list = self._load_template_list()
        wl = "、".join(sorted(self._whitelist)) if self._whitelist else "（空）"
        bl = "、".join(sorted(self._blacklist)) if self._blacklist else "（空）"

        if query_gid:
            sid = str(getattr(event.message_obj, "self_id", "") or "")
            matched_tpl = self._get_template_for_group(query_gid, sid)
            if matched_tpl:
                source = f"模板库（第 {template_list.index(matched_tpl) + 1} 条）"
            elif query_gid in templates:
                source = "群专属"
            else:
                source = "全局默认"
            tip = f"📌 群 {query_gid} 欢迎语 [{source}]：\n{self._get_welcome_template(query_gid, sid)}"
        else:
            tip = (
                f"📌 已自定义群数：{len(templates)}\n"
                f"📦 模板库条数：{len(template_list)}\n"
                f"💡 提示：私聊可带群号查询。"
            )

        result = f"""📊 group_welcome 插件状态
{"─" * 24}
名单模式：{"白名单模式" if self._whitelist else "黑名单模式"}
白名单：{wl}
黑名单：{bl}
{"─" * 24}
群人数统计：{"✅ 开启" if self._enable_member_count else "🔕 关闭"}
私聊群规：{"✅ 开启" if self._enable_private_rules else "🔕 关闭"}
AI 个性欢迎：{"✅ 开启" if self._enable_ai_welcome else "🔕 关闭"}
冷却时间：{self.config.get("cooldown_seconds", 300)}s
{"─" * 24}
{tip}"""
        yield event.plain_result(result)
