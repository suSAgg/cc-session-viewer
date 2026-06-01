# CC Session Viewer

本地 Claude Code 对话管理器 —— 卡片式浏览、搜索、恢复和清理所有对话历史。零依赖，Python 3 标准库即可运行。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 功能

- **卡片网格视图**：三列卡片布局，预览首条消息 + 末条回复
- **全文搜索**：支持按对话内容、项目名称、Session ID 搜索，高亮匹配文本
- **命名识别**：自动读取 `/rename` 命名的对话，金色标签显示
- **一键恢复**：复制完整恢复命令（含 `cd` 到正确项目目录）
- **删除管理**：单条删除 + 批量清理超过 30 天的旧对话
- **项目筛选**：按项目目录筛选对话

## 安装

```bash
git clone git@github.com:suSAgg/cc-session-viewer.git
cd cc-session-viewer
python3 server.py
```

启动后访问 **http://127.0.0.1:8767**。

也可以注册到本地反向代理（如 Nginx / Caddy）通过统一面板访问。

## 技术

- Python 3 内置 `http.server`，零外部依赖
- 扫描 `~/.claude/projects/` 下所有 JSONL 对话文件
- 从 `~/.claude/history.jsonl` 读取真实项目路径和对话命名
- 服务绑定 `127.0.0.1:8767`，仅本地可访问

## License

MIT
