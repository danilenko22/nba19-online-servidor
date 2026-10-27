"""Servidor de juego dedicado de NBA LIVE 19 (lo que en EA era el 'game server').

El netplay del partido (Online::Manager en el eboot) crea su canal de entradas (NetGameDist) contra el
cliente 'gsrv' de ConnApi, que solo existe cuando la partida es CLIENT_SERVER_DEDICATED. Este servicio
es ese servidor: hablara CommUDP + NetGameLink + el lado servidor de NetGameDist con las dos consolas
(sin ProtoTunnel, que apaga el parche «Online: sin ProtoTunnel» del NBA19_online.xml).

Fase 3: CommUDP (conexion, ACK de los datos fiables, keepalive). Lo recibido se registra en
logs/juego_udp.txt (mismo formato que el nba_udp.txt de shadPS4: ms, sentido, peer, bytes, hex) y los
paquetes de NetGameLink entregados, decodificados por encima, en el log principal.

CommUDP (recepcion del cliente en 0xd6c6500, maquina de estados en 0xd6c7090):
  cabecera [u32 seq][u32 ack], big-endian. seq: bits 28-31 = subpaquetes extra agrupados, bits 24-27 =
  metatipo (1 = 8 bytes extra de cabecera), bits 0-23 = numero. ack 0x00ffffff = nada recibido aun.
  seq < 0x100 es control: 1 INIT, 2 CONN, 3 DISC, 4 NAK, 5 POKE. Cuerpo de INIT/CONN/POKE:
  [u32 id de cliente][u8 u8 opciones = 01 01]; el ack lleva el ident de conexion, que debe coincidir.
  INIT o POKE -> se contesta CONN (el cliente pasa de 'conectando' (2) a 'conectado' (4)).
  Datos fiables desde 0x100. Agrupados: cada subpaquete extra lleva su longitud al final (1 byte si
  < 0xfb, si no 2 bytes: (b<<8)+prev-0xfa05); desde el final salen del mas antiguo (seq-n) al mas nuevo,
  y lo que queda delante es el de numero seq. Un paquete vacio con seq = siguiente es keepalive/ACK.

NetGameLink: cada paquete entregado por CommUDP termina en un byte de tipo; 0x40 = lleva sincronia
(16 bytes delante del byte de tipo): [u32 hora propia ms][u32 eco de la ultima hora del otro (+ lo que
paso desde que llego)][u16 latencia][enviados][recibidos][perdidos][naks][flags][tamano = 0x10].
Sin sincronia del otro lado la consola corta la conexion a los ~15 s (DISC).

Streams (tipo 0x0b, 0x4b si ademas lleva sincronia): 'str0' + subtipo ('ctrl') + u32 largo + datos.
En partida dedicada el servidor es el «rebroadcaster»: GameSetupConcrete ata el canal 'ctrl' de la previa
(CPreGame::SendToPeers, mensajes "remitente|destinatario:texto", p. ej. "danilenko|rival:hey") a la
conexion con el servidor, y el servidor lo reenvia a las demas consolas. Con NBA_RIVAL_FALSO=1, lo
dirigido a un jugador sin consola conectada se contesta en su nombre (eco "destinatario|remitente:texto").
"""
import os
import socket
import struct
import threading
import time
import rutas

PUERTO = int(os.environ.get("NBA_PUERTO_JUEGO", "7000"))
CAPTURA = os.path.join(rutas.BASE, "logs", "juego_udp.txt")
ID_SERVIDOR = 0x7FFF0001
SIN_ACK = 0x00FFFFFF
KEEPALIVE = 0.25                 # s entre paquetes vacios hacia cada consola conectada
SINCRONIA = 1.0                  # s sin mandar sincronia -> se manda una (normalmente se contesta a la de la consola)
REENVIO = 0.3                    # s sin ACK -> se reenvia el paquete fiable mas antiguo
RIVAL_FALSO = os.environ.get("NBA_RIVAL_FALSO") == "1"
NOMBRE_RIVAL_FALSO = "rival_ficticio"     # = _SesionFalsa.nombre de gamemanager.py
TEAM_RIVAL_FALSO = os.environ.get("NBA_TEAM_RIVAL", "27")
RIVAL_ENTRADA = os.path.join(rutas.BASE, "logs", "rival_entrada.txt")
TIPO_STREAM = 0x0b
# NetGameDist (recepcion del cliente en NetGameDistUpdate 0xd6d1d80, tabla de tipos 0xe4b00f8; envio
# _NetGameDistSendInput 0xd6d2ce0; decodificacion de multi en 0xd6d2440)
TIPO_ENTRADA = 0x02          # entrada de un cliente (cuerpo = datos de entrada, p. ej. 0344)
TIPO_MULTI = 0x03            # servidor -> cliente: entradas de los demas en un fotograma
TIPO_FLUJO = 0x08            # control de flujo: u16 (01 01 = envio/recepcion ok) + byte + u32
TIMEOUT = 15.0                   # s sin recibir nada -> conexion olvidada

INIT, CONN, DISC, NAK, POKE = 1, 2, 3, 4, 5


def _sig(seq):
    seq += 1
    return 0x100 if seq > 0xFFFFFF else seq


def _ant(seq, n=1):
    for _ in range(n):
        seq = 0xFFFFFF if seq == 0x100 else seq - 1
    return seq


def _subpaquetes(seq, extra, cuerpo):
    """[(numero, datos)] en orden del mas antiguo al mas nuevo."""
    salida = []
    num = _ant(seq, extra)
    for _ in range(extra):
        if not cuerpo:
            return salida
        b = cuerpo[-1]
        if b >= 0xFB and len(cuerpo) >= 2:
            largo, usados = (b << 8) + cuerpo[-2] - 0xFA05, 2
        else:
            largo, usados = b, 1
        cuerpo = cuerpo[:-usados]
        salida.append((num, cuerpo[len(cuerpo) - largo:]))
        cuerpo = cuerpo[:len(cuerpo) - largo]
        num = _sig(num)
    salida.append((seq, cuerpo))
    return salida


def _es_marcador(entrada):
    """Marcador de «presente» de DistMultiConnection (0xc2f1310, estado 1): [tipo 3][1 byte 0x44]."""
    return len(entrada) == 2


def describir_ngl(datos):
    """Resumen de un paquete de NetGameLink: el ultimo byte es el tipo (0x40 = lleva sincronia)."""
    if not datos:
        return "vacio"
    tipo = datos[-1]
    texto = "".join(chr(c) if 32 <= c < 127 else "." for c in datos[:-1])
    return f"tipo {tipo:#04x} ({len(datos)} B) {texto[:90]} | {datos[:40].hex()}"


class Conexion:
    def __init__(self, origen, ident, id_cliente):
        self.origen = origen
        self.ident = ident
        self.id_cliente = id_cliente
        self.conectada = False
        self.sig_envio = 0x100       # proximo numero fiable nuestro (aun no mandamos datos)
        self.esperado = 0x100        # proximo numero fiable que esperamos de la consola
        self.ultimo_rx = time.monotonic()
        self.ultimo_tx = 0.0
        self.recibidos = 0
        self.pendientes = {}         # numero -> paquete fiable enviado y aun sin ACK
        self.ultimo_reenvio = 0.0
        self.hora_otro = None        # (hora de la consola, momento en que llego)
        self.ultima_sinc = 0.0
        self.sinc_rx = 0             # sincronias recibidas desde la ultima que mandamos
        self.sinc_tx = 0
        self.nombre = None           # persona de la consola (sale de los mensajes "nombre|...")
        self.entradas = []           # entradas de NetGameDist aun no repartidas
        self.entradas_rx = 0
        self.multis_tx = 0
        self.flujo_visto = False
        self.real = False            # ya manda entradas reales (dejo el marcador de 1 byte)
        self.descartadas = 0         # marcadores propios descartados para alinear fotogramas

    def ack(self):
        return SIN_ACK if self.esperado == 0x100 and self.recibidos == 0 else _ant(self.esperado)


class Servidor:
    def __init__(self, log):
        self.log = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", PUERTO))
        self.conexiones = {}
        self.inicio = time.monotonic()
        self.captura = open(CAPTURA, "a", encoding="ascii")
        self.captura.write(f"# inicio {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.cerrojo = threading.Lock()
        self.vistos = {}
        self.vistos_stream = {}
        self._rival_texto = None

    def _registrar(self, sentido, origen, datos):
        ms = int((time.monotonic() - self.inicio) * 1000)
        self.captura.write(f"{ms} {sentido} {origen[0]}:{origen[1]} {len(datos)} {datos.hex()}\n")
        self.captura.flush()

    def enviar(self, origen, datos):
        try:
            self.sock.sendto(datos, origen)
        except OSError as e:
            self.log(f"juego: error al enviar a {origen}: {e}")
            return
        self._registrar(">", origen, datos)

    def _hora(self):
        return int((time.monotonic() - self.inicio) * 1000) & 0xFFFFFFFF

    def _enviar_fiable(self, c, carga):
        num = c.sig_envio
        c.sig_envio = _sig(num)
        paquete = struct.pack(">II", num, c.ack()) + carga
        c.pendientes[num] = paquete
        self.enviar(c.origen, paquete)
        c.ultimo_tx = time.monotonic()

    def _sincronia(self, c):
        if c.hora_otro is None:
            eco = self._hora()
        else:
            hora, llegada = c.hora_otro
            eco = (hora + int((time.monotonic() - llegada) * 1000)) & 0xFFFFFFFF
        c.sinc_tx += 1
        carga = struct.pack(">IIHBBBBBB", self._hora(), eco, 1, 1, min(c.sinc_rx, 255), 0, 0, 0, 0x10) + b"@"
        c.sinc_rx = 0
        c.ultima_sinc = time.monotonic()
        self._enviar_fiable(c, carga)
        if c.sinc_tx <= 3:
            self.log(f"juego: sincronia #{c.sinc_tx} -> {c.origen[0]}:{c.origen[1]}: {carga.hex()}")

    def _ack_recibido(self, c, ack):
        if ack == SIN_ACK:
            return
        for num in list(c.pendientes):
            # numeros en ventana: los <= ack (sin vuelta del contador en la practica)
            if num <= ack:
                del c.pendientes[num]

    def _keepalive(self, c):
        self.enviar(c.origen, struct.pack(">II", c.sig_envio, c.ack()))
        c.ultimo_tx = time.monotonic()

    def _control(self, origen, tipo, ident, cuerpo):
        texto = f"{origen[0]}:{origen[1]}"
        c = self.conexiones.get(origen)
        if tipo in (INIT, POKE):
            id_cliente = struct.unpack(">I", cuerpo[:4])[0] if len(cuerpo) >= 4 else 0
            if c is None or c.ident != ident:
                c = Conexion(origen, ident, id_cliente)
                self.conexiones[origen] = c
                self.log(f"juego: {texto} pide conexion (tipo {tipo}, ident {ident:08x}, cliente {id_cliente:08x})")
            opciones = cuerpo[4:6] if len(cuerpo) >= 6 else b"\x01\x01"
            self.enviar(origen, struct.pack(">III", CONN, ident, ID_SERVIDOR) + opciones)
            if not c.conectada:
                c.conectada = True
                self.log(f"juego: {texto} conectado (CONN enviado)")
                self._keepalive(c)
        elif tipo == CONN:
            self.log(f"juego: {texto} manda CONN (ident {ident:08x})")
        elif tipo == DISC:
            self.log(f"juego: {texto} se desconecta (ident {ident:08x})")
            self.conexiones.pop(origen, None)
        else:
            self.log(f"juego: {texto} control tipo {tipo} ident {ident:08x} cuerpo {cuerpo.hex()}")

    def _datos(self, c, seq, ack, datos):
        extra = seq >> 28
        meta = (seq >> 24) & 0xF
        num = seq & 0xFFFFFF
        cuerpo = datos[8 + (8 if meta else 0):]
        if not cuerpo:
            return                                  # keepalive/ACK de la consola
        nuevos = 0
        for n, sub in _subpaquetes(num, extra, cuerpo):
            if n != c.esperado:
                continue                            # repetido (ya entregado) o fuera de orden
            c.esperado = _sig(c.esperado)
            c.recibidos += 1
            nuevos += 1
            if sub and sub[-1] & 0x40 and len(sub) >= 17:
                c.hora_otro = (struct.unpack(">I", sub[-17:-13])[0], time.monotonic())
                c.sinc_rx += 1
                if sub[-1] == 0x40 and len(sub) == 17 and c.recibidos > 3:
                    continue                        # sincronia sola: no llenar el log
            if sub and (sub[-1] & 0x3F) == TIPO_STREAM:
                self._stream(c, sub[:-17] if sub[-1] & 0x40 else sub[:-1])
                continue
            if sub and (sub[-1] & 0x3F) in (TIPO_ENTRADA, TIPO_FLUJO):
                self._dist(c, sub[-1] & 0x3F, sub[:-17] if sub[-1] & 0x40 else sub[:-1])
                continue
            if c.recibidos <= 200 or c.recibidos % 1000 == 0:
                self.log(f"juego: {c.origen[0]}:{c.origen[1]} #{n:#x} {describir_ngl(sub)}")
        # respuesta inmediata: sincronia nueva (lleva el ACK) o, si no, ACK vacio
        if nuevos and c.sinc_rx:
            self._sincronia(c)
        else:
            self._keepalive(c)

    def _stream(self, c, cuerpo):
        """Paquete de stream de NetGameLink (sin sincronia ni byte de tipo): se reenvia a las demas consolas."""
        texto = None
        if len(cuerpo) >= 12 and cuerpo[:4] == b"str0":
            largo = struct.unpack(">I", cuerpo[8:12])[0]
            texto = cuerpo[12:12 + largo].decode("latin1")
            if "|" in texto:
                c.nombre = texto.split("|", 1)[0]
        clave = (c.origen, texto if texto is not None else cuerpo.hex())
        visto = self.vistos_stream.get(clave, 0) + 1
        self.vistos_stream[clave] = visto
        otras = [o for o in self.conexiones.values() if o is not c and o.conectada]
        if visto <= 2 or visto in (10, 100, 1000):
            subtipo = cuerpo[4:8].decode("latin1", "replace") if len(cuerpo) >= 8 else "?"
            self.log(f"juego: stream {subtipo} de {c.nombre or c.origen[0]}: {texto!r}"
                     f"{' (x%d)' % visto if visto > 1 else ''} -> {len(otras)} consola(s)")
        for o in otras:
            self._enviar_fiable(o, cuerpo + bytes([TIPO_STREAM]))
        if RIVAL_FALSO and not otras and cuerpo[4:8] == b"data":
            # str0 'data' ('atad' + hash de los datos del partido): el rival ficticio manda el mismo
            self._enviar_fiable(c, cuerpo + bytes([TIPO_STREAM]))
        if RIVAL_FALSO and texto and "|" in texto:
            remitente, resto = texto.split("|", 1)
            if ":" in resto:
                destino, mensaje = resto.split(":", 1)
                eco = None
                if destino and all(o.nombre != destino for o in otras):
                    eco = f"{destino}|{remitente}:{mensaje}"          # hey -> hey, sup -> sup
                elif not destino and mensaje.startswith(f"set:{remitente}:") and not otras:
                    # cada consola publica sus valores (team, lnp, state, rdy): el rival hace lo mismo
                    valor = mensaje.split(":", 2)[2]
                    if valor.startswith("team="):
                        valor = "team=" + TEAM_RIVAL_FALSO
                    eco = f"{NOMBRE_RIVAL_FALSO}|:set:{NOMBRE_RIVAL_FALSO}:{valor}"
                if eco:
                    eco = eco.encode("latin1")
                    paquete = cuerpo[:8] + struct.pack(">I", len(eco)) + eco + bytes([TIPO_STREAM])
                    self._enviar_fiable(c, paquete)

    def _dist(self, c, tipo, cuerpo):
        """Lado servidor de NetGameDist (lo que en EA era el NetGameDistServ del servidor dedicado)."""
        if tipo == TIPO_FLUJO:
            if not c.flujo_visto:
                c.flujo_visto = True
                self.log(f"juego: dist {c.nombre or c.origen[0]}: flujo {cuerpo.hex()} -> se contesta igual")
            self._enviar_fiable(c, cuerpo + bytes([TIPO_FLUJO]))
            return
        c.entradas.append(cuerpo)
        c.entradas_rx += 1
        if not _es_marcador(cuerpo):
            c.real = True
        if c.entradas_rx <= 3 or c.entradas_rx in (10, 100, 1000, 10000):
            self.log(f"juego: dist {c.nombre or c.origen[0]}: entrada #{c.entradas_rx} {cuerpo.hex()}")
        self._repartir()

    def _repartir(self):
        """Fotograma completo cuando todas las consolas tienen una entrada pendiente: a cada una se le manda
        un multi con las de las demas. Formato (decodificador 0xd6d2450; n = huecos de NetGameDist):
          [n de entradas propias que consume este fotograma = 1]
          [tipos: un nibble por cada OTRO jugador, bajo/alto alternando; 2/3 = entrada con datos, 1 = nada]
          [longitudes (1 byte) de los otros menos el ultimo, cuyo tamano es lo que queda]
          [entradas de los otros, en orden de indice]"""
        conectadas = [o for o in self.conexiones.values() if o.conectada]
        cons = [o for o in conectadas if o.entradas_rx]
        if not cons or (not RIVAL_FALSO and (len(cons) < 2 or len(cons) < len(conectadas))):
            return                                   # se espera a que todas las consolas manden entradas
        import gamemanager
        while True:
            # Alineacion de fotogramas: el fotograma 0 del partido de cada consola es su primera entrada real
            # (tras dejar el marcador de 1 byte). En cuanto alguna manda entradas reales, los marcadores que
            # queden de las demas se descartan y se le dice a esa consola que consuma tambien esas entradas
            # propias (byte de cabecera del multi = 1 + descartadas). Sin esto los partidos quedaban
            # desfasados (3 fotogramas en la prueba) y el DesyncDetector cortaba la partida.
            if len(cons) > 1 and any(o.real for o in cons):
                for o in cons:
                    while o.entradas and _es_marcador(o.entradas[0]):
                        o.entradas.pop(0)
                        o.descartadas += 1
            if not all(o.entradas for o in cons):
                break
            fotograma = [(o, o.entradas.pop(0)) for o in cons]
            por_uid = {x.id_cliente: e for x, e in fotograma}
            for o, propia in fotograma:
                # el NetGameDist del cliente tiene un hueco mas que jugadores (dist+0x123c = 3 en un 1 contra 1,
                # indice propio en +0x1238): el multi lleva TODOS los indices menos el propio, los que no
                # tienen consola con tipo 1 (nada). Sin ese hueco el cliente lee un byte de cabecera de mas.
                puesto = gamemanager.puesto_dist(o.id_cliente)
                if puesto is None:
                    otras = [e for x, e in fotograma if x is not o] or [self._entrada_rival(propia)]
                else:
                    propio, n, uids = puesto
                    otras = []
                    for k in range(n):
                        if k == propio:
                            continue
                        uid = uids[k] if k < len(uids) else None
                        e = por_uid.get(uid)
                        if e is None and RIVAL_FALSO and uid is not None and uid != o.id_cliente:
                            e = self._entrada_rival(propia)
                        otras.append(e or b"\x01")
                # entrada del cliente (NetGameDistInputLocalMulti 0xd6d3000, buffer +0x1718) = [tipo][datos];
                # tipo 2/3 = con datos (3 = a prueba de perdidas), 1 = sin entrada. En el multi el tipo va como
                # nibble y los datos aparte. Ojo: una entrada de 1 byte es el marcador de «presente» que el
                # juego (DistMultiConnection 0xc2f1160) espera del rival en el primer fotograma.
                tipos = bytearray((len(otras) + 1) // 2)
                for k, e in enumerate(otras):
                    tipos[k // 2] |= ((e[0] & 0x0F) if e else 1) << (4 * (k & 1))
                datos = [e[1:] for e in otras]
                largos = bytes(len(x) for x in datos[:-1])
                consumidas = 1 + o.descartadas
                if o.descartadas:
                    self.log(f"juego: dist {o.nombre or o.origen[0]}: alinea fotogramas, consume {consumidas} "
                             f"entradas propias (marcadores sobrantes)")
                    o.descartadas = 0
                multi = bytes([consumidas]) + bytes(tipos) + largos + b"".join(datos)
                o.multis_tx += 1
                if o.multis_tx <= 3 or o.multis_tx in (10, 100, 1000, 10000):
                    self.log(f"juego: dist -> {o.nombre or o.origen[0]}: multi #{o.multis_tx} {multi.hex()}")
                self._enviar_fiable(o, multi + bytes([TIPO_MULTI]))

    def _entrada_rival(self, propia):
        """Entrada del rival ficticio ([tipo][datos]). Por defecto refleja la de la consola; si existe
        logs/rival_entrada.txt (hex; se relee en caliente) se usa ese contenido ('eco' = reflejar)."""
        try:
            texto = open(RIVAL_ENTRADA, encoding="ascii").read().strip()
        except OSError:
            return propia
        if texto != self._rival_texto:
            self._rival_texto = texto
            self.log(f"juego: entrada del rival ficticio -> {texto!r}")
        try:
            return propia if texto in ("", "eco") else bytes.fromhex(texto)
        except ValueError:
            return propia

    def recibir(self, datos, origen):
        texto = f"{origen[0]}:{origen[1]}"
        self._registrar("<", origen, datos)
        n = self.vistos.get(texto, 0) + 1
        self.vistos[texto] = n
        if n <= 4 or n in (10, 100, 1000, 10000, 100000):
            self.log(f"juego: paquete #{n} de {texto} ({len(datos)} B): {datos[:48].hex()}")
        if len(datos) < 8:
            return
        seq, ack = struct.unpack(">II", datos[:8])
        c = self.conexiones.get(origen)
        if c is not None:
            c.ultimo_rx = time.monotonic()
            self._ack_recibido(c, ack)
        if (seq & 0xFFFFFF) < 0x100:
            self._control(origen, seq & 0xFFFFFF, ack, datos[8:])
        elif c is not None and c.conectada:
            self._datos(c, seq, ack, datos)

    def mantener(self):
        while True:
            time.sleep(0.05)
            ahora = time.monotonic()
            with self.cerrojo:
                for origen, c in list(self.conexiones.items()):
                    if ahora - c.ultimo_rx > TIMEOUT:
                        self.log(f"juego: {origen[0]}:{origen[1]} sin noticias en {TIMEOUT:.0f} s, se olvida")
                        del self.conexiones[origen]
                    elif not c.conectada:
                        continue
                    elif c.pendientes and ahora - c.ultimo_reenvio >= REENVIO and ahora - c.ultimo_tx >= REENVIO:
                        num = min(c.pendientes)
                        paquete = struct.pack(">II", num, c.ack()) + c.pendientes[num][8:]
                        self.enviar(origen, paquete)
                        c.ultimo_reenvio = c.ultimo_tx = ahora
                    elif ahora - c.ultima_sinc >= SINCRONIA:
                        self._sincronia(c)
                    elif ahora - c.ultimo_tx >= KEEPALIVE:
                        self._keepalive(c)

    def servir(self):
        self.log(f"servidor de juego (UDP) escuchando en :{PUERTO}")
        threading.Thread(target=self.mantener, daemon=True).start()
        while True:
            try:
                datos, origen = self.sock.recvfrom(65535)
            except OSError as e:          # Windows: WSAECONNRESET tras un ICMP 'puerto inaccesible'
                self.log(f"juego: error de recepcion {e}")
                continue
            with self.cerrojo:
                self.recibir(datos, origen)


def arrancar(log):
    threading.Thread(target=lambda: Servidor(log).servir(), daemon=True).start()
