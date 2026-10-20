#!/usr/bin/env python3
"""
mud_proxy.py - lokaler Proxy: du spielst selbst, Claude liest mit.

    dein MUD-Client  ->  127.0.0.1:4000  ->  mud_proxy.py  ->  nightfall.org:4242
    MCP-Server       ->  127.0.0.1:4001  (Mitlese-Anschluss fuer Claude)

Starten:   python mud_proxy.py                 (Claude darf nur mitlesen)
           python mud_proxy.py --allow-send    (Claude darf auch Befehle schicken)

Dann den MUD-Client (Mudlet, PuTTY "Telnet", ...) mit 127.0.0.1:4000 verbinden.
Erst dann baut der Proxy die Verbindung zum MUD auf. Endet die Verbindung zum
MUD (quit, Timeout ...), bleibt dein Client mit dem Proxy verbunden; mit
/claude connect verbindest du dich neu.

Schutz der Zugangsdaten:
  * Was du tippst, wird nur weitergeleitet - nie gespeichert, protokolliert oder
    an Claude gegeben.
  * Claude sieht nichts aus der Login-Phase. Das Mitlesen beginnt automatisch,
    sobald das MUD nach der Passwort-Eingabe das Echo wieder einschaltet
    (Telnet IAC WILL ECHO -> IAC WONT ECHO).
  * Claude kann waehrend Login und Passwort-Eingabe keine Befehle schicken.
  * Kein Protokoll auf der Platte; im Fenster stehen nur Statusmeldungen.

Befehle in deinem MUD-Client (gehen NICHT ans MUD):
    /claude on      Mitlesen einschalten (z.B. als Gast ohne Passwort)
    /claude off     Mitlesen pausieren
    /claude status  aktuellen Stand anzeigen
    /claude connect neu mit dem MUD verbinden, nachdem die Verbindung beendet wurde
"""

import argparse
import socket
import threading

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, NAWS = 1, 31
CR, LF, NUL = 13, 10, 0
HISTORY_BYTES = 64 * 1024    # Speicherpuffer fuer Claude (nur Ausgabe des MUDs)


class TelnetScanner:
    """Trennt Text von Telnet-Kommandos, auch ueber Paketgrenzen hinweg."""

    def __init__(self):
        self.state = "text"
        self.cmd = 0

    def feed(self, data: bytes):
        text, events = bytearray(), []
        for b in data:
            if self.state == "text":
                if b == IAC:
                    self.state = "iac"
                else:
                    text.append(b)
            elif self.state == "iac":
                if b == IAC:
                    text.append(IAC)
                    self.state = "text"
                elif b in (DO, DONT, WILL, WONT):
                    self.cmd, self.state = b, "opt"
                elif b == SB:
                    self.state = "sb"
                else:
                    self.state = "text"
            elif self.state == "opt":
                events.append((self.cmd, b))
                self.state = "text"
            elif self.state == "sb":
                if b == IAC:
                    self.state = "sb_iac"
            elif self.state == "sb_iac":
                self.state = "text" if b == SE else "sb"
        return bytes(text), events


def visible_line(raw: bytes) -> str:
    """Zeile so, wie sie nach Backspace-Korrekturen aussieht (\\b oder DEL)."""
    chars = []
    for ch in raw.decode("utf-8", "replace"):
        if ch in "\b\x7f":
            if chars:
                chars.pop()
        else:
            chars.append(ch)
    return "".join(chars).strip()


def log(msg: str):
    print(f"[proxy] {msg}", flush=True)


class Proxy:
    def __init__(self, mud_host, mud_port, listen_port, control_port, allow_send):
        self.mud_addr = (mud_host, mud_port)
        self.allow_send = allow_send
        self.lock = threading.RLock()
        self.client = None
        self.mud = None
        self.control = None
        self.share = None        # None = Login-Phase, True = Claude liest mit, False = pausiert
        self.echo_off = False    # MUD hat Echo abgeschaltet (Passwort-Eingabe)
        self.saw_password = False
        self.history = bytearray()
        self.listen_sock = self._listen(listen_port)
        self.control_sock = self._listen(control_port)

    @staticmethod
    def _listen(port):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(2)
        return s

    @property
    def ports(self):
        return self.listen_sock.getsockname()[1], self.control_sock.getsockname()[1]

    # --- Ausgabe ------------------------------------------------------------
    @staticmethod
    def _send(sock, data: bytes):
        if sock:
            try:
                sock.sendall(data)
            except OSError:
                pass

    def to_client(self, msg: str):
        self._send(self.client, f"\r\n*** Proxy: {msg}\r\n".encode("utf-8"))

    def to_control(self, msg: str):
        self._send(self.control, f"*** Proxy: {msg}\r\n".encode("utf-8"))

    def status_text(self) -> str:
        if not self.mud:
            return "nicht mit dem MUD verbunden - /claude connect verbindet neu"
        if self.share is None:
            state = "Login-Phase, Claude sieht noch nichts"
        elif self.share:
            state = "Claude liest mit"
        else:
            state = "Mitlesen pausiert"
        send = "darf Befehle schicken" if self.allow_send else "darf nur mitlesen"
        return f"{state}; Claude {send}"

    def set_share(self, value: bool, reason: str):
        with self.lock:
            if self.share == value:
                return
            self.share = value
            msg = ("Claude liest ab jetzt mit" if value else "Mitlesen pausiert") + f" ({reason})"
            self.to_client(msg)
            self.to_control(msg)
            log(msg)

    # --- MUD -> Client ------------------------------------------------------
    def pump_mud(self, mud):
        scanner = TelnetScanner()
        while True:
            try:
                data = mud.recv(4096)
            except OSError:
                break
            if not data:
                break
            self._send(self.client, data)            # roh weiter an deinen Client
            text, events = scanner.feed(data)
            with self.lock:
                for cmd, opt in events:
                    if opt != ECHO:
                        continue
                    if cmd == WILL:                  # Passwort-Eingabe beginnt
                        self.echo_off = True
                        self.saw_password = True
                    elif cmd == WONT and self.echo_off:
                        self.echo_off = False
                        if self.share is None and self.saw_password:
                            self.set_share(True, "Login erkannt")
                if text and self.share:
                    self.history += text
                    del self.history[:-HISTORY_BYTES]
                    self._send(self.control, text)
        self.mud_ended(mud, "MUD hat die Verbindung beendet")

    # --- Client -> MUD ------------------------------------------------------
    def pump_client(self, client):
        """Leitet deine Eingaben weiter. Nur Zeilen, die mit '/' beginnen, werden bis
        zum Zeilenende gehalten, um /claude-Befehle abzufangen. Nichts wird gespeichert.

        Telnet-Sequenzen des Clients (Antworten auf ECHO, Fenstergroesse ...) gehen
        sofort durch und aendern nichts am Zeilenanfang. Zeilenende ist \\r\\n, \\n,
        \\r\\0 (Telnet-Zeichenmodus) oder ein einzelnes \\r.
        """
        tstate = "text"          # Telnet-Zustand des Client-Stroms
        at_line_start = True
        held = None              # gehaltene Zeile, die mit '/' beginnt
        after_cr = False         # letztes Text-Byte war \r
        drop_after_cr = False    # \n bzw. \0 nach einem abgefangenen Befehl verschlucken
        while True:
            try:
                data = client.recv(4096)
            except OSError:
                break
            if not data:
                break
            out = bytearray()
            for b in data:
                # --- Telnet-Sequenzen: unveraendert weiter, Zeilenzustand bleibt ---
                if tstate != "text" or b == IAC:
                    out.append(b)
                    if tstate == "text":
                        tstate = "iac"
                    elif tstate == "iac":
                        tstate = ("opt" if b in (DO, DONT, WILL, WONT) else
                                  "sb" if b == SB else "text")
                    elif tstate == "opt":
                        tstate = "text"
                    elif tstate == "sb":
                        tstate = "sb_iac" if b == IAC else "sb"
                    elif tstate == "sb_iac":
                        tstate = "text" if b == SE else "sb"
                    continue
                # --- zweite Haelfte von \r\n bzw. \r\0 ---
                if after_cr and b in (LF, NUL):
                    after_cr = False
                    if not drop_after_cr:
                        out.append(b)
                    continue
                after_cr = b == CR
                drop_after_cr = False
                # --- gehaltene /-Zeile ---
                if held is not None:
                    if b in (CR, LF):
                        if self.proxy_command(visible_line(held)):
                            drop_after_cr = b == CR
                        else:
                            out += held
                            out.append(b)
                        held, at_line_start = None, True
                    else:
                        held.append(b)
                    continue
                if at_line_start and b == ord("/"):
                    held = bytearray([b])
                    continue
                out.append(b)
                at_line_start = b in (CR, LF)
                if at_line_start and self.mud is None:   # Zeile getippt, aber kein MUD
                    self.to_client("nicht mit dem MUD verbunden - /claude connect verbindet neu")
            mud = self.mud
            if out and mud:
                self._send(mud, bytes(out))      # ohne MUD-Verbindung wird nichts gesendet
        self.client_ended(client)

    def proxy_command(self, line: str) -> bool:
        parts = line.split()
        if not parts or parts[0].lower() != "/claude":
            return False
        arg = parts[1].lower() if len(parts) > 1 else "status"
        if arg == "on":
            if self.mud:
                self.set_share(True, "von dir eingeschaltet")
            else:
                self.to_client(self.status_text())
        elif arg == "off":
            if self.mud:
                self.set_share(False, "von dir pausiert")
            else:
                self.to_client(self.status_text())
        elif arg == "connect":
            if self.mud:
                self.to_client("schon mit dem MUD verbunden")
            else:
                self.start_mud()
        else:
            self.to_client(self.status_text())
        return True

    def start_mud(self):
        """Verbindet mit dem MUD - nur auf deine Aktion hin (Client verbunden, /claude connect)."""
        self.to_client(f"verbinde mit {self.mud_addr[0]}:{self.mud_addr[1]} ...")
        try:
            mud = socket.create_connection(self.mud_addr, timeout=10)
            mud.settimeout(None)
        except OSError as e:
            self.to_client(f"MUD nicht erreichbar: {e} - /claude connect versucht es erneut")
            log(f"MUD nicht erreichbar: {e}")
            return
        with self.lock:
            self.mud = mud
            self.share, self.echo_off, self.saw_password = None, False, False
            self.history.clear()
        log(f"Spiel gestartet: {self.mud_addr[0]}:{self.mud_addr[1]}")
        self.to_client("Claude liest erst nach dem Login mit. "
                       "Befehle: /claude on | off | status | connect")
        self.to_control("neues Spiel gestartet - Claude liest nach dem Login mit")
        threading.Thread(target=self.pump_mud, args=(mud,), daemon=True).start()

    def mud_ended(self, mud, reason):
        """Verbindung zum MUD ist zu. Dein Client bleibt verbunden."""
        with self.lock:
            if self.mud is not mud:      # dieses Spiel ist schon beendet
                return
            try:
                mud.close()
            except OSError:
                pass
            self.mud = None
            self.share, self.echo_off, self.saw_password = None, False, False
            self.history.clear()
            self.to_control(f"Spiel beendet ({reason})")
            self.to_client(f"Verbindung zum MUD beendet ({reason}). "
                           "/claude connect verbindet neu")
            log(f"Spiel beendet ({reason})")

    def client_ended(self, client):
        """Dein Client hat sich getrennt -> auch die MUD-Verbindung schliessen."""
        with self.lock:
            if self.client is not client:
                return
            self.client = None
            mud = self.mud
        try:
            client.close()
        except OSError:
            pass
        log("MUD-Client getrennt")
        if mud:
            self.mud_ended(mud, "Client hat die Verbindung beendet")

    # --- Claude (Mitlese-Anschluss) -----------------------------------------
    def pump_control(self, ctrl):
        buf = b""
        while True:
            try:
                data = ctrl.recv(4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.claude_command(line.decode("utf-8", "replace").rstrip("\r"))
        with self.lock:
            if self.control is ctrl:
                self.control = None
                log("Claude hat sich abgemeldet")

    def claude_command(self, cmd: str):
        with self.lock:
            if not self.allow_send:
                self.to_control("Senden ist nicht erlaubt - Claude darf nur mitlesen "
                                "(Proxy mit --allow-send starten)")
            elif not self.mud:
                self.to_control("kein Spiel aktiv")
            elif self.share is not True or self.echo_off:
                self.to_control("Senden gesperrt (Login, Passwort-Eingabe oder Mitlesen pausiert)")
            else:
                self._send(self.mud, cmd.encode("utf-8", "replace") + b"\r\n")
                self._send(self.client, f"\r\n*** Claude: {cmd}\r\n".encode("utf-8"))

    # --- Annehmen von Verbindungen -------------------------------------------
    def accept_clients(self):
        while True:
            client, _ = self.listen_sock.accept()
            with self.lock:
                busy = self.client is not None
            if busy:
                self._send(client, b"*** Proxy: es ist schon ein MUD-Client verbunden.\r\n")
                client.close()
                continue
            with self.lock:
                self.client = client
            log("MUD-Client verbunden")
            threading.Thread(target=self.pump_client, args=(client,), daemon=True).start()
            self.start_mud()

    def accept_control(self):
        while True:
            ctrl, _ = self.control_sock.accept()
            with self.lock:
                old, self.control = self.control, ctrl
                if old:
                    try:
                        old.close()
                    except OSError:
                        pass
                self.to_control(self.status_text() if self.mud else "kein Spiel aktiv")
                if self.share and self.history:
                    self._send(ctrl, bytes(self.history))
            log("Claude liest mit" if self.share else "Claude ist angemeldet")
            threading.Thread(target=self.pump_control, args=(ctrl,), daemon=True).start()

    def start(self):
        threading.Thread(target=self.accept_clients, daemon=True).start()
        threading.Thread(target=self.accept_control, daemon=True).start()
        return self


def main():
    ap = argparse.ArgumentParser(description="Lokaler MUD-Proxy: du spielst, Claude liest mit.")
    ap.add_argument("--mud-host", default="nightfall.org")
    ap.add_argument("--mud-port", type=int, default=4242)
    ap.add_argument("--port", type=int, default=4000, help="Port fuer deinen MUD-Client")
    ap.add_argument("--control-port", type=int, default=4001, help="Port fuer Claude")
    ap.add_argument("--allow-send", action="store_true", help="Claude darf Befehle schicken")
    args = ap.parse_args()
    proxy = Proxy(args.mud_host, args.mud_port, args.port, args.control_port, args.allow_send)
    proxy.start()
    play, ctrl = proxy.ports
    log(f"MUD-Client mit 127.0.0.1:{play} verbinden -> {args.mud_host}:{args.mud_port}")
    log(f"Claude-Anschluss: 127.0.0.1:{ctrl}; Claude "
        + ("darf Befehle schicken" if args.allow_send else "darf nur mitlesen"))
    log("Strg+C beendet")
    stop = threading.Event()
    try:
        while not stop.wait(1):      # mit Timeout, damit Strg+C unter Windows greift
            pass
    except KeyboardInterrupt:
        log("beendet")


if __name__ == "__main__":
    main()
