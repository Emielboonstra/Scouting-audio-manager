# Scouting Audio Manager

Scouting Audio Manager bedient de lokale PipeWire-audiografiek vanuit een Qt-dashboard. De systemd-gebruikersservice beheert de virtuele uitgangen, ontvangers en fysieke routes onafhankelijk van het venster.

## Installeren

Benodigd zijn een Linux-desktopsessie met systemd-user, PipeWire en pipewire-pulse, WirePlumber 0.5 of hoger, Python 3.11 of hoger met venv, en de programma's `pactl`, `paplay`, `pw-cli`, `pw-dump`, `wpctl` en `playerctl`. Gebruik bij voorkeur PipeWire 1.2 of hoger. De pakketnamen voor de PulseAudio-clients zijn bijvoorbeeld `pulseaudio-utils` op Debian/Ubuntu. Installeer de vereiste PipeWire-modules voor jouw distributie, waaronder filter-chain. De installer haalt PySide6 Essentials op via pip. Voor Spotify-albumhoezen gebruikt de GUI `curl`. Op Linux heeft Qt een werkende Wayland- of X11-sessie en de bijbehorende systeembibliotheken nodig.

Fedora Workstation met GNOME is geschikt als ontwikkelomgeving voor Ubuntu Desktop met GNOME. Dezelfde gebruikersinstaller en PipeWire-architectuur zijn bedoeld voor beide distributies. Draai de doctor opnieuw op Ubuntu om de aanwezige versies, Qt-sessie en audioapparaten te controleren. Koppel de zones op de installatiecomputer aan de daar beschikbare uitgangen.

Voer als de gebruiker van de geluidscomputer uit:

```sh
python3 -m scouting_audio.doctor
sh install.sh --autostart
```

Met `--autostart` start de engine bij het inloggen. Start **Scouting Audio Manager** vanuit het applicatiemenu. De installer schrijft in de gebruikersmappen en gebruikt de bestaande audiosessie. Zonder `--autostart` start de engine bij installatie en bij het openen vanuit het applicatiemenu. `sh uninstall.sh` verwijdert de gebruikersinstallatie en behoudt de configuratie.

## Eerste gebruik

1. Zet de eindversterkers laag en controleer of master mute aan staat.
2. Voeg onder **Zones** een zone toe per fysieke PipeWire-uitgang. Als een uitgang ontbreekt, controleer het actieve apparaatprofiel in de Linux-geluidsinstellingen.
3. Verbind **Computeraudio** met één zone onder **Routing** en selecteer **Scouting Audio** als uitgang in de browser of in de Linux-geluidsinstellingen.
4. Zet master mute uit en verhoog het volume geleidelijk. Voeg daarna de overige routes toe.
5. Voeg een microfoon onder **Routing** toe. Nieuwe ingangen zijn gedempt. Zorg dat de microfoon voldoende ver van de luidsprekers staat. PipeWire-monitoren kunnen niet als microfoon worden toegevoegd.
6. Sla werkstanden op onder **Profielen**. Laden of importeren activeert master mute voordat nieuwe routes hoorbaar worden.

## Ontvangers

| Dienst | Installatie en gedrag |
| --- | --- |
| Spotify Connect | Installeer Spotifyd met PulseAudio en MPRIS. Spotifyd gebruikt librespot. Start de ontvanger in het dashboard, kies de ingestelde apparaatnaam in de Spotify-app en controleer metadata, hoes, positie en bediening. Spotifyd en de mixer hebben gescheiden softwarevolumes. |
| AirPlay | Installeer Shairport Sync met `pa`, mDNS en bij voorkeur metadata en MPRIS. De engine start een eigen gebruikersproces op TCP 5100 met de sessie-D-Bus. Bouwvarianten en AirPlay-zenders bepalen welke metadata, hoezen en bediening beschikbaar zijn. AirPlay 2 kan NQPTP vereisen. |
| Music Assistant | Installeer de optionele Sendspin Python CLI en PortAudio met een PulseAudio- of ALSA Pulse-plug-in. Controleer met `sendspin audio-devices list` of de uitgang `pulse` beschikbaar is. Schakel Sendspin op de server in en vul het door de server opgegeven WebSocket-adres in. Kies de Scouting-speler in Music Assistant. De integratie gebruikt Sendspin voor audio en MPRIS wanneer de client dat aanbiedt. Serverbrede spelersdetectie via de beheer-API is niet geïmplementeerd. |
| Google Cast | Geen receiver opgenomen. De officiële Web Receiver SDK voert receiver-apps uit op Cast-apparatuur. Voor generieke Linux-desktops is geen voldoende geverifieerde YouTube Music-compatibele receiver gevonden. |

Voor lokale netwerkontdekking moeten de afspeelapparaten de scoutingcomputer kunnen bereiken en moet mDNS op het netwerk werken. Ontvangers staan bij een nieuwe installatie uit. Een status “Ontvanger draait” bevestigt de processtatus. De echte netwerkverbinding en afspeelstatus zijn afhankelijk van de client en het netwerk.

## Audioketen

```text
Browser / Spotifyd / Shairport / Sendspin
       -> gedeelde virtuele uitgang per bron -> sink-monitor
Fysieke ingang -----------------------------> capturebron
       -> PipeWire-loopback per actieve route
       -> virtuele stereo-zonemixer
       -> zone-EQ en zonevertraging
       -> vaste fysieke uitgang
```

Elke route heeft een eigen softwaregain en vertraging. Bronvolume en routeniveau vermenigvuldigen elkaar. De zone past mastervolume, zonevolume en EQ-headroom toe. Een extra route verandert het niveau van bestaande routes niet. Verlaag de routeniveaus als meerdere luide bronnen tegelijk spelen en de som vervormt. Exclusiviteit kiest de laatst gestarte muziekdienst en gebruikt beschikbare pauzebediening met route-mute als terugval. Computeraudio en ingangen blijven onafhankelijk.

PipeWire verwerkt audio en resampling. De aangevraagde samplerate en latency gelden voor applicatiestreams. De service verandert de globale graph clock niet. Uitgangen worden gekoppeld op WirePlumber-nodenaam, zodat vluchtige object-ID's niet in profielen terechtkomen. Identieke USB-adapters zonder serienummer kunnen afhankelijk zijn van dezelfde USB-poort of hetzelfde profiel. Een multi-output interface moet afzonderlijke PipeWire-sinks aanbieden om die uitgangen als aparte zones te kunnen kiezen. De engine configureert geen hardwareprofielen of kanaalsplitsing.

De loopbackdoellatentie gebruikt door PipeWire gerapporteerde latenties. Externe DAC's, versterkers en akoestiek kunnen extra vertraging introduceren die PipeWire niet kan meten. Stel daarom zo nodig een positieve vertraging in voor de vroegste zones. Bij onafhankelijke apparaten resamplet PipeWire waar de graph dat vereist. Synchronisatie blijft afhankelijk van de werkelijke drivers, clocks en buffers. Een gedeelde hardwareclock is geschikt voor locaties waar meerdere luidsprekers in dezelfde ruimte hoorbaar zijn.

## Testen op de doelmachine

```sh
python3 -m unittest discover -s tests -v
journalctl --user -u scouting-audio-manager.service -f
```

| Scenario | Controle |
| --- | --- |
| Twee fysieke uitgangen | Koppel twee zones aan verschillende uitgangen. Stuur dezelfde bron naar beide zones en verander per zone volume, mute en EQ. Controleer dat de andere zone gelijk blijft. |
| Pubquiz | Speel Spotify af, speel geluidseffecten vanuit de browser en open de microfoonroute. Controleer dat de drie bronnen tegelijk hoorbaar zijn. |
| Prioriteit | Start AirPlay tijdens Spotify. Controleer pauze of demping en bevestig dat browser en microfoon blijven werken. Herhaal met exclusiviteit uit. |
| Sync-puls | Gebruik **Zones → Synchronisatiepuls afspelen**. Neem uitgangen op via twee kanalen met dezelfde opnameclock. Vergelijk pulsbegin per kanaal en vul het gemeten verschil in bij de vroegste zone. Herhaal na minimaal 30 minuten en met verschillende apparaat-samplerates. |
| GUI en service | Sluit en open de GUI terwijl audio speelt. Controleer dat het geluid blijft spelen. Herstart daarna alleen de engine en controleer opgeslagen routes. |
| Audioherstel | Herstart in een gepland testvenster PipeWire, PipeWire Pulse en WirePlumber. Controleer het herstel van de routes en start de afspeelclient opnieuw als de sessie verbroken is. |
| Hotplug | Verwijder een USB-uitgang tijdens audio. De zone blijft in het dashboard. Sluit dezelfde uitgang aan en controleer herstel zonder audio via een andere fysieke uitgang. |
| Herstart | Test op een rustige locatie met autostart aan. Controleer na inloggen de engine, gekozen ontvangers, routes en de uitgangsniveaus. |

## Verificatiestatus

De Python-tests controleren configuratie, profielen, doelkeuze, gainberekening en routegedrag. Op Fedora 43 met GNOME slagen alle 10 tests en de syntaxcontrole van beide shellscripts. De doctor bevestigde een werkende gebruikerssessie met PipeWire 1.4.11, WirePlumber 0.5.14 en één actieve fysieke stereo-uitgang. De gebruikersinstallatie, systemd-service, Unix-socket, virtuele PipeWire-uitgangen en het starten van de GUI onder Wayland zijn gecontroleerd. Audioweergave via de routes staat nog open. Test de daadwerkelijke PipeWire-route, live-EQ, virtuele uitgangen, netwerkontvangers, desktopintegratie, hotplug en synchronisatie vóór inzet. De implementatie bevat geen VU-meter, hardware-inputgain, akoestische autokalibratie of gecertificeerde garantie op klikvrije overgangen. De engine schrijft status en ontvangeruitvoer naar het gebruikerslogboek. De configuratie staat standaard in `~/.config/scouting-audio-manager/config.json`.

Technische bronnen: [PipeWire filter-chain](https://docs.pipewire.org/page_module_filter_chain.html), [PipeWire Pulse-loopback](https://docs.pipewire.org/page_pulse_module_loopback.html), [WirePlumber linking](https://pipewire.pages.freedesktop.org/wireplumber/policies/linking.html), [Spotifyd](https://github.com/Spotifyd/spotifyd), [Shairport Sync](https://github.com/mikebrady/shairport-sync), [Music Assistant Sendspin](https://www.music-assistant.io/player-support/sendspin/), [Sendspin CLI](https://github.com/Sendspin/sendspin-python-cli), [Google Cast Web Receiver](https://developers.google.com/cast/docs/web_receiver).
