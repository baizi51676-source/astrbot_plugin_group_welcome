# 更新日志 (CHANGELOG)

## [v2.6.0]
*   **Added**  : 多 bot 支持——一个 AstrBot 挂多个 QQ 号（多个 aiocqhttp 实例）时，为每个 bot 分别注册入群监听，事件按 self_id 路由到对应 bot，各 bot 互不串号、独立欢迎自己群里的新人
*   **Added**  : 新增配置 `welcome_bots`——留空=全部 bot 触发欢迎；填写平台实例 id 或登录 QQ 号=只在这些 bot 上启用欢迎
*   **Added**  : SnowLuma（NapCat 官方姊妹项目，OneBot v11）后端支持——事件与所用 API（get_group_member_info / get_group_info / send_group_msg 等）协议级兼容，无需额外配置
*   **Added**  : 群欢迎模板库支持绑定 bot——模板条目新增 `bot_ids`（适用 bot，留空=全部）与 `exclusive`（专属模式：开启后该群只由名单内 bot 欢迎，其他 bot 跳过，避免同群多 bot 重复欢迎）
*   **Changed**: 模板匹配优先级：bot 专属模板优先于通用模板；被其他 bot exclusive 认领的群，通用模板不再生效
*   **Changed**: 机器人自身 ID 缓存改为按 client 分别缓存（多 bot 各自判断"自己入群"）
*   **Docs**   : README 新增「多 bot 与 SnowLuma（v2.6.0）」章节与配置说明

## [v2.5.0]
*   **Fix**    : 修复机器人被拉入群时向自己发送欢迎语的问题，检测到机器人自身入群时自动跳过
*   **Added**  : 新增群欢迎模板库（template_list），支持按群号匹配套用多套欢迎配置，模板未填写的项回退到全局配置
*   **Added**  : 欢迎图片支持配置多张，每次入群随机发送一张（全局与模板均支持，旧版单张配置自动迁移）
*   **Changed**: 模板库中的开关项（群人数统计/私聊群规/AI欢迎）按群生效，AI 模型、提示词、重试次数、冷却时间也支持按群配置
*   **Changed**: 调整 _conf_schema.json 中白名单/黑名单字段顺序，并新增模板库配置项

## [v2.4.0]
*   **Added**  : AI 欢迎语支持可配置重试次数（0-5），管理面板呈滑块控件
*   **Added**  : 支持附带欢迎图片，可填网络 URL 或本地 file:/// 路径
*   **Changed**: 优化 _conf_schema.json 字段结构，description/hint 替代废弃的 label，管理面板显示更美观

## [v2.3.0]
*   **Changed**: 黑白名单配置改为列表形式，支持在管理面板中逐条添加/删除群号
*   **Fix**    : 修复旧版字符串格式的黑白名单在新版 UI 中被逐字符展开的问题，启动时自动迁移
*   **Fix**    : 修复私聊下 `/welcome set reset/show <群号>` 传入非数字群号时静默失败，现在会提示格式错误
*   **Changed**: 私聊群规发送失败的日志级别从 debug 提升为 warning，方便排查

## [v2.2.0] 
*   **Fix**    : /welcome status 指令重复发送两条相同消息的问题
*   **Changed**:重写 README 文档，优化结构与内容排版


## [v2.1.0] - feat(AI): 接入 AI 个性化欢迎语与可视化配置增强
*   **⚙️ 可视化模型配置**: 适配 AstrBot 管理面板，支持可视化选择 AI 模型进行欢迎词编写。
