"""Servidor Blaze minimo para NBA LIVE 19 (PS4, BlazeSDK 15.1.1.7.0).

Framing (16 bytes, big endian), comprobado con el trafico real del juego:
  u32 tamano de la carga | u16 tamano de metadatos | u16 componente | u16 comando
  u24 numero de mensaje  | u8 tipo (bits 7..5)      | u8 opciones    | u8 reservado
Tipos: 0 peticion, 1 respuesta, 2 notificacion, 3 respuesta de error, 4 ping, 5 respuesta a ping.

Los formatos de las respuestas siguen lo que funciono en FIFA 17 (fifa17-ps3-online) y Mirror's
Edge Catalyst (misma familia BlazeSDK 15.1.x); lo que no conocemos se responde vacio y se registra.
"""
import hashlib
import json
import os
import socket
import threading
import time

import gamemanager
import nba
import tdf

HDR_LEN = 16
MSG_REQUEST, MSG_REPLY, MSG_NOTIFY, MSG_ERROR, MSG_PING, MSG_PING_REPLY = range(6)

AUTH = 0x0001
UTIL = 0x0009
USER_SESSIONS = 0x7802

NOMBRES = {
    (AUTH, 0x000A): "Authentication::login",
    (AUTH, 0x001D): "Authentication::listUserEntitlements2",
    (AUTH, 0x001E): "Authentication::getAccount",
    (AUTH, 0x0020): "Authentication::listEntitlements",
    (AUTH, 0x0046): "Authentication::logout",
    (AUTH, 0x0098): "Authentication::expressLogin?",
    (UTIL, 0x0001): "Util::fetchClientConfig",
    (UTIL, 0x0002): "Util::ping",
    (UTIL, 0x0005): "Util::getTelemetryServer",
    (UTIL, 0x0007): "Util::preAuth",
    (UTIL, 0x0008): "Util::postAuth",
    (UTIL, 0x000A): "Util::userSettingsLoad",
    (UTIL, 0x000B): "Util::userSettingsSave",
    (UTIL, 0x000C): "Util::userSettingsLoadAll",
    (USER_SESSIONS, 0x0014): "UserSessions::updateNetworkInfo",
    (USER_SESSIONS, 0x0008): "UserSessions::updateHardwareFlags",
    (USER_SESSIONS, 0x0032): "UserSessions::lookupUsersByPersonaNames",
    (UTIL, 0x0016): "Util::setClientMetrics",
    (UTIL, 0x0017): "Util::setConnectionState",
    (UTIL, 0x001C): "Util::setClientState",
}

# Ids de componente sacados del eboot (funciones de nombres de error: (codigo << 16) | id)
COMPONENTES = {
    0x0001: "Authentication", 0x0004: "GameManager", 0x0005: "Redirector", 0x0007: "Stats",
    0x0009: "Util", 0x000A: "CensusData", 0x000F: "Messaging", 0x0019: "AssociationLists",
    0x001B: "GpsContentController", 0x001C: "GameReporting", 0x0802: "Mut", 0x0803: "H2HSeasons",
    0x0804: "DataCollection", 0x0806: "Commerce", 0x081C: "Channels", 0x081D: "LiveUpdates",
    0x081E: "EASquared", 0x0820: "Timer", 0x0821: "TimedTrial", 0x0823: "LiveEvents",
    0x0824: "NbaUserStorage", 0x08C9: "OSDKSettings", 0x08CA: "OsdkDynamicMessaging", 0x08DF: "OSDKTournaments", 0x7802: "UserSessions",
}
COMANDOS = {
    0x0806: {1: "Handshake", 2: "GetConfiguration", 3: "GetCurrency", 4: "GetEntitlements",
             5: "ActivateEntitlement", 6: "EnterKeymasterCode", 7: "SynchronizeConsumables",
             8: "ProcessEndOfGame", 9: "GetDDPTimeRemaining"},
    0x081C: {1: "SyncFollow", 2: "GetFollowers", 3: "GetFollowees", 4: "CreateChannel", 5: "DestroyChannel",
             6: "GetChannels", 7: "UpdateChannelData", 8: "CreateFeed", 9: "DestroyFeed", 0xA: "GetFeeds",
             0xB: "AddChannelToFeed", 0xD: "RemoveChannelFromFeed", 0xE: "UpdateBlockedUsers",
             0xF: "AddEvent", 0x10: "DeleteEvent", 0x11: "MarkEventsRead", 0x12: "MarkEventsUnread",
             0x13: "EnableFeed", 0x14: "DisableFeed", 0x16: "SubscribeToPublicChannels",
             0x17: "GetChannelTypes"},
    0x081D: {1: "GetLiveUpdatePaths"},
    0x0823: {1: "GetLiveEvents", 2: "GetSummerCircuitEvents", 3: "GetLiveRunEvents", 4: "SessionBegin",
             5: "SessionEnd", 6: "GetConfiguration"},
    0x0824: {1: "GetOneCourtData", 2: "SetOneCourtData", 3: "GetBattleModeData", 4: "SetBattleModeData",
             5: "GetOpponents", 6: "GetOpponentDetails", 7: "LockCourtForBattle",
             8: "GetCampaignTimeRemaining", 9: "CourtBattleHeartbeat", 0xB: "ClaimPendingRewards",
             0xC: "GetFriendlyOpponents", 0xD: "SetHouseRules", 0xE: "SetLineup", 0xF: "GetCourtStatus",
             0x10: "GetDefenseOpponents", 0x11: "GetControlledCourts", 0x13: "GetRewardTiers",
             0x14: "UnlockCourtForBattle", 0x15: "GetCourtHype", 0x16: "GetBattleModeHudData"},
    0x08C9: {2: "fetchSettingsGroups"},
}

PERSONA_NAMESPACE = "ps4"
PLATAFORMA_PS4 = 4   # Blaze::ClientPlatformType del juego (PTFM en getEmailOptInSettings); CPFT=20 del preAuth es otro enum
QOS_PORT = 17502
PUERTO_WEB = 8088   # servicios web de NBA (HDL, rewards...): de momento solo se registran
AJUSTES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ajustes_usuario.json")


def nombre(component, command):
    if (component, command) in NOMBRES:
        return NOMBRES[(component, command)]
    comp = COMPONENTES.get(component, f"0x{component:04X}")
    cmd = COMANDOS.get(component, {}).get(command, f"0x{command:04X}")
    return f"{comp}::{cmd}"


def parse_header(h):
    return {
        "size": int.from_bytes(h[0:4], "big"),
        "meta": int.from_bytes(h[4:6], "big"),
        "component": int.from_bytes(h[6:8], "big"),
        "command": int.from_bytes(h[8:10], "big"),
        "msg_num": int.from_bytes(h[10:13], "big"),
        "type": h[13] >> 5,
        "options": h[14],
        "reserved": h[15],
        "byte13": h[13],
    }


def frame(component, command, msg_num, msg_type, payload=b"", meta=b""):
    return (len(payload).to_bytes(4, "big") + len(meta).to_bytes(2, "big") + component.to_bytes(2, "big")
            + command.to_bytes(2, "big") + msg_num.to_bytes(3, "big")
            + bytes([(msg_type & 7) << 5, 0, 0]) + meta + payload)


def frame_error(h, codigo):
    """Respuesta de error: el codigo va en los metadatos (Fire2Metadata {ERRC}), tipo 3."""
    meta = tdf.encode([("ERRC", tdf.VARINT, codigo)])
    return frame(h["component"], h["command"], h["msg_num"], MSG_ERROR, b"", meta)


def _ordenar_valor(t, v):
    if t == tdf.STRUCT:
        return ordenar(v)
    if t == tdf.LIST and v[0] == tdf.STRUCT:
        return (v[0], [ordenar(x) for x in v[1]])
    if t == tdf.MAP and v[1] == tdf.STRUCT:
        return (v[0], v[1], [(k, ordenar(x)) for k, x in v[2]])
    if t == tdf.UNION and v[1] is not None:
        tg, ty, iv = v[1]
        return (v[0], (tg, ty, _ordenar_valor(ty, iv)))
    return v


def ordenar(fields):
    """El decodificador TDF del cliente recorre los miembros en orden de tag: hay que mandarlos asi."""
    return sorted(((tg, ty, _ordenar_valor(ty, v)) for tg, ty, v in fields),
                  key=lambda f: tdf.encode_tag(f[0]))


def campo(fields, tag, default=None):
    for t, _ty, v in fields:
        if t == tag.strip():
            return v
    return default


def config_h2h():
    """NBA_H2HSEASONS: claves leidas por el cliente (cadenas junto a "NBA_H2HSEASONS" en el eboot).
    Vacia -> "No se pudo conectar a Cara a cara igualada" (H2LSTCON). Valores supuestos; los
    indices se dan desde 0 y desde 1 porque no se sabe con cual empieza el cliente."""
    ligas, torneos, rondas = 5, 4, 4
    c = {"NUM_LEAGUES": str(ligas), "NUM_TOURN": str(torneos), "MAX_TOURN_ROUNDS": str(rondas),
         "H2HLeagueCount": str(ligas)}
    for i in range(0, ligas + 1):
        c[f"LEAGUE_{i}_R"] = "3"        # puntos para mantenerse / descenso
        c[f"LEAGUE_{i}_P"] = "12"       # puntos para ascender
    for t in range(0, torneos + 1):
        c[f"TOURN_{t}_UT"] = "1"
        c[f"TOURN_{t}_LT"] = str(ligas)
        c[f"TOURN_{t}_ASSET"] = ""
        c[f"TOURN_{t}_NAME"] = f"Torneo {t}"
        c[f"TOURN_{t}_RND"] = str(rondas)
        c[f"TOURN_{t}_CHAMP"] = ""
    return c


def uid_para(name):
    """BlazeId estable por nombre PSN (unico entre jugadores, < 2^31)."""
    h = int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16)
    return 1100000000 + (h % 900000000)


def ip_u32(ip):
    return int.from_bytes(socket.inet_aton(ip), "big")


class Sesion:
    """Estado de una conexion Blaze."""

    def __init__(self, server_ip, log):
        self.ip = server_ip
        self.log = log
        self.nombre = None       # nombre PSN, del redirector o del login
        self.locale = 1702057299
        self.plataforma = 20     # CPFT que manda el juego en preAuth (PS4)
        self.conn = None
        self.envio = threading.Lock()   # GameManager envia notificaciones desde la conexion del rival
        self.ip_cliente = 0             # IP (u32) desde la que nos ve el servidor (la de la VPN/LAN)
        self.ext_id = 0

    def enviar(self, tramas):
        with self.envio:
            for t in tramas:
                self.conn.sendall(t)

    @property
    def uid(self):
        # nombre + IP de la consola: dos instancias con la misma cuenta PSN (pruebas en un solo PC)
        # siguen siendo jugadores distintos
        return uid_para(f"{self.nombre or 'jugador'}|{self.ip_cliente}")

    puerto_web = PUERTO_WEB

    # ---------- utilidades de respuesta ----------
    def reply(self, h, fields):
        return frame(h["component"], h["command"], h["msg_num"], MSG_REPLY, tdf.encode(ordenar(fields)))

    def notify(self, component, command, fields):
        return frame(component, command, 0, MSG_NOTIFY, tdf.encode(ordenar(fields)))

    # ---------- Util ----------
    def preauth(self, h, f):
        cdat = campo(f, "CDAT", [])
        cinf = campo(f, "CINF", [])
        self.locale = campo(cinf, "LOC", self.locale)
        self.plataforma = campo(cinf, "CPFT", self.plataforma)
        servicio = campo(cdat, "SVCN", "nba-2019-ps4")
        conf = sorted([
            ("associationListSkipInitialSet", "1"),
            ("autoReconnectEnabled", "0"),
            ("connIdleTimeout", "90s"),
            ("defaultRequestTimeout", "60s"),
            ("pingPeriod", "20s"),
            ("userManagerMaxCachedUsers", "0"),
            ("voipHeadsetUpdateRate", "1000"),
            ("xlspConnectionIdleTimeout", "300"),
        ] + ([] if os.environ.get("NBA_TUNEL") == "1" else [
            # Override de DirtySock (parser 0xd272540 del eboot): ConnApiControl('tunl', 0, 0, NULL)
            # apaga ProtoTunnel, asi el trafico de juego va en CommUDP sin cifrar (nuestro servidor de
            # juego dedicado no tiene que implementar el tunel). NBA_TUNEL=1 lo deja encendido.
            ("Override_ConnApi_tunl", "0,0,NULL"),
        ]))
        qos_host = "gossjcprod-qos01.ea.com"
        sitio = [("PSA ", tdf.STRING, qos_host), ("PSP ", tdf.VARINT, QOS_PORT), ("SNA ", tdf.STRING, "prod-sjc")]
        qoss = [
            ("BWPS", tdf.STRUCT, sitio),
            ("LNP ", tdf.VARINT, 1),
            ("LTPS", tdf.MAP, (tdf.STRING, tdf.STRUCT, [("ea-sjc", sitio)])),
            ("SVID", tdf.VARINT, 1161889797),
            ("TIME", tdf.VARINT, 5_000_000),
        ]
        cids = [0x1, 0x4, 0x7, 0x9, 0xF, 0x19, 0x1C, 0x7D0, 0x7800, 0x7801, 0x7802, 0x7803, 0x7805,
                0x7806, 0xF802]
        return self.reply(h, [
            ("ASRC", tdf.STRING, "303107"),
            ("CIDS", tdf.LIST, (tdf.VARINT, cids)),
            ("CLID", tdf.STRING, "NBA19-CLIENT-PS4-CLIENTID"),
            ("CONF", tdf.STRUCT, [("CONF", tdf.MAP, (tdf.STRING, tdf.STRING, conf))]),
            ("ESRC", tdf.STRING, "303107"),
            ("INST", tdf.STRING, servicio),
            ("MAID", tdf.VARINT, 1129238128),
            ("MINR", tdf.VARINT, 0),
            ("NASP", tdf.STRING, PERSONA_NAMESPACE),
            ("PILD", tdf.STRING, ""),
            ("PLAT", tdf.STRING, "ps4"),
            ("QOSS", tdf.STRUCT, qoss),
            ("RSRC", tdf.STRING, "303107"),
            ("SVER", tdf.STRING, "Blaze 15.1.1.7.0"),
        ])

    def ping(self, h, f):
        return self.reply(h, [("STIM", tdf.VARINT, int(time.time()))])

    def config_items(self, cfid):
        web = f"http://{self.ip}:{PUERTO_WEB}"
        items = {
            # claves leidas por el cliente (cadenas junto a "NBA_HDL"/"NBA_WEB" en el eboot); vacias -> OAuth con
            # client_id="" y URL vacia tras el login
            "NBA_HDL": {"HDL_SERVICE_URL": f"{web}/hdl", "HDL_ORCHESTRATOR_CLIENTID": "NBA19-HDL-PS4-CLIENTID"},
            "NBA_WEB": {"NBA_REWARDS_URL": f"{web}/rewards", "NBA_REWARDS_CLIENTID": "NBA19-REWARDS-PS4-CLIENTID"},
            # valores de un servidor OSDK que funcionaba (FIFA 14 / fifa17-ps3-online)
            "OSDK_CORE": {
                "OSDK_PEERBUFFERSIZE": "16384", "OSDK_DISTBUFFERSIZE_IN": "16384",
                "OSDK_DISTBUFFERSIZE_OUT": "16384", "OSDK_MAXGAMES": "16", "OSDK_MAXROOMS": "16",
                "OSDK_USERROOM_PREFIX": "room", "OSDK_MATCHUP_TIMEOUT": "30",
                "OSDK_KEEPALIVEINTERVAL": "30", "OSDK_STATS_EMPTY_CELL": "-1", "OSDK_TICKER_COUNT": "10",
                "JOIN_GAME_TIMEOUT": "30", "MAX_PREGAME_TIMEOUT": "120",
            },
        }
        if cfid == "NBA_H2HSEASONS":
            items[cfid] = config_h2h()
        return sorted(items.get(cfid, {}).items())

    def fetch_client_config(self, h, f):
        cfid = campo(f, "CFID", "")
        return self.reply(h, [("CONF", tdf.MAP, (tdf.STRING, tdf.STRING, self.config_items(cfid)))])

    # ---------- ajustes de usuario (persistentes, clave = nombre PSN) ----------
    def _ajustes(self):
        try:
            with open(AJUSTES, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def settings_save(self, h, f):
        todos = self._ajustes()
        todos.setdefault(self.nombre or "jugador", {})[campo(f, "KEY", "")] = campo(f, "DATA", "")
        with open(AJUSTES, "w", encoding="utf-8") as fh:
            json.dump(todos, fh, indent=1)
        return self.reply(h, [])

    def settings_load(self, h, f):
        clave = campo(f, "KEY", "")
        valor = self._ajustes().get(self.nombre or "jugador", {}).get(clave)
        if valor is None:
            return self.reply(h, [])
        return self.reply(h, [("DATA", tdf.STRING, valor), ("KEY ", tdf.STRING, clave)])

    def settings_load_all(self, h, f):
        mapa = sorted(self._ajustes().get(self.nombre or "jugador", {}).items())
        return self.reply(h, [("SMAP", tdf.MAP, (tdf.STRING, tdf.STRING, mapa))] if mapa else [])

    def telemetry(self):
        return [
            ("ADRS", tdf.STRING, self.ip), ("ANON", tdf.VARINT, 0), ("DISA", tdf.STRING, ""),
            ("EDCT", tdf.VARINT, 0), ("FILT", tdf.STRING, ""), ("LOC ", tdf.VARINT, self.locale),
            ("MINR", tdf.VARINT, 0), ("NOOK", tdf.STRING, ""), ("PORT", tdf.VARINT, 9988),
            ("SDLY", tdf.VARINT, 15000), ("SESS", tdf.STRING, "nba19"), ("SKEY", tdf.STRING, "key"),
            ("SPCT", tdf.VARINT, 0), ("STIM", tdf.STRING, "0"), ("SVNM", tdf.STRING, "nba-2019-ps4"),
        ]

    def get_telemetry_server(self, h, f):
        return self.reply(h, self.telemetry())

    def post_auth(self, h, f):
        uid = self.uid
        return self.reply(h, [
            ("TELE", tdf.STRUCT, self.telemetry()),
            ("TICK", tdf.STRUCT, [("ADRS", tdf.STRING, self.ip), ("PORT", tdf.VARINT, 8999),
                                  ("SKEY", tdf.STRING, "key")]),
            ("UROP", tdf.STRUCT, [("TMOP", tdf.VARINT, 0), ("UID ", tdf.VARINT, uid)]),
        ])

    # ---------- Authentication ----------
    def nombre_de_login(self, f):
        """El login de PS4 trae un codigo de autorizacion PSN; el nombre puede venir en algun campo de texto."""
        for tag in ("EXTB", "NAME", "PNAM", "DSNM", "MAIL"):
            v = campo(f, tag)
            if isinstance(v, (bytes, bytearray)):
                v = bytes(v).split(b"\0", 1)[0].decode("utf-8", "replace")
            if isinstance(v, str) and v.strip():
                return v.strip()
        return self.nombre or "jugador"

    def login(self, h, f):
        self.nombre = self.nombre_de_login(f)
        name = self.nombre
        uid = self.uid
        now = int(time.time())
        ext_id = campo(f, "EXTI", uid)
        self.ext_id = ext_id
        persona = [
            ("DSNM", tdf.STRING, name), ("LAST", tdf.VARINT, now), ("PID ", tdf.VARINT, uid),
            ("PLAT", tdf.VARINT, PLATAFORMA_PS4), ("STAS", tdf.VARINT, 1), ("XREF", tdf.VARINT, ext_id),
        ]
        sess = [
            ("1CON", tdf.VARINT, 0), ("BUID", tdf.VARINT, uid), ("FRST", tdf.VARINT, 0),
            ("GEO ", tdf.VARINT, 1), ("KEY ", tdf.STRING, f"nba19-{uid}"), ("LLOG", tdf.VARINT, now),
            ("MAIL", tdf.STRING, f"{name}@nba19.local"), ("PDTL", tdf.STRUCT, persona),
            ("UID ", tdf.VARINT, uid),
        ]
        self.log(f"login como {name!r} (BlazeId {uid})")
        # LoginResponse {ANON, SESS, SPAM=isOfLegalContactAge, UNDR=isUnderage} (reflexion del eboot)
        resp = self.reply(h, [
            ("ANON", tdf.VARINT, 0), ("SESS", tdf.STRUCT, sess),
            ("SPAM", tdf.VARINT, 1), ("UNDR", tdf.VARINT, 0),
        ])
        return [resp] + self.notificaciones_usuario(ext_id)

    def identidad(self, ext_id=None):
        """UserIdentification de esta sesion."""
        uid = self.uid
        ext_id = getattr(self, "ext_id", uid) if ext_id is None else ext_id
        return [
            ("AID ", tdf.VARINT, uid), ("ALOC", tdf.VARINT, self.locale), ("EXBB", tdf.BLOB, b""),
            ("EXID", tdf.VARINT, ext_id), ("ID  ", tdf.VARINT, uid), ("NAME", tdf.STRING, self.nombre),
            ("NASP", tdf.STRING, PERSONA_NAMESPACE), ("ORIG", tdf.VARINT, 0), ("PIDI", tdf.VARINT, uid),
        ]

    def presentacion(self, maci=None):
        """UserAdded + UserUpdated de esta sesion, para otra sesion que tenga que conocerla (el rival
        de una partida: el cliente busca a los jugadores del roster en su UserManager). maci: id de
        maquina con el que se presenta (ver gamemanager._red_vista)."""
        return [
            self.notify(USER_SESSIONS, 0x0002, [("DATA", tdf.STRUCT, self.datos_sesion(maci)),
                                                ("USER", tdf.STRUCT, self.identidad())]),       # UserAdded
            self.notify(USER_SESSIONS, 0x0005, [("FLGS", tdf.VARINT, 1), ("ID  ", tdf.VARINT, self.uid)]),
        ]

    def notificaciones_usuario(self, ext_id):
        name, uid, now = self.nombre, self.uid, int(time.time())
        ident = self.identidad(ext_id)
        autenticado = [
            ("1CON", tdf.VARINT, 0), ("ALOC", tdf.VARINT, self.locale), ("BUID", tdf.VARINT, uid),
            ("CGID", tdf.OBJID, (USER_SESSIONS, 2, uid)), ("DSNM", tdf.STRING, name),
            ("FRST", tdf.VARINT, 0), ("KEY ", tdf.STRING, f"nba19-{uid}"), ("LAST", tdf.VARINT, now),
            ("LLOG", tdf.VARINT, now), ("MAIL", tdf.STRING, f"{name}@nba19.local"),
            ("NASP", tdf.STRING, PERSONA_NAMESPACE), ("PID ", tdf.VARINT, uid),
            ("PLAT", tdf.VARINT, PLATAFORMA_PS4), ("UID ", tdf.VARINT, uid), ("USTP", tdf.VARINT, 0),
            ("XREF", tdf.VARINT, ext_id),
        ]
        datos = [("ADDR", tdf.UNION, (tdf.UNION_UNSET, None))]
        return [
            self.notify(USER_SESSIONS, 0x0008, autenticado),                                  # UserAuthenticated
            self.notify(USER_SESSIONS, 0x0002, [("DATA", tdf.STRUCT, datos), ("USER", tdf.STRUCT, ident)]),  # UserAdded
            self.notify(USER_SESSIONS, 0x0005, [("FLGS", tdf.VARINT, 1), ("ID  ", tdf.VARINT, uid)]),       # UserUpdated
        ]

    def get_account(self, h, f):
        name = self.nombre or "jugador"
        # AccountInfo (17 miembros segun la reflexion del eboot); adulto, cuenta activa, email verificado
        return self.reply(h, [
            ("AMU ", tdf.VARINT, 0), ("ASRC", tdf.STRING, "303107"), ("CO  ", tdf.STRING, "US"),
            ("DOB ", tdf.STRING, "1990-01-01"), ("DTCR", tdf.STRING, "2018-09-07"), ("GOPT", tdf.VARINT, 0),
            ("LATH", tdf.STRING, time.strftime("%Y-%m-%d")), ("LN  ", tdf.STRING, "es"),
            ("LOC ", tdf.STRING, "es_ES"), ("MAIL", tdf.STRING, f"{name}@nba19.local"),
            ("PML ", tdf.STRING, ""), ("RC  ", tdf.VARINT, 0), ("STAS", tdf.VARINT, 1),
            ("STAT", tdf.VARINT, 2), ("TPOT", tdf.VARINT, 0), ("UDU ", tdf.VARINT, 0),
            ("UID ", tdf.VARINT, self.uid),
        ])

    # ---------- UserSessions: datos de red ----------
    def datos_extendidos(self):
        """UserSessionExtendedDataUpdate {DATA: UserSessionExtendedData, SUBS, USID} (0x7802::0x0001)."""
        return self.notify(USER_SESSIONS, 0x0001,
                           [("DATA", tdf.STRUCT, self.datos_sesion()), ("SUBS", tdf.VARINT, 1),
                            ("USID", tdf.VARINT, self.uid)])

    def datos_sesion(self, maci=None):
        """UserSessionExtendedData: direccion de red y QoS."""
        red = getattr(self, "red", None) or {}
        ip, puerto = red.get("ip", 0), red.get("port", 0)
        maci = red.get("maci", 0) if maci is None else maci
        ipaddr = [("IP  ", tdf.VARINT, ip), ("MACI", tdf.VARINT, maci), ("PORT", tdf.VARINT, puerto)]
        par = [("EXIP", tdf.STRUCT, ipaddr), ("INIP", tdf.STRUCT, ipaddr), ("MACI", tdf.VARINT, maci)]
        datos = [
            ("ADDR", tdf.UNION, (2, ("VALU", tdf.STRUCT, par))),   # 2 = ipPairAddress
            ("BPS ", tdf.STRING, "ea-sjc"), ("CTY ", tdf.STRING, "US"),
            ("HWFG", tdf.VARINT, getattr(self, "hwfg", 0)),
            ("PSM ", tdf.MAP, (tdf.STRING, tdf.VARINT, [("ea-sjc", 20)])),
            ("QDAT", tdf.STRUCT, [("BWHR", tdf.VARINT, 0), ("DBPS", tdf.VARINT, 50_000_000),
                                  ("NAHR", tdf.VARINT, 0), ("NATT", tdf.VARINT, 0),
                                  ("UBPS", tdf.VARINT, 20_000_000)]),
        ]
        return datos

    def update_network_info(self, h, f):
        info = campo(f, "INFO", [])
        addr = campo(info, "ADDR")
        if addr and addr[1] is not None:
            inip = campo(campo(addr[1][2], "INIP", []), "IP")
            self.red = {"ip": inip or 0, "port": campo(campo(addr[1][2], "INIP", []), "PORT", 0) or 0,
                        "maci": campo(addr[1][2], "MACI", 0) or 0}
        return [self.reply(h, []), self.datos_extendidos()]

    def update_hardware_flags(self, h, f):
        self.hwfg = campo(f, "HWFG", 0) or 0
        return [self.reply(h, []), self.datos_extendidos()]

    def email_optin(self, h, f):
        return self.reply(h, [("EAMC", tdf.VARINT, 0), ("PMC ", tdf.VARINT, 0)])

    # ---------- despacho ----------
    def manejar(self, h, f):
        """Devuelve (lista de tramas, manejado?)."""
        tabla = {
            (UTIL, 0x0007): self.preauth,
            (UTIL, 0x0002): self.ping,
            (UTIL, 0x0001): self.fetch_client_config,
            (UTIL, 0x0005): self.get_telemetry_server,
            (UTIL, 0x0008): self.post_auth,
            (UTIL, 0x000A): self.settings_load,
            (UTIL, 0x000B): self.settings_save,
            (UTIL, 0x000C): self.settings_load_all,
            (AUTH, 0x000A): self.login,
            (AUTH, 0x001E): self.get_account,
            (AUTH, 0x00F2): self.email_optin,
            (USER_SESSIONS, 0x0014): self.update_network_info,
            (USER_SESSIONS, 0x0008): self.update_hardware_flags,
        }
        fn = tabla.get((h["component"], h["command"]))
        if fn is None and h["component"] == gamemanager.GAME_MANAGER:
            r = gamemanager.manejar(self, h["command"], f)
            campos, notifs = r if isinstance(r, tuple) else (r, [])
            return [self.reply(h, campos)] + [self.notify(c, n, x) for c, n, x in notifs], True
        if fn is None and h["component"] in nba.MANEJADORES:
            r = nba.MANEJADORES[h["component"]](self, h["command"], f)
            if isinstance(r, int):                                   # codigo de error Blaze
                return [frame_error(h, r)], True
            campos, notifs = r if isinstance(r, tuple) else (r, [])   # (respuesta, [(comp, cmd, campos)])
            return [self.reply(h, campos)] + [self.notify(c, n, x) for c, n, x in notifs], True
        if fn is None:
            return [frame(h["component"], h["command"], h["msg_num"], MSG_REPLY)], False
        out = fn(h, f)
        return (out if isinstance(out, list) else [out]), True


def atender(conn, addr, server_ip, log, nombre_redirector):
    s = Sesion(server_ip, lambda m: log(f"blaze {addr[1]}: {m}"))
    s.nombre = nombre_redirector()
    s.conn = conn
    s.ip_cliente = ip_u32(addr[0])
    log(f"blaze: conexion de {addr}")
    conn.settimeout(120)
    buf = b""
    ultimo, repes = None, 0   # peticiones identicas seguidas (sondeos): se cuentan, no se registran
    try:
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                log("blaze: 120 s sin datos, cierro")
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= HDR_LEN:
                h = parse_header(buf[:HDR_LEN])
                total = HDR_LEN + h["meta"] + h["size"]
                if len(buf) < total:
                    break
                payload = buf[HDR_LEN + h["meta"]:total]
                buf = buf[total:]
                if h["type"] == MSG_PING:
                    s.enviar([frame(h["component"], h["command"], h["msg_num"], MSG_PING_REPLY)])
                    continue
                fields, reached, err = tdf.decode_partial(payload)
                n = nombre(h["component"], h["command"])
                clave = (h["component"], h["command"], payload)
                silencio = clave == ultimo
                if silencio:
                    repes += 1
                else:
                    if repes:
                        log(f"   (la peticion anterior se repitio {repes} veces mas)")
                    ultimo, repes = clave, 0
                    log(f"<- {n} #{h['msg_num']} tipo={h['type']} {h['size']}B"
                        f" cab=[{h['byte13']:02x} {h['options']:02x} {h['reserved']:02x}]")
                    if fields:
                        log("   " + tdf.pretty(fields).replace("\n", "\n   "))
                    if err:
                        log(f"   TDF incompleto ({reached}/{h['size']}): {err}; crudo={payload.hex()}")
                if h["type"] != MSG_REQUEST:
                    continue
                tramas, ok = s.manejar(h, fields)
                s.enviar(tramas)
                if not silencio:
                    log(f"-> {n} {'respondido' if ok else 'NO MANEJADO (respuesta vacia)'}"
                        f" ({len(tramas)} trama/s)")
    except OSError as e:
        log(f"blaze: error de conexion {e}")
    finally:
        gamemanager.desconectado(s)
        conn.close()
        log(f"blaze: conexion cerrada {addr}")


def servir(server_ip, port, log, nombre_redirector):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)
    log(f"blaze escuchando en :{port}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=atender, args=(conn, addr, server_ip, log, nombre_redirector),
                         daemon=True).start()
