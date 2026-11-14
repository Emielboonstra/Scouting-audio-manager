"""Mobile mixer for a trusted local network."""

import argparse
import copy
import hmac
import json
import secrets
import socket
import threading

from .config import atomic_json, config_dir
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .ipc import request

PAGE = """<!doctype html>
<html lang="nl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scouting Audio Manager</title>
<style>
:root{color-scheme:light dark;font:17px system-ui;accent-color:#1685ef}
body{max-width:640px;margin:auto;padding:20px;background:Canvas;color:CanvasText}
h1{font-size:1.5rem}section{border:1px solid GrayText;border-radius:14px;padding:16px;margin:12px 0}
header,.controls{display:flex;align-items:center;justify-content:space-between;gap:12px}
input[type=range]{width:100%;min-height:44px}button,input[type=password]{font:inherit;padding:10px}
button{min-height:44px;border:1px solid GrayText;border-radius:8px}
button[aria-pressed=true]{background:#005dc7;color:white}small{color:GrayText}
</style>
<h1>⚜ Scouting Audio Manager</h1>
<form id="login"><label>Toegangscode <input id="code" type="password" required></label>
<button>Verbinden</button></form>
<p id="status" role="status"></p><main id="mixer"></main>
<script>
const form=document.querySelector('#login'), code=document.querySelector('#code'),
      status=document.querySelector('#status'), mixer=document.querySelector('#mixer');
let token='', busy=false;
async function api(change){
  const response=await fetch('/api/mixer',{
    method:change?'POST':'GET',
    headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
    body:change?JSON.stringify(change):undefined
  });
  if(!response.ok) throw Error(response.status===401?'Toegangscode onjuist':'Mixer niet bereikbaar. Probeer opnieuw.');
  return response.json();
}
function render(data){
  status.textContent=data.online?'Audio gereed':'Audio offline';
  mixer.replaceChildren();
  for(const zone of data.controls){
    const card=document.createElement('section'), head=document.createElement('header'),
      title=document.createElement('strong'), level=document.createElement('output'),
      range=document.createElement('input'), mute=document.createElement('button');
    title.textContent=zone.name;
    level.textContent=zone.volume+'%';
    range.type='range'; range.min=0; range.max=100; range.value=zone.volume;
    range.setAttribute('aria-label',zone.name+' volume');
    range.oninput=()=>level.textContent=range.value+'%';
    range.onchange=()=>update({zone:zone.id,volume:Number(range.value)});
    mute.textContent=zone.mute?'Demping opheffen':'Dempen';
    mute.setAttribute('aria-pressed',String(zone.mute));
    mute.onclick=()=>update({zone:zone.id,mute:!zone.mute});
    head.append(title,level); card.append(head,range,mute);
    if(zone.status){
      const detail=document.createElement('small');
      detail.textContent=' '+zone.status;
      card.append(detail);
    }
    mixer.append(card);
  }
}
async function update(change){
  if(busy)return;
  busy=true;
  try{render(await api(change));form.hidden=true;}
  catch(error){status.textContent=error.message; if(error.message==='Toegangscode onjuist'){form.hidden=false;mixer.replaceChildren();}}
  finally{busy=false;}
}
form.onsubmit=event=>{event.preventDefault();token=code.value;update();};
setInterval(()=>{if(form.hidden&&!mixer.contains(document.activeElement))update();},1500);
</script></html>"""


def mixer_state(state):
    cfg = state["config"]
    controls = [{"id": None, "name": "Mastervolume", "volume": cfg["master"], "mute": cfg["mute"]}]
    controls.extend({"id": key, "name": zone["name"], "volume": zone["volume"], "mute": zone["mute"],
                     "status": state["zones"].get(key, "")} for key, zone in cfg["zones"].items())
    return {"online": state["online"], "controls": controls}


def change_mixer(state, change):
    if not isinstance(change, dict) or set(change) not in ({"zone", "volume"}, {"zone", "mute"}):
        raise ValueError("Ongeldige bediening")
    zone = change["zone"]
    cfg = copy.deepcopy(state["config"])
    if zone is not None and (not isinstance(zone, str) or zone not in cfg["zones"]):
        raise ValueError("Onbekende zone")
    target = cfg if zone is None else cfg["zones"][zone]
    if "volume" in change:
        value = change["volume"]
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError("Ongeldig volume")
        target["master" if zone is None else "volume"] = value
    else:
        if type(change["mute"]) is not bool:
            raise ValueError("Ongeldige dempstand")
        target["mute"] = change["mute"]
    return request("save", config=cfg, revision=state["revision"], instance=state["instance"])


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def reply(self, status, content, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(content)

    def api(self, write=False):
        path = urlsplit(self.path).path
        if path != "/api/mixer":
            self.send_error(404)
            return
        authorization = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(authorization, self.server.authorization):
            self.send_error(401)
            return
        try:
            change = None
            if write:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 512 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Ongeldig verzoek")
                change = json.loads(self.rfile.read(length))
                if change is None:
                    raise ValueError("Ongeldig verzoek")
            state = request("state")
            if write:
                state = change_mixer(state, change)
            self.reply(200, json.dumps(mixer_state(state)).encode(), "application/json")
        except (ValueError, UnicodeError):
            self.send_error(400)
        except RuntimeError:
            self.send_error(409)
        except OSError:
            self.send_error(503)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.api()

    def do_POST(self):
        self.api(write=True)


def local_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            # UDP connect selects a route without sending a packet.
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
        except OSError:
            return ""


class WebRemote:
    def __init__(self):
        self.path = config_dir() / "web.json"
        self.settings = (json.loads(self.path.read_text()) if self.path.exists()
                         else {"enabled": False, "password": secrets.token_urlsafe(12)})
        self.server = None

    def status(self):
        address = local_address()
        return {**self.settings, "url": f"http://{address}:8765/" if address else "",
                "running": self.server is not None}

    def configure(self, enabled, password):
        if type(enabled) is not bool or not isinstance(password, str) or not (
                8 <= len(password) <= 128 and password.isascii() and all(33 <= ord(c) <= 126 for c in password)):
            raise ValueError("Gebruik 8–128 zichtbare ASCII-tekens zonder spaties voor het wachtwoord.")
        settings = {"enabled": enabled, "password": password}
        new_server = None
        if enabled and self.server is None:
            new_server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
        try:
            atomic_json(self.path, settings)
        except OSError:
            if new_server:
                new_server.server_close()
            raise
        self.settings = settings
        if new_server:
            new_server.authorization = f"Bearer {password}".encode()
            self.server = new_server
            threading.Thread(target=new_server.serve_forever, daemon=True).start()
        elif enabled:
            self.server.authorization = f"Bearer {password}".encode()
        else:
            self.close()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def main():
    parser = argparse.ArgumentParser(description="Mobiele bediening van Scouting Audio Manager")
    parser.add_argument("--bind", default="127.0.0.1", help="IP-adres waarop de webserver luistert")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    code = secrets.token_urlsafe(12)
    with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
        server.authorization = f"Bearer {code}".encode()
        print(f"Open http://{args.bind}:{server.server_port}/ · Toegangscode: {code}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
