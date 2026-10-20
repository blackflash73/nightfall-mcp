# nightfall-mcp

Ein MCP-Server, über den Claude per Telnet ein MUD spielen kann, zum Beispiel
[NightFall](http://nightfall.org) (`nightfall.org:4242`). Er funktioniert mit jedem
zeilenbasierten MUD.

Der Server liest die Ausgabe des MUDs im Hintergrund mit und gibt sie Claude als
sauberen Text zurück:

- Telnet-Optionen werden abgelehnt (einfacher Zeilenmodus).
- ANSI-Farbcodes werden entfernt, Zeilenenden auf `\n` vereinheitlicht.
- Der NightFall-Pager (`=== SMore: Lines 1..16 of 40 …`) wird automatisch
  durchgeblättert, lange Texte kommen in einem Stück zurück.
- Nachrichten anderer Spieler werden herausgefiltert, bevor sie Claude erreichen
  (siehe [Datenschutz](#datenschutz-nachrichten-anderer-spieler)).

## Tools

| Tool | Beschreibung |
|---|---|
| `mud_connect(host="nightfall.org", port=4242, wait=2.0)` | Verbindet direkt und gibt den Login-Bildschirm zurück |
| `mud_attach(port=4001, wait=0.5)` | Hängt sich an den lokalen Proxy, in dem du selbst spielst ([siehe unten](#selbst-spielen-claude-liest-mit)) |
| `mud_send(command, wait=0.8)` | Schickt einen Befehl und gibt die Antwort zurück |
| `mud_read(wait=1.0)` | Gibt neue Ausgabe seit dem letzten Lesen zurück (andere Spieler, Timer) |
| `mud_recent(lines=40)` | Die letzten Zeilen der Ausgabe, auch bereits gelesene |
| `mud_disconnect()` | Trennt die Verbindung |

`wait` ist die Wartezeit in Sekunden, bevor die Ausgabe eingesammelt wird.

## Installation

Voraussetzung: Python 3.10 oder neuer.

```powershell
git clone https://github.com/blackflash73/nightfall-mcp.git
cd nightfall-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Unter Linux/macOS heißt der Interpreter `.venv/bin/python`.

## In Claude Code einbinden

Mit absoluten Pfaden, damit der Server aus jedem Projekt heraus startet:

```powershell
claude mcp add nightfall -s user -- "C:\pfad\zu\nightfall-mcp\.venv\Scripts\python.exe" "C:\pfad\zu\nightfall-mcp\mud_mcp_server.py"
claude mcp get nightfall
```

Die Tools stehen ab der nächsten Claude-Code-Session zur Verfügung.

## Selbst spielen, Claude liest mit

Mit `mud_proxy.py` spielst du in deinem eigenen MUD-Client, und Claude liest mit
und gibt Tipps:

```
  dein MUD-Client ──► 127.0.0.1:4000 ──► mud_proxy.py ──► nightfall.org:4242
  Claude (MCP)    ──► 127.0.0.1:4001 ──┘  (Mitlese-Anschluss)
```

1. Proxy in einem eigenen Fenster starten:
   ```powershell
   .\.venv\Scripts\python.exe mud_proxy.py
   ```
2. MUD-Client mit `127.0.0.1`, Port `4000` verbinden, z.B. [Mudlet](https://www.mudlet.org/)
   oder PuTTY (Verbindungstyp „Telnet“). Erst jetzt verbindet sich der Proxy mit dem MUD.
3. In Claude Code sagen: „Häng dich an den MUD-Proxy“ (Tool `mud_attach`).
4. Normal einloggen und spielen. Claude fragen, z.B. „Wo finde ich die Gilde?“

Claude darf standardmäßig **nur mitlesen**. Mit `mud_proxy.py --allow-send` darf
Claude auch Befehle schicken; sie erscheinen in deinem Client als `*** Claude: …`.

Befehle in deinem MUD-Client (sie gehen nicht ans MUD):

| Befehl | Wirkung |
|---|---|
| `/claude on` | Mitlesen einschalten (nötig als Gast ohne Passwort) |
| `/claude off` | Mitlesen pausieren |
| `/claude status` | Stand anzeigen |
| `/claude connect` | Neu mit dem MUD verbinden, nachdem die Verbindung beendet wurde |
| `/claude disconnect` | Verbindung zum MUD trennen, z.B. im Login-Bildschirm |

Endet die Verbindung zum MUD (`quit`, Timeout, `/claude disconnect`), bleibt dein
Client mit dem Proxy verbunden. Der Proxy verbindet sich nie von selbst neu, nur
beim Verbinden deines Clients oder mit `/claude connect`.

**Schutz deiner Zugangsdaten:**

- Was du tippst, leitet der Proxy nur weiter. Es wird nie gespeichert,
  protokolliert oder an Claude gegeben, also auch nicht dein Passwort.
- Claude sieht nichts aus der Login-Phase. Das Mitlesen beginnt erst, wenn das MUD
  nach der Passwort-Eingabe das Echo wieder einschaltet (Telnet `WILL ECHO` →
  `WONT ECHO`).
- Während Login und Passwort-Eingabe kann Claude nichts senden, auch nicht mit
  `--allow-send`.
- Der Proxy schreibt nichts auf die Platte. In seinem Fenster stehen nur
  Statusmeldungen.

Grenzen: Dein Charaktername taucht nach dem Login in Spieltexten auf („Welcome
back, …“, `who`). Schaltet ein MUD das Echo dauerhaft ab (Zeichenmodus), startet
das Mitlesen nicht automatisch; dann nach dem Login `/claude on` tippen. Claude
kann in diesem Fall nichts senden.

## Datenschutz: Nachrichten anderer Spieler

Alles, was ein Tool zurückgibt, landet im Claude-Code-Session-Protokoll und wird an
das Modell geschickt. Damit Gespräche anderer Spieler dort nicht auftauchen,
entfernt der Server sie **vorher**. Statt der Nachricht erscheint nur ein Hinweis
wie `[2 Nachrichten anderer Spieler ausgeblendet]`, ohne Namen und Inhalt.

Gesteuert wird das über die Umgebungsvariable `MUD_FILTER` (Komma-Liste):

| Wert | Blendet aus |
|---|---|
| `channels` | Kanalzeilen wie `[Bill] text` oder `[news:Bob enters the world]` |
| `tells` | Private Nachrichten: `Bob tells you: …`, `Bob whispers to you: …` |
| `say` | Sprache im Raum: `Bob says: …`. Trifft auch NPCs, darum nicht im Standard |
| `none` | Kein Filter |

Standard ist `channels,tells`. Anders einstellen:

```powershell
claude mcp add nightfall -s user -e MUD_FILTER=channels,tells,say -- <python> <pfad>\mud_mcp_server.py
```

Grenzen: Der Filter erkennt Nachrichten an ihrem Muster. Emotes (`Bob smiles at
you`), Raummeldungen (`Bob arrives`) und die `who`-Liste bleiben sichtbar. Die
Muster für Tells und Says folgen üblichen LPmud-Formaten und sind für NightFall
nicht im Detail geprüft.

## Rücksicht auf andere Spieler

Jedes Ein- und Ausloggen wird im MUD an **alle** Spieler gemeldet. Bitte nicht
gegen den echten Server testen, sondern gegen den lokalen Test-Server (unten).
Wenn doch live gespielt wird: eine Sitzung, viele Befehle, statt vieler kurzer
Logins.

Alles, was über `mud_send` geschickt wird, auch ein Passwort, steht im
Chatverlauf. Für Experimente den Namen `guest` oder einen Wegwerf-Charakter
verwenden.

## Tests

`test/fake_mud.py` ist ein kleiner Fake-MUD, der nur auf `127.0.0.1` lauscht. Er
simuliert Telnet-Aushandlung, ANSI-Farben, `\r\n`, Gast-Login, Login mit Passwort
(Name ungleich `guest`), den SMore-Pager und Nachrichten anderer Spieler. Die Tests
starten MCP-Server und Proxy als echte Prozesse, genau wie im Betrieb. Sie prüfen
unter anderem, dass das Passwort beim MUD ankommt, aber nie bei Claude oder in der
Ausgabe des Proxys:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s test -v
```

Den Fake-MUD kann man auch von Hand starten und dann aus Claude heraus spielen:

```powershell
.\.venv\Scripts\python.exe test\fake_mud.py --port 4000
```

```
mud_connect(host="127.0.0.1", port=4000)
```

Befehle im Fake-MUD: `look`, `read map` (Pager), `chatter` (Nachrichten anderer
Spieler), `split` (über zwei Pakete verteilte Nachricht), `quit`.
