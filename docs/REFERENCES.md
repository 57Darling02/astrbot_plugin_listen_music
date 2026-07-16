# 参考与致谢

本项目只借鉴成熟项目的设计思路和 AstrBot 集成约定；不把它们的架构或代码机械搬入这个插件。任何后续复用都应单独核对上游许可证与当前版本。这一页说明不同问题应参考哪个项目、哪个层次。

## NeriPlayer：页级媒体安全

感谢 [NeriPlayer](https://github.com/cwuom/NeriPlayer) 维护者提供的成熟参考。NeriPlayer 使用 GPL-3.0；本项目仅将其作为概念与设计参考，不复制、翻译或改写其代码。

NeriPlayer 是历史设计参考，帮助本项目厘清视频与具体分 P 的安全边界：

- 搜索视频与实际可播放分 P 是不同层次，交付前必须固定到具体媒体身份。
- 已选页解析失败时不应悄悄改选另一页或另一首歌。
- 自动交付前需要明确的可交付性边界，不能把搜索结果标题直接当作媒体身份。

建议开发者阅读：

- [SearchManager.kt](https://github.com/cwuom/NeriPlayer/blob/e76bc4f21e010f67c05f9e6c9f846ec958b7985f/app/src/main/java/moe/ouom/neriplayer/core/api/search/SearchManager.kt)：了解歌名优先、艺人佐证与可信度拒绝的候选判断思路；本项目不复制其评分公式、权重或阈值。
- [PlayerManagerNeteaseAutoSourceSwitch.kt](https://github.com/cwuom/NeriPlayer/blob/e76bc4f21e010f67c05f9e6c9f846ec958b7985f/app/src/main/java/moe/ouom/neriplayer/core/player/resolver/netease/PlayerManagerNeteaseAutoSourceSwitch.kt)：理解视频结果展开为具体分 P 与固定媒体身份的必要性；不是本项目评分、阈值或自动切源的蓝图。

不要迁移：NeriPlayer 的多平台播放器、Android 生命周期、音质偏好链、网易云自动切源、YouTube 代码及其 Kotlin 实现。本项目不复制数值评分、权重或分数阈值：LLM 只借鉴歌名优先、艺人佐证、版本尊重与低可信拒绝的定性证据顺序，在插件提供的受限候选集内完成语义判断；插件仍负责页身份与交付安全。

## astrbot_plugin_music：AstrBot 集成

感谢上游 [Zhalslar/astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music) 提供 AstrBot 插件接入的实践参考。开发时审计过的本地 fork 是 [57Darling02/astrbot_plugin_music](https://github.com/57Darling02/astrbot_plugin_music/tree/6cf27cb1fc603dcac6c0b390a616741c4abdab4a)；它用于定位当时的实现状态，不替代上游致谢。

本项目参考的范围是 AstrBot 侧约定：

- `Star` 生命周期与资源关闭方式。
- `filter.command`、`filter.llm_tool`/FunctionTool、`AstrMessageEvent` 的消息处理语义。
- `SessionWaiter` 的会话内候选选择模式。
- `Record.fromFileSystem()`、文件消息和插件数据目录的使用方式。

不要迁移：旧项目的平台注册、`BaseMusicPlayer` 继承层、配置树、歌词渲染、卡片渲染、下载器与多平台选择逻辑。它们服务的是另一种可扩展播放器定位，与“我想听歌！”的单源、少配置、直接交付目标相冲突。

## 本项目的参考使用准则

当需要改动时，先判断问题属于哪一类：

| 问题 | 优先阅读 | 保持的原则 |
| --- | --- | --- |
| 候选是否可交付、是否该自动播放 | NeriPlayer 的页解析安全参考；本项目 `core/matcher.py` 测试 | 具体分 P、受限候选、可交付性守卫、身份不偷换 |
| Bilibili 请求、WBI、DASH、备用 URL | 本项目 `core/bilibili.py` 与 `core/media.py` | 单源、具体 `bvid:cid`、失败不换歌 |
| AstrBot 命令、LLM、等待、媒体发送、WebUI | `astrbot_plugin_music` 的 AstrBot 使用方式；本项目 `main.py` 测试 | 维持当前小边界，不带回旧架构 |

本项目仓库为 [57Darling02/astrbot_plugin_listen_music](https://github.com/57Darling02/astrbot_plugin_listen_music)。上面两个链接是参考项目的确切 GitHub 地址，不是本项目的镜像或依赖。
