# 开发维护指南

`astrbot_plugin_listen_music` 是 AstrBot 4.26+ 的独立插件；展示名为“我想听歌！”。展示名、Python 包名、数据目录和 WebUI 路由前缀承担不同职责，不要为了改显示名而改变技术标识。

## 修改前的阅读顺序

1. 阅读根目录 `README.md`，确认用户可见行为与运行前提。
2. 阅读 [ARCHITECTURE.md](ARCHITECTURE.md)，确认架构、行为契约和安全边界。
3. 先读对应测试，再读实现文件。测试是当前行为最精确的可执行说明。
4. 需要外部设计或 AstrBot 接入参考时，阅读 [REFERENCES.md](REFERENCES.md)，并仅参考其中指定的范围。

## 模块定位

| 问题 | 首先看 | 通常只应修改 |
| --- | --- | --- |
| 候选是否可交付、版本会不会偷换 | `tests/test_matcher.py` | `core/matcher.py` |
| 搜索、受限候选快照与选中候选交付 | `tests/test_services.py`、`tests/test_selection.py` | `core/services.py`、`core/selection.py` |
| Bilibili WBI、视频页、DASH | `tests/test_bilibili.py` | `core/bilibili.py` |
| 下载、封装和单次临时文件 | `tests/test_media.py` | `core/media.py` |
| 命令、LLM、消息、WebUI 路由 | `tests/test_main.py` | `main.py` |
| Cookie、二维码与管理员权限 | `tests/test_accounts.py` | `core/accounts.py`、`main.py` |

## 核心约束

- Bilibili 是唯一的曲库、解析和播放源；不引入网易云、YouTube、跨源兜底、平台注册表或 `BaseSource`。
- 平台搜索只使用清理后的用户关键词，绝不自动追加“原版”或其他版本词。普通听歌仅在本地排除标题或分 P 已明确标注的变体；明确请求 Live、翻唱、DJ、Remix、伴奏等版本时，版本约束优先。
- `MV` 是中性内容标签，不能单独作为排除或加分理由；“混剪”归为剪辑变体，普通听歌不能自动选择。
- LLM 负责理解对话、整理歌曲条件，并在直听时从插件返回的最多十条受限候选中判断最合适的一条。Bilibili 负责召回；插件只做具体分 P 身份判定和安全硬过滤，不以本地数值评分重排候选。
- 候选身份必须是具体 `bvid:cid`。选定后失败就失败，不能静默切换视频、分 P 或歌曲。
- 直听采用两步受限协作：先取得隐藏候选，再由 LLM 选择，最后由终止型交付工具发送一条简短前言和语音。交付成功后必须停止该轮 LLM 输出，不能追加解释、检索过程或确认文本。
- 下载和明确的搜歌/找歌只走用户选择路径：插件展示最多十个“标题 + 时长”候选，不显示 UP 主；用户回复“第 N 首”听歌，回复“第 N 首 下载”发送文件。下载不能由 LLM 自动挑选版本。
- 隐式与显式候选都必须绑定当前会话和短期快照。LLM 只能从本次隐藏候选中选择，不能提交链接、`bvid`、`cid` 或编造的候选标识；用户选择同样只能从当前显式列表取回。
- 候选数量固定为十条。账号页只管理 Bilibili 登录与运行健康状态；将来确需调整此类策略，应走 AstrBot 标准插件配置，不能在账号页增加设置或自建持久化配置。
- 媒体必须在发送完成后通过 `release()` 立即删除；账号 Cookie 不能进入聊天、LLM 工具结果、日志或 WebUI 响应。
- 终止型交付成功时返回真正的 `None`。不要把工具执行过程泄漏为聊天文本，也不要用 `stop_event()` 模拟工具终止。
- 人工选歌真正等待满 90 秒时应发送一条本次搜索已结束的提示；同一会话的新非选择消息、新搜索或替换操作必须静默使旧候选状态失效，不能干扰下一轮对话。

## 修改原则

- 只改所属层；不要为了少量重复引入跨层框架或过早抽象。
- 改查询清理、分页判定或硬过滤前先写正反例。特别关注受限候选数量、版本约束、非音乐内容、候选身份不变和不确定分页的拒绝。
- 改 LLM 候选交互时，测试模型只能选择本次受限集合，直听成功后无尾随文本，下载始终进入用户选择路径。
- 改交付路径时检查媒体准备失败、前言发送失败、取消、发送失败、临时文件删除和插件停止。
- 改账号或 WebUI 时检查管理员身份、二维码会话归属、SSE 脱敏和 Cookie 文件权限。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile main.py core/*.py tests/*.py
ruff format --check .
ruff check .
```

涉及实际 AstrBot 适配器、二维码 SSE、Bilibili 流或 ffmpeg 封装的改动，还必须在 AstrBot `>=4.26,<5` 中手工验收。
