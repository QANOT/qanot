"""Static HTML template string for the Qanot AI web dashboard."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Qanot AI — Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --void: #06090f;
  --deep: #0a0e17;
  --surface: #0f1520;
  --glass: rgba(15,21,32,.72);
  --border: rgba(0,240,255,.08);
  --border-hover: rgba(0,240,255,.18);
  --text: #c8d6e5;
  --text-bright: #edf2f7;
  --muted: #4a5568;
  --cyan: #00f0ff;
  --amber: #f6ad55;
  --emerald: #48bb78;
  --rose: #fc8181;
  --indigo: #7f9cf5;
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:'Outfit',sans-serif;background:var(--void);color:var(--text);min-height:100vh;overflow-x:hidden}

/* Noise overlay */
body::before{content:'';position:fixed;inset:0;opacity:.035;pointer-events:none;z-index:999;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 512 512' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.65' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

/* Scanline effect */
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:998;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 4px)}

/* Header */
.header{padding:20px 28px;display:flex;justify-content:space-between;align-items:center;
  border-bottom:1px solid var(--border);position:relative}
.header::after{content:'';position:absolute;bottom:-1px;left:0;width:100%;height:1px;
  background:linear-gradient(90deg,transparent,var(--cyan),transparent);opacity:.3}
.brand{display:flex;align-items:center;gap:14px}
.brand-mark{width:36px;height:36px;border:2px solid var(--cyan);border-radius:8px;
  display:flex;align-items:center;justify-content:center;font-size:16px;
  box-shadow:0 0 15px rgba(0,240,255,.15),inset 0 0 15px rgba(0,240,255,.05)}
.brand h1{font-size:17px;font-weight:600;letter-spacing:-.3px;color:var(--text-bright)}
.brand h1 em{font-style:normal;color:var(--cyan);font-weight:800}
.header-right{display:flex;align-items:center;gap:16px}
.live-badge{display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:11px;
  color:var(--emerald);letter-spacing:.5px;text-transform:uppercase;
  padding:4px 10px;border:1px solid rgba(72,187,120,.2);border-radius:4px;background:rgba(72,187,120,.04)}
.live-badge::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--emerald);
  box-shadow:0 0 6px var(--emerald);animation:blink 1.5s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.clock{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted)}

/* Grid */
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding:20px 28px}
@media(max-width:1100px){.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.grid{grid-template-columns:1fr}}

/* Cards */
.card{background:var(--glass);backdrop-filter:blur(12px);border:1px solid var(--border);
  border-radius:12px;padding:22px;transition:border-color .3s,box-shadow .3s;
  animation:fadeUp .5s ease both}
.card:hover{border-color:var(--border-hover);box-shadow:0 4px 30px rgba(0,240,255,.03)}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.card:nth-child(1){animation-delay:.05s}
.card:nth-child(2){animation-delay:.1s}
.card:nth-child(3){animation-delay:.15s}
.card:nth-child(4){animation-delay:.2s}
.card:nth-child(5){animation-delay:.25s}
.card-wide{grid-column:1/-1;animation-delay:.3s}

.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.card-label{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;
  text-transform:uppercase;letter-spacing:2px;color:var(--muted)}
.card-icon{font-size:14px;opacity:.5}

/* Big stat */
.big{font-size:34px;font-weight:800;letter-spacing:-1.5px;color:var(--text-bright);line-height:1}
.big-sub{font-size:12px;color:var(--muted);margin-top:6px;font-family:'IBM Plex Mono',monospace}

/* Rows */
.row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;
  border-bottom:1px solid rgba(255,255,255,.03)}
.row:last-child{border:none}
.row-k{font-size:12px;color:var(--muted)}
.row-v{font-size:12px;font-family:'IBM Plex Mono',monospace;color:var(--text-bright);font-weight:500}

/* Pills */
.pill{display:inline-block;padding:3px 9px;border-radius:3px;font-size:10px;font-weight:600;
  font-family:'IBM Plex Mono',monospace;letter-spacing:.5px}
.pill-cyan{background:rgba(0,240,255,.08);color:var(--cyan);border:1px solid rgba(0,240,255,.15)}
.pill-amber{background:rgba(246,173,85,.08);color:var(--amber);border:1px solid rgba(246,173,85,.15)}
.pill-emerald{background:rgba(72,187,120,.08);color:var(--emerald);border:1px solid rgba(72,187,120,.15)}
.pill-indigo{background:rgba(127,156,245,.08);color:var(--indigo);border:1px solid rgba(127,156,245,.15)}

/* Context bar */
.ctx-track{height:4px;background:rgba(255,255,255,.04);border-radius:2px;margin-top:14px;overflow:hidden}
.ctx-fill{height:100%;border-radius:2px;transition:width .8s cubic-bezier(.4,0,.2,1),background .5s}
.ctx-label{display:flex;justify-content:space-between;margin-top:5px;font-size:10px;
  font-family:'IBM Plex Mono',monospace;color:var(--muted)}

/* Scroll areas */
.scroll{max-height:260px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.scroll::-webkit-scrollbar{width:4px}
.scroll::-webkit-scrollbar-track{background:transparent}
.scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* Tools */
.tool{padding:7px 0;border-bottom:1px solid rgba(255,255,255,.02);font-size:11px;display:flex;gap:8px}
.tool:last-child{border:none}
.tool-n{color:var(--cyan);font-family:'IBM Plex Mono',monospace;font-weight:600;white-space:nowrap;font-size:11px}
.tool-d{color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* Memory */
.mem-layout{display:grid;grid-template-columns:240px 1fr;gap:16px;min-height:380px}
@media(max-width:700px){.mem-layout{grid-template-columns:1fr;min-height:auto}}
.mem-sidebar{border-right:1px solid var(--border);padding-right:14px}
.mem-file{padding:8px 10px;margin:2px 0;border-radius:6px;cursor:pointer;display:flex;
  justify-content:space-between;align-items:center;transition:background .15s;font-size:12px}
.mem-file:hover{background:rgba(0,240,255,.04)}
.mem-file.active{background:rgba(0,240,255,.07);border-left:2px solid var(--cyan);padding-left:8px}
.mem-file-name{color:var(--indigo);font-weight:500}
.mem-file-size{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted)}
.mem-viewer{background:var(--deep);border:1px solid var(--border);border-radius:10px;
  padding:18px;overflow-y:auto;max-height:380px}
.mem-viewer-title{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;
  color:var(--cyan);margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.mem-viewer-content{font-family:'IBM Plex Mono',monospace;font-size:12px;line-height:1.7;
  color:var(--text);white-space:pre-wrap;word-break:break-word}

/* Footer */
.footer{text-align:center;padding:16px;font-size:10px;color:var(--muted);
  font-family:'IBM Plex Mono',monospace;letter-spacing:.5px;border-top:1px solid var(--border)}
</style>
</head>
<body>

<div class="header">
  <div class="brand">
    <div class="brand-mark">&#x1fab6;</div>
    <h1><em>QANOT</em> AI</h1>
  </div>
  <div class="header-right">
    <div class="live-badge">SYSTEM ONLINE</div>
    <div class="clock" id="clock"></div>
  </div>
</div>

<div class="grid">

  <!-- STATUS -->
  <div class="card">
    <div class="card-head"><span class="card-label">&#x25cf; Status</span><span class="card-icon">&#x1f4ca;</span></div>
    <div class="big" id="s-model">&#x2014;</div>
    <div class="big-sub" id="s-provider">loading...</div>
    <div class="ctx-track"><div class="ctx-fill" id="ctx-bar" style="width:0%;background:var(--emerald)"></div></div>
    <div class="ctx-label"><span>Context</span><span id="s-ctx">0%</span></div>
    <div style="margin-top:12px">
      <div class="row"><span class="row-k">Uptime</span><span class="row-v" id="s-up">&#x2014;</span></div>
      <div class="row"><span class="row-k">Turns</span><span class="row-v" id="s-turns">0</span></div>
      <div class="row"><span class="row-k">API Calls</span><span class="row-v" id="s-api">0</span></div>
      <div class="row"><span class="row-k">Active Users</span><span class="row-v" id="s-users">0</span></div>
    </div>
  </div>

  <!-- COSTS -->
  <div class="card">
    <div class="card-head"><span class="card-label">&#x25cf; Costs</span><span class="card-icon">&#x1f4b0;</span></div>
    <div class="big" id="c-total">$0.00</div>
    <div class="big-sub">total spent</div>
    <div id="c-users" style="margin-top:14px"></div>
  </div>

  <!-- ROUTING -->
  <div class="card">
    <div class="card-head"><span class="card-label">&#x25cf; Routing</span><span class="card-icon">&#x1f500;</span></div>
    <div id="r-info"><div class="big-sub">loading...</div></div>
  </div>

  <!-- CONFIG -->
  <div class="card">
    <div class="card-head"><span class="card-label">&#x25cf; Config</span><span class="card-icon">&#x2699;&#xfe0f;</span></div>
    <div class="scroll" id="cfg"></div>
  </div>

  <!-- TOOLS -->
  <div class="card">
    <div class="card-head"><span class="card-label">&#x25cf; Tools</span><span class="pill pill-cyan" id="t-count">0</span></div>
    <div class="scroll" id="t-list"></div>
  </div>

  <!-- MEMORY (wide) -->
  <div class="card card-wide">
    <div class="card-head"><span class="card-label">&#x25cf; Memory &amp; Workspace</span><span class="card-icon">&#x1f4c1;</span></div>
    <div class="mem-layout">
      <div class="mem-sidebar scroll" id="m-files"></div>
      <div class="mem-viewer">
        <div class="mem-viewer-title" id="mv-title">Select a file</div>
        <div class="mem-viewer-content" id="mv-content">Click a file to view its contents.</div>
      </div>
    </div>
  </div>

</div>

<div class="footer">QANOT AI v1.9.0 &#x2022; Built in Tashkent &#x1f1fa;&#x1f1ff;</div>

<script>
// Clock
function tick(){document.getElementById('clock').textContent=new Date().toLocaleTimeString()}
tick();setInterval(tick,1000);

// Data loader
function api(p){return fetch(p).then(function(x){return x.json()})}
async function load(){
  try{
    var[s,c,r,cf,t,m]=await Promise.all([
      api('/api/status'),
      api('/api/costs'),
      api('/api/routing'),
      api('/api/config'),
      api('/api/tools'),
      api('/api/memory')
    ]);

    // Status
    document.getElementById('s-model').textContent=s.model;
    document.getElementById('s-provider').textContent=s.provider+' \u2022 '+s.bot_name;
    document.getElementById('s-up').textContent=s.uptime;
    document.getElementById('s-ctx').textContent=s.context_percent+'%';
    var bar=document.getElementById('ctx-bar');
    bar.style.width=s.context_percent+'%';
    bar.style.background=s.context_percent>70?'var(--rose)':s.context_percent>40?'var(--amber)':'var(--emerald)';
    document.getElementById('s-turns').textContent=s.turn_count;
    document.getElementById('s-api').textContent=s.api_calls;
    document.getElementById('s-users').textContent=s.active_conversations;

    // Costs
    document.getElementById('c-total').textContent='$'+c.total_cost.toFixed(2);
    var cu=document.getElementById('c-users');cu.innerHTML='';
    Object.entries(c.users||{}).forEach(function(e){
      var uid=e[0],d=e[1];
      cu.innerHTML+='<div class="row"><span class="row-k">User '+uid.slice(-4)+'</span>'
        +'<span class="pill pill-emerald">$'+(d.total_cost||0).toFixed(3)+' \u2022 '+(d.turns||0)+' turns</span></div>';
    });

    // Routing
    var ri=document.getElementById('r-info');
    if(r.stats){
      var st=r.stats;
      ri.innerHTML='<div class="big" style="font-size:28px">'+st.savings_pct+'%</div>'
        +'<div class="big-sub">cost savings</div>'
        +'<div style="margin-top:12px">'
        +'<div class="row"><span class="row-k">Simple</span><span class="pill pill-emerald">'+r.cheap_model+'</span></div>'
        +'<div class="row"><span class="row-k">Primary</span><span class="pill pill-indigo">'+r.primary_model+'</span></div>'
        +'<div class="row"><span class="row-k">Requests</span><span class="row-v">'+st.total+' ('+st.routed_cheap+' cheap)</span></div>'
        +'</div>';
    }else{ri.innerHTML='<div class="big-sub">Routing disabled</div>';}

    // Config
    var ci=document.getElementById('cfg');ci.innerHTML='';
    Object.entries(cf).forEach(function(e){
      ci.innerHTML+='<div class="row"><span class="row-k">'+e[0]+'</span><span class="row-v">'+e[1]+'</span></div>';
    });

    // Tools
    document.getElementById('t-count').textContent=t.count;
    var tl=document.getElementById('t-list');
    tl.innerHTML=t.tools.map(function(x){
      return '<div class="tool"><span class="tool-n">'+x.name+'</span><span class="tool-d">'+(x.description||'').slice(0,50)+'</span></div>';
    }).join('');

    // Memory files
    var mf=document.getElementById('m-files');
    mf.innerHTML=m.files.map(function(f){
      return '<div class="mem-file" onclick="viewFile(this)" data-name="'+f.name+'">'
        +'<span class="mem-file-name">'+f.name+'</span>'
        +'<span class="mem-file-size">'+((f.size||0)/1024).toFixed(1)+'K</span></div>';
    }).join('');

  }catch(e){console.error('Dashboard load error:',e)}
}

async function viewFile(el){
  document.querySelectorAll('.mem-file').forEach(function(x){x.classList.remove('active')});
  el.classList.add('active');
  var name=el.getAttribute('data-name');
  var title=document.getElementById('mv-title');
  var content=document.getElementById('mv-content');
  title.textContent=name;
  content.textContent='Loading...';
  try{
    var fn=name.split('/').pop();
    var r=await fetch('/api/memory/'+fn);
    var d=await r.json();
    content.textContent=d.content||d.error||'Empty file';
  }catch(e){content.textContent='Error loading file';}
}

load();
setInterval(load,8000);
</script>
</body>
</html>"""
