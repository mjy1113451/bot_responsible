# AstrBot 人际关系管理插件

> AstrBot 人际关系合集 - Bot 关系处理插件

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)

## 📖 简介

这是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的**人际关系管理插件**，提供全面的黑名单管理、好友/群组管理、请求处理等功能。插件支持自动处理好友申请和群邀请，并提供智能的黑名单防护机制。

## ✨ 功能特性

### 🔒 黑名单管理
- 拉黑/解封用户
- 拉黑/解封群组
- 自动拒绝黑名单用户的好友申请和群邀请
- Bot 被踢出群后自动拉黑该群
- Bot 被拉入黑名单群后自动退出

### 👥 好友管理
- 查看好友列表
- 主动添加好友（SnowLuma 下依赖实验性 `send_packet`，非标准 OneBot 能力）
- 删除好友

### 🏠 群组管理
- 查看群列表
- 主动加入群（SnowLuma 下依赖实验性 `send_packet`，非标准 OneBot 能力）
- 退出群组

### 📩 请求处理
- 自动接收并通知管理员好友申请（SnowLuma 标准 `request.friend` 事件）
- 自动接收并通知管理员群邀请/加群请求（SnowLuma 标准 `request.group` 事件）
- 同步 SnowLuma 可疑好友申请和被过滤入群请求到待处理列表
- 支持引用消息快速同意/拒绝/拉黑
- 待处理请求列表查看

### 🔔 通知设置
- 设置通知群接收所有提醒
- 支持私聊通知（未设置通知群时）

## 🛠️ 安装

### 前置要求
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 已安装并运行
- Python 3.8+
- OneBot v11/v12 协议支持

### 安装步骤

1. 进入 AstrBot 插件目录：
```bash
cd /path/to/AstrBot/data/plugins
```

2. 克隆本仓库：
```bash
git clone https://github.com/mjy1113451/bot_responsible.git astrbot_plugin_relationship_manager
```

3. 重启 AstrBot 或在管理面板中重新加载插件

## 📝 使用说明

### 命令列表

| 命令 | 别名 | 说明 | 权限 |
|------|------|------|------|
| `/好友` | `/fl` | 查看好友列表 | 管理员 |
| `/群` | `/gl` | 查看群列表 | 管理员 |
| `/拉黑` | `/addbl`, `/屏蔽` | 拉黑用户；引用通知时拒绝并拉黑请求 | 管理员 |
| `/解封` | `/rmbl`, `/取消屏蔽` | 解封用户 | 管理员 |
| `/黑名单` | `/lsbl` | 查看黑名单 | 管理员 |
| `/拉黑群` | `/addblg` | 拉黑群组 | 管理员 |
| `/解封群` | `/rmblg` | 解封群组 | 管理员 |
| `/待处理` | `/pending` | 查看待处理请求 | 管理员 |
| `/加好友` | `/addfriend` | 添加好友；SnowLuma 下走实验性 `send_packet` | 管理员 |
| `/加群` | `/addgroup` | 加入群组；SnowLuma 下走实验性 `send_packet` | 管理员 |
| `/删好友` | `/deletefriend` | 删除好友 | 管理员 |
| `/退群` | `/leavegroup` | 退出群组 | 管理员 |
| `/同意` | `/accept` | 同意请求；支持引用通知或 `/同意 编号` | 管理员 |
| `/拒绝` | `/reject` | 拒绝请求；支持引用通知或 `/拒绝 编号` | 管理员 |
| `/拉黑请求` | `/blockreply` | 拒绝并拉黑请求；支持引用通知或 `/拉黑请求 编号` | 管理员 |
| `/通知群` | `/setnotify`, `/setgroup` | 设置通知群 | 管理员 |

### 使用示例

#### 拉黑用户
```
/拉黑 123456789
/拉黑 123456789 987654321  # 批量拉黑
```

#### 处理好友申请
1. 收到好友申请通知后
2. 引用该消息回复，或使用通知里的编号：
   - `/同意` - 同意好友申请
   - `/拒绝` - 拒绝好友申请
   - `/拉黑` - 拒绝并拉黑该用户
   - `/同意 ab12cd34ef` - 按编号同意

#### 设置通知群
```
/通知群 123456789  # 设置通知群
/通知群 取消       # 取消通知群（改为私聊通知）
```

#### 查看待处理请求
```
/待处理
```
该命令会同时同步 SnowLuma 的可疑好友申请和被过滤入群请求。同步到列表后，可使用 `/同意 编号`、`/拒绝 编号`、`/拉黑请求 编号` 处理。

## ⚙️ 配置

插件支持以下配置项（可在 AstrBot 配置面板中设置）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `data_path` | string | `data` | 数据存储路径 |
| `notify_group` | string | `None` | 通知群号（为空则私聊通知） |

## 🔧 工作原理

### 自动处理机制

1. **好友申请处理**
   - 监听好友申请事件
   - 检查发送者是否在黑名单中
   - 自动拒绝黑名单用户
   - 通知管理员进行人工处理

2. **群邀请处理**
   - 监听群邀请事件
   - 检查邀请人和群组是否在黑名单中
   - 自动拒绝黑名单邀请
   - 通知管理员进行人工处理

3. **被动防护**
   - 监听 Bot 被踢出群事件
   - 自动将该群加入黑名单
   - 监听 Bot 被拉入群事件
   - 如果是黑名单群，自动退出并通知

### 数据持久化

插件使用 JSON 文件存储数据：
- `blacklist.json` - 黑名单数据
- `pending.json` - 待处理请求数据

## 🐛 已知问题

- `/加好友`、`/加群` 不是 OneBot v11 标准 action。SnowLuma 提供 `send_packet`，但这里的主动添加逻辑仍属于原始包实验路径，可能因 QQ/SnowLuma 版本变化失效。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建你的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交你的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

## 📄 许可证

本项目基于 MIT 许可证开源 - 查看 [LICENSE](LICENSE) 文件了解详情

## 🔗 相关链接

- [AstrBot 官方仓库](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档（中文）](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 插件开发文档（英文）](https://docs.astrbot.app/en/dev/star/plugin-new.html)

## 📧 联系方式

如有问题或建议，请通过 GitHub Issues 联系。

---

**感谢使用 AstrBot 人际关系管理插件！** 🎉
