"""
Tests fuer mud_proxy.py: du spielst, Claude liest mit - alles lokal mit dem Fake-MUD.

    .\\.venv\\Scripts\\python.exe -m unittest discover -s test -v
"""

import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT))
from fake_mud import FakeMud  # noqa: E402
from mud_proxy import TelnetScanner  # noqa: E402
from test_mcp_server import McpClient  # noqa: E402

PASSWORD = "Sup3r-Geheim!42"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UserClient:
    """Simuliert deinen MUD-Client (Mudlet, PuTTY ...).

    negotiate=True: antwortet wie ein echter Client auf Telnet-Optionen
    (DO/DONT/WILL/WONT). eol: Zeilenende, das der Client schickt.
    """

    def __init__(self, port, negotiate=False, eol=b"\r\n"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = bytearray()
        self.scanner = TelnetScanner()
        self.negotiate, self.eol = negotiate, eol
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        from mud_proxy import DO, DONT, IAC, WILL, WONT
        answer = {WILL: DO, WONT: DONT, DO: WONT, DONT: WONT}
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError:
                return
            if not data:
                return
            text, events = self.scanner.feed(data)
            self.buf += text
            if self.negotiate:
                for cmd, opt in events:
                    self.sock.sendall(bytes((IAC, answer[cmd], opt)))

    @property
    def text(self) -> str:
        return self.buf.decode("utf-8", "replace")

    def send(self, line: str):
        self.sock.sendall(line.encode("utf-8") + self.eol)

    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def wait_for(self, needle: str, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.text:
                return
            time.sleep(0.05)
        raise AssertionError(f"{needle!r} kam nicht an. Bisher:\n{self.text}")

    def close(self):
        self.sock.close()


class ProxyTest(unittest.TestCase):
    allow_send = False

    def setUp(self):
        self.mud = FakeMud().start()
        self.play_port, self.ctrl_port = free_port(), free_port()
        args = [sys.executable, str(ROOT / "mud_proxy.py"), "--mud-host", "127.0.0.1",
                "--mud-port", str(self.mud.port), "--port", str(self.play_port),
                "--control-port", str(self.ctrl_port)]
        if self.allow_send:
            args.append("--allow-send")
        self.proxy = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8")
        self._wait_listening(self.ctrl_port)
        self.mcp = McpClient()
        self.user = None

    def _wait_listening(self, port):
        end = time.time() + 5
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Proxy startet nicht")

    def tearDown(self):
        self.mcp.close()
        if self.user:
            self.user.close()
        self.proxy.terminate()
        self.proxy_output = self.proxy.communicate(timeout=5)[0]
        self.mud.shutdown()
        self.mud.server_close()
        # Das Passwort darf auch nicht in der Ausgabe des Proxys stehen
        self.assertNotIn(PASSWORD, self.proxy_output)

    # --- Helfer ---------------------------------------------------------------
    def attach(self):
        return self.mcp.call("mud_attach", port=self.ctrl_port, wait=0.3)

    def user_login(self, name="tester", **client_opts):
        self.user = UserClient(self.play_port, **client_opts)
        self.user.wait_for("Enter your name")
        self.user.send(name)
        if name == "guest":
            self.user.wait_for("terminal type")
            self.user.send("dumb")
        else:
            self.user.wait_for("Password:")
            self.user.send(PASSWORD)
        self.user.wait_for("white painted test room")

    def read(self, wait=0.5):
        return self.mcp.call("mud_read", wait=wait)

    def mud_lines(self):
        return self.mud.connections[0].lines


class TestReadOnly(ProxyTest):
    def test_password_never_reaches_claude(self):
        self.assertIn("kein Spiel aktiv", self.attach())
        self.user_login()
        out = self.read()
        self.assertIn(PASSWORD, self.mud_lines())            # beim MUD angekommen
        self.assertNotIn(PASSWORD, out)                      # aber nie bei Claude
        self.assertNotIn(PASSWORD, self.mcp.call("mud_recent", lines=500))
        self.assertNotIn("Enter your name", out)             # Login-Phase verborgen
        self.assertNotIn("Password:", out)
        self.assertIn("Claude liest ab jetzt mit (Login erkannt)", out)
        self.assertIn("white painted test room", out)

    def test_user_input_is_not_shown_to_claude(self):
        self.attach()
        self.user_login()
        self.user.send("read map")
        self.user.wait_for("Map line 16")
        out = self.read()
        self.assertIn("Map line  1", out)
        self.assertIn("SMore", out)                          # Pager wird NICHT geblaettert
        self.assertNotIn("Map line 17", out)
        self.assertNotIn("read map", out)                    # deine Eingabe sieht Claude nicht

    def test_attach_later_gets_history(self):
        self.user_login()
        out = self.attach()
        self.assertIn("Claude liest mit", out)
        self.assertIn("white painted test room", out)
        self.assertNotIn("Enter your name", out)

    def test_claude_cannot_send_by_default(self):
        self.attach()
        self.user_login()
        self.read()
        out = self.mcp.call("mud_send", command="look", wait=0.5)
        self.assertIn("Senden ist nicht erlaubt", out)
        self.assertNotIn("look", self.mud_lines())
        self.assertNotIn("*** Claude:", self.user.text)

    def test_pause_and_resume(self):
        self.attach()
        self.user_login()
        self.read()
        self.user.send("/claude off")
        self.user.wait_for("Mitlesen pausiert")
        self.user.send("chatter")
        self.user.wait_for("The light brightens.")
        out = self.read()
        self.assertIn("Mitlesen pausiert (von dir pausiert)", out)
        self.assertNotIn("light brightens", out)
        self.user.send("/claude on")
        self.user.send("look")
        time.sleep(0.5)
        out = self.read()
        self.assertIn("white painted test room", out)
        self.assertNotIn("/claude off", self.mud_lines())    # /claude geht nicht ans MUD
        self.assertNotIn("/claude on", self.mud_lines())

    def test_guest_needs_manual_on(self):
        self.attach()
        self.user_login("guest")
        self.assertNotIn("white painted", self.read())       # kein Passwort -> kein Auto-Start
        self.user.send("/claude on")
        self.user.send("look")
        time.sleep(0.5)
        self.assertIn("white painted test room", self.read())

    def test_channel_filter_through_proxy(self):
        self.attach()
        self.user_login()
        self.read()
        self.user.send("chatter")
        self.user.wait_for("The light brightens.")
        self.assertIn("[Bill]", self.user.text)              # du siehst alles
        out = self.read()
        self.assertNotIn("Bill", out)                        # Claude nicht
        self.assertIn("ausgeblendet", out)
        self.assertIn("The light brightens.", out)

    def test_game_end_is_reported(self):
        self.attach()
        self.user_login()
        self.read()
        self.user.send("quit")
        time.sleep(0.5)
        self.assertIn("Spiel beendet", self.read())


class TestRealClient(ProxyTest):
    """/claude-Befehle mit einem Client, der sich wie Mudlet/PuTTY verhaelt."""

    def check_commands(self):
        self.attach()
        self.read()
        self.user.send("/claude status")
        self.user.wait_for("Claude liest mit; Claude darf nur mitlesen")
        self.user.send("/claude off")
        self.user.wait_for("Mitlesen pausiert")
        self.user.send("/claude on")
        self.user.wait_for("Claude liest ab jetzt mit (von dir eingeschaltet)")
        for cmd in ("/claude status", "/claude off", "/claude on"):
            self.assertNotIn(cmd, self.mud_lines())          # nichts davon ans MUD

    def test_with_telnet_negotiation(self):
        self.user_login(negotiate=True)                      # Client antwortet auf ECHO usw.
        self.check_commands()

    def test_with_cr_nul_line_end(self):
        self.user_login(negotiate=True, eol=b"\r\0")         # Telnet-Zeichenmodus
        self.check_commands()

    def test_with_lf_line_end(self):
        self.user_login(negotiate=True, eol=b"\n")
        self.check_commands()

    def test_negotiation_right_before_command(self):
        from mud_proxy import IAC, NAWS, SB, SE
        self.user_login(negotiate=True)
        self.attach()
        self.read()
        # Fenstergroesse geaendert (NAWS) direkt vor dem Befehl
        self.user.send_raw(bytes((IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE)))
        self.user.send("/claude off")
        self.user.wait_for("Mitlesen pausiert")
        self.assertNotIn("/claude off", self.mud_lines())

    def test_backspace_while_typing(self):
        self.user_login(negotiate=True)
        self.attach()
        self.read()
        self.user.send("/claude ofx\x7ff")                   # Tippfehler mit Backspace korrigiert
        self.user.wait_for("Mitlesen pausiert")


class TestReconnect(ProxyTest):
    """MUD trennt, dein Client bleibt verbunden."""

    def quit_game(self):
        self.attach()
        self.user_login(negotiate=True)
        self.read()
        self.user.send("quit")
        self.user.wait_for("Verbindung zum MUD beendet")

    def test_client_stays_connected_and_commands_work(self):
        self.quit_game()
        self.user.send("/claude status")
        self.user.wait_for("nicht mit dem MUD verbunden - /claude connect verbindet neu")
        self.user.send("look")                               # geht nirgends hin
        time.sleep(0.3)
        self.assertEqual(1, len(self.mud.connections))       # kein heimliches Neuverbinden
        self.assertIn("Spiel beendet (MUD hat die Verbindung beendet)", self.read())

    def test_reconnect_and_login_again(self):
        self.quit_game()
        self.user.send("/claude connect")
        end = time.time() + 3
        while self.user.text.count("Enter your name") < 2 and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(2, len(self.mud.connections))
        self.user.send("tester")
        self.user.wait_for("Password:")
        self.user.send(PASSWORD)
        time.sleep(0.5)
        out = self.read()
        self.assertIn("neues Spiel gestartet", out)
        self.assertIn("Claude liest ab jetzt mit (Login erkannt)", out)
        self.assertIn("white painted test room", out)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn("Enter your name", out)

    def test_disconnect_in_login_screen(self):
        self.attach()
        self.user = UserClient(self.play_port, negotiate=True)
        self.user.wait_for("Enter your name")
        self.user.send("/claude disconnect")
        self.user.wait_for("Verbindung zum MUD beendet (von dir getrennt)")
        end = time.time() + 2
        while not self.mud.connections[0].closed and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(self.mud.connections[0].closed)      # MUD-Verbindung wirklich zu
        self.assertNotIn("/claude disconnect", self.mud_lines())
        self.user.send("/claude status")                     # Client ist noch verbunden
        self.user.wait_for("nicht mit dem MUD verbunden")
        self.user.send("/claude disconnect")                 # zweimal ist harmlos
        time.sleep(0.3)
        self.assertEqual(1, self.user.text.count("Verbindung zum MUD beendet"))
        self.user.send("/claude connect")                    # und wieder verbinden
        end = time.time() + 3
        while self.user.text.count("Enter your name") < 2 and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(2, len(self.mud.connections))
        self.assertIn("Spiel beendet (von dir getrennt)", self.read())

    def test_client_disconnect_ends_game(self):
        self.attach()
        self.user_login(negotiate=True)
        self.read()
        self.user.close()
        time.sleep(0.5)
        self.assertIn("Spiel beendet (Client hat die Verbindung beendet)", self.read())


class TestAllowSend(ProxyTest):
    allow_send = True

    def test_claude_can_send_after_login(self):
        self.attach()
        self.user_login()
        self.read()
        out = self.mcp.call("mud_send", command="look", wait=0.5)
        self.assertIn("white painted test room", out)
        self.assertIn("look", self.mud_lines())
        self.user.wait_for("*** Claude: look")               # du siehst Claudes Befehl

    def test_claude_cannot_send_during_login(self):
        self.attach()
        self.user = UserClient(self.play_port)
        self.user.wait_for("Enter your name")
        out = self.mcp.call("mud_send", command="eindringling", wait=0.5)
        self.assertIn("Senden gesperrt", out)
        self.user.send("tester")
        self.user.wait_for("Password:")
        out = self.mcp.call("mud_send", command="eindringling", wait=0.5)
        self.assertIn("Senden gesperrt", out)                # erst recht nicht ins Passwortfeld
        self.assertNotIn("eindringling", self.mud_lines())


if __name__ == "__main__":
    unittest.main()
