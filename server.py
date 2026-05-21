#!/usr/bin/env python3
"""Claude Code 对话管理器

扫描 ~/.claude/projects/ 下所有对话，提供 Web 界面浏览和搜索。
通过公共后端代理访问：http://localhost:3999/tools/cc-session-viewer/
直接访问：http://127.0.0.1:8767
"""

import json
import os
import time
import http.server
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORT = 8767
HOST = "127.0.0.1"


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
    """将 -Users-susu-mytools-xxx 还原为 /Users/susu/mytools/xxx"""
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
        if not proj_dir.is_dir():
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
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
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
        if not proj_dir.is_dir():
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

HTML = r"""<!doctype html>
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
    --radius:10px;
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
  .header-inner{max-width:860px;margin:0 auto}
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
    display:none;max-width:860px;margin:0 auto 12px;padding:12px 16px;
    border-radius:var(--radius-sm);font-size:12px;
    background:#fef2f2;border:1px solid #fecaca;color:var(--red);
  }
  .cleanup-msg.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
  /* ── main list ── */
  .main{padding:0 24px 40px}
  .list{max-width:860px;margin:0 auto}
  /* ── date separator ── */
  .date-sep{
    font-family:Charter,Georgia,serif;
    font-size:12px;color:var(--faint);
    padding:20px 0 8px;letter-spacing:.05em;
    text-transform:uppercase;
  }
  /* ── card ── */
  .card{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:var(--radius);
    margin-bottom:6px;
    box-shadow:var(--shadow);
    transition:box-shadow .2s,border-color .2s;
    overflow:hidden;
  }
  .card:hover{box-shadow:var(--shadow-md);border-color:#d8d0c4}
  .card.named{border-left:3px solid var(--amber)}
  .card-row{
    display:flex;align-items:flex-start;
    padding:14px 16px;gap:14px;cursor:pointer;
    user-select:none;
  }
  .card-left{
    flex-shrink:0;width:110px;
    font-size:12px;color:var(--muted);
    line-height:1.5;
  }
  .card-left .time{font-weight:600;color:var(--text);font-size:13px}
  .card-left .count{font-size:11px;color:var(--faint)}
  .card-name{
    display:inline-block;font-size:10px;font-weight:600;
    background:var(--amber-bg);color:var(--amber);
    padding:1px 6px;border-radius:3px;
    margin-bottom:2px;max-width:100px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  }
  .card-center{flex:1;min-width:0}
  .card-project{
    font-size:11px;color:var(--faint);
    margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  }
  .card-msgs{font-size:12px;line-height:1.55}
  .card-msgs .q{color:var(--accent)}
  .card-msgs .a{color:#4b5563}
  .card-msgs span{display:block}
  .card-msgs span+span{margin-top:1px}
  .card-right{
    flex-shrink:0;display:flex;gap:6px;align-items:center;
  }
  /* ── detail ── */
  .detail{
    display:none;padding:0 16px 16px;
    border-top:1px solid var(--border);
    background:var(--code-bg);
  }
  .detail.open{display:block}
  .detail-meta{
    font-size:12px;color:var(--muted);margin-bottom:10px;padding-top:14px;
    line-height:1.6;
  }
  .detail-meta strong{color:var(--text);font-weight:600}
  .cmd-box{
    background:var(--card);border:1px solid var(--border);
    border-radius:var(--radius-sm);padding:10px 14px;
    font-family:"SF Mono",Menlo,monospace;font-size:12px;
    color:var(--text);margin-bottom:10px;
    overflow-x:auto;white-space:pre-wrap;word-break:break-all;
  }
  .detail-actions{display:flex;gap:8px;flex-wrap:wrap}
  /* ── small buttons ── */
  .pill{
    font-size:11px;padding:4px 10px;border-radius:20px;
    border:1px solid var(--border);background:var(--card);
    color:var(--muted);cursor:pointer;font-family:inherit;
    transition:all .15s;
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
  }
  /* ── mark ── */
  mark{background:#fef3c7;color:var(--amber);padding:0 1px;border-radius:2px}
  /* ── responsive ── */
  @media(max-width:600px){
    .header{padding:14px 16px 12px}
    .main{padding:0 16px 32px}
    .card-row{padding:12px;gap:10px}
    .card-left{width:90px}
    .card-right{flex-direction:column;align-items:flex-end}
    .toolbar{flex-direction:column}
    .search-wrap{min-width:100%}
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
  <div class="list" id="list"></div>
</div>

<script>
let all=[],sortKey="time",sortAsc=true;

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
  return (s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;")
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
  const q=document.getElementById("q").value
  const proj=document.getElementById("proj-filter").value
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

  data.sort((a,b)=>{
    let va,vb
    if(sortKey==="time"){va=a.mtime;vb=b.mtime}
    else if(sortKey==="project"){va=a.project||"";vb=b.project||""}
    else if(sortKey==="msgs"){va=a.msgs;vb=b.msgs}
    if(va<vb)return sortAsc?1:-1
    if(va>vb)return sortAsc?-1:1
    return 0
  })

  let lastSep="",html=""

  data.forEach(s=>{
    const sep=dateLabel(s.mtime)
    if(sep!==lastSep){
      lastSep=sep
      html+=`<div class="date-sep">${sep}</div>`
    }

    const namedClass=s.name?" named":""
    const nameTag=s.name?`<span class="card-name">${esc(s.name)}</span>`:""
    const msgsHtml=s.last.map(m=>
      `<span class="${m.role==='user'?'q':'a'}">${highlight(m.text.slice(0,140),q)}</span>`
    ).join("")

    html+=`<div class="card${namedClass}">
      <div class="card-row" onclick="toggleDetail('${s.id}')">
        <div class="card-left">
          ${nameTag}
          <div class="time">${fmtTime(s.mtime)}</div>
          <div class="count">${s.msgs} 条消息 · ${s.size_kb}KB</div>
        </div>
        <div class="card-center">
          <div class="card-project">${esc(shortProject(s.project))}</div>
          <div class="card-msgs">${msgsHtml||'<span class="a" style="color:var(--faint)">(空对话)</span>'}</div>
        </div>
        <div class="card-right">
          <button class="pill go" onclick="event.stopPropagation();copyText('cd &quot;${escAttr(s.project)}&quot; && claude --resume &quot;${s.id}&quot;',this)">恢复</button>
        </div>
      </div>
      <div class="detail" id="detail-${s.id}">
        <div class="detail-meta">
          ${s.name?`<strong style="color:var(--amber)">${esc(s.name)}</strong> · `:""}
          <strong>Session:</strong> ${esc(s.id)}<br>
          <strong>项目:</strong> ${esc(s.project)}<br>
          <strong>时间:</strong> ${fmtTime(s.mtime)} · ${s.msgs} 条消息 · ${s.size_kb}KB
        </div>
        ${s.first?`<div style="font-size:11px;color:var(--muted);margin-bottom:4px">第一条消息:</div><div style="font-size:12px;margin-bottom:10px;line-height:1.5;color:var(--text)">${esc(s.first)}</div>`:""}
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px">恢复命令 (复制到终端):</div>
        <div class="cmd-box">cd "${esc(s.project)}" && claude --resume "${s.id}"</div>
        <div class="detail-actions">
          <button class="pill go" onclick="copyText('cd &quot;${escAttr(s.project)}&quot; && claude --resume &quot;${s.id}&quot;',this)">复制恢复命令</button>
          <button class="pill" onclick="copyText('${s.id}',this)">复制 Session ID</button>
          <button class="pill danger" onclick="deleteOne('${s.id}',this)">删除此对话</button>
        </div>
      </div>
    </div>`
  })

  document.getElementById("list").innerHTML=html||'<div class="empty">没有匹配的对话</div>'
  document.getElementById("total").textContent=all.length
}

function toggleDetail(sid){
  const el=document.getElementById("detail-"+sid)
  if(!el)return
  el.classList.toggle("open")
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
    const r=await fetch("api/sessions")
    all=await r.json()
  }catch(e){
    all=[]
    console.error(e)
  }
  // fill project filter
  const projects=[...new Set(all.map(s=>s.project).filter(Boolean))].sort()
  const sel=document.getElementById("proj-filter")
  const cur=sel.value
  sel.innerHTML='<option value="">全部项目</option>'+projects.map(p=>{
    const label=p.split("/").filter(Boolean).slice(-2).join("/")||p
    return `<option value="${esc(p)}">${esc(label)}</option>`
  }).join("")
  sel.value=cur
  render()
}

async function deleteOne(sid,btn){
  if(!confirm("确认删除这个对话?\n\n此操作不可恢复。"))return
  const orig=btn.textContent
  btn.textContent="..."
  btn.disabled=true
  try{
    const r=await fetch("api/sessions/"+sid,{method:"DELETE"})
    const d=await r.json()
    if(d.ok){
      btn.textContent="已删除"
      btn.classList.add("done")
      setTimeout(()=>loadSessions(),800)
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
  const btn=document.getElementById("btn-cleanup")
  btn.textContent="扫描中..."
  btn.disabled=true
  try{
    const r=await fetch("api/cleanup")
    const d=await r.json()
    const msg=document.getElementById("cleanup-msg")
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
    const r=await fetch("api/cleanup",{method:"POST"})
    const d=await r.json()
    document.getElementById("cleanup-msg").className="cleanup-msg ok"
    document.getElementById("cleanup-msg").innerHTML="已删除 "+d.count+" 个对话"
    loadSessions()
  }catch(e){
    alert("删除失败: "+e.message)
  }
}

// sort — keep it simple, always by time for now
document.getElementById("q").addEventListener("input",render)
document.getElementById("proj-filter").addEventListener("change",render)
document.getElementById("btn-refresh").addEventListener("click",loadSessions)
document.getElementById("btn-cleanup").addEventListener("click",doCleanup)

loadSessions()
</script>
</body>
</html>"""


# ── HTTP 服务 ─────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            self.send_html(HTML)
        elif path == "/api/sessions":
            self.send_json(scan_sessions())
        elif path == "/api/cleanup":
            self.send_json(cleanup_old(dry_run=True))
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.split("?")[0] == "/api/cleanup":
            self.send_json(cleanup_old(dry_run=False))
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        # /api/sessions/<session-id>
        if path.startswith("/api/sessions/") and len(path) > len("/api/sessions/"):
            sid = path[len("/api/sessions/"):]
            self.send_json(delete_session(sid))
        else:
            self.send_error(404)

    def send_html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"\n  CC 对话管理器 → http://{HOST}:{PORT}")
    print(f"  通过公共后端访问 → http://localhost:3999/tools/cc-session-viewer/\n")
    httpd = http.server.HTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出\n")
