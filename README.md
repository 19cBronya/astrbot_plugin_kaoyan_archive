# astrbot_plugin_kaoyan_archive

AstrBot 私聊考研答疑归档插件。它不会接管或修改 AstrBot 的正常回答，只在精确配置的私聊 UMO 中旁路保存消息，并在用户表达“我问完了”时把上一结束边界到当前边界之间的有效对话整理为一道题目。

## 当前能力

- UMO 白名单默认为空；群聊和不在白名单内的私聊完全忽略。
- 保存用户消息、AI 回答、原始消息链、平台消息 ID、模型信息及图片/文件附件。
- 原始事件表通过 SQLite 触发器禁止更新和删除。
- 识别“我问完了”“整理入库”等结束短语，并优先排除“我还没问完”等否定表达。
- 从上一结束边界之后取到本次边界，过滤 `/`、`!` 指令及配置的自然语言控制操作。
- 归档时调用一次可配置的 AstrBot Provider，生成科目、标题和 Markdown 摘要；模型不可用时自动使用本地规则，不影响编号入库。
- 按科目事务化分配连续 ID，例如 `操作系统0001`。
- 提供 AstrBot Plugin Page：总览、UMO 白名单、题目筛选、完整时间线、软删除、恢复和失败重试。

## 安装与配置

要求 AstrBot `>=4.27.2,<5`，首个正式适配平台为 OneBot v11 / `aiocqhttp`。

本仓库目前仅用于本地开发，没有发布到插件市场，也没有配置可推送的 `origin`。开发测试时可把目录放入：

```text
AstrBot/data/plugins/astrbot_plugin_kaoyan_archive/
```

重载插件后，先在插件配置或 Plugin Page 中填写 `umo_whitelist`。完整 UMO 可通过 AstrBot 内置 `/sid` 查看，例如：

```text
default:FriendMessage:123456789
```

白名单为空时，插件不会记录、分析或调用任何模型。

主要配置：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `umo_whitelist` | `[]` | 精确私聊 UMO 白名单 |
| `end_phrases` | 我问完了等 | 结束边界短语 |
| `command_prefixes` | `/`, `!` | 不进入题目正文的指令前缀 |
| `subjects` | 数学、英语、政治、408 各科等 | 科目目录 |
| `enable_ai_archive` | `true` | 只在归档边界调用一次模型 |
| `archive_provider_id` | 空 | 空值表示沿用该 UMO 当前 Provider |
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

如果结束短语和正文出现在同一条消息中，例如“最后补充……我问完了”，插件会移除结束短语并保留其余正文。

## 调试命令

命令只在白名单私聊中工作：

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

正式安装前还应在目标服务器做一次只读检查，确认 AstrBot 版本、UMO、OneBot 主动消息能力、Provider 和数据目录权限。

## 许可证

沿用官方模板的 AGPL-3.0 许可证。
