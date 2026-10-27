# NBA LIVE 19 online – servidor propio para shadPS4

Servidor que reemplaza a los servidores de EA (cerrados) de **NBA LIVE 19** (PS4, CUSA11355 v1.00) para jugar el **partido clasificado 1 contra 1** online con el emulador [shadPS4](https://github.com/danilenko22/shadPS4/tree/nba19-fix) (rama `nba19-fix`, que trae los cambios que necesita el cliente).

Probado entre dos PCs (Intel y AMD): previa, carga y partido completo sincronizados.

## Qué hace

| Módulo | Función |
|---|---|
| `servidor.py` | Arranque; redirector HTTPS (42230), Nucleus/OAuth falso (443), QoS (17502 TCP/UDP), servicios web (8088), proxy WebAPI de ShadNet (31315), telemetría y ticker (9988, 8999). |
| `blaze.py`, `tdf.py`, `nba.py` | Protocolo Blaze de EA (TDF «Heat2») en el puerto 10041: login, configuración y componentes que pide el juego. |
| `gamemanager.py` | Emparejamiento y partidas con **topología de servidor dedicado** (el servidor es el anfitrión de la partida). |
| `juego.py` | **Servidor de juego** (UDP 7000): CommUDP, sincronía de NetGameLink, rebroadcaster de los streams de la previa y lado servidor de **NetGameDist** (reparte las entradas de cada fotograma y alinea el inicio del partido). |

## Uso

Requiere Python 3.10+ y `openssl` en el PATH (o los `redirector.crt`/`.key` del paquete publicado; shadPS4 no verifica el certificado).

```
python -u servidor.py <IP>
```

`<IP>` es la IP con la que los jugadores llegan al servidor (la de una VPN de juego, la pública con los puertos abiertos o la local). En Windows: editar y abrir `Iniciar_servidor.bat`.

Puertos: TCP 42230, 10041, 443, 8088, 31315, 9988, 8999, 17502 · UDP 17502, 7000.

Cada jugador usa el shadPS4 de la rama `nba19-fix` con:
- `user/hosts.txt` que mande `*.ea.com` y `srv.shadps4.net` a la IP del servidor;
- `NBA_GSRV=10.99.0.1:1899=<IP>:7000` (el servidor de juego se anuncia con una IP virtual);
- el parche `NBA19_online.xml` y la configuración del juego con red y ShadNet activados.

El paquete publicado trae todo eso listo (carpeta `Cliente`).

## Variables de entorno (depuración)

| Variable | Efecto |
|---|---|
| `NBA_DEDICADO=0` | Topología P2P antigua (no sirve para jugar; solo para investigar). |
| `NBA_RIVAL_FALSO=1` | Empareja al momento con un rival inventado para probar con una sola PC. |
| `NBA_PUERTO_JUEGO` | Puerto UDP del servidor de juego (7000). |

Los registros quedan en `logs/`.

## Aviso

Proyecto no oficial, sin relación con EA ni con el equipo de shadPS4. No incluye el juego ni archivos de la PS4.

## Licencia

GPL-2.0 (ver `LICENSE`), la misma que shadPS4.
