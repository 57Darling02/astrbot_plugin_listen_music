# 我想听歌！开发文档

`我想听歌！` 是 `astrbot_plugin_listen_music` 的用户可见名称。它是一个面向 AstrBot 4.26+ 的小型 Bilibili 听歌插件：LLM 理解听歌意图后在受限候选中选歌并交付语音；下载请求和明确的“搜歌/找歌”会展示最多十个候选，由用户选择版本后交付。

这组文档服务于项目的开发、维护和审阅。开始修改前按下面顺序阅读：

1. [ARCHITECTURE.md](ARCHITECTURE.md)：唯一的架构、行为契约、匹配算法、资源与安全事实来源。
2. [DEVELOPMENT.md](DEVELOPMENT.md)：模块定位、改动约束、测试地图与验证方式。
3. [REFERENCES.md](REFERENCES.md)：NeriPlayer 与 `astrbot_plugin_music` 的明确参考范围、固定 GitHub 链接和致谢。
4. 对应的测试文件，再阅读要改动的实现文件。测试是当前行为最精确的可执行说明。

项目的稳定标识仍是 `astrbot_plugin_listen_music`；不要为了显示名而改动数据目录、路由前缀或 Python 包名。
