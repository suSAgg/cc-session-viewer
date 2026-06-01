#!/usr/bin/env python3
"""Claude Code 对话管理器

扫描 ~/.claude/projects/ 下所有对话，提供 Web 界面浏览和搜索。
直接访问：http://127.0.0.1:8767
"""

import json
import os
import re
import secrets
import time
import http.server
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORT = 8767
HOST = "127.0.0.1"
AUTH_TOKEN = secrets.token_hex(32)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_safe_in_dir(path, root):
    """Reject symlinks and paths that resolve outside root."""
    try:
        if path.is_symlink():
            return False
        rp = path.resolve()
        rr = root.resolve()
        return str(rp).startswith(str(rr) + os.sep) or rp == rr
    except Exception:
        return False


# ── 项目路径映射 ──────────────────────────────────────

def load_history_meta():
    """从 history.jsonl 读取 sessionId → {project, name} 的映射"""
    pmap = {}  # sessionId → project
    nmap = {}  # sessionId → name
    if not HISTORY_FILE.exists():
        return pmap, nmap
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = d.get("sessionId", "")
                if not sid:
                    continue
                # 项目路径（取第一次出现的）
                proj = d.get("project", "")
                if proj and sid not in pmap:
                    pmap[sid] = proj
                # rename 名称（取最后一次 /rename 的值）
                display = d.get("display", "")
                if display.startswith("/rename "):
                    name = display[len("/rename "):].strip()
                    if name:
                        nmap[sid] = name
    except Exception:
        pass
    return pmap, nmap


# ── 对话扫描 ──────────────────────────────────────────

def decode_project(name):
    """将 -Users-name-mytools-xxx 还原为 /Users/name/mytools/xxx 格式"""
    return "/" + name.replace("-", "/")


def extract_text(content):
    """从消息 content 中提取纯文本（可能是 str 或 list）"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type", "")
                if t == "text" and "text" in item:
                    parts.append(item["text"])
                elif t == "tool_use":
                    parts.append(f"[tool: {item.get('name', '?')}]")
                elif t == "tool_result":
                    parts.append("[tool result]")
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return ""


def parse_session(filepath):
    """解析 JSONL 文件，提取元数据和最近消息"""
    messages = []
    first_user = ""

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = d.get("type", "")
                if t in ("user", "assistant"):
                    msg = d.get("message", {})
                    role = msg.get("role", t)
                    text = extract_text(msg.get("content", ""))
                    if text:
                        messages.append({"role": role, "text": text[:300]})
                        if not first_user and role == "user":
                            first_user = text[:200]
    except Exception:
        pass

    return {
        "msg_count": len(messages),
        "first_user": first_user,
        "last_msgs": messages[-3:],
    }


def scan_sessions():
    """扫描所有项目目录，返回对话列表"""
    sessions = []
    if not PROJECTS_DIR.exists():
        return sessions

    pdir_map = {}  # project_encoded → real_path
    pmap, nmap = load_history_meta()

    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir() or not _is_safe_in_dir(proj_dir, PROJECTS_DIR):
            continue
        real_path = decode_project(proj_dir.name)

        # 尝试从 history.jsonl 获取更准确的项目路径
        # 如果 pmap 中有任一 session 属于这个 project_encoded，就用那个路径
        if proj_dir.name not in pdir_map:
            pdir_map[proj_dir.name] = real_path  # fallback

        for f in proj_dir.glob("*.jsonl"):
            sid = f.stem
            try:
                st = f.stat()
            except OSError:
                continue

            info = parse_session(f)

            # 用 history.jsonl 里的真实项目路径
            resolved_project = pmap.get(sid, real_path)

            sessions.append({
                "id": sid,
                "id_short": sid[:8],
                "name": nmap.get(sid, ""),
                "project": resolved_project,
                "project_encoded": proj_dir.name,
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": int(st.st_mtime),
                "mtime_str": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
                "msgs": info["msg_count"],
                "first": info["first_user"],
                "last": info["last_msgs"],
            })

    # 回填 project_encoded → real_path 映射
    for s in sessions:
        if s["project"] != decode_project(s["project_encoded"]):
            pdir_map[s["project_encoded"]] = s["project"]

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def delete_session(session_id):
    """删除指定 session_id 的 JSONL 文件"""
    if not UUID_RE.match(session_id):
        return {"ok": False, "error": "无效的 session ID"}
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir() or not _is_safe_in_dir(proj_dir, PROJECTS_DIR):
            continue
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            try:
                os.remove(f)
                return {"ok": True, "id": session_id}
            except OSError as e:
                return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "未找到该对话"}


def cleanup_old(dry_run=True):
    """查找超过 30 天的对话，dry_run=True 只报告不删除"""
    now = time.time()
    cutoff = now - 30 * 86400
    to_delete = []

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir() or not _is_safe_in_dir(proj_dir, PROJECTS_DIR):
            continue
        for f in proj_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    to_delete.append(str(f))
            except OSError:
                pass

    if not dry_run:
        for f in to_delete:
            try:
                os.remove(f)
            except OSError:
                pass

    return {
        "dry_run": dry_run,
        "cutoff": time.strftime("%Y-%m-%d", time.localtime(cutoff)),
        "count": len(to_delete),
        "files": [str(Path(f).relative_to(PROJECTS_DIR)) for f in to_delete],
    }


# ── HTML ──────────────────────────────────────────────

HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CC 对话管理器</title>
<style>
  :root{
    --bg:#faf8f5;
    --card:#fffefb;
    --text:#2c2420;
    --muted:#8c8279;
    --faint:#bfb8af;
    --accent:#2563eb;
    --accent-hover:#1d4ed8;
    --amber:#b45309;
    --amber-bg:#fffbeb;
    --border:#e8e2d9;
    --hover:#f6f3ee;
    --code-bg:#f3efea;
    --red:#dc2626;
    --red-hover:#b91c1c;
    --shadow:0 1px 2px rgba(0,0,0,.04);
    --shadow-md:0 4px 12px rgba(0,0,0,.06);
    --shadow-lg:0 12px 32px rgba(0,0,0,.1);
    --radius:12px;
    --radius-sm:6px;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:system-ui,-apple-system,sans-serif;
    background:var(--bg);
    color:var(--text);
    min-height:100vh;
    -webkit-font-smoothing:antialiased;
  }
  /* ── header ── */
  .header{
    position:sticky;top:0;z-index:10;
    background:var(--bg);
    border-bottom:1px solid var(--border);
    padding:20px 24px 16px;
  }
  .header-inner{max-width:1100px;margin:0 auto}
  .title{
    font-family:Charter,Georgia,serif;
    font-size:22px;font-weight:600;
    letter-spacing:-0.02em;
    color:var(--text);
    margin-bottom:4px;
  }
  .meta{font-size:12px;color:var(--muted)}
  .meta em{font-style:normal;color:var(--amber);font-weight:600}
  /* ── toolbar ── */
  .toolbar{
    display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;align-items:center;
  }
  .search-wrap{
    flex:1;min-width:200px;position:relative;
  }
  .search-wrap svg{
    position:absolute;left:12px;top:50%;transform:translateY(-50%);
    width:15px;height:15px;color:var(--faint);pointer-events:none;
  }
  input,select,button{
    font-family:inherit;font-size:13px;
    border:1px solid var(--border);border-radius:var(--radius-sm);
    padding:8px 12px;background:var(--card);
    color:var(--text);
    transition:border-color .15s,box-shadow .15s;
  }
  input{padding-left:34px;width:100%}
  input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,.1)}
  select{cursor:pointer;padding-right:28px;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%238c8279'%3E%3Cpath d='M3 5l3 3 3-3'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 8px center}
  button{cursor:pointer;white-space:nowrap;font-weight:500;display:inline-flex;align-items:center;gap:5px}
  button:hover{background:var(--hover)}
  .btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
  .btn-primary:hover{background:var(--accent-hover)}
  .btn-danger{color:var(--red);border-color:var(--red)}
  .btn-danger:hover{background:#fef2f2}
  /* ── cleanup msg ── */
  .cleanup-msg{
    display:none;max-width:1100px;margin:0 auto 12px;padding:12px 16px;
    border-radius:var(--radius-sm);font-size:12px;
    background:#fef2f2;border:1px solid #fecaca;color:var(--red);
  }
  .cleanup-msg.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
  /* ── main grid ── */
  .main{padding:0 24px 40px}
  .grid-wrap{max-width:1100px;margin:0 auto}
  /* ── date separator ── */
  .date-sep{
    grid-column:1/-1;
    font-family:Charter,Georgia,serif;
    font-size:16px;font-weight:600;
    color:var(--muted);
    padding:24px 0 10px;letter-spacing:.03em;
  }
  .date-sep:first-child{padding-top:16px}
  /* ── card grid ── */
  .card-grid{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:16px;
  }
  /* ── card ── */
  .card{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:var(--radius);
    box-shadow:var(--shadow);
    transition:box-shadow .2s,border-color .2s,transform .15s;
    cursor:pointer;
    display:flex;flex-direction:column;
    overflow:hidden;
  }
  .card:hover{
    box-shadow:var(--shadow-md);
    border-color:#d8d0c4;
    transform:translateY(-1px);
  }
  .card.named{border-left:3px solid var(--amber)}
  /* card header */
  .card-top{
    padding:16px 18px 0;
    display:flex;align-items:flex-start;justify-content:space-between;
    gap:8px;
  }
  .card-project{
    font-size:11px;color:var(--faint);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    flex:1;min-width:0;
  }
  .card-name{
    flex-shrink:0;
    font-size:10px;font-weight:600;
    background:var(--amber-bg);color:var(--amber);
    padding:2px 8px;border-radius:4px;
    max-width:130px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  }
  /* card body — content preview */
  .card-body{
    padding:10px 18px 14px;flex:1;
    display:flex;flex-direction:column;gap:6px;
  }
  .card-body .msg-block{
    font-size:13px;line-height:1.55;
    overflow:hidden;
  }
  .card-body .msg-block.user{
    color:var(--accent);
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  }
  .card-body .msg-block.assistant{
    color:#4b5563;
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  }
  .card-body .msg-label{
    font-size:10px;font-weight:600;text-transform:uppercase;
    letter-spacing:.04em;margin-bottom:2px;
  }
  .card-body .msg-label.u{color:var(--accent)}
  .card-body .msg-label.a{color:var(--muted)}
  /* card footer */
  .card-foot{
    padding:10px 18px;
    display:flex;align-items:center;gap:12px;
    border-top:1px solid var(--border);
    font-size:11px;color:var(--muted);
  }
  .card-foot .time{font-weight:600;color:var(--text);white-space:nowrap}
  .card-foot .stats{white-space:nowrap}
  .card-foot .spacer{flex:1}
  .card-foot .act{
    font-size:11px;padding:4px 10px;border-radius:20px;
    border:1px solid var(--border);background:var(--card);
    color:var(--muted);cursor:pointer;font-family:inherit;
    transition:all .15s;white-space:nowrap;
  }
  .card-foot .act:hover{color:var(--text);border-color:#c8c0b4}
  .card-foot .act.go{color:var(--accent);border-color:var(--accent)}
  .card-foot .act.go:hover{background:var(--accent);color:#fff}
  /* ── modal ── */
  .modal-overlay{
    display:none;position:fixed;inset:0;z-index:100;
    background:rgba(0,0,0,.25);
    backdrop-filter:blur(2px);
    align-items:center;justify-content:center;
    padding:24px;
  }
  .modal-overlay.open{display:flex}
  .modal{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:var(--radius);
    box-shadow:var(--shadow-lg);
    max-width:640px;width:100%;max-height:85vh;
    overflow-y:auto;
    animation:modalIn .2s ease;
  }
  @keyframes modalIn{
    from{opacity:0;transform:translateY(12px) scale(.98)}
    to{opacity:1;transform:translateY(0) scale(1)}
  }
  .modal-header{
    padding:20px 24px 0;
    display:flex;align-items:flex-start;justify-content:space-between;
    gap:12px;
  }
  .modal-header .m-title{
    font-family:Charter,Georgia,serif;
    font-size:17px;font-weight:600;
    color:var(--text);
    flex:1;min-width:0;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  }
  .modal-close{
    flex-shrink:0;width:28px;height:28px;
    border-radius:50%;border:1px solid var(--border);
    background:var(--card);color:var(--muted);
    display:flex;align-items:center;justify-content:center;
    cursor:pointer;font-size:16px;line-height:1;
    transition:all .15s;
  }
  .modal-close:hover{background:var(--hover);color:var(--text)}
  .modal-body{padding:16px 24px 24px}
  .modal-body .field{
    margin-bottom:16px;
  }
  .modal-body .field-label{
    font-size:11px;font-weight:600;color:var(--muted);
    text-transform:uppercase;letter-spacing:.04em;
    margin-bottom:4px;
  }
  .modal-body .field-value{
    font-size:13px;color:var(--text);line-height:1.6;
    word-break:break-word;
  }
  .modal-body .field-value.mono{
    font-family:"SF Mono",Menlo,monospace;
    font-size:12px;background:var(--code-bg);
    padding:10px 14px;border-radius:var(--radius-sm);
    border:1px solid var(--border);
    overflow-x:auto;white-space:pre-wrap;word-break:break-all;
  }
  .modal-body .first-msg{
    background:var(--code-bg);
    border:1px solid var(--border);
    border-radius:var(--radius-sm);
    padding:12px 16px;
    font-size:13px;line-height:1.65;
    color:var(--accent);
    max-height:200px;overflow-y:auto;
  }
  .modal-actions{
    display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;
  }
  /* ── pills ── */
  .pill{
    font-size:11px;padding:4px 10px;border-radius:20px;
    border:1px solid var(--border);background:var(--card);
    color:var(--muted);cursor:pointer;font-family:inherit;
    transition:all .15s;white-space:nowrap;
  }
  .pill:hover{color:var(--text);border-color:#c8c0b4}
  .pill.go{color:var(--accent);border-color:var(--accent)}
  .pill.go:hover{background:var(--accent);color:#fff}
  .pill.danger{color:var(--red);border-color:transparent}
  .pill.danger:hover{background:#fef2f2;border-color:#fecaca}
  .pill.done{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
  /* ── empty state ── */
  .empty{
    text-align:center;padding:80px 20px;color:var(--faint);
    font-family:Charter,Georgia,serif;font-size:15px;
    grid-column:1/-1;
  }
  /* ── mark ── */
  mark{background:#fef3c7;color:var(--amber);padding:0 2px;border-radius:2px}
  /* ── responsive ── */
  @media(max-width:900px){
    .card-grid{grid-template-columns:repeat(2,1fr);gap:10px}
  }
  @media(max-width:600px){
    .card-grid{grid-template-columns:1fr;gap:10px}
    .header{padding:14px 16px 12px}
    .main{padding:0 16px 32px}
    .toolbar{flex-direction:column}
    .search-wrap{min-width:100%}
    .modal-overlay{padding:12px}
    .modal-header{padding:16px 18px 0}
    .modal-body{padding:12px 18px 18px}
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <div class="title">Claude Code 对话</div>
    <div class="meta">共 <em id="total">0</em> 个会话 · 点击卡片查看详情</div>
    <div class="toolbar">
      <div class="search-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input id="q" placeholder="搜索对话内容、项目名称、Session ID ..." autofocus>
      </div>
      <select id="proj-filter"><option value="">全部项目</option></select>
      <button id="btn-refresh" class="pill">刷新</button>
      <button id="btn-cleanup" class="pill danger">清理旧对话</button>
    </div>
  </div>
</div>

<div class="cleanup-msg" id="cleanup-msg"></div>

<div class="main">
  <div class="grid-wrap" id="list"></div>
</div>

<!-- Modal -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal" id="modal" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div class="m-title" id="modal-title"></div>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const AUTH_TOKEN = "__AUTH_TOKEN__";
let all=[];

function $(id){return document.getElementById(id)}

function fmtTime(ts){
  const d=new Date(ts*1000)
  const pad=n=>String(n).padStart(2,"0")
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function dateLabel(ts){
  const d=new Date(ts*1000),now=new Date()
  const today=new Date(now.getFullYear(),now.getMonth(),now.getDate())
  const yesterday=new Date(today-864e5)
  const dday=new Date(d.getFullYear(),d.getMonth(),d.getDate())
  if(dday>=today)return "今天"
  if(dday>=yesterday)return "昨天"
  const weekAgo=new Date(today-7*864e5)
  if(dday>=weekAgo)return "本周"
  return "更早"
}

function esc(s){
  const d=document.createElement("div")
  d.textContent=s||""
  return d.innerHTML
}

function escAttr(s){
  return (s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/\\/g,"\\\\").replace(/'/g,"&#39;")
}

function highlight(text,q){
  if(!q)return esc(text)
  const lo=text.toLowerCase(),ql=q.toLowerCase()
  const idx=lo.indexOf(ql)
  if(idx<0)return esc(text)
  return esc(text.slice(0,idx))+"<mark>"+esc(text.slice(idx,idx+q.length))+"</mark>"+esc(text.slice(idx+q.length))
}

function shortProject(path){
  const parts=(path||"").split("/").filter(Boolean)
  if(parts.length<=2)return parts.join("/")
  return parts.slice(-2).join("/")
}

function render(){
  const q=$("q").value
  const proj=$("proj-filter").value
  let data=[...all]

  if(proj) data=data.filter(s=>s.project===proj||s.project_encoded===proj)
  if(q){
    const lo=q.toLowerCase()
    data=data.filter(s=>
      s.id.includes(lo)||
      (s.name||"").toLowerCase().includes(lo)||
      (s.project||"").toLowerCase().includes(lo)||
      (s.first||"").toLowerCase().includes(lo)||
      s.last.some(m=>m.text.toLowerCase().includes(lo))
    )
  }

  // sort by time desc
  data.sort((a,b)=>b.mtime-a.mtime)

  let lastSep="",cards="",openGrid=false

  data.forEach(s=>{
    const sep=dateLabel(s.mtime)
    if(sep!==lastSep){
      if(openGrid){cards+="</div>";openGrid=false}
      lastSep=sep
      cards+=`<div class="date-sep">${sep}</div><div class="card-grid">`
      openGrid=true
    }

    const namedClass=s.name?" named":""
    const nameTag=s.name?`<span class="card-name">${esc(s.name)}</span>`:""

    // Build message preview blocks
    let msgsHtml=""
    if(s.first){
      msgsHtml+=`<div class="msg-block user"><div class="msg-label u">Q</div>${highlight(s.first.slice(0,300),q)}</div>`
    }
    // Show last assistant response if available
    const lastA=s.last.filter(m=>m.role==="assistant").slice(-1)
    if(lastA.length&&lastA[0].text){
      msgsHtml+=`<div class="msg-block assistant"><div class="msg-label a">A</div>${highlight(lastA[0].text.slice(0,300),q)}</div>`
    }
    if(!msgsHtml){
      msgsHtml='<div class="msg-block assistant" style="color:var(--faint)">(空对话)</div>'
    }

    cards+=`<div class="card${namedClass}" onclick="openModal('${escAttr(s.id)}')">
      <div class="card-top">
        <span class="card-project">${esc(shortProject(s.project))}</span>
        ${nameTag}
      </div>
      <div class="card-body">${msgsHtml}</div>
      <div class="card-foot">
        <span class="time">${fmtTime(s.mtime)}</span>
        <span class="stats">${s.msgs} 条 · ${s.size_kb}KB</span>
        <span class="spacer"></span>
        <button class="act go" onclick="event.stopPropagation();copyResume('${escAttr(s.project)}','${escAttr(s.id)}',this)">恢复</button>
      </div>
    </div>`
  })

  if(openGrid){cards+="</div>"}

  $("list").innerHTML=cards||'<div class="empty">没有匹配的对话</div>'
  $("total").textContent=all.length
}

// ── Modal ──

function openModal(sid){
  const s=all.find(x=>x.id===sid)
  if(!s)return

  $("modal-title").innerHTML=(s.name?esc(s.name):esc(s.id_short+"..."))
  const lastA=s.last.filter(m=>m.role==="assistant").slice(-1)
  const lastAText=lastA.length?lastA[0].text:""

  $("modal-body").innerHTML=`
    <div class="field">
      <div class="field-label">会话名称</div>
      <div class="field-value">${s.name?esc(s.name):'<span style="color:var(--faint)">未命名</span>'}</div>
    </div>
    <div class="field">
      <div class="field-label">Session ID</div>
      <div class="field-value mono">${esc(s.id)}</div>
    </div>
    <div class="field">
      <div class="field-label">项目路径</div>
      <div class="field-value">${esc(s.project)}</div>
    </div>
    <div class="field">
      <div class="field-label">时间 & 统计</div>
      <div class="field-value">${fmtTime(s.mtime)} · ${s.msgs} 条消息 · ${s.size_kb}KB</div>
    </div>
    ${s.first?`
    <div class="field">
      <div class="field-label">第一条消息</div>
      <div class="first-msg">${esc(s.first)}</div>
    </div>`:""}
    ${lastAText?`
    <div class="field">
      <div class="field-label">最后回复</div>
      <div class="first-msg" style="color:#4b5563">${esc(lastAText.slice(0,500))}</div>
    </div>`:""}
    <div class="field">
      <div class="field-label">恢复命令</div>
      <div class="field-value mono">cd ${esc(shellQuote(s.project))} && claude --resume ${esc(shellQuote(s.id))}</div>
    </div>
    <div class="modal-actions">
      <button class="pill go" onclick="copyResume('${escAttr(s.project)}','${escAttr(s.id)}',this)">复制恢复命令</button>
      <button class="pill" onclick="copySid('${escAttr(s.id)}',this)">复制 Session ID</button>
      <button class="pill danger" onclick="deleteOne('${escAttr(s.id)}',this)">删除此对话</button>
    </div>
  `

  $("modal-overlay").classList.add("open")
  document.body.style.overflow="hidden"
}

function closeModal(e){
  if(e&&e.target!==$("modal-overlay"))return
  $("modal-overlay").classList.remove("open")
  document.body.style.overflow=""
}

document.addEventListener("keydown",e=>{
  if(e.key==="Escape")closeModal()
})

// ── Actions ──

function shellQuote(s){
  return "'"+s.replace(/'/g,"'\\''")+"'"
}

async function copyResume(project,sid,btn){
  const text=`cd ${shellQuote(project)} && claude --resume ${shellQuote(sid)}`
  copyText(text,btn)
}

async function copySid(sid,btn){
  copyText(sid,btn)
}

async function copyText(text,btn){
  try{
    await navigator.clipboard.writeText(text)
    const orig=btn.textContent
    btn.textContent="已复制"
    btn.classList.add("done")
    setTimeout(()=>{btn.textContent=orig;btn.classList.remove("done")},1500)
  }catch(e){
    prompt("手动复制:",text)
  }
}

async function loadSessions(){
  try{
    const r=await fetch("api/sessions",{headers:{"X-Auth-Token":AUTH_TOKEN}})
    all=await r.json()
  }catch(e){
    all=[]
    console.error(e)
  }
  const projects=[...new Set(all.map(s=>s.project).filter(Boolean))].sort()
  const sel=$("proj-filter")
  const cur=sel.value
  sel.textContent=""
  const defOpt=document.createElement("option")
  defOpt.value="";defOpt.textContent="全部项目"
  sel.appendChild(defOpt)
  projects.forEach(p=>{
    const opt=document.createElement("option")
    opt.value=p
    opt.textContent=p.split("/").filter(Boolean).slice(-2).join("/")||p
    sel.appendChild(opt)
  })
  sel.value=cur
  render()
}

async function deleteOne(sid,btn){
  if(!confirm("确认删除这个对话?\n\n此操作不可恢复。"))return
  const orig=btn.textContent
  btn.textContent="..."
  btn.disabled=true
  try{
    const r=await fetch("api/sessions/"+sid,{method:"DELETE",headers:{"X-Auth-Token":AUTH_TOKEN}})
    const d=await r.json()
    if(d.ok){
      btn.textContent="已删除"
      btn.classList.add("done")
      all=all.filter(x=>x.id!==sid)
      render()
      closeModal()
    }else{
      alert("删除失败: "+(d.error||"未知错误"))
      btn.textContent=orig
      btn.disabled=false
    }
  }catch(e){
    alert("请求失败: "+e.message)
    btn.textContent=orig
    btn.disabled=false
  }
}

async function doCleanup(){
  const btn=$("btn-cleanup")
  btn.textContent="扫描中..."
  btn.disabled=true
  try{
    const r=await fetch("api/cleanup",{headers:{"X-Auth-Token":AUTH_TOKEN}})
    const d=await r.json()
    const msg=$("cleanup-msg")
    if(d.count===0){
      msg.className="cleanup-msg ok"
      msg.style.display="block"
      msg.innerHTML="没有超过 30 天的旧对话"
    }else{
      msg.className="cleanup-msg"
      msg.style.display="block"
      msg.innerHTML='发现 <b>'+d.count+'</b> 个超过 30 天的对话 (截止 '+d.cutoff+')：<br><br>'
        +d.files.map(f=>"&middot; "+esc(f)).join("<br>")
        +'<br><br><button onclick="doDelete()" style="border-color:#dc2626;color:#dc2626;font-weight:600">确认删除这 '+d.count+' 个对话</button>'
        +'<span style="color:var(--muted);margin-left:8px;font-size:11px">删除后不可恢复</span>'
    }
  }catch(e){
    alert("扫描失败: "+e.message)
  }
  btn.textContent="清理旧对话"
  btn.disabled=false
}

async function doDelete(){
  try{
    const r=await fetch("api/cleanup",{method:"POST",headers:{"X-Auth-Token":AUTH_TOKEN}})
    const d=await r.json()
    $("cleanup-msg").className="cleanup-msg ok"
    $("cleanup-msg").innerHTML="已删除 "+d.count+" 个对话"
    loadSessions()
  }catch(e){
    alert("删除失败: "+e.message)
  }
}

$("q").addEventListener("input",render)
$("proj-filter").addEventListener("change",render)
$("btn-refresh").addEventListener("click",loadSessions)
$("btn-cleanup").addEventListener("click",doCleanup)

loadSessions()
</script>
</body>
</html>"""


# ── HTTP 服务 ─────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def _check_auth(self):
        """验证 API 请求的 auth token"""
        token = self.headers.get("X-Auth-Token", "")
        return secrets.compare_digest(token, AUTH_TOKEN)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            html = HTML_TEMPLATE.replace("__AUTH_TOKEN__", AUTH_TOKEN)
            self.send_html(html)
        elif path == "/api/sessions":
            if not self._check_auth():
                return self.send_error(403)
            self.send_json(scan_sessions())
        elif path == "/api/cleanup":
            if not self._check_auth():
                return self.send_error(403)
            self.send_json(cleanup_old(dry_run=True))
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._check_auth():
            return self.send_error(403)
        if self.path.split("?")[0] == "/api/cleanup":
            self.send_json(cleanup_old(dry_run=False))
        else:
            self.send_error(404)

    def do_DELETE(self):
        if not self._check_auth():
            return self.send_error(403)
        path = self.path.split("?")[0]
        if path.startswith("/api/sessions/") and len(path) > len("/api/sessions/"):
            sid = path[len("/api/sessions/"):]
            self.send_json(delete_session(sid))
        else:
            self.send_error(404)

    def send_html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def send_error(self, code, message=None, explain=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Forbidden" if code == 403 else b"Not Found")

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"\n  CC Session Viewer → http://{HOST}:{PORT}")
    print(f"  Auth token: {AUTH_TOKEN}\n")
    httpd = http.server.HTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出\n")
