"""Browser stream endpoint — remote browser interaction via WebSocket.

Serves a single-file HTML5 client at ``GET /stream/{token}`` and a
WebSocket relay at ``WS /stream/{token}/ws``.  Used by PAYMENT_GATE to
let a remote user complete checkout on the agent's live browser tab.

No frame data or keystroke values are logged anywhere in this module.
"""

from __future__ import annotations

import json
import re

from fastapi.responses import HTMLResponse

from core.logging_config import get_logger
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from services.browser.stream_session import get_stream_session_manager
from ui.core.security import reject_websocket

logger = get_logger(__name__)

router = APIRouter(tags=["browser-stream"])

# ---------------------------------------------------------------------------
# Token format validation (FIX H6)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# -----------------------------------------------------------------------
# HTTP — serve the HTML client
# -----------------------------------------------------------------------


@router.get("/stream/{token}", response_class=HTMLResponse)
async def stream_page(token: str) -> HTMLResponse:
    """Serve the single-file HTML5 client for a streaming session."""
    # Validate token format BEFORE any lookup (defense-in-depth)
    if not _TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail="invalid_token_format")
    mgr = get_stream_session_manager()
    bridge = mgr.get_session(token)
    if bridge is None:
        return HTMLResponse(
            content=(
                "<html><body style='background:#000;color:#aaa;"
                "font-family:sans-serif;display:flex;align-items:center;"
                "justify-content:center;height:100vh'>"
                "<h2>Session expired or invalid</h2></body></html>"
            ),
            status_code=404,
        )
    return HTMLResponse(content=_CLIENT_HTML.replace("{{TOKEN}}", token))


# -----------------------------------------------------------------------
# WebSocket — frame + input relay
# -----------------------------------------------------------------------


@router.websocket("/stream/{token}/ws")
async def stream_websocket(websocket: WebSocket, token: str) -> None:
    """WebSocket relay: binary JPEG frames out, JSON input events in."""
    mgr = get_stream_session_manager()
    bridge = mgr.get_session(token)

    if bridge is None:
        await reject_websocket(websocket, code=4001, reason="Invalid or expired token")
        return

    if not mgr.mark_connected(token):
        await reject_websocket(websocket, code=4002, reason="Session already connected")
        return

    await websocket.accept()
    logger.info("Stream client connected: %s", token[:8])

    # Wire the frame callback — bridge pushes JPEG bytes here
    async def _send_frame(jpeg_bytes: bytes) -> None:
        try:
            await websocket.send_bytes(jpeg_bytes)
        except Exception:
            pass  # Disconnect handled below

    bridge.on_frame = _send_frame

    try:
        while True:
            data = await websocket.receive_text()
            try:
                event = json.loads(data)
                await bridge.relay_input(event)
            except (json.JSONDecodeError, Exception):
                pass  # Malformed input silently dropped
    except WebSocketDisconnect:
        logger.info("Stream client disconnected: %s", token[:8])
    except Exception:
        logger.debug("Stream WebSocket error: %s", token[:8])
    finally:
        bridge.on_frame = None
        mgr.mark_disconnected(token)


# -----------------------------------------------------------------------
# Inline HTML5 client  (~180 lines, no build step, no external files)
# -----------------------------------------------------------------------

_CLIENT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Viola — Complete Payment</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#000;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
#bar{position:fixed;top:0;left:0;right:0;height:36px;
  background:rgba(13,13,13,.95);display:flex;align-items:center;
  justify-content:space-between;padding:0 12px;z-index:10;
  border-bottom:1px solid rgba(255,255,255,.1)}
#bar span{color:rgba(255,255,255,.6);font-size:12px}
#bar button{background:rgba(255,255,255,.15);color:#fff;border:none;
  border-radius:4px;padding:4px 14px;font-size:12px;cursor:pointer}
#bar button:active{background:rgba(255,255,255,.3)}
canvas{position:fixed;top:36px;left:0;width:100%;height:calc(100% - 36px);
  touch-action:none;cursor:pointer;object-fit:contain}
#status{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
  color:rgba(255,255,255,.5);font-size:14px;text-align:center}
</style>
</head>
<body>
<div id="bar">
  <span>Powered by Viola</span>
  <button id="done">Done</button>
</div>
<canvas id="c"></canvas>
<div id="status">Connecting&hellip;</div>

<script>
(function(){
  var token='{{TOKEN}}';
  var canvas=document.getElementById('c');
  var ctx=canvas.getContext('2d');
  var status=document.getElementById('status');
  var ws,connected=false;
  var fw=1280,fh=720; // frame dimensions (updated from decoded bitmap)

  function connect(){
    var proto=location.protocol==='https:'?'wss:':'ws:';
    ws=new WebSocket(proto+'//'+location.host+'/stream/'+token+'/ws');
    ws.binaryType='arraybuffer';

    ws.onopen=function(){
      connected=true;
      status.style.display='none';
    };

    ws.onmessage=function(e){
      if(!(e.data instanceof ArrayBuffer))return;
      var blob=new Blob([e.data],{type:'image/jpeg'});
      createImageBitmap(blob).then(function(bmp){
        fw=bmp.width; fh=bmp.height;
        canvas.width=fw; canvas.height=fh;
        ctx.drawImage(bmp,0,0);
        bmp.close();
      });
    };

    ws.onclose=function(){
      connected=false;
      status.textContent='Session ended';
      status.style.display='block';
    };
    ws.onerror=function(){
      status.textContent='Connection error';
      status.style.display='block';
    };
  }

  function send(obj){
    if(connected&&ws.readyState===1) ws.send(JSON.stringify(obj));
  }

  function xy(cx,cy){
    var r=canvas.getBoundingClientRect();
    return{x:Math.round((cx-r.left)*(fw/r.width)),
           y:Math.round((cy-r.top)*(fh/r.height))};
  }

  // --- Mouse ---
  canvas.addEventListener('mousedown',function(e){
    var p=xy(e.clientX,e.clientY);
    send({type:'mouse',action:'mousePressed',x:p.x,y:p.y,button:'left',clickCount:1});
  });
  canvas.addEventListener('mouseup',function(e){
    var p=xy(e.clientX,e.clientY);
    send({type:'mouse',action:'mouseReleased',x:p.x,y:p.y,button:'left',clickCount:1});
  });
  canvas.addEventListener('mousemove',function(e){
    if(!e.buttons)return;
    var p=xy(e.clientX,e.clientY);
    send({type:'mouse',action:'mouseMoved',x:p.x,y:p.y});
  });

  // --- Touch ---
  function touchPts(ev){
    var pts=[];
    for(var i=0;i<ev.touches.length;i++){
      var p=xy(ev.touches[i].clientX,ev.touches[i].clientY);
      pts.push({x:p.x,y:p.y});
    }
    return pts;
  }
  canvas.addEventListener('touchstart',function(e){
    e.preventDefault();
    send({type:'touch',action:'touchStart',touchPoints:touchPts(e)});
  },{passive:false});
  canvas.addEventListener('touchmove',function(e){
    e.preventDefault();
    send({type:'touch',action:'touchMove',touchPoints:touchPts(e)});
  },{passive:false});
  canvas.addEventListener('touchend',function(e){
    e.preventDefault();
    send({type:'touch',action:'touchEnd',touchPoints:[]});
  },{passive:false});

  // --- Keyboard ---
  document.addEventListener('keydown',function(e){
    var t=e.target.tagName;
    if(t==='INPUT'||t==='TEXTAREA')return;
    send({type:'key',action:'keyDown',key:e.key,code:e.code,
          text:e.key.length===1?e.key:''});
  });
  document.addEventListener('keyup',function(e){
    var t=e.target.tagName;
    if(t==='INPUT'||t==='TEXTAREA')return;
    send({type:'key',action:'keyUp',key:e.key,code:e.code});
  });

  // --- Scroll ---
  canvas.addEventListener('wheel',function(e){
    e.preventDefault();
    var p=xy(e.clientX,e.clientY);
    send({type:'scroll',x:p.x,y:p.y,deltaX:e.deltaX,deltaY:e.deltaY});
  },{passive:false});

  // --- Done button ---
  document.getElementById('done').addEventListener('click',function(){
    if(ws)ws.close();
    document.body.innerHTML=
      '<div style="display:flex;align-items:center;justify-content:center;'+
      'height:100%;color:rgba(255,255,255,.6);font-family:sans-serif">'+
      'Payment session closed. You can close this tab.</div>';
  });

  connect();
})();
</script>
</body>
</html>"""
