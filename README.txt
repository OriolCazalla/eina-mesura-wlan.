===============================================================================
 EINA DE MESURA DE PRESTACIONS EN ENLLAÇOS WLAN
===============================================================================

Mesura cabal, retard i pèrdua entre dos equips, i desa amb cada mesura la
freqüència, l'amplada, el senyal i la taxa negociada de la interfície.
Escrita en Python sobre sòcols, sense biblioteques de mesura externes.


-------------------------------------------------------------------------------
 1. PROGRAMES
-------------------------------------------------------------------------------

  server.py            Extrem passiu. Escolta, respon els ecos i rep la ràfega
                       UDP. S'executa a l'equip servidor.

  client.py            Llança les tres proves, llegeix l'estat de l'enllaç i
                       desa els resultats. S'executa a l'equip client.

  control_iface.py     Consulta i configura la interfície WLAN local.

  control_ap.py        Configura les ràdios del punt d'accés per l'API de la
                       controladora UniFi.

  medir_referencia.sh  Tanda de contrast amb iperf3 i ping. No forma part de
                       l'eina.


-------------------------------------------------------------------------------
 2. REQUISITS
-------------------------------------------------------------------------------

  - Dos equips amb Linux associats a la mateixa xarxa sense fil.
    El client necessita interfície WLAN; el servidor pot anar per cable.

  - Python 3.8 o superior.

  - Una dependència:              pip install rich
  - Opcional:                     pip install psutil
      Si hi és, s'afegeix el cabal comptat pel sistema operatiu. Si no, el
      programa funciona igual i avisa en arrencar.

  - Del sistema: iw
    Només per a les tandes de referència: iperf3 i ping

  - Per a control_ap.py: un punt d'accés Ubiquiti gestionat amb la seva
    controladora UniFi accessible per xarxa. Sense això, la resta funciona
    igual; el canal, l'amplada i la potència es fixen a mà des del panell.


-------------------------------------------------------------------------------
 3. POSADA EN MARXA
-------------------------------------------------------------------------------

  3.1  Identificar la interfície

       python3 control_iface.py --listar

       Dona el nom (wlp8s0, wlan0...) i el dispositiu físic (phy0).


  3.2  Comprovar l'enllaç

       python3 control_iface.py --iface wlp8s0 --estado


  3.3  Desactivar l'estalvi d'energia ALS DOS EQUIPS

       sudo iw dev wlp8s0 set power_save off
       iw dev wlp8s0 get power_save          # ha de dir: off

       És el pas que més afecta el resultat: amb l'estalvi actiu el retard es
       multiplica per nou sense que canviï res de l'enllaç. Cal repetir-ho
       després de cada reassociació.


  3.4  Credencials de la controladora (només per a control_ap.py)

       MAI no s'escriuen al codi. Van per variables d'entorn:

       export UNIFI_HOST=192.168.1.1
       export UNIFI_USER=el_teu_usuari
       export UNIFI_PASS=la_teva_contrasenya
       export UNIFI_PORT=8443        # opcional
       export UNIFI_SITE=default     # opcional

       Comprovació:   python3 control_ap.py --listar


  3.5  Arrencar el servidor (a l'equip servidor)

       python3 server.py --iface wlp8s0

       Escolta al port 5001 fins a Ctrl+C. No cal reiniciar-lo entre escenaris.


  3.6  Primera mesura (al client)

       python3 client.py --server 192.168.1.28 --test all --iface wlp8s0

       Si surten les tres proves, el muntatge funciona.


-------------------------------------------------------------------------------
 4. client.py
-------------------------------------------------------------------------------

  python3 client.py --server <IP> [opcions]

    --server <IP>         obligatori. IP del servidor
    --port <n>            port del servidor                      [5001]
    --test <t>            rtt | tput | udp | both | all          [both]
                            both = rtt+tput,  all = rtt+tput+udp
    --duration <s>        durada de la prova de cabal              [10]
    --pings <n>           nombre d'ecos de la prova de retard      [10]
    --udp-paquetes <n>    datagrames de la ràfega UDP             [200]
    --udp-tam <n>         mida de cada datagrama, en bytes        [512]
    --udp-rate <n>        datagrames per segon                    [200]
    --iface <if>          interfície a llegir i monitoritzar
    --repeat <n>          repeteix la prova sencera N vegades       [1]
    --label <text>        etiqueta de l'escenari
    --csv <fitxer>        acumula una fila per repetició

  Exemples:

    # les tres proves, una vegada
    python3 client.py --server 192.168.1.28 --test all --iface wlp8s0

    # escenari complet de deu repeticions, desat al CSV
    python3 client.py --server 192.168.1.28 --test all --repeat 10 \
            --label b5_ch36_w40_p20 --csv resultados_tfg.csv --iface wlp8s0

    # només el retard, amb trenta ecos
    python3 client.py --server 192.168.1.28 --test rtt --pings 30

    # ràfega UDP més llarga i més ràpida
    python3 client.py --server 192.168.1.28 --test udp \
            --udp-paquetes 1000 --udp-rate 500

  Cada repetició afegeix al CSV una fila amb vint columnes: marca de temps,
  etiqueta, repetició, condicions de ràdio llegides just abans de mesurar i
  mètriques de les tres proves.


-------------------------------------------------------------------------------
 5. server.py
-------------------------------------------------------------------------------

  python3 server.py [opcions]

    --host <IP>     IP on escoltar                           [0.0.0.0]
    --port <n>      port                                        [5001]
    --iface <if>    interfície a comptar amb psutil

  Atén un client cada vegada i torna a escoltar en acabar.


-------------------------------------------------------------------------------
 6. control_iface.py
-------------------------------------------------------------------------------

  python3 control_iface.py --iface <if> [acció]

    --listar                 interfícies sense fils de l'equip
    --estado                 estat de l'enllaç i estadístiques de capa MAC
    --capacidades            bandes, canals i amplades que admet l'adaptador
    --regulador              domini regulador vigent
    --potencia <dBm|auto>    fixa la potència de transmissió
    --modo <m>               managed | monitor | ibss
    --regulador-set <PAIS>   canvia el domini regulador (codi ISO, p. ex. ES)
    --phy <phy>              dispositiu físic, per a --capacidades

  Les consultes no demanen privilegis; les modificacions, sí.

    python3 control_iface.py --iface wlp8s0 --estado
    python3 control_iface.py --phy phy0 --capacidades
    sudo python3 control_iface.py --iface wlp8s0 --potencia 15


-------------------------------------------------------------------------------
 7. control_ap.py
-------------------------------------------------------------------------------

  python3 control_ap.py [opcions]

    --listar              punts d'accés adoptats i configuració actual
    --banda <2.4|5>       banda                                      [5]
    --canal <n|auto>      número de canal
    --ancho <20|40|80>    amplada de canal en MHz
    --potencia <dBm|auto> potència
    --espera <s>          espera després d'aplicar el canvi          [25]
    --ap <nom|MAC|IP>     quin punt d'accés, si n'hi ha més d'un
    --host --puerto --usuario --password --sitio
                          sobreescriuen les variables d'entorn

    python3 control_ap.py --listar
    python3 control_ap.py --banda 5 --canal 36 --ancho 40 --potencia 20
    python3 control_ap.py --banda 2.4 --canal 1 --ancho 20 --potencia 4

  ATENCIÓ: després d'un canvi, el punt d'accés reinicia la ràdio i les
  estacions s'han de tornar a associar. El programa espera els segons de
  --espera i rellegeix la configuració per confirmar-ho, però aquesta espera
  NO sempre basta perquè el control de taxa de l'adaptador recuperi el punt
  on era. Comprovar la taxa negociada amb --estado abans de donar l'escenari
  per bo.


-------------------------------------------------------------------------------
 8. medir_referencia.sh
-------------------------------------------------------------------------------

  ./medir_referencia.sh <etiqueta> [repeticions] [pausa_seg] [ip_servidor]

  Executa iperf3 sobre TCP, iperf3 sobre UDP i ping amb els mateixos
  paràmetres que client.py, i desa la sortida a referencia_iperf/<etiqueta>/.
  Al servidor cal tenir "iperf3 -s" escoltant.

    ./medir_referencia.sh b5_ch36_w40_p20 1 0 192.168.1.28


-------------------------------------------------------------------------------
 9. UN ESCENARI COMPLET
-------------------------------------------------------------------------------

  # 1. configurar la ràdio del punt d'accés
  python3 control_ap.py --banda 5 --canal 36 --ancho 40 --potencia 20

  # 2. comprovar la reassociació i la taxa, i apagar l'estalvi
  python3 control_iface.py --iface wlp8s0 --estado
  sudo iw dev wlp8s0 set power_save off

  # 3. tanda de referència (al servidor: iperf3 -s)
  ./medir_referencia.sh b5_ch36_w40_p20 1 0 192.168.1.28

  # 4. deu repeticions amb l'eina
  python3 client.py --server 192.168.1.28 --test all --repeat 10 \
          --label b5_ch36_w40_p20 --csv resultados_tfg.csv --iface wlp8s0


-------------------------------------------------------------------------------
 10. LIMITACIONS CONEGUDES
-------------------------------------------------------------------------------

  Les que la memòria recull a l'apartat 6.4 i al capítol 8:

  - El sòcol UDP del servidor no es buida entre repeticions: un datagrama
    endarrerit pot entrar a la repetició següent i falsejar-ne el jitter.
  - La mitjana del retard no separa els ecos anòmals, i un sol eco disparat
    l'arrossega.
  - El resum i el fitxer anomenen "desviació típica" dues magnituds
    diferents: la dispersió entre repeticions i la dispersió entre ecos.
  - L'estat de l'estalvi d'energia no es desa amb la resta de l'estat de la
    interfície, tot i ser el paràmetre que més afecta el retard.

  L'eina llegeix només la ràdio de l'equip on s'executa: en una topologia de
  dos salts, el tram entre el punt d'accés i l'altre extrem no queda
  registrat. El control del punt d'accés depèn de l'API de UniFi.


-------------------------------------------------------------------------------
 11. DADES DE LA CAMPANYA
-------------------------------------------------------------------------------

  Les 275 mesures preses amb aquesta eina en 27 escenaris, i les mesures de
  referència amb iperf3 i ping amb què es contrasten, es recullen als annexos
  de la memòria del treball.

===============================================================================
