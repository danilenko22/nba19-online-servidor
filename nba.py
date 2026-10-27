"""Componentes propios de NBA LIVE 19 / OSDK (fase 4).

Formatos sacados de la reflexion TDF del eboot (tools/tdfinfo.py; volcado completo en
logs/tdf_respuestas.txt y logs/tdf_peticiones.txt). Con respuestas vacias el cliente veia
success=false / ENUM_UNKNOWN / intervalos 0, sondeaba sin parar y acababa cerrando sesion.
"""
import threading
import time

import tdf

V, S, BL, ST, L, M = tdf.VARINT, tdf.STRING, tdf.BLOB, tdf.STRUCT, tdf.LIST, tdf.MAP

CHANNELS = 0x081C
LIVE_UPDATES = 0x081D
LIVE_EVENTS = 0x0823
NBA_USER_STORAGE = 0x0824
COMMERCE = 0x0806
OSDK_SETTINGS = 0x08C9
DYNAMIC_MESSAGING = 0x08CA

REFRESCO = 600   # segundos entre consultas de contenido (eventos, mensajes)

_lock = threading.Lock()
_estado = {}     # nombre PSN -> {"canales": {id: (CTYP, BID)}, "feeds": {id: (CTYP, CRID)}, "batalla": campos}
_ids = [1000]


def _nuevo_id():
    with _lock:
        _ids[0] += 1
        return _ids[0]


def _de(nombre):
    with _lock:
        return _estado.setdefault(nombre, {"canales": {}, "feeds": {}, "batalla": None})


def campo(fields, tag, default=None):
    for t, _ty, v in fields:
        if t == tag.strip():
            return v
    return default


# ---------- Channels (0x081C) ----------
def channels(s, cmd, f):
    st = _de(s.nombre or "jugador")
    uid = s.uid
    if cmd in (2, 3):                                   # GetFollowers / GetFollowees
        return [("EIDL", L, (V, []))]
    if cmd == 4:                                        # CreateChannel {CCHI{BID, CTYP}}
        info = campo(f, "CCHI", [])
        cid = _nuevo_id()
        st["canales"][cid] = (campo(info, "CTYP", 0), campo(info, "BID", 0) or uid)
        return [("NCHI", V, cid)]
    if cmd == 6:                                        # GetChannels -> mapa id -> ChannelInfo
        mapa = [(cid, [("BID ", V, bid), ("CCIB", M, (S, S, [])), ("CTYP", V, ctyp)])
                for cid, (ctyp, bid) in sorted(st["canales"].items())]
        return [("CIL ", M, (V, ST, mapa))]
    if cmd == 8:                                        # CreateFeed {CRID, CTYP}
        ctyp = campo(f, "CTYP", 0)
        fid = _nuevo_id()
        st["feeds"][fid] = (ctyp, campo(f, "CRID", uid))
        return [("CTYP", V, ctyp), ("FEID", V, fid)]
    if cmd == 0xA:                                      # GetFeeds -> lista FeedInfo
        lista = [[("CRID", V, crid), ("CTYP", V, ctyp), ("FID ", V, fid), ("KVM ", M, (S, S, []))]
                 for fid, (ctyp, crid) in sorted(st["feeds"].items())]
        return [("CIL ", L, (ST, lista))]
    if cmd == 0xB:                                      # AddChannelToFeed
        return [("ACTF", V, 1)]
    if cmd == 0xF:                                      # AddEvent
        return [("EVID", V, _nuevo_id())]
    if cmd == 0x16:                                     # SubscribeToPublicChannels
        return [("SPCR", V, 1)]
    if cmd == 0x17:                                     # GetChannelTypes
        return [("CTIS", V, 1), ("CTLS", L, (ST, []))]
    return []


# ---------- OsdkDynamicMessaging (0x08CA) ----------
def dynamic_messaging(s, cmd, f):
    if cmd == 1:                                        # MessageRequest -> MessageResponse (sin mensajes)
        return [("ENUM", V, 1), ("GVID", S, ""), ("MSGS", L, (ST, []))]   # 1 = DYNAMICMESSAGE_ENUM_SUCCESS
    if cmd == 2:                                        # ConfigResponse
        return [("CDRD", V, 5), ("CERD", V, 120), ("CMDI", V, 10), ("CMMC", V, 5), ("CMRI", V, REFRESCO)]
    return []


# ---------- LiveEvents (0x0823) / LiveUpdates (0x081D) ----------
def live_events(s, cmd, f):
    if cmd in (1, 3):                                   # GetLiveEvents / GetLiveRunEvents
        r = [("LIST", L, (ST, [])), ("MODE", V, campo(f, "MODE", 0) or 0), ("REFH", V, REFRESCO)]
        return ([("CDLB", V, 0)] + r) if cmd == 1 else r
    if cmd == 2:                                        # GetSummerCircuitEvents
        return [("ELST", L, (ST, [])), ("GLST", L, (V, [])), ("MODE", V, campo(f, "MODE", 0) or 0),
                ("REFH", V, REFRESCO)]
    if cmd == 6:                                        # GetConfiguration
        return [("REFS", V, REFRESCO)]
    return []


def live_updates(s, cmd, f):
    if cmd == 1:   # GetLiveUpdatePaths {VERS} -> LiveUpdatePathsResponseList {LLUP: [{BASE, DIR}]}
        # No hay contenido de live update que servir. Con rutas validas el cliente intenta usar
        # /download0/LCU, falla (error 1), emite LiveUpdateError y el script cierra la sesion
        # (NBA-DCFEA). El callback del cliente trata aparte el error Blaze 0x40020000 (estado 4).
        return LU_SIN_ACTUALIZACION
    return []


LU_SIN_ACTUALIZACION = 0x40020000


# ---------- Commerce (0x0806) ----------
def commerce(s, cmd, f):
    if cmd == 2:                                        # GetConfiguration
        return [("BUCK", L, (ST, [])), ("CONT", V, 30), ("UNLK", L, (ST, []))]
    if cmd == 3:                                        # GetCurrency
        return [("BUCK", V, 0), ("COIN", V, 0)]
    if cmd == 4:                                        # GetEntitlements
        return [("ENTL", L, (ST, []))]
    if cmd == 7:                                        # SynchronizeConsumables
        return [("BCON", V, 0), ("ENTL", L, (ST, [])), ("GATE", V, 0),
                ("MSG ", ST, [("RCDE", V, 0), ("RSTR", S, ""), ("TIME", tdf.TIME, int(time.time()) * 1000000)]),
                ("PS4T", V, 0)]
    if cmd == 8:                                        # ProcessEndOfGame
        return [("CRWD", L, (ST, []))]
    return []


# ---------- NbaUserStorage (0x0824) ----------
def nba_user_storage(s, cmd, f):
    st = _de(s.nombre or "jugador")
    uid = s.uid
    estado_ok = ("RSPS", ST, [("SMSG", S, ""), ("STAT", V, 0)])
    if cmd == 4:                                        # SetBattleModeData: se guarda tal cual
        st["batalla"] = f
        return []
    if cmd == 3:                                        # GetBattleModeData
        b = st["batalla"] or []
        batd = campo(b, "BATD") or [("CONT", V, 0), ("HYPE", V, 0), ("LOCK", V, 0), ("ORTA", V, 0),
                                     ("OWNR", V, uid)]
        lind = campo(b, "LIND") or [("BLAZ", V, uid)]
        hrud = campo(b, "HRUD") or [("BLAZ", V, uid)]
        return [("BATD", ST, batd), ("BLUD", ST, lind), ("MRTA", V, 3), estado_ok, ("RULD", ST, hrud)]
    if cmd == 2:                                        # SetOneCourtData
        return [("PROF", M, (V, S, []))]
    if cmd == 0xF:                                      # GetCourtStatus
        return [("CSTS", V, 1)]
    return []


# ---------- OSDKSettings (0x08C9) ----------
def osdk_settings(s, cmd, f):
    if cmd == 1:                                        # fetchSettings
        return [("LSIN", L, (ST, [])), ("LSST", L, (ST, []))]
    if cmd == 2:                                        # fetchSettingsGroups
        return [("LGRP", L, (ST, []))]
    return []


# ---------- Stats (0x0007) ----------
# Sin estos, la carrera (The One) registraba "CareerLoadUserStatsGroupListDone() - iGroupListSize < 0".
STATS = 0x0007
GRUPOS_STATS = ("LiveRunWinsLossesGrp", "ProAmStatsGrp")   # nombres "...Grp" del eboot
TIPO_USUARIO = (0x7802, 1)                                  # ObjectType de un usuario (UserSessions)

# Nombres de estadistica H2H del eboot (cadenas junto a "streak"). El cliente las busca por nombre
# en los resultados (H2HSeasonalPlay, H2HSeasonalLeaguePlay, NormalPostGameStatsReport...) y no
# comprueba si faltan: con una lista vacia se colgaba al entrar en "Online Head2Head".
ESTADISTICAS = (
    "seasons rankingPoints currentLeague seasonWins seasonLosses gamesPlayed points projectedPts "
    "prevSeasonWins prevSeasonLosses prevPoints prevProjectedPts tournamentsWon1 tournamentsWon2 "
    "tournamentsWon3 tournamentsWon4 tournamentsLost1 tournamentsLost2 tournamentsLost3 tournamentsLost4 "
    "totalTournamentsWon tournamentsEntered tournamentsWinPercent tournamentRankingPoints tournamentRound "
    "count wins losses leaguesWon leaguesWon1 leaguesWon2 leaguesWon3 leaguesWon4 promotions relegations "
    "previousLeague bestLeague bestPoints currentSeasonMov lastMatch0 lastMatch1 lastMatch2 lastMatch3 "
    "lastMatch4 lastMatch5 overallRankingPoints bestTournament bestRound streak lastGameSkillPoints "
    "lastGamePointsFor lastGamePointsAgainst skill_level"
).split()
VALOR_INICIAL = {"currentLeague": "1", "bestLeague": "1", "previousLeague": "1", "skill_level": "1"}


def _desc_estadistica(nombre):
    return [("CATG", S, ""), ("DFLT", S, VALOR_INICIAL.get(nombre, "0")), ("DRVD", V, 0), ("FRMT", S, "%d"),
            ("KIND", S, ""), ("LDSC", S, nombre), ("META", S, ""), ("NAME", S, nombre), ("SDSC", S, nombre),
            ("TYPE", V, 0)]


def stats(s, cmd, f):
    if cmd == 3:                                        # getStatGroupList -> StatGroupList {GRPS}
        grupos = [[("DESC", S, g), ("KSUM", M, (S, V, [])), ("META", S, ""), ("NAME", S, g)]
                  for g in GRUPOS_STATS]
        return [("GRPS", L, (ST, grupos))]
    if cmd == 0xF:                                      # getKeyScopesMap -> KeyScopes {KSIT}
        return [("KSIT", M, (S, ST, []))]
    if cmd == 4:                                        # getStatGroup -> StatGroupResponse
        nombre = campo(f, "NAME", "")
        return [("CNAM", S, ""), ("DESC", S, nombre), ("ETYP", tdf.OBJTYPE, TIPO_USUARIO),
                ("KSUM", M, (S, V, [])), ("META", S, ""), ("NAME", S, nombre),
                ("STAT", L, (ST, [_desc_estadistica(n) for n in ESTADISTICAS]))]
    if cmd == 0x10:                                     # getStatsByGroupAsync: los datos van por notificacion
        eids = (campo(f, "EID", (V, [])) or (V, []))
        eids = eids[1] if isinstance(eids, tuple) else eids
        valores = [VALOR_INICIAL.get(n, "0") for n in ESTADISTICAS]
        filas = [[("EID ", V, eid), ("ETYP", tdf.OBJTYPE, TIPO_USUARIO), ("POFF", V, 0),
                  ("STAT", L, (S, valores))] for eid in eids]
        notif = [("GRNM", S, campo(f, "NAME", "")), ("KEY ", S, ""), ("LAST", V, 1),
                 ("STS ", ST, [("STAT", L, (ST, filas))]),
                 ("VID ", V, campo(f, "VID", 0) or 0)]
        return [], [(STATS, 0x0032, notif)]            # GetStatsAsyncNotification
    return []


# ---------- AssociationLists (0x0019) ----------
# getLists vacio -> "AssociationListManagerConcrete::UpdateAssociationListFailure".
ASSOCIATION_LISTS = 0x0019


def association_lists(s, cmd, f):
    if cmd == 6:                                        # getLists {ALST} -> Lists {LMAP}
        listas = []
        for info in (campo(f, "ALST", (ST, [])) or (ST, []))[1]:
            lid = campo(info, "LID", [])
            info_resp = [("BOID", tdf.OBJID, (0x7802, 1, s.uid)), ("FLGS", V, campo(info, "FLGS", 0) or 0),
                         ("LID ", ST, [("LNM ", S, campo(lid, "LNM", "") or ""),
                                       ("TYPE", V, campo(lid, "TYPE", 0) or 0)]),
                         ("LMS ", V, campo(info, "LMS", 0) or 100), ("PNAM", S, campo(info, "PNAM", "") or ""),
                         ("PRID", V, campo(info, "PRID", 0) or 0), ("PRMS", V, campo(info, "PRMS", 0) or 0)]
            listas.append([("INFO", ST, info_resp), ("MEML", L, (ST, [])), ("OFRC", V, 0), ("TOCT", V, 0)])
        return [("LMAP", L, (ST, listas))]
    return []


# ---------- H2HSeasons (0x0803) ----------
H2H_SEASONS = 0x0803


def h2h_seasons(s, cmd, f):
    if cmd == 1:                                        # GetH2HTournamentInfo -> {INFO: H2HTournamentInfo}
        ahora = int(time.time())
        return [("INFO", ST, [("CET ", V, ahora + 7 * 86400), ("CID ", V, 1), ("CST ", V, ahora - 86400),
                              ("QDIV", V, 1), ("STAT", V, 0), ("TELG", V, 0), ("TID ", V, 1),
                              ("TSTA", V, 0)])]
    return []


MANEJADORES = {
    H2H_SEASONS: h2h_seasons,
    STATS: stats,
    ASSOCIATION_LISTS: association_lists,
    CHANNELS: channels,
    DYNAMIC_MESSAGING: dynamic_messaging,
    LIVE_EVENTS: live_events,
    LIVE_UPDATES: live_updates,
    COMMERCE: commerce,
    NBA_USER_STORAGE: nba_user_storage,
    OSDK_SETTINGS: osdk_settings,
}
