#!/usr/bin/env python3
"""
mud_mcp_server.py — MCP-Server: Telnet-Bruecke zu NightFall (oder jedem MUD).

Tools:
    mud_connect(host, port, wait)   -> verbindet, gibt Begruessungstext zurueck
    mud_send(command, wait)         -> schickt einen Befehl, gibt die Antwort zurueck
    mud_read(wait)                  -> gibt neu aufgelaufene Ausgabe zurueck
    mud_disconnect()                -> trennt

Transport: stdio.  Abhaengigkeit:  pip install -r requirements.txt   (mcp>=2,<3, Python 3.10+)

Ein Hintergrund-Thread liest den Stream fortlaufend in einen Puffer; die Tools
ziehen daraus. Telnet-Optionen werden abgelehnt (einfacher Zeilenmodus),
ANSI-Farbcodes werden fuer die Rueckgabe entfernt. Der NightFall-Pager
("=== SMore: ...") wird automatisch durchgeblaettert.

Datenschutz: Nachrichten anderer Spieler werden VOR der Rueckgabe entfernt und
landen so nicht im Claude-Session-Protokoll. Gesteuert ueber die Umgebungs-
variable MUD_FILTER (Komma-Liste, Standard "channels,tells"):
    channels  Kanalzeilen wie "[Bill] text" oder "[news:Bob enters the world]"
    tells     private Nachrichten: "Bob tells you: ...", "Bob whispers to you: ..."
    say       Sprache im Raum: "Bob says: ..." (blendet auch NPC-Saetze aus!)
    none      keinen Filter verwenden
"""

import os
import re
import socket
import threading
import time

from mcp.server.mcpserver import MCPServer

# Telnet-Steuerbytes
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Pager-Prompt von NightFall, z.B. "=== SMore: Lines 1..16 of 40 [CR,u,f,l,p,q,...] "
PAGER_RE = re.compile(r"=== SMore: Lines \d+\.\.\d+ of \d+ \[[^\]\n]*\][^\n]*\n?$")
PAGE_WAIT = 0.6   # Sekunden Wartezeit pro weitergeblaetterter Seite
MAX_PAGES = 50    # Schutz gegen Endlosschleifen

# --- Filter fuer Nachrichten anderer Spieler ---------------------------------
PROMPT_PREFIX_RE = re.compile(r"^((?:> )*)")
FILTER_PATTERNS = {
    # "[Bill] wer ist online?", "[news:Bob enters the world]", "[chat:Bob] hi"
    "channels": re.compile(r"^\[[^\]\n]{1,60}\](?: .*)?$"),
    "tells": re.compile(r"^[A-Z][\w-]* (?:tells|whispers to|asks) you(?: [^:\n]*)?: "),
    "say": re.compile(r"^[A-Z][\w-]*(?: [\w-]+)? (?:says|asks|exclaims)(?: [^:\n]*)?: "),
}
# Unvollstaendige letzte Zeile zurueckhalten, wenn sie schon als Chat erkennbar ist
PARTIAL_PATTERNS = {
    "channels": re.compile(r"^\["),
    "tells": FILTER_PATTERNS["tells"],
    "say": FILTER_PATTERNS["say"],
}


def active_filters() -> list[str]:
    raw = os.environ.get("MUD_FILTER", "channels,tells")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    if "none" in names:
        return []
    return [n for n in names if n in FILTER_PATTERNS]


def filter_chat(text: str, filters: list[str]) -> tuple[str, int]:
    """Entfernt Chat-Zeilen (samt eingerueckter Folgezeilen). Gibt (Text, Anzahl) zurueck."""
    if not filters:
        return text, 0
    out, hidden, in_msg = [], 0, False
    for line in text.split("\n"):
        prefix = PROMPT_PREFIX_RE.match(line).group(1)   # "> " vor der Nachricht behalten
        body = line[len(prefix):]
        if in_msg and body[:1] in (" ", "\t") and body.strip():
            continue                                     # Folgezeile einer umbrochenen Nachricht
        in_msg = False
        if any(FILTER_PATTERNS[f].match(body) for f in filters):
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

    def connect(self, host, port):
        self.disconnect()
        s = socket.create_connection((host, port), timeout=10)
        self.sock = s
        self.running = True
        self.leftover = b""
        self.buf = bytearray()
        self.closed_note = None
        self.pending = ""
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
        text, hidden = filter_chat(text, filters)
        if hidden:
            word = "Nachricht" if hidden == 1 else "Nachrichten"
            text += f"\n[{hidden} {word} anderer Spieler ausgeblendet]"
        return text

    def send(self, command: str):
        if not self.sock or not self.running:
            raise RuntimeError("nicht verbunden - erst mud_connect aufrufen")
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
    while PAGER_RE.search(out) and session.running:
        if pages >= MAX_PAGES:
            return out + f"\n[Pager nach {MAX_PAGES} Seiten angehalten - mud_send('') blaettert weiter, 'q' beendet]"
        out = PAGER_RE.sub("", out)
        session.send("")
        time.sleep(PAGE_WAIT)
        out += session.drain()
        pages += 1
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
def mud_send(command: str, wait: float = 0.8) -> str:
    """Schickt einen Befehl ans MUD und gibt die darauffolgende Ausgabe zurueck.

    ACHTUNG: 'command' landet im Chatverlauf. Kein echtes Passwort hier verwenden -
    lieber einen Wegwerf-Charakter nehmen oder dir bewusst sein, dass es im Log steht.
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
