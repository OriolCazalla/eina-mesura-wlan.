#!/usr/bin/env python3
"""
Cliente de medida WLAN. Fusiona las dos versiones anteriores:
  - Protocolo de comandos y prueba RTT/jitter de "VERSION 1".
  - Interfaz rich y CLI configurable (--duration, etc.) de "VERSION INICIAL".

Ejecuta primero server.py en el otro equipo. Luego:
    python3 client.py --server 192.168.1.50 --test both --duration 10 --pings 10
"""
import argparse
import csv
import os
import re
import socket
import statistics
import struct
import subprocess
import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table

try:
    import psutil
except ImportError:
    psutil = None

console = Console()

COLUMNAS_CSV = [
    "timestamp", "label", "repeticion", "servidor", "ssid", "freq_mhz",
    "ancho_mhz", "senal_dbm", "tx_bitrate_mbps", "rx_bitrate_mbps",
    "rtt_medio_ms", "rtt_stdev_ms", "jitter_ms", "tput_mbps_app", "tput_mbps_so",
    "udp_enviados", "udp_recibidos", "udp_perdidos", "udp_perdida_pct", "udp_jitter_ms",
]

# Misma cabecera que en server.py: nº de secuencia, instante de envío y
# marca de "es el último paquete".
FORMATO_CABECERA_UDP = "!IdB"
TAM_CABECERA_UDP = struct.calcsize(FORMATO_CABECERA_UDP)

# Espera máxima por los resultados que devuelve el servidor tras la ráfaga
# UDP. Tiene que ser holgadamente mayor que el plazo de silencio que aplica
# el servidor (5 s): ese plazo se reinicia con cada datagrama que llega, así
# que si el enlace va lento el servidor puede tardar bastante más de 5 s en
# total. Con una espera igual, el cliente se rendía antes de que el servidor
# terminara de contar y la prueba se perdía.
TIEMPO_ESPERA_RESULTADOS_S = 20.0

# Pausa entre pruebas consecutivas de una misma repetición. La prueba de
# throughput satura el enlace durante diez segundos y deja las colas llenas;
# medir la pérdida UDP justo después caracterizaba esa recuperación en lugar
# del enlace en reposo. Medido en el mismo escenario: 9,5 % de pérdida con la
# prueba UDP aislada frente a 79 % lanzada inmediatamente después del
# throughput.
PAUSA_ENTRE_PRUEBAS_S = 3.0


def snapshot_iface(iface):
    if psutil is None or not iface:
        return None
    return psutil.net_io_counters(pernic=True).get(iface)


def leer_link_wifi(iface):
    """
    Lee el estado real del enlace WiFi (SSID, frecuencia, ancho, señal,
    tasas negociadas) con `iw dev <iface> link`, para dejar constancia de
    bajo qué parámetros se hizo cada medida sin tener que anotarlo a mano.
    """
    if not iface:
        return {}
    try:
        salida = subprocess.run(
            ["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=2
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}

    if "Not connected" in salida or not salida.strip():
        return {}

    datos = {}
    if m := re.search(r"SSID:\s*(.+)", salida):
        datos["ssid"] = m.group(1).strip()
    if m := re.search(r"freq:\s*([\d.]+)", salida):
        datos["freq_mhz"] = float(m.group(1))
    if m := re.search(r"signal:\s*(-?\d+)", salida):
        datos["senal_dbm"] = int(m.group(1))
    if m := re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", salida):
        datos["tx_bitrate_mbps"] = float(m.group(1))
    if m := re.search(r"rx bitrate:\s*([\d.]+)\s*MBit/s", salida):
        datos["rx_bitrate_mbps"] = float(m.group(1))

    # El ancho de canal se lee de `iw dev`, no de la línea de tasa de
    # `iw dev <iface> link`. Esa línea indica el ancho empleado en la última
    # trama transmitida, que con el enlace en reposo baja a 20 MHz aunque el
    # canal esté operando a 40 u 80; `iw dev` da el ancho de operación real.
    try:
        info = subprocess.run(
            ["iw", "dev"], capture_output=True, text=True, timeout=2
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return datos

    bloque = re.search(rf"Interface {re.escape(iface)}\b(.*?)(?=\n\tInterface |\Z)",
                       info, re.S)
    if bloque and (m := re.search(r"width:\s*(\d+)\s*MHz", bloque.group(1))):
        datos["ancho_mhz"] = int(m.group(1))
    return datos


def guardar_csv(ruta, filas):
    existe = os.path.exists(ruta)
    with open(ruta, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNAS_CSV, extrasaction="ignore")
        if not existe:
            writer.writeheader()
        for fila in filas:
            writer.writerow(fila)


def mostrar_resumen(filas):
    metricas = [
        ("rtt_medio_ms", "RTT medio (ms)"),
        ("jitter_ms", "Jitter (ms)"),
        ("tput_mbps_app", "Throughput (Mbps)"),
    ]
    tabla = Table(title=f"Resumen de {len(filas)} repeticiones")
    tabla.add_column("Métrica")
    tabla.add_column("Media", justify="right")
    tabla.add_column("Desv. típica", justify="right")

    for clave, nombre in metricas:
        valores = [f[clave] for f in filas if f.get(clave) is not None]
        if not valores:
            continue
        media = statistics.mean(valores)
        desviacion = statistics.pstdev(valores) if len(valores) > 1 else 0.0
        tabla.add_row(nombre, f"{media:.2f}", f"{desviacion:.2f}")

    console.print(tabla)


def prueba_rtt(ip, port, n_pings):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(b"RTT\n")
        time.sleep(0.1)  # separa el comando de los pings

        tiempos_ms = []
        for _ in range(n_pings):
            t0 = time.perf_counter()
            s.sendall(b"ping")
            eco = s.recv(1024)
            # Si el servidor cierra la conexión, recv() devuelve b"" al
            # instante y el tiempo medido sería de casi 0 ms. Registrarlo
            # como si fuera un RTT falsearía la serie entera, así que se
            # aborta en vez de anotar un valor que no es una medida.
            if not eco:
                raise ConnectionError(
                    "el servidor cerró la conexión durante la prueba de RTT; "
                    "comprueba que server.py sigue en marcha"
                )
            tiempos_ms.append((time.perf_counter() - t0) * 1000)

    media = statistics.mean(tiempos_ms)
    desviacion = statistics.pstdev(tiempos_ms)
    diffs = [abs(tiempos_ms[i] - tiempos_ms[i - 1]) for i in range(1, len(tiempos_ms))]
    jitter = statistics.mean(diffs) if diffs else 0.0

    tabla = Table(title="Medidas de RTT")
    tabla.add_column("#")
    tabla.add_column("RTT (ms)", justify="right")
    for i, t in enumerate(tiempos_ms, start=1):
        tabla.add_row(str(i), f"{t:.2f}")
    console.print(tabla)

    console.print(Panel(
        f"RTT medio:         {media:.2f} ms\n"
        f"Desviación típica: {desviacion:.2f} ms\n"
        f"Jitter (media de diferencias consecutivas): {jitter:.2f} ms",
        title="Resultados RTT / Jitter", style="bold blue"
    ))

    return {"rtt_medio_ms": media, "rtt_stdev_ms": desviacion, "jitter_ms": jitter}


def prueba_tput(ip, port, duracion, iface):
    chunk = b"x" * (1024 * 1024)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(b"TPUT\n")
        time.sleep(0.1)

        antes = snapshot_iface(iface)
        total_enviado = 0
        inicio = time.perf_counter()

        with Progress() as progress:
            tarea = progress.add_task("[cyan]Enviando ráfaga de datos...", total=duracion)
            while True:
                transcurrido = time.perf_counter() - inicio
                if transcurrido >= duracion:
                    break
                s.sendall(chunk)
                total_enviado += len(chunk)
                progress.update(tarea, completed=min(transcurrido, duracion))

    duracion_real = time.perf_counter() - inicio
    despues = snapshot_iface(iface)

    mbps = (total_enviado * 8) / (duracion_real * 1_000_000) if duracion_real > 0 else 0.0
    mbps_os = None
    texto = (
        f"Duración:   {duracion_real:.2f} s\n"
        f"Enviado:    {total_enviado / (1024 * 1024):.2f} MB\n"
        f"Throughput: [bold yellow]{mbps:.2f} Mbps[/bold yellow] (medido por la app, lado cliente)"
    )

    if antes and despues:
        delta_bytes = despues.bytes_sent - antes.bytes_sent
        mbps_os = (delta_bytes * 8) / (duracion_real * 1_000_000) if duracion_real > 0 else 0.0
        texto += f"\nThroughput: {mbps_os:.2f} Mbps (contado por el SO en {iface}, incluye cabeceras)"

    texto += (
        "\n\n[dim]La medida del servidor suele ser la más fiable: el emisor puede "
        "reportar datos ya entregados al buffer del socket, no necesariamente ya "
        "recibidos al otro lado.[/dim]"
    )

    console.print(Panel(texto, title="Resultados throughput (vista cliente)", style="bold blue"))

    return {"tput_mbps_app": mbps, "tput_mbps_so": mbps_os}


def prueba_udp(ip, port, n_paquetes, tam_paquete, pps):
    relleno = b"x" * max(tam_paquete - TAM_CABECERA_UDP, 0)
    intervalo = 1.0 / pps if pps > 0 else 0.0

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.connect((ip, port))
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp.sendall(f"UDP {n_paquetes}\n".encode())
        time.sleep(0.1)  # separa el comando de la ráfaga de datagramas

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            with Progress() as progress:
                tarea = progress.add_task("[cyan]Enviando paquetes UDP...", total=n_paquetes)
                for seq in range(n_paquetes):
                    es_fin = 1 if seq == n_paquetes - 1 else 0
                    cabecera = struct.pack(FORMATO_CABECERA_UDP, seq, time.time(), es_fin)
                    udp.sendto(cabecera + relleno, (ip, port))
                    progress.update(tarea, completed=seq + 1)
                    if intervalo:
                        time.sleep(intervalo)

        # UDP no confirma entrega, así que las estadísticas (cuántos
        # llegaron, pérdida, jitter) las calcula el servidor y las manda de
        # vuelta por el canal TCP, que sí es fiable.
        tcp.settimeout(TIEMPO_ESPERA_RESULTADOS_S)
        try:
            respuesta = tcp.recv(1024).decode()
        except socket.timeout:
            respuesta = ""

    resultado = dict(campo.split("=") for campo in respuesta.strip().split(";") if "=" in campo)

    # La pérdida y el jitter los calcula el servidor: sin su respuesta no
    # hay medida. Antes se rellenaba con valores por defecto, lo que
    # producía filas contradictorias (200 perdidos y un 0,00 % de pérdida)
    # que parecían datos y se escribían al CSV como tales.
    faltan = {"RECIBIDOS", "ENVIADOS", "PERDIDOS", "PERDIDA_PCT", "JITTER_MS"} - resultado.keys()
    if faltan:
        raise ConnectionError(
            "el servidor no devolvió los resultados de la prueba UDP "
            f"(faltan {', '.join(sorted(faltan))}); comprueba que server.py "
            "sigue en marcha y revisa su terminal por si ha fallado"
        )

    recibidos = int(resultado["RECIBIDOS"])
    enviados = int(resultado["ENVIADOS"])
    perdidos = int(resultado["PERDIDOS"])
    perdida_pct = float(resultado["PERDIDA_PCT"])
    jitter_ms = float(resultado["JITTER_MS"])

    console.print(Panel(
        f"Paquetes enviados:     {n_paquetes}\n"
        f"Recibidos (servidor):  {recibidos} / {enviados}\n"
        f"Perdidos:              [bold yellow]{perdidos} ({perdida_pct:.2f}%)[/bold yellow]\n"
        f"Jitter (RFC 3550):     {jitter_ms:.3f} ms",
        title="Resultados UDP (pérdida y jitter)", style="bold blue"
    ))

    return {
        "udp_enviados": enviados,
        "udp_recibidos": recibidos,
        "udp_perdidos": perdidos,
        "udp_perdida_pct": perdida_pct,
        "udp_jitter_ms": jitter_ms,
    }


def main():
    parser = argparse.ArgumentParser(description="Cliente de medida WLAN (RTT + throughput)")
    parser.add_argument("--server", required=True, help="IP del servidor")
    parser.add_argument("--port", type=int, default=5001, help="Puerto del servidor (por defecto: 5001)")
    parser.add_argument("--test", choices=["rtt", "tput", "udp", "both", "all"], default="both",
                         help="Qué prueba ejecutar ('both' = rtt+tput, 'all' = rtt+tput+udp)")
    parser.add_argument("--duration", type=int, default=10, help="Duración de la prueba de throughput en segundos")
    parser.add_argument("--pings", type=int, default=10, help="Número de medidas para la prueba de RTT")
    parser.add_argument("--udp-paquetes", type=int, default=200, help="Nº de datagramas UDP a enviar")
    parser.add_argument("--udp-tam", type=int, default=512, help="Tamaño de cada datagrama UDP en bytes")
    parser.add_argument("--udp-rate", type=float, default=200, help="Paquetes UDP por segundo a enviar")
    parser.add_argument("--iface", default=None, help="Interfaz a monitorizar (ej. wlp8s0), opcional")
    parser.add_argument("--repeat", type=int, default=1, help="Repite la prueba completa N veces (por defecto: 1)")
    parser.add_argument("--label", default="", help="Etiqueta del escenario (ej. canal36_ancho80_5g)")
    parser.add_argument("--csv", default=None, help="Archivo CSV donde acumular los resultados de cada repetición")
    args = parser.parse_args()

    console.print(Panel(f"Conectando a {args.server}:{args.port}", title="Cliente WLAN", style="bold green"))

    filas = []
    for rep in range(1, args.repeat + 1):
        if args.repeat > 1:
            console.rule(f"[bold]Repetición {rep}/{args.repeat}[/bold]")

        fila = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "label": args.label,
            "repeticion": rep,
            "servidor": args.server,
        }
        fila.update(leer_link_wifi(args.iface))

        if args.test in ("rtt", "both", "all"):
            console.rule("Prueba RTT / Jitter")
            fila.update(prueba_rtt(args.server, args.port, args.pings))

        if args.test in ("tput", "both", "all"):
            if args.test in ("both", "all"):
                time.sleep(PAUSA_ENTRE_PRUEBAS_S)
            console.rule("Prueba Throughput")
            fila.update(prueba_tput(args.server, args.port, args.duration, args.iface))

        if args.test in ("udp", "all"):
            if args.test == "all":
                # Deja que el enlace se asiente tras la ráfaga de throughput.
                time.sleep(PAUSA_ENTRE_PRUEBAS_S)
            console.rule("Prueba UDP (pérdida y jitter)")
            fila.update(prueba_udp(args.server, args.port, args.udp_paquetes, args.udp_tam, args.udp_rate))

        filas.append(fila)

    if args.csv:
        guardar_csv(args.csv, filas)
        console.print(f"[green]Resultados guardados en {args.csv}[/green]")

    if args.repeat > 1:
        mostrar_resumen(filas)


if __name__ == "__main__":
    main()
