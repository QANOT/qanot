"""WebChat widget — self-contained HTML/CSS/JS chat interface."""

from __future__ import annotations

WEBCHAT_WIDGET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Qanot AI Chat</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; height: 100vh; display: flex; flex-direction: column; }
#header { padding: 12px 16px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 8px; }
#header h1 { font-size: 16px; font-weight: 600; }
#status { width: 8px; height: 8px; border-radius: 50%; background: #ef4444; }
#status.connected { background: #22c55e; }
#messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.msg { max-width: 80%; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.msg.user { align-self: flex-end; background: #1d4ed8; color: white; border-bottom-right-radius: 4px; }
.msg.assistant { align-self: flex-start; background: #1e293b; border: 1px solid #334155; border-bottom-left-radius: 4px; }
.msg.tool { align-self: flex-start; background: #1e293b; border: 1px solid #334155; font-size: 12px; color: #94a3b8; font-style: italic; padding: 6px 10px; }
.msg.error { align-self: center; background: #7f1d1d; color: #fca5a5; font-size: 12px; }
#input-area { padding: 12px 16px; background: #1e293b; border-top: 1px solid #334155; display: flex; gap: 8px; }
#input { flex: 1; padding: 10px 14px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; font-size: 14px; outline: none; }
#input:focus { border-color: #1d4ed8; }
#send { padding: 10px 20px; background: #1d4ed8; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; }
#send:hover { background: #1e40af; }
#send:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
</head>
<body>
<div id="header">
  <div id="status"></div>
  <h1>Qanot AI</h1>
</div>
<div id="messages"></div>
<div id="input-area">
  <input id="input" type="text" placeholder="Xabar yozing..." autocomplete="off">
  <button id="send">Yuborish</button>
</div>
<script>
const PORT = '{{PORT}}';
const TOKEN = '{{TOKEN}}';
const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const host = location.hostname || 'localhost';
const wsUrl = protocol + '//' + host + ':' + PORT + '/ws/chat' + (TOKEN ? '?token=' + TOKEN : '');

let ws = null;
let sessionId = localStorage.getItem('qanot_session') || '';
let currentAssistantMsg = null;
let reconnectTimer = null;

const messages = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const status = document.getElementById('status');

function connect() {
  const url = sessionId ? wsUrl + (wsUrl.includes('?') ? '&' : '?') + 'session_id=' + sessionId : wsUrl;
  ws = new WebSocket(url);

  ws.onopen = () => {
    status.classList.add('connected');
    sendBtn.disabled = false;
  };

  ws.onclose = () => {
    status.classList.remove('connected');
    sendBtn.disabled = true;
    reconnectTimer = setTimeout(connect, 3000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);

    if (data.type === 'connected') {
      sessionId = data.session_id;
      localStorage.setItem('qanot_session', sessionId);
    }
    else if (data.type === 'text_delta') {
      if (!currentAssistantMsg) {
        currentAssistantMsg = addMsg('', 'assistant');
      }
      currentAssistantMsg.textContent += data.text;
      scrollBottom();
    }
    else if (data.type === 'tool_use') {
      addMsg('Using: ' + data.tool_name, 'tool');
    }
    else if (data.type === 'done') {
      currentAssistantMsg = null;
    }
    else if (data.type === 'error') {
      addMsg(data.message, 'error');
    }
    else if (data.type === 'reset') {
      messages.innerHTML = '';
      addMsg('Conversation cleared', 'tool');
    }
  };
}

function addMsg(text, cls) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  messages.appendChild(div);
  scrollBottom();
  return div;
}

function scrollBottom() {
  messages.scrollTop = messages.scrollHeight;
}

function send() {
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== 1) return;

  addMsg(text, 'user');
  ws.send(JSON.stringify({ type: 'message', text: text }));
  input.value = '';
  currentAssistantMsg = null;
}

input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
sendBtn.addEventListener('click', send);

connect();
</script>
</body>
</html>"""
