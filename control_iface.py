#!/usr/bin/env python3
"""
Consulta y control de la interfaz WLAN local desde la propia aplicación.

Complementa a control_ap.py: aquel configura el punto de acceso (canal,
ancho, potencia del AP), y este actúa sobre la interfaz de la propia
máquina a través de las utilidades que ofrece el sistema operativo, que
son las descritas en el apartado 2.6 de la memoria.

Se apoya en `iw`, la utilidad que habla nl80211 de forma nativa, y en
`nmcli` para las operaciones que implican al gestor de conexiones.

Reparto de permisos: consultar el estado no requiere privilegios;
modificarlo sí (capacidad CAP_NET_ADMIN, en la práctica ejecutar como
root). El módulo lo comprueba y lo dice claramente en vez de fallar con un
mensaje críptico.

Uso:
    # solo lectura, sin privilegios
    python3 control_iface.py --iface wlp8s0 --estado
    python3 control_iface.py --iface wlp8s0 --capacidades
    python3 control_iface.py --regulador

    # requieren root
    sudo python3 control_iface.py --iface wlp8s0 --potencia 15
    sudo python3 control_iface.py --regulador-set ES
"""
import argparse
import os
import re
import subprocess
import sys

TIEMPO_MAXIMO = 5  # segundos por orden


class ErrorIface(Exception):
    """Fallo al consultar o configurar la interfaz."""


# ----------------------------------------------------------------------
# Utilidades base
# ----------------------------------------------------------------------
def _ejecutar(orden, permitir_fallo=False):
    """Lanza una orden y devuelve su salida estándar."""
    try:
        proceso = subprocess.run(
            orden, capture_output=True, text=True, timeout=TIEMPO_MAXIMO
        )
    except FileNotFoundError:
        raise ErrorIface(f"no se encontró la orden «{orden[0]}»; ¿está instalada?")
    except subprocess.TimeoutExpired:
        raise ErrorIface(f"«{' '.join(orden)}» no respondió en {TIEMPO_MAXIMO} s")

    if proceso.returncode != 0 and not permitir_fallo:
        detalle = (proceso.stderr or proceso.stdout).strip()
        raise ErrorIface(f"«{' '.join(orden)}» falló: {detalle}")
    return proceso.stdout


def hay_privilegios():
    return os.geteuid() == 0


def _exigir_privilegios(accion):
    if not hay_privilegios():
        raise ErrorIface(
            f"{accion} requiere privilegios de administración "
            f"(CAP_NET_ADMIN). Vuelve a lanzarlo con sudo."
        )


# ----------------------------------------------------------------------
# Consulta
# ----------------------------------------------------------------------
def listar_interfaces():
    """Interfaces inalámbricas presentes, con el dispositivo físico de cada una."""
    salida = _ejecutar(["iw", "dev"])
    interfaces = []
    phy_actual = None
    for linea in salida.splitlines():
        limpia = linea.strip()
        if limpia.startswith("phy#"):
            phy_actual = "phy" + limpia[4:]
        elif limpia.startswith("Interface "):
            interfaces.append({"iface": limpia.split(None, 1)[1], "phy": phy_actual})
        elif interfaces and limpia.startswith("type "):
            interfaces[-1]["modo"] = limpia.split(None, 1)[1]
        elif interfaces and limpia.startswith("channel "):
            # channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz
            if m := re.search(r"channel\s+(\d+)", limpia):
                interfaces[-1]["canal"] = int(m.group(1))
            if m := re.search(r"width:\s*(\d+)\s*MHz", limpia):
                interfaces[-1]["ancho_mhz"] = int(m.group(1))
        elif interfaces and limpia.startswith("txpower "):
            if m := re.search(r"txpower\s+([\d.]+)", limpia):
                interfaces[-1]["potencia_dbm"] = float(m.group(1))
    return interfaces


def estado_enlace(iface):
    """
    Estado instantáneo del enlace: `iw dev <iface> link`.

    Es la misma información que client.py registra junto a cada medida.
    """
    salida = _ejecutar(["iw", "dev", iface, "link"], permitir_fallo=True)
    if "Not connected" in salida or not salida.strip():
        return {}

    datos = {}
    if m := re.search(r"Connected to ([0-9a-f:]{17})", salida):
        datos["bssid"] = m.group(1)
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

    # El ancho de canal se toma de `iw dev`, no de la línea de tasa: aquella
    # indica el ancho de la última trama transmitida, que con el enlace en
    # reposo cae a 20 MHz aunque el canal opere a 40 u 80.
    for entrada in listar_interfaces():
        if entrada["iface"] != iface:
            continue
        if entrada.get("ancho_mhz"):
            datos["ancho_mhz"] = entrada["ancho_mhz"]
        if entrada.get("canal"):
            datos["canal"] = entrada["canal"]
        break
    return datos


def estadisticas_estacion(iface):
    """
    Estadísticas acumuladas del enlace con el AP: `iw dev <iface> station dump`.

    Aporta datos que `link` no da y que resultan valiosos al interpretar las
    medidas: los reintentos y las tramas fallidas cuantifican las
    retransmisiones de capa de enlace, y el preámbulo es uno de los
    parámetros de interés del estudio.
    """
    salida = _ejecutar(["iw", "dev", iface, "station", "dump"], permitir_fallo=True)
    if not salida.strip():
        return {}

    datos = {}
    if m := re.search(r"Station ([0-9a-f:]{17})", salida):
        datos["bssid"] = m.group(1)

    enteros = {
        "rx bytes": "rx_bytes", "tx bytes": "tx_bytes",
        "rx packets": "rx_paquetes", "tx packets": "tx_paquetes",
        "tx retries": "tx_reintentos", "tx failed": "tx_fallidas",
        "beacon loss": "beacons_perduts", "rx drop misc": "rx_descartats",
    }
    for etiqueta, clave in enteros.items():
        if m := re.search(rf"{re.escape(etiqueta)}:\s*(\d+)", salida):
            datos[clave] = int(m.group(1))

    if m := re.search(r"signal:\s*(-?\d+)", salida):
        datos["senal_dbm"] = int(m.group(1))
    if m := re.search(r"signal avg:\s*(-?\d+)", salida):
        datos["senal_media_dbm"] = int(m.group(1))
    if m := re.search(r"preamble:\s*(\w+)", salida):
        datos["preambulo"] = m.group(1)

    for sentido, clave in (("tx", "tx"), ("rx", "rx")):
        patron = rf"{sentido} bitrate:\s*([\d.]+)\s*MBit/s(.*)"
        if m := re.search(patron, salida):
            datos[f"{clave}_bitrate_mbps"] = float(m.group(1))
            resto = m.group(2)
            if mm := re.search(r"(?:VHT-|HE-|EHT-)?MCS\s+(\d+)", resto):
                datos[f"{clave}_mcs"] = int(mm.group(1))
            if mm := re.search(r"(?:VHT-|HE-|EHT-)?NSS\s+(\d+)", resto):
                datos[f"{clave}_nss"] = int(mm.group(1))
            if mm := re.search(r"(\d+)MHz", resto):
                datos[f"{clave}_ancho_mhz"] = int(mm.group(1))

    # Proporción de reintentos: indicador directo de la calidad del enlace
    # a nivel MAC, independiente de lo que vean las capas superiores.
    if datos.get("tx_paquetes"):
        reintentos = datos.get("tx_reintentos", 0)
        datos["tx_reintentos_pct"] = round(
            100.0 * reintentos / (datos["tx_paquetes"] + reintentos), 3
        )
    return datos


def capacidades(phy):
    """
    Capacidades estáticas del adaptador: `iw phy <phy> info`.

    Determina qué escenarios experimentales son viables antes de plantearlos:
    modos admitidos, bandas, canales disponibles y potencia máxima permitida
    en cada uno.
    """
    salida = _ejecutar(["iw", "phy", phy, "info"])

    modos = []
    if m := re.search(r"Supported interface modes:\n((?:\s+\*.*\n)+)", salida):
        modos = re.findall(r"\*\s*(\S+)", m.group(1))

    bandas = []
    for bloque in re.finditer(r"Band (\d+):(.*?)(?=\n\tBand \d+:|\Z)", salida, re.S):
        canales = []
        for c in re.finditer(
            r"\*\s*([\d.]+)\s*MHz\s*\[(\d+)\]\s*\(([^)]*)\)", bloque.group(2)
        ):
            estado = c.group(3)
            canales.append({
                "freq_mhz": float(c.group(1)),
                "canal": int(c.group(2)),
                "potencia_max_dbm": (
                    None if "disabled" in estado
                    else float(re.sub(r"[^\d.]", "", estado.split()[0]) or 0)
                ),
                "disponible": "disabled" not in estado,
                "radar": "radar detection" in bloque.group(2)[:0] or "DFS" in estado,
            })
        bandas.append({"banda": int(bloque.group(1)), "canales": canales})

    return {"phy": phy, "modos": modos, "bandas": bandas}


def dominio_regulador():
    """Dominio regulador vigente y sus límites: `iw reg get`."""
    salida = _ejecutar(["iw", "reg", "get"])
    pais = None
    if m := re.search(r"country ([A-Z]{2}):", salida):
        pais = m.group(1)

    reglas = []
    for m in re.finditer(
        r"\((\d+)\s*-\s*(\d+)\s*@\s*(\d+)\),\s*\([^,]*,\s*(\d+)\)(.*)", salida
    ):
        reglas.append({
            "desde_mhz": int(m.group(1)),
            "hasta_mhz": int(m.group(2)),
            "ancho_max_mhz": int(m.group(3)),
            "potencia_max_dbm": int(m.group(4)),
            "dfs": "DFS" in m.group(5),
            "solo_interior": "NO-OUTDOOR" in m.group(5),
        })
    return {"pais": pais, "reglas": reglas}


# ----------------------------------------------------------------------
# Configuración (requiere privilegios)
# ----------------------------------------------------------------------
def fijar_potencia(iface, dbm):
    """
    Fija la potencia de transmisión de la interfaz local.

    `iw` trabaja en mBm (milibelios-milivatio), es decir, dBm × 100. El
    valor solicitado queda acotado por el máximo que permita el dominio
    regulador para el canal en uso, así que conviene releer el estado
    después de aplicarlo.
    """
    _exigir_privilegios("fijar la potencia de transmisión")

    if str(dbm).lower() == "auto":
        _ejecutar(["iw", "dev", iface, "set", "txpower", "auto"])
        return "auto"

    mbm = int(round(float(dbm) * 100))
    _ejecutar(["iw", "dev", iface, "set", "txpower", "fixed", str(mbm)])
    return float(dbm)


def potencia_actual(iface):
    for entrada in listar_interfaces():
        if entrada["iface"] == iface:
            return entrada.get("potencia_dbm")
    raise ErrorIface(f"la interfaz «{iface}» no existe o no es inalámbrica")


def fijar_dominio_regulador(pais):
    """
    Cambia el dominio regulador, que determina qué canales y potencias son
    legales. El cambio es global al sistema, no por interfaz.
    """
    _exigir_privilegios("cambiar el dominio regulador")
    if not re.fullmatch(r"[A-Za-z]{2}", pais):
        raise ErrorIface(f"«{pais}» no es un código de país ISO de dos letras")
    _ejecutar(["iw", "reg", "set", pais.upper()])
    return pais.upper()


def fijar_modo(iface, modo):
    """
    Cambia el modo de la interfaz (managed, monitor...).

    Exige bajar la interfaz antes de cambiarla y volver a levantarla, lo
    que interrumpe cualquier comunicación en curso.
    """
    _exigir_privilegios("cambiar el modo de la interfaz")
    validos = ("managed", "monitor", "ibss")
    if modo not in validos:
        raise ErrorIface(f"modo «{modo}» no contemplado (admitidos: {validos})")

    _ejecutar(["ip", "link", "set", iface, "down"])
    try:
        _ejecutar(["iw", "dev", iface, "set", "type", modo])
    finally:
        _ejecutar(["ip", "link", "set", iface, "up"], permitir_fallo=True)
    return modo


def asociar_a_bssid(nombre_conexion, bssid):
    """
    Fuerza la asociación a un BSSID concreto mediante NetworkManager.

    En un AP de doble banda, cada radio anuncia su propio BSSID, de modo que
    fijarlo equivale a elegir en qué banda y canal se asocia la estación —
    algo que de otro modo decide el gestor de conexiones por su cuenta.
    """
    _ejecutar([
        "nmcli", "connection", "modify", nombre_conexion,
        "802-11-wireless.bssid", bssid,
    ])
    _ejecutar(["nmcli", "connection", "up", nombre_conexion])
    return bssid


# ----------------------------------------------------------------------
# Interfaz de línea de órdenes
# ----------------------------------------------------------------------
def _mostrar_estado(iface):
    print(f"\nInterfaz: {iface}")
    for entrada in listar_interfaces():
        if entrada["iface"] == iface:
            print(f"  dispositivo físico: {entrada.get('phy')}")
            print(f"  modo:               {entrada.get('modo')}")
            print(f"  potencia:           {entrada.get('potencia_dbm')} dBm")

    enlace = estado_enlace(iface)
    if not enlace:
        print("  (no asociada a ninguna red)\n")
        return
    print(f"\n  Enlace")
    print(f"    SSID:     {enlace.get('ssid')}")
    print(f"    BSSID:    {enlace.get('bssid')}")
    print(f"    frec.:    {enlace.get('freq_mhz')} MHz")
    print(f"    canal:    {enlace.get('canal')}   ancho: {enlace.get('ancho_mhz')} MHz")
    print(f"    señal:    {enlace.get('senal_dbm')} dBm")
    print(f"    tasa TX:  {enlace.get('tx_bitrate_mbps')} Mbit/s")
    print(f"    tasa RX:  {enlace.get('rx_bitrate_mbps')} Mbit/s")

    est = estadisticas_estacion(iface)
    if est:
        print(f"\n  Estadísticas de capa MAC")
        print(f"    preámbulo:        {est.get('preambulo')}")
        print(f"    MCS / NSS (TX):   {est.get('tx_mcs')} / {est.get('tx_nss')}")
        print(f"    señal media:      {est.get('senal_media_dbm')} dBm")
        print(f"    paquetes TX:      {est.get('tx_paquetes')}")
        print(f"    reintentos TX:    {est.get('tx_reintentos')} "
              f"({est.get('tx_reintentos_pct')} %)")
        print(f"    tramas fallidas:  {est.get('tx_fallidas')}")
        print(f"    beacons perdidos: {est.get('beacons_perduts')}")
    print()


def _mostrar_capacidades(phy):
    cap = capacidades(phy)
    print(f"\nDispositivo físico: {cap['phy']}")
    print(f"  Modos admitidos: {', '.join(cap['modos'])}")
    if "AP" in cap["modos"]:
        print("  → admite modo AP: el equipo puede actuar como punto de acceso")
    for banda in cap["bandas"]:
        libres = [c for c in banda["canales"] if c["disponible"]]
        if not libres:
            continue
        maxima = max(c["potencia_max_dbm"] or 0 for c in libres)
        print(f"\n  Banda {banda['banda']}: {len(libres)} canales disponibles, "
              f"potencia máxima {maxima} dBm")
        print("    canales: " + ", ".join(str(c["canal"]) for c in libres))
    print()


def _mostrar_regulador():
    reg = dominio_regulador()
    print(f"\nDominio regulador: {reg['pais']}")
    for r in reg["reglas"]:
        marcas = []
        if r["dfs"]:
            marcas.append("DFS")
        if r["solo_interior"]:
            marcas.append("solo interior")
        extra = f"  [{', '.join(marcas)}]" if marcas else ""
        print(f"  {r['desde_mhz']}–{r['hasta_mhz']} MHz  "
              f"ancho máx. {r['ancho_max_mhz']} MHz  "
              f"potencia máx. {r['potencia_max_dbm']} dBm{extra}")
    print()


def main():
    p = argparse.ArgumentParser(
        description="Consulta y control de la interfaz WLAN local."
    )
    p.add_argument("--iface", default=None, help="interfaz inalámbrica (p. ej. wlp8s0)")
    p.add_argument("--phy", default=None, help="dispositivo físico (p. ej. phy0)")

    p.add_argument("--listar", action="store_true", help="interfaces inalámbricas")
    p.add_argument("--estado", action="store_true", help="estado del enlace y de la MAC")
    p.add_argument("--capacidades", action="store_true", help="qué admite el adaptador")
    p.add_argument("--regulador", action="store_true", help="dominio regulador vigente")

    p.add_argument("--potencia", default=None, help="fija la potencia en dBm, o «auto»")
    p.add_argument("--regulador-set", default=None, metavar="PAIS",
                   help="cambia el dominio regulador (código ISO, p. ej. ES)")
    p.add_argument("--modo", default=None, help="managed | monitor | ibss")

    args = p.parse_args()

    try:
        if args.listar or not any(vars(args).values()):
            print()
            for e in listar_interfaces():
                print(f"  {e['iface']}  ({e.get('phy')}, modo {e.get('modo')}, "
                      f"{e.get('potencia_dbm')} dBm)")
            print()
            return 0

        if args.regulador:
            _mostrar_regulador()
        if args.regulador_set:
            nuevo = fijar_dominio_regulador(args.regulador_set)
            print(f"Dominio regulador cambiado a {nuevo}.")
            _mostrar_regulador()

        if args.capacidades:
            phy = args.phy
            if phy is None and args.iface:
                phy = next(
                    (e.get("phy") for e in listar_interfaces()
                     if e["iface"] == args.iface), None
                )
            if phy is None:
                raise ErrorIface("indica --phy o --iface para ver las capacidades")
            _mostrar_capacidades(phy)

        if args.potencia is not None:
            if not args.iface:
                raise ErrorIface("indica la interfaz con --iface")
            antes = potencia_actual(args.iface)
            fijar_potencia(args.iface, args.potencia)
            despues = potencia_actual(args.iface)
            print(f"Potencia: {antes} dBm → {despues} dBm")

        if args.modo:
            if not args.iface:
                raise ErrorIface("indica la interfaz con --iface")
            fijar_modo(args.iface, args.modo)
            print(f"Modo de {args.iface} cambiado a {args.modo}.")

        if args.estado:
            if not args.iface:
                raise ErrorIface("indica la interfaz con --iface")
            _mostrar_estado(args.iface)

    except ErrorIface as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
