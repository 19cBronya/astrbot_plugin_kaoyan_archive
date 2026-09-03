# astrbot_plugin_kaoyan_archive

AstrBot 私聊考研答疑归档插件。它不会接管或修改 AstrBot 的正常回答，只在精确配置的私聊 UMO 中旁路保存消息。每条自然语言消息由 LLM 判断为“问题、归档边界、其他软指令”，遇到归档边界后把上一边界到当前边界之间的有效对话整理为一道题目。

## 当前能力

- UMO 白名单默认为空；群聊和不在白名单内的私聊完全忽略。
- 保存用户消息、AI 回答、原始消息链、平台消息 ID、模型信息及图片/文件附件。
- 原始事件表通过 SQLite 触发器禁止更新和删除。
- 白名单门禁通过后，每条自然语言消息调用一次分类 Provider；分类规则和 Prompt 固定在插件代码中，不维护关键词表。
- 自然语言分类为 `question`、`archive` 或 `instruction`；软指令保存但不进入题目正文。
- `/kaoyan ...` 使用 AstrBot 的命令注册机制，不自定义或配置命令前缀。
- 从上一归档边界之后取到本次边界，排除框架命令和 LLM 判定的软指令。
- 归档时调用一次可配置的 AstrBot Provider，生成科目、标题、列表概览、知识点和 Markdown 总结；模型不可用时自动使用本地规则，不影响编号入库。
- 按科目事务化分配连续 ID，例如 `操作系统0001`。
- 提供 AstrBot Plugin Page：全量题目列表、科目/状态/会话下拉筛选、知识点与总结编辑、内嵌题图的完整原始会话、软删除、恢复和失败重试。
- Plugin Page 使用随插件打包的 KaTeX 离线渲染总结与原始会话中的 LaTeX 公式，无需访问 CDN。

## 安装与配置

要求 AstrBot `>=4.27.2,<5`，首个正式适配平台为 OneBot v11 / `aiocqhttp`。

本仓库目前仅用于开发，没有发布到 AstrBot 插件市场。开发测试时可把目录放入：

```text
AstrBot/data/plugins/astrbot_plugin_kaoyan_archive/
```

重载插件后，先在 AstrBot 标准插件配置中填写 `umo_whitelist`。完整 UMO 可通过 AstrBot 内置 `/sid` 查看，例如：

```text
default:FriendMessage:123456789
```

白名单为空时，插件不会记录、分析或调用任何模型。

如果该 UMO 被路由到独立 AstrBot 配置（日志前缀会显示配置名称），还必须确认该配置的“插件集合 / `plugin_set`”为“全部插件”，或显式包含 `astrbot_plugin_kaoyan_archive`。AstrBot 会先按这里过滤处理器，再把消息交给插件；仅填写插件自己的 UMO 白名单不能绕过框架级过滤。

主要配置：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `umo_whitelist` | `[]` | 精确私聊 UMO 白名单 |
| `classification_provider_id` | 空 | 逐消息分类模型；空值表示沿用该 UMO 当前 Provider |
| `fallback_provider_id` | 空 | 分类或归档专用模型失败后尝试的备用模型；仍失败时退回 UMO 当前 Provider |
| `subjects` | 数学、英语、政治、408 各科等 | 科目目录 |
| `enable_ai_archive` | `true` | 边界建立后再调用模型生成归档摘要 |
| `archive_provider_id` | 空 | 归档整理模型；空值表示沿用该 UMO 当前 Provider |
| `max_archive_chars` | `30000` | 归档模型输入字符上限，完整原文不截断保存 |
| `max_attachment_mb` | `20` | 单个附件保存上限 |
| `send_archive_notice` | `true` | 完成后主动发送题号通知 |

## 区间语义

假设时间线如下：

```text
我问完了                  ← 上一边界，不属于新题正文
用户：新题第一问          ┐
助手：回答                │
/kaoyan status            │ 保存但排除
用户：继续追问            │ 归档为同一道题
助手：继续回答            │
我问完了                  ┘ 当前边界，保存但排除
```

如果归档意图和正文出现在同一条消息中，例如“最后补充……我问完了”，分类 LLM 会把补充内容放入 `content`，该事件同时作为本题正文和结束边界。

## 消息判定顺序

1. 群聊或白名单外 UMO 立即返回，不保存也不调用模型。
2. 已注册的 `/kaoyan ...` 命令交给 AstrBot 命令系统，不调用自然语言分类器。
3. 其余每条用户消息调用一次分类 Provider，并保存分类 Provider、模型和 Prompt 版本。
4. `question` 进入当前题目正文；`archive` 建立边界；`instruction` 保存为 `excluded`。
5. 分类调用失败时完整原文仍按 `question` 保存，并记录失败信息；可使用 `/kaoyan archive` 手动恢复边界。

因此，一轮普通问答会包含 AstrBot 原本的答疑调用和插件新增的一次分类调用；结束归档时还可能增加一次归档整理调用。

分类和归档调用均按“操作专用 Provider → `fallback_provider_id` → UMO 当前 Provider”的顺序尝试，并自动跳过空值和重复 Provider。分类链全部失败时按本地安全规则保留原文；归档链全部失败时仍使用本地规则生成题号、标题和摘要。

## AstrBot 注册命令

命令内容固定在插件中，只在白名单私聊中工作。命令前缀由 AstrBot 框架处理，不提供插件配置：

- `/kaoyan status`：查看当前 UMO 的事件与归档数量。
- `/kaoyan archive`：手动建立结束边界并提交归档。
- `/kaoyan retry [题号]`：重试失败的归档。
- `/kaoyan latest`：查看最近一道已归档题目。

## 数据目录

运行数据位于 AstrBot 规范目录：

```text
data/plugin_data/astrbot_plugin_kaoyan_archive/
├── archive.sqlite3
└── attachments/
```

数据库使用 WAL、外键、参数化查询和事务编号。题目删除是软删除；原始事件和审计记录不允许覆盖或删除。

## 开发测试

核心模块不依赖正在运行的 AstrBot：

```bash
python3 -m pytest -q
python3 -m compileall -q main.py kaoyan_archive
```

页面公式支持 `\\(...\\)`、`\\[...\\]`、`$...$`、`$$...$$` 和常见的 `equation`、`align`、`gather` 环境。KaTeX 0.18.5 的运行文件、字体和 MIT 许可证位于 `pages/archive/vendor/katex/`，插件页面可完全离线加载。

正式安装前还应在目标服务器做一次只读检查，确认 AstrBot 版本、UMO、OneBot 主动消息能力、Provider 和数据目录权限。

## 许可证

沿用官方模板的 AGPL-3.0 许可证。
