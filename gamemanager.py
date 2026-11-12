"""GameManager (0x0004) para el 1v1 de NBA LIVE 19 ("Online Head2Head" -> "Partido clasificado").

Formatos de la reflexion TDF del eboot (logs/tdf_gamemanager.txt); ids de comando de la tabla de
nombres del eboot; ids de notificacion = tabla del eboot + 3 (contrastado con fifa17-ps3-online:
NotifyGameSetup 0x14, NotifyGameRemoved 0x10).

El cliente pide NTOP=1 (CLIENT_SERVER_DEDICATED: servidor de juego de EA). El netplay del partido
solo funciona asi, de modo que la partida se anuncia como dedicada contra nuestro servidor de juego
(juego.py, hueco de conexion 0); el primer jugador emparejado sigue siendo el anfitrion de plataforma.
NBA_DEDICADO=0 vuelve a la partida entre pares (CLIENT_SERVER_PEER_HOSTED).
"""
import base64
import itertools
import os
import socket
import threading
import time

import tdf

V, S, BL, ST, L, M, U = tdf.VARINT, tdf.STRING, tdf.BLOB, tdf.STRUCT, tdf.LIST, tdf.MAP, tdf.UNION

GAME_MANAGER = 0x0004
USER_SESSIONS = 0x7802

# comandos (tabla del eboot)
CREATE_GAME, DESTROY_GAME, ADVANCE_GAME_STATE = 0x01, 0x02, 0x03
SET_GAME_SETTINGS, SET_GAME_ATTRIBUTES, SET_PLAYER_ATTRIBUTES = 0x04, 0x07, 0x08
JOIN_GAME, REMOVE_PLAYER = 0x09, 0x0B
START_MATCHMAKING, CANCEL_MATCHMAKING, FINALIZE_GAME_CREATION = 0x0D, 0x0E, 0x0F
# Live Run busca con escenarios (StartMatchmakingScenarioRequest: CMGD, PLJD, SCNA, SCNM); 0x11 supuesto.
START_MATCHMAKING_SCENARIO, CANCEL_MATCHMAKING_SCENARIO = 0x10, 0x11
MESH_CONNECTED, MESH_DISCONNECTED, MESH_LOST = 0x41, 0x42, 0x43
CHANGE_GAME_TEAM_ID, SWAP_PLAYERS = 0x6D, 0x70      # tabla de comandos 0xd1fffe0 (id - 1)

# notificaciones (tabla del eboot + 3)
N_MATCHMAKING_FAILED = 0x0A
N_MATCHMAKING_ASYNC_STATUS = 0x0C
N_GAME_REMOVED = 0x10
N_GAME_SETUP = 0x14
N_PLAYER_JOINING = 0x15
N_PLAYER_JOIN_COMPLETED = 0x1E
N_PLAYER_REMOVED = 0x28
N_PLATFORM_HOST_INITIALIZED = 0x47
N_GAME_ATTRIB_CHANGE = 0x50
N_PLAYER_ATTRIB_CHANGE = 0x5A
N_GAME_STATE_CHANGE = 0x64
N_PLAYER_TEAM_ROLE_SLOT_CHANGE = 0x75       # NotifyGamePlayerTeamRoleSlotChange
N_GAME_TEAM_ID_CHANGE = 0x76

# enums de Blaze
PEER_HOSTED = 0                          # GameNetworkTopology::CLIENT_SERVER_PEER_HOSTED
DEDICATED = 1                            # GameNetworkTopology::CLIENT_SERVER_DEDICATED
INITIALIZING, PRE_GAME, IN_GAME = 1, 130, 131
PLAYER_ACTIVE_CONNECTING, PLAYER_ACTIVE_CONNECTED = 2, 4
SUCCESS_CREATED_GAME, SUCCESS_JOINED_NEW_GAME = 0, 1
SESSION_CANCELED = 4                     # MatchmakingResult (enum del eboot)
MMSC = 3                                 # GameSetupReason: matchmakingSetupContext
PING_SITE = "ea-sjc"
LOCALE = 1702057299                      # 'esES'

# Partido contra un servidor de juego dedicado (el de la comunidad, juego.py): el netplay del partido
# (Online::Manager) solo crea su canal de entradas contra el cliente 'gsrv' de ConnApi, que existe en
# la topologia CLIENT_SERVER_DEDICATED. NBA_DEDICADO=0 vuelve a la partida entre pares.
DEDICADO = os.environ.get("NBA_DEDICADO", "1") != "0"
PUERTO_JUEGO = int(os.environ.get("NBA_PUERTO_JUEGO", "7000"))
# ConnApi conecta con el servidor dedicado usando la IP de DNET pero su propio puerto de juego (1899),
# asi que se anuncia una IP virtual que el emulador traduce (NBA_GSRV=10.99.0.1:1899=<ip real>:7000).
IP_JUEGO_VIRTUAL = os.environ.get("NBA_IP_JUEGO", "10.99.0.1")
PUERTO_JUEGO_CONNAPI = 1899
SERVIDOR_UID = 0x7FFF0001                # BlazeId del servidor dedicado (no es un jugador)
SLOT_INVALIDO = 255                      # el host dedicado no ocupa hueco de jugador
RIVAL_FALSO = os.environ.get("NBA_RIVAL_FALSO") == "1"   # emparejar al momento con un rival inventado

_lock = threading.RLock()
_ids = itertools.count(int(time.time()) % 100000 * 10 + 1)
_cola = []          # sesiones esperando rival: dict(sesion, msid, peticion)
_partidas = {}      # gid -> dict


def _ahora_us():
    return int(time.time() * 1_000_000)


def campo(fields, tag, default=None):
    for t, _ty, v in fields or []:
        if t == tag.strip():
            return v
    return default


# ---------------------------------------------------------------- estructuras
def _direccion(red):
    ep = [("IP  ", V, red.get("ip", 0)), ("MACI", V, red.get("maci", 0)), ("PORT", V, red.get("port", 0))]
    return (2, ("VALU", ST, [("EXIP", ST, ep), ("INIP", ST, ep), ("MACI", V, red.get("maci", 0))]))


def _red_vista(x, j):
    """Direccion del jugador x tal como se le envia al jugador j. Si los dos tienen el mismo id de
    maquina (MACI: dos emuladores en un PC) DirtySock toma al rival por un usuario local de la misma
    consola, da la malla por conectada sin red y el anfitrion nunca contesta al invitado; al rival se
    le da entonces un MACI propio."""
    if x is j or x["red"].get("maci") != j["red"].get("maci"):
        return x["red"]
    maci = x["uid"] & 0xFFFFFFFF
    if maci == j["red"].get("maci"):
        maci ^= 1
    return dict(x["red"], maci=maci)


def _csid(slot):
    """Hueco de conexion (connectionSlotId) del jugador en la posicion slot del roster.

    ConnApi mete a cada jugador en el hueco CSID (ConnApiAddClient rechaza en silencio uno ya
    ocupado) y el juego toma como id de maquina CSID - 1 (Online::Manager::GetMachineId:
    ConnApiStatus('self') - 1). El hueco 0 es el del servidor dedicado, asi que los jugadores van
    en 1, 2...: con el anfitrion en el 0 su id de maquina era -1 y el PauseManager online escribia
    fuera de su tabla y corrompia el procesador de pausa (caida al empezar el partido)."""
    return slot + 1


def _host_info(j):
    return [("CONG", V, j["uid"]), ("CSID", V, _csid(0)), ("HPID", V, j["uid"]), ("HSES", V, j["uid"]),
            ("HSLT", V, 0)]


def _host_servidor():
    """HostInfo del servidor dedicado: hueco de conexion 0 (por eso el juego usa CSID - 1 como id
    de maquina) y ningun hueco de jugador."""
    return [("CONG", V, SERVIDOR_UID), ("CSID", V, 0), ("HPID", V, SERVIDOR_UID), ("HSES", V, SERVIDOR_UID),
            ("HSLT", V, SLOT_INVALIDO)]


def _red_servidor(p):
    """Direccion de red del servidor de juego tal como la ve ConnApi (IP virtual, puerto de juego)."""
    ip = int.from_bytes(socket.inet_aton(IP_JUEGO_VIRTUAL), "big")
    return {"ip": ip, "port": PUERTO_JUEGO_CONNAPI, "maci": 1}


# Ids de equipo (TIDS): el cliente los fija con changeGameTeamId (0x6D) al crear la sala de Live Run (indice 0 -> 0,
# indice 1 -> 1); el servidor los guarda en p["tids"] y avisa con NotifyGameTeamIdChange. (Probado 310/210 del pabellon:
# no cambiaba nada.) Indice 0 = local, 1 = visitante (botones de la sala).
def _ids_equipos(p):
    return list(p.get("tids") or [0, 1])


def _equipo(p, j, slot):
    """Indice de equipo (TIDX) del jugador: el que tenga asignado (cambio de equipo) o, en Live Run, alternando
    local/visitante por orden de entrada (antes TIDX = hueco: el tercero quedaba en un equipo inexistente)."""
    if "equipo" in j:
        return j["equipo"]
    if not p["escenario"]:
        return slot
    # Live Run: equipo inicial fijo desde la primera vez (antes se recalculaba por posicion y cambiaba al irse
    # alguien). Alterna local/visitante; el cambio automatico de equipo al entrar lo pasa al otro, asi que los
    # equipos finales quedan parejos.
    if "equipo_ini" not in j:
        j["equipo_ini"] = slot % 2
    return j["equipo_ini"]


def _jugador(p, j, slot, estado, red=None):
    """ReplicatedGamePlayer."""
    red = red or j["red"]
    return [
        ("BLOB", BL, b""), ("CNTY", V, 0), ("CONG", V, j["uid"]), ("CSID", V, _csid(slot)), ("DSUI", V, 0),
        ("ENCR", S, ""), ("EXBL", BL, b""), ("EXID", V, j["ext"]), ("GID ", V, p["gid"]),
        ("JFPS", V, 1), ("JVMM", V, 1), ("LOC ", V, LOCALE), ("NAME", S, j["nombre"]), ("NASP", S, "ps4"),
        ("PATT", M, (S, S, sorted(j["patt"].items()))), ("PID ", V, j["uid"]),
        ("PNET", U, _direccion(red)), ("RCRE", tdf.TIME, 0), ("ROLE", S, _rol_en_sala(p, j) if p["escenario"] else ""),
        ("SID ", V, slot),
        ("SLOT", V, 0), ("STAT", V, estado), ("TIDX", V, _equipo(p, j, slot)), ("TIME", tdf.TIME, _ahora_us()),
        ("UGID", tdf.OBJID, (0, 0, 0)), ("UID ", V, j["uid"]), ("UUID", S, ""),
    ]


def _datos_partida(p, para=None):
    """ReplicatedGameData (los campos que tienen sentido sin servidor de EA), vista por el jugador para."""
    anfitrion = p["jugadores"][0]
    red_anfitrion = _red_vista(anfitrion, para) if para else anfitrion["red"]
    gm = p["gmcd"]
    if DEDICADO:
        servidor = _red_servidor(p)
        topologia = [("DHST", ST, _host_servidor()), ("DNET", L, (U, [_direccion(servidor)])),
                     ("HNET", L, (U, [_direccion(servidor)])), ("NTOP", V, DEDICATED),
                     ("THST", ST, _host_servidor())]
    else:
        topologia = [("HNET", L, (U, [_direccion(red_anfitrion)])), ("NTOP", V, PEER_HOSTED),
                     ("THST", ST, _host_info(anfitrion))]
    return sorted(topologia + [
        ("ADMN", L, (V, [anfitrion["uid"]])), ("APRS", V, 0),
        ("ATTR", M, (S, S, sorted(p["attr"].items()))), ("CAP ", L, (V, [p["cap"], 0, 0, 0])),
        ("CRIT", M, (S, S, sorted((campo(gm, "CRIT") or (S, S, []))[2]))),
        ("CTIM", tdf.TIME, p["creada"]), ("GGTY", V, 0), ("GID ", V, p["gid"]), ("GMRG", V, 0),
        ("GNAM", S, f"NBA19 {p['escenario'] or '1v1'} {p['gid']}"), ("GPVH", V, 0), ("GSET", V, campo(gm, "GSET", 0) or 0),
        ("GSID", V, p["gid"]), ("GSTA", V, p["estado"]), ("GTYP", S, campo(gm, "GTYP", "") or ""),
        ("MCAP", V, p["cap"]), ("MNCP", V, p["minimo"]),
        ("NRES", V, 0), ("PHST", ST, _host_info(anfitrion)),
        ("PRES", V, campo(gm, "PRES", 1) or 1), ("PSAS", S, PING_SITE), ("QCAP", V, 0),
        ("SEED", V, p["gid"] & 0xFFFFFFFF), ("TIDS", L, (V, _ids_equipos(p))),
        ("UUID", S, f"nba19-{p['gid']}"), ("VOIP", V, campo(gm, "VOIP", 2) or 2),
    ] + _roles_partida(p) + [
        ("VSTR", S, p["version"]),
    ], key=lambda f: tdf.encode_tag(f[0]))


def _rol(j):
    """Rol con el que entra el jugador (PLJD.DFRL; Live Run: ANY_PLAYER_ROLE)."""
    return campo(campo(j["peticion"], "PLJD", []), "DFRL", "") or ""


def _rol_en_sala(p, j):
    """Rol de Blaze del jugador en la sala de Live Run. Los roles "0".."2" son los puestos de la alineacion (la sala
    pinta a cada humano en el puesto de su rol; traza 01:18). Al entrar el jugador no tiene puesto
    (ANY_PLAYER_ROLE); el cambio de equipo automatico (requestSwap -> swapPlayers del admin) le da uno elegido por el
    propio admin. Antes el servidor calculaba el puesto y lo recalculaba con cada entrada: con 3-4 jugadores las
    consolas veian puestos distintos (un jugador en los dos equipos, otro sin puesto -> espectador)."""
    return j.get("rol") or "ANY_PLAYER_ROLE"


def _roles_partida(p):
    """RNFO de las salas por escenario: el rol de los jugadores con capacidad por equipo. (La division
    por cero de Live Run en 0xb91bfc2 era por la config NBA_LIVE_RUN vacia, ver blaze.config_live_run.)"""
    if not p["escenario"]:
        return []
    # + rol vacio: el cambio de equipo de la sala (TeamSwitchingManager -> 0xd23f3a0 -> 0xd23f430) pide el rol ""
    # y sin el en el mapa el SDK del cliente lo rechaza con ROLE_NOT_ALLOWED (traza 01:03, marcas d23f4f0/d23f676)
    por_equipo = max(1, p["cap"] // 2)
    # puestos de la alineacion "0".."n-1" (1 jugador por equipo cada uno) + los genericos (rol pedido al entrar)
    capacidades = {str(k): 1 for k in range(por_equipo)}
    for r in ({_rol(j) for j in p["jugadores"]} | {"ANY_PLAYER_ROLE", ""}):
        capacidades.setdefault(r, por_equipo)
    crit = [(r, [("CRIT", M, (S, S, [])), ("RCAP", V, capacidades[r])])
            for r in sorted(capacidades, key=str.lower)]
    return [("RNFO", ST, [("CRIT", M, (S, ST, crit)), ("RCRT", M, (S, S, []))])]


def _notify_game_setup(p, j):
    """NotifyGameSetup para el jugador j, con su contexto de emparejamiento (MMSC)."""
    resultado = SUCCESS_CREATED_GAME if j is p["jugadores"][0] else SUCCESS_JOINED_NEW_GAME
    mmsc = [("ETTM", tdf.TIME, 0), ("FIT ", V, 100), ("GENT", V, 0), ("MAXF", V, 100), ("MSCD", V, j["msid"] if j.get("escenario") else 0),
            ("MSID", V, j["msid"]), ("RSLT", V, resultado), ("TOUT", tdf.TIME, 0),
            ("TTM ", tdf.TIME, 1_000_000), ("TUGM", V, 2), ("TUMM", V, 2), ("TUSR", V, 2),
            ("USID", V, j["uid"])]
    estado_j = PLAYER_ACTIVE_CONNECTING
    # Como Blaze: el anfitrion recibe la partida solo consigo mismo y despues NotifyPlayerJoining por
    # cada invitado (eso le hace abrir su lado de la conexion P2P); el invitado recibe el roster entero.
    # Con el roster entero el anfitrion nunca contestaba al invitado.
    presentes = p["jugadores"][:1] if j is p["jugadores"][0] else p["jugadores"]
    roster = [_jugador(p, x, i, estado_j, _red_vista(x, j)) for i, x in enumerate(presentes)]
    return (GAME_MANAGER, N_GAME_SETUP, [
        ("GAME", ST, _datos_partida(p, j)), ("LFPJ", V, 0), ("MNAM", S, "OSDK_gameMode"),
        ("PROS", L, (ST, roster)), ("QOSV", V, 0), ("QUEU", L, (ST, [])),
        ("REAS", U, (MMSC, ("MMSC", ST, mmsc))),
    ])


# ---------------------------------------------------------------- utilidades
def _info_jugador(s, msid, peticion):
    cmgd = campo(peticion, "CMGD", [])
    pnet = campo(cmgd, "PNET")
    red = dict(getattr(s, "red", None) or {})
    if pnet and pnet[1] is not None:                     # direccion que da el propio cliente
        inip = campo(pnet[1][2], "INIP", [])
        red = {"ip": campo(inip, "IP", 0) or red.get("ip", 0), "port": campo(inip, "PORT", 0) or 3659,
               "maci": campo(pnet[1][2], "MACI", 0) or 0}
    if getattr(s, "ip_cliente", None):                   # direccion desde la que nos ve el servidor (VPN)
        red["ip"] = s.ip_cliente
    return {"sesion": s, "uid": s.uid, "nombre": s.nombre or "jugador", "ext": getattr(s, "ext_id", 0) or 0,
            "msid": msid, "red": red, "patt": {}, "peticion": peticion,
            "escenario": campo(peticion, "SCNM", "") or "", "scna": _atributos_escenario(peticion)}


def _atributos_escenario(peticion):
    """scenarioAttributes (mapa cadena -> generico) como cadenas: Live Run manda PLAYER_CAPACITY,
    MIN_PLAYER_COUNT, TeamType, GameSessionState, OSDK_gameMode..."""
    scna = campo(peticion, "SCNA")
    return {k: str(v[1]) for k, v in (scna[2] if scna else []) if v and v[1] is not None}


class _SesionFalsa:
    """Sesion de un rival ficticio: no tiene conexion, lo que se le manda se descarta."""
    nombre = "rival_ficticio"
    uid = 0x7FFF0002

    def __init__(self, real):
        self.ip = real.ip
        self.log = real.log

    def enviar(self, tramas):
        pass

    def notify(self, c, n, f):
        return b""

    def presentacion(self, maci=None):
        return []


def _rival_falso(yo):
    """Jugador inventado para probar en un solo PC (NBA_RIVAL_FALSO=1): el que busca partida queda de
    anfitrion y recibe su NotifyGameSetup al momento, sin esperar a la otra consola."""
    s = _SesionFalsa(yo["sesion"])
    return {"sesion": s, "uid": s.uid, "nombre": s.nombre, "ext": 0, "msid": next(_ids),
            "red": {"ip": 0x7F000002, "port": 3659, "maci": 2}, "patt": {}, "peticion": [],
            "escenario": yo["escenario"], "scna": yo["scna"]}


def _enviar(j, notifs):
    try:
        j["sesion"].enviar([j["sesion"].notify(c, n, f) for c, n, f in notifs])
    except OSError as e:
        j["sesion"].log(f"GameManager: no se pudo avisar a {j['nombre']}: {e}")


def _a_todos(p, notifs, excepto=None):
    for j in p["jugadores"]:
        if j is not excepto:
            _enviar(j, notifs)


def _partida_de(s, gid=None):
    with _lock:
        if gid:                       # una peticion sobre una partida ya cerrada no toca la actual
            return _partidas.get(gid)
        for p in _partidas.values():
            if any(j["sesion"] is s for j in p["jugadores"]):
                return p
    return None


# ---------------------------------------------------------------- comandos
def manejar(s, cmd, f):
    """Devuelve campos de respuesta (lista), o (campos, notificaciones_para_mi)."""
    if cmd in (START_MATCHMAKING, START_MATCHMAKING_SCENARIO):
        while (vieja := _partida_de(s)) is not None:     # partida anterior que el cliente no cerro
            _terminar(vieja, next((j for j in vieja["jugadores"] if j["sesion"] is s), None), cmd)
        msid = next(_ids)
        yo = _info_jugador(s, msid, f)
        if yo["escenario"]:
            s.log(f"GameManager: escenario {yo['escenario']} {yo['scna']}")
            sala = _sala_abierta(yo)
            if sala is not None:                    # sala de Live Run con sitio: entra en ella
                threading.Timer(0.5, _entrar_en_sala, args=(sala, yo)).start()
                return _respuesta_mm(msid, escenario=True)
        with _lock:
            _cola[:] = [x for x in _cola if x["sesion"] is not s]
            rival = next((x for x in _cola if x["escenario"] == yo["escenario"]), None)
            if rival is not None:
                _cola.remove(rival)
            else:
                _cola.append(yo)
        s.log(f"GameManager: {yo['nombre']} busca partida (MSID {msid}); "
              f"{'emparejado con ' + rival['nombre'] if rival else 'esperando rival'}")
        if rival is None and yo["escenario"] and _minimo(yo) <= 1:
            with _lock:                             # sala de 1 jugador como minimo: se crea ya
                _cola[:] = [x for x in _cola if x is not yo]
            s.log(f"GameManager: sala {yo['escenario']} creada con {yo['nombre']} solo (minimo 1)")
            threading.Timer(0.5, _crear_partida, args=(yo,)).start()
            return _respuesta_mm(msid, escenario=True)
        if rival is None and RIVAL_FALSO:
            with _lock:
                _cola[:] = [x for x in _cola if x is not yo]
            rival, yo = yo, _rival_falso(yo)
            s.log("GameManager: emparejado con un rival ficticio (NBA_RIVAL_FALSO)")
        if rival is not None:
            threading.Timer(0.5, _crear_partida, args=(rival, yo)).start()
        return _respuesta_mm(msid, escenario=bool(yo["escenario"]))

    if cmd in (CANCEL_MATCHMAKING, CANCEL_MATCHMAKING_SCENARIO):
        with _lock:
            antes = len(_cola)
            _cola[:] = [x for x in _cola if x["sesion"] is not s]
        s.log(f"GameManager: emparejamiento cancelado ({antes - len(_cola)} en cola)")
        p = _partida_de(s)                   # partida que el cliente no llego a aceptar
        if p is not None:
            _terminar(p, next((j for j in p["jugadores"] if j["sesion"] is s), None), cmd)
        # NotifyMatchmakingFailed (TDF NotifyMatchmakingFinished): sin ella el cliente sigue esperando
        msid_c = campo(f, "MSID", 0)
        fin = [("DURA", tdf.TIME, 0), ("MAXF", V, 100),
               ("MSCD", V, msid_c if cmd == CANCEL_MATCHMAKING_SCENARIO else 0), ("MSID", V, msid_c),
               ("RSLT", V, SESSION_CANCELED), ("USID", V, s.uid)]
        return [], [(GAME_MANAGER, N_MATCHMAKING_FAILED, fin)]

    p = _partida_de(s, campo(f, "GID"))
    if p is None:
        return []
    yo = next((j for j in p["jugadores"] if j["sesion"] is s), None)

    if cmd == FINALIZE_GAME_CREATION:                    # el anfitrion ya esta listo
        if yo is p["jugadores"][0]:
            _anfitrion_listo(p)
        return []
    if cmd == MESH_CONNECTED:                            # conexion P2P establecida con otro jugador
        tcg = campo(f, "TCG") or (0, 0, 0)
        if DEDICADO and tcg[2] == SERVIDOR_UID and yo is not None:
            # dedicado: cada consola solo conecta con el servidor de juego; el invitado queda unido al
            # conectar (y el rival ficticio, que no tiene red, en cuanto conecta el jugador real)
            if not yo.get("en_servidor"):
                yo["en_servidor"] = True
                s.log(f"GameManager: {yo['nombre']} conectado al servidor de juego")
            _completar_uniones(p)
            return []
        if DEDICADO:
            # dedicado: la malla entre consolas (P2P/voz) no cuenta para nada. Si se tratara como en P2P
            # marcaria al invitado como unido y le mandaria su PlayerJoinCompleted antes de conectar con
            # el servidor de juego; con ese orden su canal 'ctrl' hacia el servidor no se crea y la previa
            # (equipos, estados) no se sincroniza.
            s.log(f"GameManager: malla {yo['nombre'] if yo else '?'} -> {tcg[2]:#x} (ignorada en dedicado)")
            return []
        otro = next((j for j in p["jugadores"] if j["uid"] == tcg[2]), None)
        if yo is None or otro is None or otro is yo:     # el cliente tambien anuncia su propio extremo
            return []
        invitado = yo if yo is not p["jugadores"][0] else otro
        s.log(f"GameManager: malla {yo['nombre']} <-> {otro['nombre']} conectada")
        if not invitado.get("unido"):
            invitado["unido"] = True
            _a_todos(p, [(GAME_MANAGER, N_PLAYER_JOIN_COMPLETED,
                          [("GID ", V, p["gid"]), ("PID ", V, invitado["uid"]),
                           ("TIME", tdf.TIME, _ahora_us())])])
        return []
    if cmd == ADVANCE_GAME_STATE:
        p["estado"] = campo(f, "GSTA", p["estado"])
        _a_todos(p, [(GAME_MANAGER, N_GAME_STATE_CHANGE, [("GID ", V, p["gid"]), ("GSTA", V, p["estado"])])])
        s.log(f"GameManager: partida {p['gid']}: estado {p['estado']} (NotifyGameStateChange a todos)")
        return []
    if cmd == SET_GAME_ATTRIBUTES:
        nuevos = dict((campo(f, "ATTR") or (S, S, []))[2])
        p["attr"].update(nuevos)
        _a_todos(p, [(GAME_MANAGER, N_GAME_ATTRIB_CHANGE,
                      [("ATTR", M, (S, S, sorted(nuevos.items()))), ("GID ", V, p["gid"])])])
        return []
    if cmd == SET_PLAYER_ATTRIBUTES:
        nuevos = dict((campo(f, "ATTR") or (S, S, []))[2])
        pid = campo(f, "PID", yo["uid"] if yo else 0)
        for j in p["jugadores"]:
            if j["uid"] == pid:
                j["patt"].update(nuevos)
        _a_todos(p, [(GAME_MANAGER, N_PLAYER_ATTRIB_CHANGE,
                      [("ATTR", M, (S, S, sorted(nuevos.items()))), ("GID ", V, p["gid"]), ("PID ", V, pid)])])
        return []
    if cmd == CHANGE_GAME_TEAM_ID:
        tidx, nuevo = campo(f, "TIDX", 0) or 0, campo(f, "NTID", 0) or 0
        tids = _ids_equipos(p)
        while len(tids) <= tidx:
            tids.append(len(tids))
        viejo, tids[tidx] = tids[tidx], nuevo
        p["tids"] = tids
        s.log(f"GameManager: partida {p['gid']}: equipo {tidx} id {viejo} -> {nuevo}")
        _a_todos(p, [(GAME_MANAGER, N_GAME_TEAM_ID_CHANGE, [("GID ", V, p["gid"]), ("NTID", V, nuevo),
                                                             ("OTID", V, viejo), ("TIDX", V, tidx)])])
        return []
    if cmd == SWAP_PLAYERS:
        # cambio de equipo de la sala de Live Run (TeamSwitchingManager del admin): SwapPlayersRequest
        # {GID, LGAM: [SwapPlayerData {PID, ROLE, SLOT, TIDX}]} -> NotifyGamePlayerTeamRoleSlotChange a todos
        avisos = []
        for d in (campo(f, "LGAM") or (ST, []))[1]:
            pid, tidx = campo(d, "PID", 0), campo(d, "TIDX", 0)
            rol, slot = campo(d, "ROLE", "") or "", campo(d, "SLOT", 0) or 0
            for j in p["jugadores"]:
                if j["uid"] == pid:
                    j["equipo"] = tidx
                    if rol:
                        j["rol"] = rol
                    s.log(f"GameManager: {j['nombre']} pasa al equipo {tidx} (rol {rol!r})")
                    if p.get("cambio_en_curso") is j:
                        p["cambio_en_curso"] = None
                        threading.Timer(0.5, _siguiente_cambio, (p,)).start()
            avisos.append((GAME_MANAGER, N_PLAYER_TEAM_ROLE_SLOT_CHANGE,
                           [("GID ", V, p["gid"]), ("PID ", V, pid), ("ROLE", S, rol or "ANY_PLAYER_ROLE"),
                            ("SLOT", V, slot), ("TIDX", V, tidx)]))
        _a_todos(p, avisos)
        return []
    if cmd == MESH_LOST and DEDICADO:
        tcg = campo(f, "TCG") or (0, 0, 0)
        if tcg[2] != SERVIDOR_UID:
            # dedicado: el juego solo depende de la conexion con el servidor de juego; la consola avisa
            # igualmente de que no llega al otro jugador (malla P2P/voz que no existe) -> no es un error
            s.log(f"GameManager: {yo['nombre'] if yo else '?'} sin conexion directa con {tcg[2]:#x} (ignorado)")
            return []
    if cmd in (REMOVE_PLAYER, DESTROY_GAME, MESH_LOST):
        _terminar(p, yo, cmd)
        return []
    return []


def _reglas_genericas(campos):
    """{atributo: valor} de las reglas genericas (GARC) de una peticion de emparejamiento, buscadas
    a cualquier profundidad; se omiten las que el jugador deja en 'abstain'."""
    reglas = {}
    for tag, t, v in campos:
        if t == ST:
            reglas.update(_reglas_genericas(v))
        elif t == M and tag == "GARC":
            for nombre, regla in v[2]:
                valores = (campo(regla, "VALU") or (S, []))[1]
                if valores and valores[0] != "abstain":
                    reglas[nombre] = valores[0]
        elif t == M and v[1] == ST:
            for _k, x in v[2]:
                reglas.update(_reglas_genericas(x))
        elif t == L and v[0] == ST:
            for x in v[1]:
                reglas.update(_reglas_genericas(x))
    return reglas


def _respuesta_mm(msid, escenario=False):
    """StartMatchmakingResponse / StartMatchmakingScenarioResponse."""
    if escenario:
        return [("COID", S, ""), ("ESNM", S, ""), ("ETTM", tdf.TIME, 0), ("MSID", V, msid), ("SCID", S, ""),
                ("STMN", S, ""), ("VARI", S, "")]
    return [("COID", S, ""), ("ESNM", S, ""), ("MSID", V, msid), ("SCID", S, ""), ("STMN", S, "")]


def _entero(texto, defecto):
    try:
        return int(texto)
    except (TypeError, ValueError):
        return defecto


def _minimo(j):
    return _entero(j["scna"].get("MIN_PLAYER_COUNT"), 2)


def _sala_abierta(yo):
    """Partida del mismo escenario con sitio libre (Live Run: salas de hasta PLAYER_CAPACITY)."""
    with _lock:
        for p in _partidas.values():
            # solo salas que no empezaron: un jugador metido en un partido en curso quedaba de espectador y
            # al salir se colgaba el juego (log de los betatesters, v2.2: entrada con estado 131 = IN_GAME)
            if (p["escenario"] == yo["escenario"] and len(p["jugadores"]) < p["cap"]
                    and p["estado"] != IN_GAME and not p.get("cerrada")
                    and all(j["sesion"] is not yo["sesion"] for j in p["jugadores"])):
                return p
    return None


def _entrar_en_sala(p, yo):
    """Un jugador mas en una sala ya creada: su NotifyGameSetup y NotifyPlayerJoining a los demas."""
    with _lock:
        if p["gid"] not in _partidas:
            return
        p["jugadores"].append(yo)
    yo["sesion"].log(f"GameManager: {yo['nombre']} entra en la sala {p['gid']} ({len(p['jugadores'])}/{p['cap']})")
    if p.get("anfitrion_listo"):
        _enviar_setup(p, yo)
        _avisar_entrada(p, yo)
        _completar_uniones(p)


def _crear_partida(anfitrion, *invitados):
    gid = next(_ids)
    escenario = anfitrion["escenario"]
    p = {"gid": gid, "jugadores": [anfitrion, *invitados], "estado": INITIALIZING, "creada": _ahora_us(),
         "escenario": escenario,
         "cap": _entero(anfitrion["scna"].get("PLAYER_CAPACITY"), 2) if escenario else 2,
         "minimo": _minimo(anfitrion) if escenario else 2,
         "gmcd": campo(anfitrion["peticion"], "GMCD", []), "attr": {},
         "version": campo(campo(anfitrion["peticion"], "CMGD", []), "GVER", "") or ""}
    p["attr"] = dict((campo(p["gmcd"], "ATTR") or (S, S, []))[2])
    p["attr"].update(anfitrion["scna"])          # escenario: sus atributos pasan a la partida
    # Blaze copia a los atributos de la partida el valor de cada regla generica (GARC) del
    # emparejamiento; el juego lee de ahi ST_QTR_LEN (minutos por cuarto) y otros ajustes. Sin
    # ST_QTR_LEN los cuartos duran 0 y terminan nada mas sacar.
    for nombre, valor in _reglas_genericas(anfitrion["peticion"]).items():
        p["attr"].setdefault(nombre, valor)
    with _lock:
        _partidas[gid] = p
    anfitrion["sesion"].log(f"GameManager: partida {gid} creada: anfitrion {anfitrion['nombre']} "
                            f"({anfitrion['red']}), invitados {[(j['nombre'], j['red']) for j in invitados]}; "
                            f"atributos {p['attr']}")
    variantes = [v for v in os.environ.get("NBA_GM_VARIANTES", "").split(";") if v]
    if variantes:
        for j in p["jugadores"]:
            _enviar_setup(p, j, solo_presentacion=True)
        threading.Thread(target=_probar_variantes, args=(p, variantes), daemon=True).start()
        return
    # Orden de Blaze: partida al anfitrion -> finalizeGameCreation del anfitrion -> PlatformHostInitialized
    # y PRE_GAME -> entra el invitado (su NotifyGameSetup + NotifyPlayerJoining al anfitrion). Sin el
    # PlatformHostInitialized el anfitrion no deja su red lista y no abre la conexion con el invitado.
    _enviar_setup(p, anfitrion)
    threading.Timer(5.0, _anfitrion_listo, args=(p,)).start()     # por si el finalize no llega


def _enviar_setup(p, j, solo_presentacion=False):
    rivales = [t for x in p["jugadores"] if x is not j
               for t in x["sesion"].presentacion(_red_vista(x, j).get("maci"))]
    try:
        j["sesion"].enviar(rivales + ([] if solo_presentacion else [j["sesion"].notify(*_notify_game_setup(p, j))]))
    except OSError as e:
        j["sesion"].log(f"GameManager: no se pudo avisar a {j['nombre']}: {e}")


def _anfitrion_listo(p):
    """El anfitrion termino de crear la partida: red lista, PRE_GAME y entrada de los invitados."""
    with _lock:
        if p.get("anfitrion_listo") or p["gid"] not in _partidas:
            return
        p["anfitrion_listo"] = True
    anfitrion = p["jugadores"][0]
    p["estado"] = PRE_GAME
    _enviar(anfitrion, [
        (GAME_MANAGER, N_PLATFORM_HOST_INITIALIZED, [("GID ", V, p["gid"]), ("PHID", V, anfitrion["uid"]),
                                                     ("PHST", V, 0)]),
        (GAME_MANAGER, N_GAME_STATE_CHANGE, [("GID ", V, p["gid"]), ("GSTA", V, PRE_GAME)]),
    ])
    anfitrion["sesion"].log(f"GameManager: partida {p['gid']}: anfitrion listo (PlatformHostInitialized, PRE_GAME)")
    for invitado in p["jugadores"][1:]:
        _enviar_setup(p, invitado)
        _avisar_entrada(p, invitado)
    _completar_uniones(p)


def _completar_uniones(p):
    """Dedicado: PlayerJoinCompleted de cada jugador ya conectado al servidor de juego (el rival ficticio,
    sin red, cuenta como conectado cuando lo estan los reales). Solo tras NotifyPlayerJoining.
    Tambien el del propio anfitrion: GameSessionConcrete::onPlayerJoinComplete (0xd3e1230) marca el
    rebroadcaster (servidor) como listo (+0x174) solo al completarse la union del jugador LOCAL en una
    partida dedicada; sin eso doGamePlayerJoined no crea el canal 'ctrl' (addStreamedPeerConnection) y los
    mensajes de la previa (CPreGame::SendToPeers) no salen. En orden: primero el local, luego el rival."""
    if not DEDICADO or not p.get("anfitrion_listo"):
        return
    reales = [j for j in p["jugadores"] if not isinstance(j["sesion"], _SesionFalsa)]
    for j in p["jugadores"]:
        falso = isinstance(j["sesion"], _SesionFalsa)
        if not j.get("unido") and (j.get("en_servidor")
                                   or (falso and all(r.get("en_servidor") for r in reales))):
            j["unido"] = True
            p["jugadores"][0]["sesion"].log(f"GameManager: {j['nombre']} unido (PlayerJoinCompleted)")
    unidos = [j for j in p["jugadores"] if j.get("unido")]
    for r in reales:
        if not r.get("unido"):
            continue                         # aun no conecto: su propia union tiene que llegarle primero
        avisados = r.setdefault("uniones_avisadas", set())
        orden = [r] + [j for j in unidos if j is not r]
        notifs = [(GAME_MANAGER, N_PLAYER_JOIN_COMPLETED,
                   [("GID ", V, p["gid"]), ("PID ", V, j["uid"]), ("TIME", tdf.TIME, _ahora_us())])
                  for j in orden if j["uid"] not in avisados]
        nuevos = [j for j in orden if j["uid"] not in avisados]
        avisados.update(j["uid"] for j in orden)
        if notifs:
            _enviar(r, notifs)
        if p["escenario"] and nuevos:
            _avisar_puestos(p, r, nuevos)


def _aviso_puesto(p, j):
    k = p["jugadores"].index(j)
    return (GAME_MANAGER, N_PLAYER_TEAM_ROLE_SLOT_CHANGE,
            [("GID ", V, p["gid"]), ("PID ", V, j["uid"]), ("ROLE", S, _rol_en_sala(p, j)), ("SLOT", V, 0),
             ("TIDX", V, _equipo(p, j, k))])


def _avisar_puestos(p, r, jugadores):
    """Sala de Live Run: el jugador creado de cada humano solo aparece tras un cambio de equipo hecho por el flujo
    del admin (TeamSwitchingManager: requestSwap -> switchStart -> swapPlayers -> syncQueue -> switchEnd). El
    servidor manda un requestSwap en nombre de cada jugador (lo mismo que «Unirse al equipo ...»), de uno en uno.
    Se manda cuando la consola del jugador ya esta en la sala (su mensaje timerGo, tras OtpSession
    JoinSessionCompleted): mandado antes, el admin lo ignoraba (log v2.2, 12:27). Si no llega timerGo, a los 8 s."""
    for j in jugadores:
        if not j.get("cambio_programado"):
            j["cambio_programado"] = True
            t = threading.Timer(8.0, _encolar_cambio, (p, j))
            t.daemon = True
            t.start()


def _encolar_cambio(p, j):
    with _lock:
        if j.get("cambio_inicial") or "equipo" in j or j not in p["jugadores"]:
            return
        j["cambio_inicial"] = True
        j.setdefault("intentos_cambio", 0)
        p.setdefault("cambios_pend", []).append(j)
        libre = not p.get("cambio_en_curso")
    if libre:
        t = threading.Timer(0.5, _siguiente_cambio, (p,))
        t.daemon = True
        t.start()


def _siguiente_cambio(p):
    with _lock:
        if p.get("cambio_en_curso") or p["gid"] not in _partidas:
            return
        cola = p.setdefault("cambios_pend", [])
        while cola and (cola[0] not in p["jugadores"] or "equipo" in cola[0]):
            cola.pop(0)                              # se fue, o ya cambio de equipo por su cuenta
        if not cola:
            return
        j = p["cambio_en_curso"] = cola.pop(0)
    datos = b"\x01\x08blazeId\x00\x04" + str(j["uid"]).encode() + b"\x00\x09"
    attr = [(7170404, "requestSwap"), (7239021, "1"), (1684108385, base64.b64encode(datos).decode()),
            (1718775661, str(j["uid"])), (1936292453, str(len(datos)))]
    p["jugadores"][0]["sesion"].log(f"GameManager: requestSwap automatico de {j['nombre']} "
                                    f"(para que la sala muestre su jugador)")
    _a_todos(p, [_aviso_mensaje(j, attr, OBJ_GAME, [p["gid"]])])

    def vencido():                                   # sin swapPlayers en 4 s: reintento (2 veces) y el siguiente
        with _lock:
            if p.get("cambio_en_curso") is not j:
                return
            p["cambio_en_curso"] = None
            j["intentos_cambio"] = j.get("intentos_cambio", 0) + 1
            if j["intentos_cambio"] <= 2 and "equipo" not in j and j in p["jugadores"]:
                p.setdefault("cambios_pend", []).append(j)
        _siguiente_cambio(p)
    t = threading.Timer(4.0, vencido)
    t.daemon = True
    t.start()


def _avisar_entrada(p, invitado):
    """NotifyPlayerJoining al anfitrion (y demas jugadores) por la entrada del invitado."""
    for j in p["jugadores"]:
        if j is invitado:
            continue
        slot = p["jugadores"].index(invitado)
        pdat = _jugador(p, invitado, slot, PLAYER_ACTIVE_CONNECTING, _red_vista(invitado, j))
        _enviar(j, [(GAME_MANAGER, N_PLAYER_JOINING, [("GID ", V, p["gid"]), ("PDAT", ST, pdat),
                                                      ("QOST", V, 0)])])
        j["sesion"].log(f"GameManager: NotifyPlayerJoining({invitado['nombre']}) -> {j['nombre']}")


def _variante(campos, spec):
    """Diagnostico: 'solo:A,B' deja esos campos, 'sin:A,B' los quita, 'game_sin:X' / 'pros_sin:X' quitan
    campos de GAME o de cada jugador del roster."""
    if spec.startswith("tv+"):                       # TimeValue enviado como entero (tipo 0)
        return _variante(_tiempo_como_varint(campos), spec[3:])
    if spec.startswith("st+"):                       # sin ningun campo TimeValue
        return _variante(_sin_tiempo(campos), spec[3:])
    modo, _, lista = spec.partition(":")
    tags = {t.strip() for t in lista.split(",") if t.strip()}
    if modo == "solo":
        return [c for c in campos if c[0].strip() in tags]
    if modo == "sin":
        return [c for c in campos if c[0].strip() not in tags]
    out = []
    for tag, ty, v in campos:
        if modo == "game_sin" and tag == "GAME":
            v = [c for c in v if c[0].strip() not in tags]
        if modo == "pros_sin" and tag == "PROS":
            v = (v[0], [[c for c in x if c[0].strip() not in tags] for x in v[1]])
        out.append((tag, ty, v))
    return out


def _recorrer(campos, f):
    """Aplica f(tag, tipo, valor) -> campo o None a todos los campos, tambien dentro de structs/listas/uniones."""
    out = []
    for tag, ty, v in campos:
        if ty == ST:
            v = _recorrer(v, f)
        elif ty == L and v[0] == ST:
            v = (ST, [_recorrer(x, f) for x in v[1]])
        elif ty == U and v[1] is not None and v[1][1] == ST:
            v = (v[0], (v[1][0], ST, _recorrer(v[1][2], f)))
        c = f(tag, ty, v)
        if c is not None:
            out.append(c)
    return out


def _tiempo_como_varint(campos):
    return _recorrer(campos, lambda t, ty, v: (t, V if ty == tdf.TIME else ty, v))


def _sin_tiempo(campos):
    return _recorrer(campos, lambda t, ty, v: None if ty == tdf.TIME else (t, ty, v))


def _probar_variantes(p, variantes, pausa=6.0):
    for spec in variantes:
        for j in p["jugadores"]:
            comp, cmd, campos = _notify_game_setup(p, j)
            try:
                j["sesion"].enviar([j["sesion"].notify(comp, cmd, _variante(campos, spec))])
            except OSError:
                return
        p["jugadores"][0]["sesion"].log(f"GameManager: variante NotifyGameSetup enviada: {spec}")
        time.sleep(pausa)


def _terminar(p, yo, motivo):
    with _lock:
        _partidas.pop(p["gid"], None)
    p["jugadores"][0]["sesion"].log(f"GameManager: partida {p['gid']} cerrada (motivo {motivo if isinstance(motivo, str) else hex(motivo)}, "
                                    f"por {yo['nombre'] if yo else '?'})")
    uid = yo["uid"] if yo else 0
    _a_todos(p, [(GAME_MANAGER, N_PLAYER_REMOVED, [("CNTX", V, 0), ("GID ", V, p["gid"]), ("LFPJ", V, 0),
                                                   ("PID ", V, uid), ("REAS", V, 0)]),
                 (GAME_MANAGER, N_GAME_REMOVED, [("GID ", V, p["gid"]), ("REAS", V, 0)])], excepto=yo)


def desconectado(s):
    """Llamar al cerrarse la conexion de una sesion."""
    with _lock:
        _cola[:] = [x for x in _cola if x["sesion"] is not s]
    p = _partida_de(s)
    if p:
        _terminar(p, next((j for j in p["jugadores"] if j["sesion"] is s), None), "desconexion")


def puesto_dist(uid):
    """Para el servidor de juego (juego.py): (indice de NetGameDist del jugador con BlazeId uid, numero de
    jugadores de NetGameDist, [uids por indice]). El NetGameDist del cliente cuenta un hueco mas que jugadores
    (dist+0x123c = 3 en un 1 contra 1) y el indice propio es la posicion en la partida (anfitrion 0, invitado
    1). None si el uid no esta en ninguna partida."""
    with _lock:
        for p in _partidas.values():
            uids = [j["uid"] for j in p["jugadores"]]
            if uid in uids:
                # huecos: 1 contra 1 = 3 (2 jugadores + 1). En las salas por escenario (Live Run) el NetGameDist
                # del cliente tiene 11 (10 = maximo de Live Run 5v5, + 1) aunque la sala sea 3v3: con 3 o 7 el
                # decodificador (0xd6d2450) leia longitudes de mas y hacia memcpy con tamano negativo
                # (pruebas 00:29 y 00:36: -11 y -5, que cuadran exactamente con 11 huecos)
                huecos = 11 if p["escenario"] else len(uids) + 1
                return uids.index(uid), max(huecos, len(uids) + 1), uids
    return None


# ---------------------------------------------------------------- Messaging (0x000F)
MESSAGING = 0x000F
N_MESSAGE = 0x01                         # Messaging::NotifyMessage (ServerMessage)
OBJ_GAME = (GAME_MANAGER, 1)             # ObjectType de una partida
OBJ_USER = (USER_SESSIONS, 1)            # ObjectType de un usuario


def _aviso_mensaje(j, attr, ttyp, tids):
    """NotifyMessage (Messaging 0x01) en nombre del jugador j con un ClientMessage armado por el servidor."""
    pyld = [("ATTR", M, (V, S, attr)), ("FLAG", V, 16), ("STAT", V, 0), ("TAG ", V, 0),
            ("TIDS", L, (V, list(tids))), ("TTYP", tdf.OBJTYPE, ttyp), ("TYPE", V, 1633972340)]
    return (MESSAGING, N_MESSAGE, [
        ("FLAG", V, 0), ("MGID", V, next(_ids)), ("PYLD", ST, pyld),
        ("SRCE", tdf.OBJID, (USER_SESSIONS, 1, j["uid"])), ("TIME", V, int(time.time())),
        ("USER", ST, [("EXBB", BL, b""), ("EXID", V, j.get("ext", 0)), ("ID  ", V, j["uid"]),
                      ("NAME", S, j["nombre"]), ("NASP", S, "ps4")]),
    ])


def mensaje(s, f):
    """Messaging::sendMessage (ClientMessage). La sala de Live Run se coordina con mensajes a la partida
    (sm_LiveRun_Init, timerGo, requestSwap...): sin entregarlos el temporizador no arranca. Se reenvian
    como NotifyMessage a todos los jugadores de la partida (remitente incluido) o a los usuarios de TIDS."""
    ttyp = tuple(campo(f, "TTYP", (0, 0)) or (0, 0))
    tids = list((campo(f, "TIDS") or (V, []))[1])
    with _lock:
        jugadores = [j for p in _partidas.values() for j in p["jugadores"]]
        if ttyp == OBJ_GAME:
            destinos = [j for gid in tids if gid in _partidas for j in _partidas[gid]["jugadores"]]
        else:
            destinos = [j for j in jugadores if j["uid"] in tids]
        yo = next((j for j in jugadores if j["sesion"] is s), None)
    if not destinos and ttyp == OBJ_USER and s.uid in tids:
        destinos = None                                  # solo a si mismo (fuera de partida)
    mgid = next(_ids)
    nombre = yo["nombre"] if yo else (s.nombre or "")
    ext = yo["ext"] if yo else getattr(s, "ext_id", 0)
    pyld = [(tg, ty, v) for tg, ty, v in f if tg in ("ATTR", "FLAG", "STAT", "TAG", "TIDS", "TTYP", "TYPE")]
    aviso = (MESSAGING, N_MESSAGE, [
        ("FLAG", V, 0), ("MGID", V, mgid), ("PYLD", ST, pyld),
        ("SRCE", tdf.OBJID, (USER_SESSIONS, 1, s.uid)), ("TIME", V, int(time.time())),
        ("USER", ST, [("EXBB", BL, b""), ("EXID", V, ext), ("ID  ", V, s.uid), ("NAME", S, nombre),
                      ("NASP", S, "ps4")]),
    ])
    atr = dict((campo(f, "ATTR") or (V, S, []))[2])
    if atr.get(7170404) == "timerGo" and yo is not None:
        for p in list(_partidas.values()):
            if p["escenario"] and yo in p["jugadores"] and not yo.get("cambio_inicial"):
                t = threading.Timer(0.5, _encolar_cambio, (p, yo))
                t.daemon = True
                t.start()
    s.log(f"Messaging: {nombre} -> {ttyp} {tids}: {atr.get(7170404, '?')} "
          f"({len(destinos) if destinos is not None else 1} destinos)")
    respuesta = [("MGID", V, mgid), ("MIDS", L, (V, [mgid]))]
    if destinos is None:
        return respuesta, [aviso]
    for j in destinos:
        if j["sesion"] is not s:
            _enviar(j, [aviso])
    propio = [aviso] if any(j["sesion"] is s for j in destinos) else []
    return respuesta, propio
