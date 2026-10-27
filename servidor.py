"""Servidor NBA LIVE 19 (shadPS4): redirector + Blaze + QoS.

- Redirector HTTPS (puerto 42230): registra getServerInstance y responde con la
  direccion de nuestro Blaze (secure=0: el juego habla Blaze en claro).
- Blaze (puerto 10041): ver blaze.py.
- Nucleus (puerto 443, accounts.ea.com): OAuth falso, devuelve un codigo cualquiera.
- QoS (puerto 17502): HTTP/TLS por TCP y eco UDP; de momento registra lo que pide el juego.

Uso: python -I servidor.py [ip_publicada]   (por defecto 127.0.0.1)
La IP publicada debe ser la de la red local/VPN: el juego liga sus sockets a la IP
local y no llega a 127.0.0.1.
"""
import datetime
import json
import os
import re
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import blaze
import juego

AQUI = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(AQUI, "logs")
os.makedirs(LOGS, exist_ok=True)

IP = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PUERTO_REDIRECTOR = 42230
PUERTO_BLAZE = 10041
PUERTO_QOS = 17502
PUERTO_NUCLEUS = 443

_lock = threading.Lock()
_ultimo_nombre = [None]   # firstpartyid (nombre PSN) del ultimo getServerInstance


def log(msg):
    linea = f"[{datetime.datetime.now():%H:%M:%S.%f}] {msg}"
    with _lock:
        print(linea, flush=True)
        with open(os.path.join(LOGS, "servidor.log"), "a", encoding="utf-8") as f:
            f.write(linea + "\n")


def guardar(nombre, datos):
    ruta = os.path.join(LOGS, f"{datetime.datetime.now():%H%M%S_%f}_{nombre}")
    with open(ruta, "wb") as f:
        f.write(datos)
    return ruta


def ip_entera(ip):
    a, b, c, d = (int(x) for x in ip.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


class Redirector(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("redirector: " + fmt % args)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(n)
        log(f"redirector: POST {self.path} ({n} bytes)")
        guardar("getServerInstance.xml", cuerpo)
        m = re.search(rb"<firstpartyid[^>]*>\s*<valu>([^<]+)</valu>", cuerpo)
        if m:
            _ultimo_nombre[0] = m.group(1).decode("utf-8", "replace")
            log(f"redirector: jugador PSN {_ultimo_nombre[0]!r}")
        respuesta = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<serverinstanceinfo>
  <address member="0">
    <valu>
      <hostname>{IP}</hostname>
      <ip>{ip_entera(IP)}</ip>
      <port>{PUERTO_BLAZE}</port>
    </valu>
  </address>
  <secure>0</secure>
  <trialservicename></trialservicename>
  <defaultdnsaddress>0</defaultdnsaddress>
</serverinstanceinfo>
""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(respuesta)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(respuesta)
        log(f"redirector: respondido -> Blaze en {IP}:{PUERTO_BLAZE} (secure=0)")

    do_GET = do_POST


CODIGO_NUCLEUS = "NBA19_CODIGO_LOCAL"


class Nucleus(BaseHTTPRequestHandler):
    """accounts.ea.com falso: /connect/auth responde 302 con ?code=... en el redirect_uri.
    El juego (ProtoHttp, sin seguir la redireccion) saca el codigo del Location y lo usa en el login Blaze."""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("nucleus: " + fmt % args)

    def do_GET(self):
        u = urlsplit(self.path)
        cab = {k: (f"<{len(v)} caracteres>" if "ticket" in k.lower() else v) for k, v in self.headers.items()}
        log(f"nucleus: {self.command} {self.path} cabeceras={cab}")
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n:
            log("nucleus: cuerpo " + self.rfile.read(n).decode("utf-8", "replace")[:2000])
        if u.path.endswith("/connect/auth"):
            q = parse_qs(u.query)
            destino = (q.get("redirect_uri") or ["http://127.0.0.1/success"])[0]
            sep = "&" if "?" in destino else "?"
            self.send_response(302)
            self.send_header("Location", f"{destino}{sep}code={CODIGO_NUCLEUS}")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            log(f"nucleus: 302 -> {destino}{sep}code={CODIGO_NUCLEUS}")
            return
        nombre = _ultimo_nombre[0] or "jugador"
        uid = blaze.uid_para(nombre)
        datos = None
        if u.path.endswith("/connect/token"):
            datos = {"access_token": f"NBA19AT{uid}", "token_type": "Bearer", "expires_in": 14400,
                     "refresh_token": f"NBA19RT{uid}", "refresh_token_expires_in": 86400,
                     "id_token": f"NBA19ID{uid}"}
        elif u.path.endswith("/connect/tokeninfo"):
            datos = {"client_id": "NBA19-CLIENT-PS4-CLIENTID", "scope": "basic.identity offline signin",
                     "expires_in": 14400, "pid_id": str(uid), "pid_type": "NUCLEUS", "user_id": str(uid),
                     "persona_id": uid, "is_underage": False, "stopProcess": "OFF",
                     "telemetry_id": str(uid)}
        elif "/identity/pids/me" in u.path:
            datos = {"pid": {"externalRefType": "PS3", "externalRefValue": nombre, "pidId": uid,
                             "email": f"{nombre}@nba19.local", "emailStatus": "VERIFIED", "strength": "WEAK",
                             "dob": "1990-01-01", "age": 36, "country": "US", "language": "es",
                             "locale": "es_ES", "status": "ACTIVE", "reasonCode": "",
                             "tosVersion": "1.0", "parentalEmail": "", "thirdPartyOptin": "false",
                             "globalOptin": "false", "dateCreated": "2019-01-01T00:00Z",
                             "dateModified": "2019-01-01T00:00Z", "lastAuthDate": "2026-01-01T00:00Z",
                             "registrationSource": "eadm", "authenticationSource": "eadm",
                             "showEmail": "false", "discoverableEmail": "false", "anonymousPid": "false",
                             "underagePid": "false", "defaultBillingAddressUri": "",
                             "defaultShippingAddressUri": "", "passwordSignature": ""}}
        if datos is not None:
            cuerpo = json.dumps(datos).encode()
            self.send_response(200)
            log(f"nucleus: 200 {u.path} -> {cuerpo.decode()[:300]}")
        else:
            cuerpo = b"{}"
            self.send_response(404)
            log(f"nucleus: 404 {u.path} (sin implementar)")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(cuerpo)

    do_POST = do_GET


def servir_https(puerto, handler, nombre):
    srv = ThreadingHTTPServer(("0.0.0.0", puerto), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    ctx.load_cert_chain(os.path.join(AQUI, "redirector.crt"), os.path.join(AQUI, "redirector.key"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log(f"{nombre} HTTPS escuchando en :{puerto}")
    srv.serve_forever()


class Web(BaseHTTPRequestHandler):
    """Servicios web de NBA (HDL, rewards, ...): registra todo y responde 200 {}."""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        cuerpo = self.rfile.read(n).decode("utf-8", "replace")[:1500] if n else ""
        log(f"web: {self.command} {self.path} cabeceras={dict(self.headers)} cuerpo={cuerpo!r}")
        resp = b"{}"
        # /hdl = detalles del escaneo facial (HD Likeness); un usuario sin escaneo -> 404
        self.send_response(404 if urlsplit(self.path).path.startswith("/hdl") else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(resp)

    do_POST = do_PUT = do_DELETE = do_GET


SHADNET_WEBAPI = ("srv.shadps4.net", 31315)
PUERTO_WEBAPI = 31315


class WebApiProxy(BaseHTTPRequestHandler):
    """WebAPI de PSN (ShadNet): el juego la usa a traves de sceHttp, asi que hosts.txt la trae aqui.
    /v1/users/me/entitlements no existe en ShadNet (404) y sin ella el juego cierra todo el online;
    se responde aqui con una lista vacia valida. El resto se reenvia tal cual al ShadNet real."""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _responder(self, estado, cuerpo, tipo="application/json"):
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        datos = self.rfile.read(n) if n else None
        if urlsplit(self.path).path.endswith("/entitlements"):
            log(f"webapi: {self.command} {self.path} -> lista vacia (local)")
            self._responder(200, b'{"entitlements":[],"start":0,"size":0,"totalResults":0}')
            return
        import http.client
        try:
            c = http.client.HTTPConnection(*SHADNET_WEBAPI, timeout=20)
            cab = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            cab["Host"] = f"{SHADNET_WEBAPI[0]}:{SHADNET_WEBAPI[1]}"
            c.request(self.command, self.path, body=datos, headers=cab)
            r = c.getresponse()
            cuerpo = r.read()
            log(f"webapi: {self.command} {self.path} -> ShadNet {r.status} ({len(cuerpo)} B)")
            self._responder(r.status, cuerpo, r.getheader("Content-Type", "application/json"))
        except OSError as e:
            log(f"webapi: {self.command} {self.path} -> error reenviando a ShadNet: {e}")
            self._responder(502, b"{}")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_GET


QOS_XML = ('<?xml version="1.0" encoding="UTF-8"?><qos><numprobes>0</numprobes>'
           '<qosport>{port}</qosport><probesize>0</probesize><qosip>{ip}</qosip>'
           '<requestid>1</requestid><reqsecret>0</reqsecret></qos>')


def atender_qos_tcp(conn, addr):
    conn.settimeout(10)
    try:
        datos = conn.recv(4096)
        if datos[:1] == b"\x16":
            log(f"qos tcp {addr}: ClientHello TLS ({len(datos)} bytes), sin soporte aun")
            return
        linea = datos.split(b"\r\n", 1)[0].decode("latin-1")
        log(f"qos tcp {addr}: {linea}")
        cuerpo = QOS_XML.format(port=PUERTO_QOS, ip=ip_entera(IP)).encode()
        cab = (f"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: {len(cuerpo)}\r\n"
               "Connection: close\r\n\r\n").encode()
        conn.sendall(cab + cuerpo)
    except OSError as e:
        log(f"qos tcp {addr}: error {e}")
    finally:
        conn.close()


def servir_qos():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PUERTO_QOS))
    s.listen(8)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind(("0.0.0.0", PUERTO_QOS))

    def udp():
        while True:
            try:
                datos, addr = u.recvfrom(2048)
            except OSError:
                continue
            log(f"qos udp {addr}: {len(datos)} bytes {datos[:48].hex(' ')}")
            u.sendto(datos, addr)

    threading.Thread(target=udp, daemon=True).start()
    log(f"qos escuchando en :{PUERTO_QOS} (tcp+udp)")
    while True:
        conn, addr = s.accept()
        threading.Thread(target=atender_qos_tcp, args=(conn, addr), daemon=True).start()


def servir_registro_tcp(puerto, nombre):
    """Receptor TCP que solo registra lo que llega (telemetria 9988, ticker 8999)."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", puerto))
    s.listen(8)
    log(f"{nombre} (registro) escuchando en :{puerto}")

    def atender(conn, addr):
        conn.settimeout(60)
        total = b""
        try:
            while len(total) < 1 << 20:
                datos = conn.recv(65536)
                if not datos:
                    break
                total += datos
                imprimible = "".join(chr(c) if 32 <= c < 127 else "." for c in datos[:600])
                log(f"{nombre} {addr[1]}: {len(datos)} B: {imprimible}")
        except OSError:
            pass
        finally:
            if total:
                guardar(f"{nombre}.bin", total)
            conn.close()

    while True:
        conn, addr = s.accept()
        threading.Thread(target=atender, args=(conn, addr), daemon=True).start()


def asegurar_certificado():
    """El redirector y nucleus hablan TLS: hace falta un certificado (cualquiera; shadPS4 no lo verifica).
    Si no existe, se genera uno autofirmado con openssl."""
    crt, key = os.path.join(AQUI, "redirector.crt"), os.path.join(AQUI, "redirector.key")
    if os.path.exists(crt) and os.path.exists(key):
        return
    import subprocess
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-subj", "/CN=*.ea.com", "-keyout", key, "-out", crt],
                       check=True, capture_output=True)
        log("self-signed certificate generated (redirector.crt / redirector.key)")
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"redirector.crt/redirector.key are missing and could not be generated with openssl ({e}). "
                 "Use the ones from the released package or install OpenSSL.")


if __name__ == "__main__":
    asegurar_certificado()
    threading.Thread(target=servir_registro_tcp, args=(9988, "telemetria"), daemon=True).start()
    threading.Thread(target=servir_registro_tcp, args=(8999, "ticker"), daemon=True).start()
    threading.Thread(target=blaze.servir, args=(IP, PUERTO_BLAZE, log, lambda: _ultimo_nombre[0]),
                     daemon=True).start()
    threading.Thread(target=servir_qos, daemon=True).start()
    juego.arrancar(log)
    threading.Thread(target=servir_https, args=(PUERTO_NUCLEUS, Nucleus, "nucleus"), daemon=True).start()
    web = ThreadingHTTPServer(("0.0.0.0", blaze.PUERTO_WEB), Web)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    log(f"web HTTP escuchando en :{blaze.PUERTO_WEB}")
    webapi = ThreadingHTTPServer(("0.0.0.0", PUERTO_WEBAPI), WebApiProxy)
    threading.Thread(target=webapi.serve_forever, daemon=True).start()
    log(f"proxy WebAPI (ShadNet) escuchando en :{PUERTO_WEBAPI}")
    servir_https(PUERTO_REDIRECTOR, Redirector, "redirector")
