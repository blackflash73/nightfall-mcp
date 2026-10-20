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
    quit       speichert und trennt

Manuell starten:   python test/fake_mud.py --port 4000
Dann in Claude:    mud_connect(host="127.0.0.1", port=4000)
"""

import argparse
import socketserver
import threading
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, TTYPE = 1, 24
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


class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        self.raw = bytearray()          # alles, was der Client geschickt hat
        self.server.connections.append(self)
        self.page = None                # aktuelle Pager-Seite oder None
        self.state = "name"

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
        self.request.sendall(bytes((IAC, WILL, ECHO, IAC, DO, TTYPE)))
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
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not self.command(line.decode("utf-8", "replace").strip("\r ")):
                    return

    def command(self, cmd: str) -> bool:
        if self.state == "name":
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
    """Entfernt die Telnet-Antworten des Clients (IAC + 2 Bytes)."""
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == IAC and i + 1 < len(data):
            i += 3 if data[i + 1] in (DO, DONT, WILL, WONT) else 2
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
