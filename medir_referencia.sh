#!/bin/bash
# ---------------------------------------------------------------------------
# Tanda de referencia con iperf3 y ping, para contrastar con la aplicación.
#
#   ./medir_referencia.sh <etiqueta> [repeticiones] [pausa_seg] [ip_servidor]
#
# Ejemplos:
#   ./medir_referencia.sh b5_ch36_w40_p20_d0
#   ./medir_referencia.sh b5_ch36_w40_p20_d0 5 60
#
# Guarda la salida completa en referencia_iperf/<etiqueta>/rNN_*.txt y añade
# una fila por repetición a referencia_iperf/resumen.csv
#
# Los parámetros coinciden con los de client.py: 10 s de TCP, 10 medidas de
# RTT y 200 datagramas de 512 bytes a 200 paq/s (820 Kbit/s).
# ---------------------------------------------------------------------------
set -u

ETIQUETA="${1:-}"
REPES="${2:-1}"
PAUSA="${3:-0}"
SERVIDOR="${4:-192.168.1.28}"

# Pausa entre las tres pruebas de una misma tanda (no confundir con PAUSA,
# que es la espera entre tandas y la elige quien llama).
# La prueba de throughput deja el enlace saturado y con las colas llenas;
# medir pérdida o latencia justo detrás caracteriza esa recuperación en vez
# del enlace en reposo. Es la misma espera que aplica client.py, e igualarla
# en las dos herramientas es lo que permite comparar sus resultados.
PAUSA_PRUEBAS=3

if [ -z "$ETIQUETA" ]; then
    echo "Uso: $0 <etiqueta> [repeticiones] [pausa_seg] [ip_servidor]" >&2
    echo "Ej.: $0 b5_ch36_w40_p20_d0 5 60" >&2
    exit 1
fi

BASE="$(cd "$(dirname "$0")" && pwd)/referencia_iperf"
DESTINO="$BASE/$ETIQUETA"
RESUMEN="$BASE/resumen.csv"
mkdir -p "$DESTINO"

if [ ! -f "$RESUMEN" ]; then
    echo "timestamp,etiqueta,repeticion,tput_mbps_rx,tput_mbps_tx,retransmisiones,udp_jitter_ms,udp_perdidos,udp_enviados,udp_perdida_pct,rtt_min_ms,rtt_medio_ms,rtt_max_ms,rtt_mdev_ms" > "$RESUMEN"
fi

# Comprueba que el servidor responde antes de empezar la tanda entera.
if ! ping -c 1 -W 3 "$SERVIDOR" > /dev/null 2>&1; then
    echo "ERROR: $SERVIDOR no responde. ¿Está el portátil conectado y encendido?" >&2
    exit 1
fi

# Y que iperf3 está escuchando: sin esto la tanda se completa entera
# dejando ficheros con "Connection refused" y filas vacías en el resumen.
if ! timeout 3 bash -c "echo > /dev/tcp/$SERVIDOR/5201" 2>/dev/null; then
    echo "ERROR: no hay nadie escuchando en $SERVIDOR:5201." >&2
    echo "       Arranca 'iperf3 -s' en el portátil, en otra terminal." >&2
    echo "       (puede convivir con server.py: usan puertos distintos)" >&2
    exit 1
fi

echo "Etiqueta:   $ETIQUETA"
echo "Servidor:   $SERVIDOR"
echo "Tandas:     $REPES  (pausa de $PAUSA s entre ellas)"
echo "Destino:    $DESTINO"
echo

for (( i=1; i<=REPES; i++ )); do
    N=$(printf "r%02d" "$i")
    MARCA=$(date +%Y-%m-%dT%H:%M:%S)
    echo "───── $N de $REPES  ($MARCA) ─────"

    TCP="$DESTINO/${N}_iperf_tcp.txt"
    UDP="$DESTINO/${N}_iperf_udp.txt"
    RTT="$DESTINO/${N}_ping_rtt.txt"

    echo "  TCP  ..."; iperf3 -c "$SERVIDOR" -t 10                    > "$TCP" 2>&1
    sleep "$PAUSA_PRUEBAS"
    echo "  UDP  ..."; iperf3 -c "$SERVIDOR" -u -b 820K -l 512 -t 10  > "$UDP" 2>&1
    sleep "$PAUSA_PRUEBAS"
    echo "  ping ..."; ping  -c 10 "$SERVIDOR"                        > "$RTT" 2>&1

    # --- extracción de las cifras clave -----------------------------------
    # Línea "receiver" del TCP: ... <caudal> Mbits/sec  receiver
    TPUT_RX=$(awk '/receiver/ {print $(NF-2)}' "$TCP" | tail -1)
    # Línea "sender" del TCP: ... <caudal> Mbits/sec <retr>  sender
    TPUT_TX=$(awk '/sender/   {print $(NF-3)}' "$TCP" | tail -1)
    RETR=$(awk    '/sender/   {print $(NF-1)}' "$TCP" | tail -1)

    # Línea "receiver" del UDP: ... <jitter> ms  <perdidos>/<total> (<pct>%)  receiver
    UDP_JIT=$(awk '/receiver/ {print $(NF-4)}' "$UDP" | tail -1)
    UDP_FRAC=$(awk '/receiver/ {print $(NF-2)}' "$UDP" | tail -1)
    UDP_PCT=$(awk '/receiver/ {gsub(/[()%]/,"",$(NF-1)); print $(NF-1)}' "$UDP" | tail -1)
    UDP_PERD="${UDP_FRAC%%/*}"
    UDP_ENV="${UDP_FRAC##*/}"

    # rtt min/avg/max/mdev = 1.476/130.060/291.725/112.738 ms
    CIFRAS=$(awk -F'= ' '/rtt min/ {print $2}' "$RTT" | awk '{print $1}')
    RTT_MIN=$(echo "$CIFRAS" | cut -d/ -f1)
    RTT_AVG=$(echo "$CIFRAS" | cut -d/ -f2)
    RTT_MAX=$(echo "$CIFRAS" | cut -d/ -f3)
    RTT_DEV=$(echo "$CIFRAS" | cut -d/ -f4)

    echo "$MARCA,$ETIQUETA,$i,${TPUT_RX:-},${TPUT_TX:-},${RETR:-},${UDP_JIT:-},${UDP_PERD:-},${UDP_ENV:-},${UDP_PCT:-},${RTT_MIN:-},${RTT_AVG:-},${RTT_MAX:-},${RTT_DEV:-}" >> "$RESUMEN"

    printf "  → TCP %s Mbps rx (%s retr) · UDP jitter %s ms, pérdida %s%% · RTT medio %s ms\n\n" \
        "${TPUT_RX:-?}" "${RETR:-?}" "${UDP_JIT:-?}" "${UDP_PCT:-?}" "${RTT_AVG:-?}"

    if [ "$i" -lt "$REPES" ] && [ "$PAUSA" -gt 0 ]; then
        echo "  (pausa de $PAUSA s)"; sleep "$PAUSA"; echo
    fi
done

# Deja el enlace asentado antes de devolver el control: lo habitual es que
# a continuación se lance client.py sobre este mismo enlace.
sleep "$PAUSA_PRUEBAS"

echo "Listo. Resumen acumulado en:"
echo "  $RESUMEN"
