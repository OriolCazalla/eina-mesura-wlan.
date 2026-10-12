#!/usr/bin/env python3
"""
Control del punto de acceso UniFi desde la propia aplicación.

Permite leer y modificar los parámetros de radio del AP (canal, ancho de
canal y potencia de transmisión) hablando con la API REST de la UniFi
Network Application, en vez de tener que tocarlos a mano en su interfaz web.

Es la pieza que hace que la aplicación no solo mida, sino que también
configure el enlace sobre el que mide.

Uso desde la línea de órdenes:

    export UNIFI_USER="tu_usuario"
    export UNIFI_PASS="tu_contraseña"

    # ver qué APs conoce la controladora y cómo están configurados ahora
    python3 control_ap.py --listar

    # cambiar la radio de 5 GHz al canal 36, 40 MHz de ancho y 15 dBm
    python3 control_ap.py --banda 5 --canal 36 --ancho 40 --potencia 15

Nota sobre la capa de transporte: el motor de medida de esta aplicación
(client.py / server.py) trabaja directamente sobre sockets TCP y UDP. Este
módulo es plano de control, no de medida, y se apoya en http.client, que
forma parte de la biblioteca estándar y se asienta a su vez sobre un socket
TCP envuelto en TLS por el módulo ssl. No se emplea ninguna dependencia
externa.
"""
import argparse
import http.client
import json
import os
import ssl
import sys
import time

# La radio de 2,4 GHz aparece en la API como "ng" y la de 5 GHz como "na".
BANDAS = {"2.4": "ng", "2,4": "ng", "24": "ng", "5": "na"}

# Anchos admitidos en cada banda, en MHz.
ANCHOS_VALIDOS = {"ng": (20, 40), "na": (20, 40, 80, 160)}


class ErrorUnifi(Exception):
    """Cualquier fallo al hablar con la controladora."""


class ControladorUnifi:
    """
    Sesión contra la UniFi Network Application.

    La controladora usa un certificado autofirmado, así que por defecto no
    se valida la cadena TLS: es un servicio local levantado por nosotros
    mismos y no hay ninguna autoridad que lo haya firmado.
    """

    def __init__(self, host="localhost", puerto=8443, usuario=None,
                 password=None, sitio="default", verificar_tls=False):
        self.host = host
        self.puerto = puerto
        self.usuario = usuario
        self.password = password
        self.sitio = sitio
        self.cookies = {}
        self.csrf = None

        if verificar_tls:
            self.contexto = ssl.create_default_context()
        else:
            self.contexto = ssl._create_unverified_context()

    # ------------------------------------------------------------------
    # Capa de transporte
    # ------------------------------------------------------------------
    def _peticion(self, metodo, ruta, cuerpo=None):
        """
        Lanza una petición HTTPS y devuelve el campo "data" de la respuesta.

        La controladora responde siempre con un sobre del tipo
        {"meta": {"rc": "ok"|"error", ...}, "data": [...]}, así que se
        comprueba "meta.rc" antes de devolver nada.
        """
        conexion = http.client.HTTPSConnection(
            self.host, self.puerto, context=self.contexto, timeout=15
        )

        cabeceras = {"Content-Type": "application/json"}
        if self.cookies:
            cabeceras["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies.items()
            )
        # Las versiones recientes exigen el testigo CSRF en toda petición
        # que modifique estado.
        if self.csrf:
            cabeceras["X-CSRF-Token"] = self.csrf

        datos = json.dumps(cuerpo) if cuerpo is not None else None

        try:
            conexion.request(metodo, ruta, body=datos, headers=cabeceras)
            respuesta = conexion.getresponse()
            crudo = respuesta.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise ErrorUnifi(
                f"no se pudo contactar con la controladora en "
                f"{self.host}:{self.puerto} ({e})"
            ) from e
        finally:
            conexion.close()

        self._guardar_cookies(respuesta)
        if respuesta.getheader("X-CSRF-Token"):
            self.csrf = respuesta.getheader("X-CSRF-Token")

        try:
            payload = json.loads(crudo)
        except json.JSONDecodeError:
            raise ErrorUnifi(
                f"respuesta no interpretable (HTTP {respuesta.status}): "
                f"{crudo[:200]}"
            )

        meta = payload.get("meta", {})
        if meta.get("rc") != "ok":
            raise ErrorUnifi(
                f"la controladora rechazó la petición: "
                f"{meta.get('msg', 'sin detalle')} (HTTP {respuesta.status})"
            )
        return payload.get("data", [])

    def _guardar_cookies(self, respuesta):
        """Acumula las cookies de sesión que va devolviendo la controladora."""
        for nombre, valor in respuesta.getheaders():
            if nombre.lower() != "set-cookie":
                continue
            for trozo in valor.split(","):
                par = trozo.split(";")[0].strip()
                if "=" not in par:
                    continue
                clave, dato = par.split("=", 1)
                clave, dato = clave.strip(), dato.strip()
                if not dato:
                    continue
                self.cookies[clave] = dato
                # El testigo CSRF viaja como una cookie más.
                if clave.lower() in ("csrf_token", "x-csrf-token"):
                    self.csrf = dato

    # ------------------------------------------------------------------
    # Sesión
    # ------------------------------------------------------------------
    def iniciar_sesion(self):
        if not self.usuario or not self.password:
            raise ErrorUnifi(
                "faltan credenciales: define UNIFI_USER y UNIFI_PASS "
                "o pásalas con --usuario/--password"
            )
        self._peticion(
            "POST", "/api/login",
            {"username": self.usuario, "password": self.password},
        )
        return self

    def cerrar_sesion(self):
        try:
            self._peticion("POST", "/api/logout")
        except ErrorUnifi:
            pass  # cerrar sesión nunca debe hacer fallar al programa

    def __enter__(self):
        return self.iniciar_sesion()

    def __exit__(self, *_):
        self.cerrar_sesion()
        return False

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------
    def listar_aps(self):
        """Devuelve los puntos de acceso que la controladora tiene adoptados."""
        dispositivos = self._peticion(
            "GET", f"/api/s/{self.sitio}/stat/device"
        )
    
        return [d for d in dispositivos if d.get("type") == "uap"]

    def buscar_ap(self, identificador=None):
        """
        Localiza un AP por nombre, MAC o IP. Sin identificador, devuelve el
        único que haya
        """
        aps = self.listar_aps()
        if not aps:
            raise ErrorUnifi("la controladora no tiene ningún AP adoptado")

        if identificador is None:
            if len(aps) > 1:
                nombres = ", ".join(a.get("name", a.get("mac", "?")) for a in aps)
                raise ErrorUnifi(
                    f"hay {len(aps)} APs adoptados ({nombres}); "
                    f"indica cuál con --ap"
                )
            return aps[0]

        buscado = identificador.lower()
        for ap in aps:
            campos = (
                str(ap.get("name", "")).lower(),
                str(ap.get("mac", "")).lower(),
                str(ap.get("ip", "")).lower(),
            )
            if buscado in campos:
                return ap
        raise ErrorUnifi(f"no se encontró ningún AP que coincida con «{identificador}»")

    @staticmethod
    def estado_radios(ap):
        """Extrae la configuración actual de cada radio del AP."""
        estado = []
        for radio in ap.get("radio_table", []):
            estado.append({
                "banda": "2.4 GHz" if radio.get("radio") == "ng" else "5 GHz",
                "radio": radio.get("radio"),
                "canal": radio.get("channel"),
                "ancho_mhz": radio.get("ht"),
                "modo_potencia": radio.get("tx_power_mode"),
                "potencia_dbm": radio.get("tx_power"),
            })
        return estado

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------
    def configurar_radio(self, ap, banda, canal=None, ancho=None, potencia=None):
        """
        Modifica canal, ancho y/o potencia de una radio del AP.

        Se parte de la radio_table que ya tiene el dispositivo y solo se
        cambian los campos indicados, de modo que el resto de su
        configuración queda intacta.

        `potencia` en dBm activa el modo "custom"; el texto "auto" devuelve
        la radio a la gestión automática.
        """
        clave = BANDAS.get(str(banda).lower())
        if clave is None:
            raise ErrorUnifi(f"banda no reconocida: «{banda}» (usa 2.4 o 5)")

        tabla = json.loads(json.dumps(ap.get("radio_table", [])))  # copia
        objetivo = next((r for r in tabla if r.get("radio") == clave), None)
        if objetivo is None:
            raise ErrorUnifi(
                f"este AP no tiene radio de {banda} GHz "
                f"(radios disponibles: "
                f"{[r.get('radio') for r in tabla]})"
            )

        if canal is not None:
            objetivo["channel"] = canal if canal == "auto" else int(canal)

        if ancho is not None:
            ancho = int(ancho)
            if ancho not in ANCHOS_VALIDOS[clave]:
                raise ErrorUnifi(
                    f"ancho de {ancho} MHz no válido en {banda} GHz "
                    f"(admitidos: {ANCHOS_VALIDOS[clave]})"
                )
            objetivo["ht"] = ancho

        if potencia is not None:
            if str(potencia).lower() == "auto":
                objetivo["tx_power_mode"] = "auto"
                objetivo.pop("tx_power", None)
            else:
                objetivo["tx_power_mode"] = "custom"
                objetivo["tx_power"] = int(potencia)

        self._peticion(
            "PUT",
            f"/api/s/{self.sitio}/rest/device/{ap['_id']}",
            {"radio_table": tabla},
        )
        return objetivo

    def esperar_aplicacion(self, segundos=25):
        """
        Espera tras un cambio de radio.

        El AP reinicia la radio y las estaciones tienen que reasociarse.
        """
        time.sleep(segundos)


# ----------------------------------------------------------------------
# Interfaz de línea de órdenes
# ----------------------------------------------------------------------
def _linea_radio(radio):
    """Una línea con la configuración de una radio, potencia incluida."""
    potencia = radio["potencia_dbm"]
    modo = radio["modo_potencia"]
    texto_pot = f"{modo} ({potencia} dBm)" if potencia is not None else str(modo)
    return (f"  · {radio['banda']:<8} canal {str(radio['canal']):<5} "
            f"{str(radio['ancho_mhz']):>3} MHz   potencia {texto_pot}")


def _imprimir_aps(controlador):
    for ap in controlador.listar_aps():
        nombre = ap.get("name") or "(sin nombre)"
        estado = "conectado" if ap.get("state") == 1 else "desconectado"
        print(f"\n  {nombre}  [{ap.get('model', '?')}]  {estado}")
        print(f"    MAC: {ap.get('mac')}    IP: {ap.get('ip')}")
        print(f"    id:  {ap.get('_id')}")
        for radio in controlador.estado_radios(ap):
            print("  " + _linea_radio(radio))
    print()


def main():
    p = argparse.ArgumentParser(
        description="Lee y modifica los parámetros de radio del AP UniFi."
    )
    p.add_argument("--host", default=os.environ.get("UNIFI_HOST", "localhost"),
                   help="host de la controladora (por defecto: localhost)")
    p.add_argument("--puerto", type=int,
                   default=int(os.environ.get("UNIFI_PORT", 8443)))
    p.add_argument("--usuario", default=os.environ.get("UNIFI_USER"))
    p.add_argument("--password", default=os.environ.get("UNIFI_PASS"))
    p.add_argument("--sitio", default=os.environ.get("UNIFI_SITE", "default"))
    p.add_argument("--ap", default=None,
                   help="nombre, MAC o IP del AP (si solo hay uno, no hace falta)")

    p.add_argument("--listar", action="store_true",
                   help="muestra los APs adoptados y su configuración actual")
    p.add_argument("--banda", default="5", help="2.4 o 5 (por defecto: 5)")
    p.add_argument("--canal", default=None, help="número de canal, o «auto»")
    p.add_argument("--ancho", default=None, help="ancho de canal en MHz (20/40/80)")
    p.add_argument("--potencia", default=None, help="potencia en dBm, o «auto»")
    p.add_argument("--espera", type=int, default=25,
                   help="segundos de espera tras aplicar el cambio")

    args = p.parse_args()

    controlador = ControladorUnifi(
        host=args.host, puerto=args.puerto, usuario=args.usuario,
        password=args.password, sitio=args.sitio,
    )

    try:
        with controlador:
            if args.listar or not any((args.canal, args.ancho, args.potencia)):
                _imprimir_aps(controlador)
                return 0

            ap = controlador.buscar_ap(args.ap)
            nombre = ap.get("name") or ap.get("mac")

            print(f"AP: {nombre}  ({ap.get('model')}, {ap.get('ip')})")
            print("Antes:")
            for radio in controlador.estado_radios(ap):
                print(_linea_radio(radio))

            controlador.configurar_radio(
                ap, args.banda, canal=args.canal,
                ancho=args.ancho, potencia=args.potencia,
            )
            print(f"\nCambio enviado. Esperando {args.espera} s a que la radio "
                  f"se reinicie y las estaciones se reasocien...")
            controlador.esperar_aplicacion(args.espera)

            ap = controlador.buscar_ap(args.ap)  # relee el estado real
            print("Después:")
            for radio in controlador.estado_radios(ap):
                print(_linea_radio(radio))
            print()
    except ErrorUnifi as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
