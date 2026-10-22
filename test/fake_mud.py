#!/usr/bin/env python3
"""
fake_mud.py - lokaler Fake-MUD zum Testen von mud_mcp_server.py ohne nightfall.org.

Lauscht nur auf 127.0.0.1. Ahmt nach, was die Bruecke verarbeiten muss:
Telnet-Aushandlung, ANSI-Farben, \\r\\n-Zeilenenden, Gast-Login, den SMore-Pager
und Nachrichten anderer Spieler.

Befehle im Spiel:
    look       Raumbeschreibung
    read map   40 Zeilen im SMore-Pager (Leerzeile = weiter, q = beenden)
    chatter    schickt Channel-, Tell- und Say-Nachrichten "anderer Spieler"
    split      schickt eine Channel-Nachricht in zwei Paketen (0.6 s Abstand)
    prompted   Channel-Nachricht hinter einem "->>"-Prompt (wie nach "pray")
    who        Spielerliste im NightFall-Format (Joe, Alice)
    talk       Saetze von Spielern (Alice) und NPCs (guard, story-teller)
    tell X Y   eigener Tell: Echo "tell X Y" und "You told X: Y"
    pagerprompt  Tell direkt hinter einem stehengebliebenen Pager-Prompt
    quit       speichert und trennt

Manuell starten:   python test/fake_mud.py --port 4000
Dann in Claude:    mud_connect(host="127.0.0.1", port=4000)
"""

import argparse
import socketserver
import threading
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA, TTYPE = 1, 3, 24
RED, RESET = "\x1b[1;31m", "\x1b[0m"

BANNER = (
    f"{RED}Fakefall{RESET} - lokaler Test-MUD\r\n"
    "LDMud-Imitat, kein echtes Spiel\r\n"
    "\r\n"
    "Enter your name, or type 'create' for a new player: "
)
ROOM = (
    "You are standing in a white painted test room. A map hangs on the wall.\r\n"
    "There is one exit: south.\r\n"
    "> "
)
MAP_LINES = [f"Map line {i:2d}" for i in range(1, 41)]
PAGE = 16

CHATTER = (
    "[Bill] wer ist online?\r\n"
    "> [news:Bob enters the world]\r\n"
    "Bob tells you: geheimes Passwort 1234\r\n"
    "Bob says: hallo zusammen\r\n"
    "[chat:Alice] eine lange Nachricht, die der MUD umbricht und in der\r\n"
    "    naechsten Zeile eingerueckt fortsetzt\r\n"
    "The light brightens.\r\n"
    "> "
)

PROMPTED = (
    "You hear a hollow voice asking you :\r\n"
    "Tell me the name!\r\n"
    "->>[fan:Joe sings a very long song about cheese and keeps on singing about it while\r\n"
    "   the sun goes down over the test hills.]\r\n"
    "That's wrong.\r\n"
    "> "
)
WHO = (
    "Players\r\n"
    " Guest1 the guest                                              [Hu,  1, ??v6]\r\n"
    " Lucky Old Joe the well known bard                             [Ba, 22, USv4]  HLP\r\n"
    "\r\n"
    "Wizards\r\n"
    " Alice is                                                          [Ro, DEv6]  Lib\r\n"
    "> "
)
TALK = (
    "Alice says: geheimes Gespraech\r\n"
    "   mit Folgezeile\r\n"
    "The guard says: Welcome, stranger.\r\n"
    "The story-teller says : Once upon a time there was a test room...\r\n"
    "> "
)


class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        self.raw = bytearray()          # alles, was der Client geschickt hat
        self.server.connections.append(self)
        self.page = None                # aktuelle Pager-Seite oder None
        self.state = "name"
        self.lines = []                 # empfangene Zeilen (fuer Tests)
        self.closed = False             # Client hat die Verbindung getrennt

    def finish(self):
        self.closed = True

    def out(self, text: str):
        self.request.sendall(text.encode("utf-8"))

    def pager_page(self):
        start = self.page * PAGE
        lines = MAP_LINES[start:start + PAGE]
        end = start + len(lines)
        self.out("".join(l + "\r\n" for l in lines))
        self.out(f"=== SMore: Lines {start + 1}..{end} of {len(MAP_LINES)} "
                 "[CR,u,f,l,p,q,/<regexp>,<line>,?] ")

    def handle(self):
        # Telnet-Optionen anbieten/anfragen - die Bruecke muss sie ablehnen
        self.request.sendall(bytes((IAC, WILL, SGA, IAC, DO, TTYPE)))
        self.request.sendall(bytes((IAC, SB, TTYPE, 1, IAC, SE)))
        self.out(BANNER)
        pending = b""
        while True:
            try:
                data = self.request.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.raw += data
            pending += strip_telnet(data)
            # Zeilenenden wie ein Telnet-Server: \r\n, \r\0 und \n
            pending = pending.replace(b"\r\n", b"\n").replace(b"\r\0", b"\n")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip("\r ")
                self.lines.append(text)
                if not self.command(text):
                    return

    def command(self, cmd: str) -> bool:
        if self.state == "name" and cmd.lower() != "guest":
            # echter Charakter: Passwort ohne Echo abfragen, wie LDMud (input_to noecho)
            self.state = "password"
            self.name = cmd.capitalize()
            self.out("Password: ")
            self.request.sendall(bytes((IAC, WILL, ECHO)))
        elif self.state == "password":
            self.state = "game"
            self.request.sendall(bytes((IAC, WONT, ECHO)))
            self.out(f"\r\nWelcome back, {self.name}!\r\n" + ROOM)
        elif self.state == "name":
            self.state = "term"
            self.out("Welcome to Fakefall!\r\n"
                     "Please select a terminal type (You can change later with <stty>)\r\n")
        elif self.state == "term":
            self.state = "game"
            self.out("vt100, ansi, dumb [dumb]\r\n\r\n" + ROOM)
        elif self.page is not None:            # im Pager
            if cmd == "q":
                self.page = None
                self.out("> ")
            elif cmd == "":
                self.page += 1
                if self.page * PAGE >= len(MAP_LINES):
                    self.page = None
                    self.out("> ")
                else:
                    self.pager_page()
            else:
                self.pager_page()
        elif cmd == "look":
            self.out(ROOM)
        elif cmd == "read map":
            self.page = 0
            self.pager_page()
        elif cmd == "chatter":
            self.out(CHATTER)
        elif cmd == "prompted":
            self.out(PROMPTED)
        elif cmd.startswith("tell "):
            name, _, msg = cmd[5:].partition(" ")
            # Echo des Befehls wie bei NightFall, dann die Bestaetigung
            self.out(f"{cmd}\r\nYou told {name.capitalize()}: {msg}\r\n> ")
        elif cmd == "pagerprompt":
            self.out("=== SMore: Lines 1..26 of 31 [CR,u,f,l,p,q] You told Bob: geheim\r\n> ")
        elif cmd == "who":
            self.out(WHO)
        elif cmd == "talk":
            self.out(TALK)
        elif cmd == "split":
            self.out("[Jame")
            time.sleep(0.6)
            self.out("son] geheim geteilt\r\n> ")
        elif cmd == "quit":
            self.out("Saving Guest1.\r\n")
            return False
        else:
            self.out("What?\r\n> ")
        return True


def strip_telnet(data: bytes) -> bytes:
    """Entfernt Telnet-Sequenzen des Clients (Optionen und IAC SB ... IAC SE)."""
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == IAC and i + 1 < len(data):
            cmd = data[i + 1]
            if cmd == SB:
                end = data.find(bytes((IAC, SE)), i + 2)
                i = len(data) if end < 0 else end + 2
            else:
                i += 3 if cmd in (DO, DONT, WILL, WONT) else 2
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


class FakeMud(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.connections = []

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--port", type=int, default=4000)
    args = ap.parse_args()
    srv = FakeMud(args.port)
    print(f"Fake-MUD laeuft auf 127.0.0.1:{srv.port} (Strg+C beendet)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
