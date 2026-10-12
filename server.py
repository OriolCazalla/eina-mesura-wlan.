#!/usr/bin/env python3
"""
Servidor de medida WLAN. Fusiona las dos versiones anteriores:
  - Protocolo de comandos y prueba RTT de "VERSION 1".
  - Interfaz rich y CLI configurable de "VERSION INICIAL".

Atiende tres tipos de prueba según lo que pida el cliente:
  - RTT  : modo eco, para medir retardo ida-vuelta y jitter (TCP).
  - TPUT : recibe un bloque de datos y calcula el throughput (TCP).
  - UDP  : recibe una ráfaga de datagramas numerados y calcula pérdida
           de paquetes y jitter (RFC 3550) sobre UDP.

Uso:
    python3 server.py --host 0.0.0.0 --port 5001 --iface wlp8s0
"""
import argparse
import socket
import struct
import time

from rich.console import Console
from rich.panel import Panel

try:
    import psutil
except ImportError:
    psutil = None

console = Console()

# Cabecera de cada datagrama UDP: nº de secuencia (I, 4 bytes), instante de
# envío (d, 8 bytes) y marca de "es el último paquete" (B, 1 byte).
FORMATO_CABECERA_UDP = "!IdB"
TAM_CABECERA_UDP = struct.calcsize(FORMATO_CABECERA_UDP)

# Tras recibir el paquete marcado como último se sigue escuchando durante
# este margen por si quedan rezagados. Es necesario porque el orden de
# llegada no está garantizado: un punto de acceso que retiene tramas para
# una estación en ahorro de energía las entrega a ráfagas, y el último
# paquete puede adelantar a otros que aún están encolados. Sin este margen
# esos paquetes se contarían como perdidos aunque acaben llegando.
MARGEN_REZAGADOS_S = 0.3


def leer_comando(conn):
    # Se lee byte a byte hasta el salto de línea en vez de pedir un bloque
    # de 64: TCP es un flujo de bytes y no conserva los límites de los
    # envíos, de modo que un recv() grande puede llevarse el comando y,
    # pegado detrás, el principio de los datos de la prueba. Cuando eso
    # pasaba, el primer "ping" desaparecía y el cliente se quedaba
    # bloqueado esperando un eco que el servidor ya se había comido.
    trozos = []
    while True:
        byte = conn.recv(1)
        if not byte or byte == b"\n":
            break
        trozos.append(byte)
    return b"".join(trozos).decode(errors="ignore").strip().upper()


def snapshot_iface(iface):
    if psutil is None or not iface:
        return None
    return psutil.net_io_counters(pernic=True).get(iface)


def atender_rtt(conn):
    console.print("[yellow]Modo RTT (eco): devolviendo cada paquete recibido.[/yellow]")
    while True:
        datos = conn.recv(1024)
        if not datos:
            break
        conn.sendall(datos)
    console.print("[cyan]Prueba RTT finalizada.[/cyan]")


def atender_tput(conn, addr, iface):
    console.print("[yellow]Modo TPUT: recibiendo datos...[/yellow]")
    antes = snapshot_iface(iface)
    total = 0
    inicio = time.perf_counter()

    while True:
        datos = conn.recv(1024 * 1024)
        if not datos:
            break
        total += len(datos)

    duracion = time.perf_counter() - inicio
    despues = snapshot_iface(iface)

    mbps = (total * 8) / (duracion * 1_000_000) if duracion > 0 else 0.0
    texto = (
        f"Duración:   {duracion:.2f} s\n"
        f"Recibido:   {total / (1024 * 1024):.2f} MB\n"
        f"Throughput: [bold yellow]{mbps:.2f} Mbps[/bold yellow] (medido por la app)"
    )

    if antes and despues:
        delta_bytes = despues.bytes_recv - antes.bytes_recv
        mbps_os = (delta_bytes * 8) / (duracion * 1_000_000) if duracion > 0 else 0.0
        texto += f"\nThroughput: {mbps_os:.2f} Mbps (contado por el SO en {iface}, incluye cabeceras)"

    console.print(Panel(texto, title=f"Resultados TPUT de {addr[0]}", style="bold blue"))


def atender_udp(conn, addr, udp_sock, n_paquetes):
    console.print(f"[yellow]Modo UDP: esperando {n_paquetes} paquetes...[/yellow]")

    # Se guardan los números de secuencia vistos en lugar de contar
    # llegadas: si un datagrama se duplicara por el camino, contar llegadas
    # incrementaría el total y enmascararía una pérdida real.
    vistos = set()
    jitter = 0.0
    transito_anterior = None
    udp_sock.settimeout(5.0)
    fin_recibido = False

    while len(vistos) < n_paquetes:
        try:
            datos, origen = udp_sock.recvfrom(65536)
        except socket.timeout:
            if not fin_recibido:
                console.print("[red]Timeout esperando paquetes UDP (se han perdido los últimos).[/red]")
            break

        if origen[0] != addr[0]:
            continue  # paquete de otro origen: se ignora (un cliente a la vez)

        seq, ts_envio, es_fin = struct.unpack(FORMATO_CABECERA_UDP, datos[:TAM_CABECERA_UDP])
        ts_llegada = time.time()
        duplicado = seq in vistos
        vistos.add(seq)

        # Jitter según el estimador de la RFC 3550 (6.4.1): la diferencia
        # entre los tiempos de tránsito de dos paquetes consecutivos. No
        # hace falta sincronizar los relojes de las dos máquinas: al restar
        # dos tránsitos calculados con el mismo par de relojes, el desfase
        # constante entre ellos se cancela.
        if not duplicado:
            transito = ts_llegada - ts_envio
            if transito_anterior is not None:
                diferencia = abs(transito - transito_anterior)
                jitter += (diferencia - jitter) / 16
            transito_anterior = transito

        if es_fin and not fin_recibido:
            # Ha llegado el último paquete, pero puede haber rezagados en
            # camino. Se acorta el plazo de espera y se sigue escuchando.
            fin_recibido = True
            udp_sock.settimeout(MARGEN_REZAGADOS_S)

    recibidos = len(vistos)
    perdidos = max(n_paquetes - recibidos, 0)
    perdida_pct = (perdidos / n_paquetes * 100) if n_paquetes > 0 else 0.0

    console.print(Panel(
        f"Recibidos: {recibidos} / {n_paquetes}\n"
        f"Perdidos:  [bold yellow]{perdidos} ({perdida_pct:.2f}%)[/bold yellow]\n"
        f"Jitter (RFC 3550): {jitter * 1000:.3f} ms",
        title=f"Resultados UDP de {addr[0]}", style="bold blue"
    ))

    respuesta = (
        f"RECIBIDOS={recibidos};ENVIADOS={n_paquetes};PERDIDOS={perdidos};"
        f"PERDIDA_PCT={perdida_pct:.2f};JITTER_MS={jitter * 1000:.3f}\n"
    )
    conn.sendall(respuesta.encode())


def atender(conn, addr, iface, udp_sock):
    comando = leer_comando(conn)
    partes = comando.split()
    tipo = partes[0] if partes else ""
    console.print(f"\n[bold green]Conexión de {addr}, prueba solicitada: {comando}[/bold green]")

    if tipo == "RTT":
        atender_rtt(conn)
    elif tipo == "TPUT":
        atender_tput(conn, addr, iface)
    elif tipo == "UDP":
        n_paquetes = int(partes[1]) if len(partes) > 1 else 0
        atender_udp(conn, addr, udp_sock, n_paquetes)
    else:
        console.print(f"[red]Comando no reconocido: '{comando}'[/red]")


def start_server(host, port, iface):
    console.print(Panel(f"Servidor de medida WLAN en {host}:{port}", title="Servidor WLAN", style="bold green"))

    # TCP: canal de control (negociación RTT/TPUT/UDP) y datos de RTT/TPUT.
    # UDP: mismo número de puerto, pero es un socket y un espacio de
    # direcciones completamente aparte (TCP y UDP no comparten puerto entre
    # sí), usado solo para la ráfaga de datos de la prueba UDP.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, \
         socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_sock:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen()
        udp_sock.bind((host, port))
        console.print("[yellow]Servidor listo. Esperando conexiones... (Ctrl+C para salir)[/yellow]")

        while True:
            try:
                conn, addr = s.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with conn:
                    atender(conn, addr, iface, udp_sock)
                console.print("[cyan]Cliente desconectado. Esperando nueva conexión...[/cyan]")
            except KeyboardInterrupt:
                console.print("\n[bold red]Cerrando el servidor...[/bold red]")
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Servidor de medida WLAN (RTT + throughput)")
    parser.add_argument("--host", default="0.0.0.0", help="IP donde escuchar (por defecto: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5001, help="Puerto donde escuchar (por defecto: 5001)")
    parser.add_argument("--iface", default=None, help="Interfaz a monitorizar con psutil (ej. wlp8s0), opcional")
    args = parser.parse_args()

    start_server(args.host, args.port, args.iface)
