# CC Session Viewer

本地 Claude Code 对话管理器 —— 浏览、搜索、恢复和清理所有对话历史。

## 功能

- **对话浏览**：卡片式列表，按今天/昨天/本周/更早分组
- **全文搜索**：支持按对话内容、项目名称、Session ID 搜索
- **命名识别**：自动读取 `/rename` 命名的对话，金色标签显示
- **一键恢复**：复制完整恢复命令（含 `cd` 到正确项目目录）
- **删除管理**：单独删除 + 批量清理超过 30 天的旧对话
- **项目筛选**：按项目目录筛选对话

## 安装

```bash
# 1. Clone 仓库
git clone git@github.com:suSAgg/cc-session-viewer.git
cd cc-session-viewer

# 2. 启动服务
python3 server.py
```

或者注册到**本地工具管家**（`/Users/susu/mytools/公共后端`）通过统一面板启动。

## 使用

| 途径 | 地址 |
|------|------|
| 直接访问 | http://127.0.0.1:8767 |
| 通过公共后端 | http://localhost:3999/tools/cc-session-viewer/ |

## 技术

- Python 3 内置 `http.server`，零依赖
- 扫描 `~/.claude/projects/` 下所有 JSONL 对话文件
- 从 `~/.claude/history.jsonl` 读取真实项目路径和对话命名

## 端口

默认 `8767`，与公共后端 `tools.json` 中配置一致。
