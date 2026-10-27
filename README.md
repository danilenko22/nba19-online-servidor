# NBA LIVE 19 online – community server for shadPS4

A server that replaces EA's (shut down) servers for **NBA LIVE 19** (PS4, CUSA11355 v1.00) so the **ranked 1 vs 1 match** can be played online with the [shadPS4](https://github.com/danilenko22/shadPS4/tree/nba19-fix) emulator (branch `nba19-fix`, which has the client-side changes).

Tested between two PCs (Intel and AMD): pre-game, loading and full matches stay in sync.

**Ready-to-use package (client + server):** see [Releases](https://github.com/danilenko22/nba19-online-servidor/releases).

## What it does

| Module | Role |
|---|---|
| `servidor.py` | Entry point; HTTPS redirector (42230), fake Nucleus/OAuth (443), QoS (17502 TCP/UDP), web services (8088), ShadNet WebAPI proxy (31315), telemetry and ticker (9988, 8999). |
| `blaze.py`, `tdf.py`, `nba.py` | EA's Blaze protocol (TDF "Heat2") on port 10041: login, configuration and the components the game asks for. |
| `gamemanager.py` | Matchmaking and games with a **dedicated server topology** (the server hosts the game). |
| `juego.py` | **Match server** (UDP 7000): CommUDP, NetGameLink sync, rebroadcaster for the pre-game streams, and the server side of **NetGameDist** (distributes each frame's inputs and aligns the start of the match). |

## Usage

**Easiest:** download the [release](https://github.com/danilenko22/nba19-online-servidor/releases) and double-click `Server\NBA19_Server.exe` (no Python needed). The first time it asks for the server IP and saves it in `server_ip.txt`.

**From source:** Python 3.10+, no extra packages.

```
python -u servidor.py [IP]
```

`IP` is the IP the players use to reach the server (a gaming VPN IP, the public IP with forwarded ports, or the local IP). Without it, the server asks for it and saves it in `server_ip.txt`. The TLS certificate (`redirector.crt`/`.key`) is generated with `openssl` if missing; any certificate works, shadPS4 does not verify it.

**Building the exe:** `pip install pyinstaller`, then run `build_exe.bat` (output in `dist\`).

Ports: TCP 42230, 10041, 443, 8088, 31315, 9988, 8999, 17502 · UDP 17502, 7000.

Each player uses the shadPS4 build from branch `nba19-fix` with:
- `user/hosts.txt` sending `*.ea.com` and `srv.shadps4.net` to the server IP;
- `NBA_GSRV=10.99.0.1:1899=<IP>:7000` (the match server is announced with a virtual IP);
- the `NBA19_online.xml` patch and the game config with network and ShadNet enabled.

The released package has all of that ready (`Client` folder).

## Environment variables (debugging)

| Variable | Effect |
|---|---|
| `NBA_DEDICADO=0` | Old P2P topology (not playable; research only). |
| `NBA_RIVAL_FALSO=1` | Matches you instantly with a fake opponent, to test with a single PC. |
| `NBA_PUERTO_JUEGO` | UDP port of the match server (7000). |

Logs are written to `logs/`.

## Disclaimer

Unofficial project, not affiliated with EA or the shadPS4 team. The game and PS4 files are not included.

## License

GPL-2.0 (see `LICENSE`), the same as shadPS4.
