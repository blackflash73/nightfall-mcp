#!/usr/bin/env python3
"""
mud_mcp_server.py — MCP-Server: Telnet-Bruecke zu NightFall (oder jedem MUD).

Tools:
    mud_connect(host, port, wait)   -> verbindet direkt, gibt Begruessungstext zurueck
    mud_attach(port, wait)          -> haengt sich an mud_proxy.py (du spielst, Claude liest mit)
    mud_send(command, wait)         -> schickt einen Befehl, gibt die Antwort zurueck
    mud_read(wait)                  -> gibt neu aufgelaufene Ausgabe zurueck
    mud_recent(lines)               -> die letzten Zeilen der Ausgabe
    mud_disconnect()                -> trennt

Im Proxy-Modus (mud_attach) haelt mud_proxy.py die Verbindung zum MUD. Claude
sieht nur die Ausgabe des MUDs nach dem Login, nie deine Eingaben, und darf
nur senden, wenn der Proxy mit --allow-send laeuft. Der Pager wird dann nicht
automatisch geblaettert, weil das dein Spiel stoeren wuerde.

Transport: stdio.  Abhaengigkeit:  pip install -r requirements.txt   (mcp>=2,<3, Python 3.10+)

Ein Hintergrund-Thread liest den Stream fortlaufend in einen Puffer; die Tools
ziehen daraus. Telnet-Optionen werden abgelehnt (einfacher Zeilenmodus),
ANSI-Farbcodes werden fuer die Rueckgabe entfernt. Der NightFall-Pager
("=== SMore: ...") wird automatisch durchgeblaettert.

Datenschutz: Nachrichten anderer Spieler werden VOR der Rueckgabe entfernt und
landen so nicht im Claude-Session-Protokoll. Gesteuert ueber die Umgebungs-
variable MUD_FILTER (Komma-Liste, Standard "channels,tells,say"):
    channels  Kanalzeilen wie "[Bill] text" oder "[news:Bob enters the world]"
    tells     private Nachrichten: "Bob tells you: ...", "Bob whispers to you: ..."
    say       Sprache von Spielern im Raum: "Bob says: ...". Saetze von NPCs bleiben
              sichtbar. Als Spieler gilt, wer in der who-Liste, in einem Kanal
              oder in einem Tell vorkam (nur im Speicher). Unbekannte Sprecher
              gelten als NPC, darum einmal "who" eingeben.
    none      keinen Filter verwenden
"""

import os
import re
import socket
import threading
import time
from collections import deque

from mcp.server.mcpserver import MCPServer

# Telnet-Steuerbytes
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Pager-Prompt von NightFall, z.B. "=== SMore: Lines 1..16 of 40 [CR,u,f,l,p,q,...] "
PAGER_RE = re.compile(r"=== SMore: Lines \d+\.\.\d+ of \d+ \[[^\]\n]*\][^\n]*\n?$")
PAGE_WAIT = 0.6   # Sekunden Wartezeit pro weitergeblaetterter Seite
MAX_PAGES = 50    # Schutz gegen Endlosschleifen
HISTORY_LINES = 2000   # fuer mud_recent

# --- Filter fuer Nachrichten anderer Spieler ---------------------------------
# Prompt vor einer Nachricht, z.B. "> ", "> > " oder "->>" (nach Fragen des MUDs)
PROMPT_PREFIX_RE = re.compile(r"^((?:-?>+ ?)*)")
FILTER_PATTERNS = {
    # "[Bill] wer ist online?", "[news:Bob enters the world]", "[chat:Bob] hi"
    # "[fan:Joe sings ...", auch wenn die "]" erst in einer Folgezeile kommt
    "channels": re.compile(r"^\[(?:[^\]\n]{1,60}\](?: .*)?$|[\w-]{1,20}:)"),
    "tells": re.compile(r"^[A-Z][\w-]* (?:tells|whispers to|asks) you(?: [^:\n]*)?: "),
    "say": re.compile(r"^([A-Z][\w-]*(?: [\w-]+)?) (?:says|asks|exclaims)(?: [^:\n]*)?: "),
}
# Spielernamen lernen: who-Liste (" Alice is   [Ro, DEv6]  Lib"), Kanaele, Tells
WHO_HEADERS = ("Players", "Wizards")
WHO_LINE_RE = re.compile(r"^ (\S.*?)\s+\[[^\]\n]*\]")
CHANNEL_NAME_RE = re.compile(r"^\[[\w-]{1,20}:([A-Z][\w-]*)")
TELL_NAME_RE = re.compile(r"^([A-Z][\w-]*) (?:tells|whispers to|asks) you")
NAME_WORD_RE = re.compile(r"\b[A-Z][\w-]*")
# Unvollstaendige letzte Zeile zurueckhalten, wenn sie schon als Chat erkennbar ist
PARTIAL_PATTERNS = {
    "channels": re.compile(r"^\["),
    "tells": FILTER_PATTERNS["tells"],
    "say": FILTER_PATTERNS["say"],
}


def active_filters() -> list[str]:
    raw = os.environ.get("MUD_FILTER", "channels,tells,say")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    if "none" in names:
        return []
    return [n for n in names if n in FILTER_PATTERNS]


def learn_players(text: str, players: set[str]) -> None:
    """Sammelt Spielernamen aus who-Liste, Kanaelen und Tells in 'players'."""
    in_who = False
    for line in text.split("\n"):
        body = line[len(PROMPT_PREFIX_RE.match(line).group(1)):]
        if body.strip() in WHO_HEADERS:
            in_who = True
            continue
        if in_who:
            m = WHO_LINE_RE.match(body)
            if m:
                # alle gross geschriebenen Woerter, auch Vortitel wie "Lucky Old"
                players.update(NAME_WORD_RE.findall(m.group(1)))
                continue
            in_who = False
        m = CHANNEL_NAME_RE.match(body) or TELL_NAME_RE.match(body)
        if m:
            players.add(m.group(1))


def is_hidden(body: str, filters: list[str], players: set[str] | None) -> bool:
    for f in filters:
        m = FILTER_PATTERNS[f].match(body)
        if not m:
            continue
        if f == "say" and players is not None:
            # nur Saetze von bekannten Spielern ausblenden, NPCs durchlassen
            if not set(NAME_WORD_RE.findall(m.group(1))) & players:
                continue
        return True
    return False


def filter_chat(text: str, filters: list[str],
                players: set[str] | None = None) -> tuple[str, int]:
    """Entfernt Chat-Zeilen (samt eingerueckter Folgezeilen). Gibt (Text, Anzahl) zurueck.

    Mit 'players' blendet der say-Filter nur Saetze dieser Spieler aus.
    """
    if not filters:
        return text, 0
    out, hidden, in_msg = [], 0, False
    for line in text.split("\n"):
        prefix = PROMPT_PREFIX_RE.match(line).group(1)   # "> " vor der Nachricht behalten
        body = line[len(prefix):]
        if in_msg and body[:1] in (" ", "\t") and body.strip():
            continue                                     # Folgezeile einer umbrochenen Nachricht
        in_msg = False
        if is_hidden(body, filters, players):
            hidden += 1
            in_msg = True
            if prefix:
                out.append(prefix.rstrip(" ") + " ")
            continue
        out.append(line)
    return "\n".join(out), hidden

mcp = MCPServer("nightfall-mud")


class Session:
    def __init__(self):
        self.sock = None
        self.reader = None
        self.running = False
        self.buf = bytearray()
        self.leftover = b""
        self.lock = threading.Lock()
        self.closed_note = None
        self.pending = ""   # zurueckgehaltene, unvollstaendige Zeile (Filter)
        self.auto_page = True
        self.history = deque(maxlen=HISTORY_LINES)   # gefilterte Ausgabe, nur im Speicher
        self.players = set()   # bekannte Spielernamen fuer den say-Filter, nur im Speicher

    def _process(self, data: bytes):
        """Trennt sichtbaren Text von Telnet-Kommandos, lehnt Optionen ab."""
        out = bytearray()
        reply = bytearray()
        buf = self.leftover + data
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:                       # IAC ohne Kommando -> aufheben
                self.leftover = bytes(buf[i:])
                break
            cmd = buf[i + 1]
            if cmd == IAC:                       # IAC IAC = echtes 0xFF
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:                   # Option fehlt noch -> aufheben
                    self.leftover = bytes(buf[i:])
                    break
                opt = buf[i + 2]
                answer = DONT if cmd in (WILL, WONT) else WONT   # alles ablehnen
                reply += bytes((IAC, answer, opt))
                i += 3
            elif cmd == SB:                      # Subnegotiation bis IAC SE weg
                j = i + 2
                while j + 1 < n and not (buf[j] == IAC and buf[j + 1] == SE):
                    j += 1
                if j + 1 >= n:
                    self.leftover = bytes(buf[i:])
                    break
                i = j + 2
            else:                                # einzelnes Kommando ignorieren
                i += 2
        else:
            self.leftover = b""
        return bytes(out), bytes(reply)

    def _read_loop(self):
        self.sock.settimeout(0.5)
        while self.running:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                self.closed_note = "[Server hat die Verbindung geschlossen]"
                break
            text, reply = self._process(data)
            if reply:
                try:
                    self.sock.sendall(reply)
                except OSError:
                    pass
            if text:
                with self.lock:
                    self.buf += text
        self.running = False

    def connect(self, host, port, auto_page=True):
        self.disconnect()
        s = socket.create_connection((host, port), timeout=10)
        self.sock = s
        self.running = True
        self.leftover = b""
        self.buf = bytearray()
        self.closed_note = None
        self.pending = ""
        self.auto_page = auto_page
        self.history.clear()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def drain(self) -> str:
        with self.lock:
            data = bytes(self.buf)
            self.buf = bytearray()
        data = ANSI_RE.sub(b"", data)
        # Telnet-Zeilenenden (\r\n) auf \n normalisieren, einzelne \r entfernen
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"")
        text = self.pending + data.decode("utf-8", "replace")
        self.pending = ""
        filters = active_filters()
        if not filters:
            return text
        # Eine angefangene Chat-Zeile erst filtern, wenn sie komplett ist
        head, sep, tail = text.rpartition("\n")
        tail_body = tail[len(PROMPT_PREFIX_RE.match(tail).group(1)):]
        if self.running and tail_body and any(PARTIAL_PATTERNS[f].match(tail_body) for f in filters):
            self.pending = tail
            text = head + sep
        learn_players(text, self.players)
        text, hidden = filter_chat(text, filters, self.players)
        if hidden:
            word = "Nachricht" if hidden == 1 else "Nachrichten"
            text += f"\n[{hidden} {word} anderer Spieler ausgeblendet]"
        return text

    def send(self, command: str):
        if not self.sock or not self.running:
            raise RuntimeError("nicht verbunden - erst mud_connect oder mud_attach aufrufen")
        self.sock.sendall(command.encode("utf-8", "replace") + b"\r\n")

    def disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


session = Session()


def collect(wait: float) -> str:
    """Wartet, liest die Ausgabe und blaettert den MUD-Pager automatisch durch.

    Endet die Ausgabe mit einem SMore-Prompt, wird eine Leerzeile (CR) geschickt,
    bis der Pager zu ist. Die Prompt-Zeilen werden aus dem Ergebnis entfernt.
    """
    time.sleep(max(0.0, wait))
    out = session.drain()
    pages = 0
    while session.auto_page and PAGER_RE.search(out) and session.running:
        if pages >= MAX_PAGES:
            out += f"\n[Pager nach {MAX_PAGES} Seiten angehalten - mud_send('') blaettert weiter, 'q' beendet]"
            break
        out = PAGER_RE.sub("", out)
        session.send("")
        time.sleep(PAGE_WAIT)
        out += session.drain()
        pages += 1
    if out:
        session.history.extend(out.split("\n"))
    return out


@mcp.tool()
def mud_connect(host: str = "nightfall.org", port: int = 4242, wait: float = 2.0) -> str:
    """Verbindet mit einem MUD und gibt den Begruessungstext (Login-Screen) zurueck."""
    try:
        session.connect(host, port)
    except OSError as e:
        return f"Verbindung fehlgeschlagen: {e}"
    return collect(wait) or "(verbunden, noch keine Ausgabe)"


@mcp.tool()
def mud_attach(port: int = 4001, wait: float = 0.5) -> str:
    """Haengt sich an den lokalen mud_proxy.py, in dem der Benutzer selbst spielt.

    Gibt den Stand des Proxys und die bisherige Ausgabe seit dem Login zurueck.
    Danach mit mud_read/mud_recent mitlesen und dem Benutzer Tipps geben.
    Senden (mud_send) klappt nur, wenn der Proxy mit --allow-send laeuft.
    """
    try:
        session.connect("127.0.0.1", port, auto_page=False)
    except OSError as e:
        return (f"Proxy nicht erreichbar auf 127.0.0.1:{port}: {e}\n"
                "Laeuft 'python mud_proxy.py' in einem eigenen Fenster?")
    return collect(wait) or "(angemeldet, noch keine Ausgabe)"


@mcp.tool()
def mud_recent(lines: int = 40) -> str:
    """Gibt die letzten Zeilen der (gefilterten) Ausgabe zurueck, inkl. bereits gelesener.

    Gut, um im Proxy-Modus den aktuellen Spielstand zu ueberblicken.
    """
    collect(0)
    recent = list(session.history)[-max(1, lines):]
    text = "\n".join(recent).strip("\n")
    if session.closed_note and not session.running:
        text += ("\n" if text else "") + session.closed_note
    return text or "(noch keine Ausgabe)"


@mcp.tool()
def mud_send(command: str, wait: float = 0.8) -> str:
    """Schickt einen Befehl ans MUD und gibt die darauffolgende Ausgabe zurueck.

    ACHTUNG: 'command' landet im Chatverlauf. Kein echtes Passwort hier verwenden -
    lieber einen Wegwerf-Charakter nehmen oder dir bewusst sein, dass es im Log steht.
    Im Proxy-Modus nur moeglich, wenn der Benutzer den Proxy mit --allow-send startet.
    """
    try:
        session.send(command)
    except RuntimeError as e:
        return str(e)
    out = collect(wait)
    if session.closed_note and not session.running:
        out += ("\n" if out else "") + session.closed_note
    return out or "(keine Ausgabe)"


@mcp.tool()
def mud_read(wait: float = 1.0) -> str:
    """Wartet kurz und gibt neue, seit dem letzten Lesen aufgelaufene Ausgabe zurueck.

    Nuetzlich bei verzoegerten Ereignissen (andere Spieler, Timer, langsame Antworten).
    """
    out = collect(wait)
    if session.closed_note and not session.running:
        out += ("\n" if out else "") + session.closed_note
    return out or "(keine neue Ausgabe)"


@mcp.tool()
def mud_disconnect() -> str:
    """Trennt die Verbindung zum MUD."""
    session.disconnect()
    return "getrennt"


if __name__ == "__main__":
    mcp.run()
