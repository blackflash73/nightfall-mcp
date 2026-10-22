"""
Tests fuer mud_mcp_server.py gegen den lokalen Fake-MUD - ohne nightfall.org.

Startet den MCP-Server als echten Subprozess und spricht MCP (JSON-RPC ueber
stdio) mit ihm, genau wie Claude Code.

    .\\.venv\\Scripts\\python.exe -m unittest discover -s test -v
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_mud import DONT, IAC, SGA, TTYPE, WONT, FakeMud  # noqa: E402

SERVER = Path(__file__).parent.parent / "mud_mcp_server.py"


class McpClient:
    def __init__(self, env_extra=None):
        env = dict(os.environ, **(env_extra or {}))
        self.p = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
        self.rpc("notifications/initialized", notify=True)

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("MCP-Server beendet: " + self.p.stderr.read())
        return json.loads(line)

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        return r["result"]["content"][0]["text"]

    def close(self):
        try:
            self.call("mud_disconnect")
        finally:
            self.p.stdin.close()
            self.p.terminate()
            self.p.wait(5)
            self.p.stdout.close()
            self.p.stderr.close()


class BridgeTest(unittest.TestCase):
    env = None   # MUD_FILTER-Variante je Testklasse

    def setUp(self):
        self.mud = FakeMud().start()
        self.mcp = McpClient(self.env)
        self.banner = self.mcp.call("mud_connect", host="127.0.0.1", port=self.mud.port, wait=0.5)

    def tearDown(self):
        self.mcp.close()
        self.mud.shutdown()
        self.mud.server_close()

    def login(self):
        self.send("guest")
        return self.send("dumb")

    def send(self, cmd, wait=0.3):
        return self.mcp.call("mud_send", command=cmd, wait=wait)


class TestBasics(BridgeTest):
    def test_banner_is_clean_text(self):
        self.assertIn("Fakefall - lokaler Test-MUD", self.banner)
        self.assertIn("Enter your name", self.banner)
        self.assertNotIn("\x1b", self.banner)       # ANSI entfernt
        self.assertNotIn("\r", self.banner)         # Zeilenenden normalisiert
        self.assertNotIn("\xff", self.banner)       # keine Telnet-Bytes

    def test_telnet_options_refused(self):
        self.send("guest")                          # sorgt dafuer, dass Antworten da sind
        raw = bytes(self.mud.connections[0].raw)
        self.assertIn(bytes((IAC, DONT, SGA)), raw)
        self.assertIn(bytes((IAC, WONT, TTYPE)), raw)

    def test_login_and_look(self):
        self.assertIn("white painted test room", self.login())
        self.assertIn("There is one exit: south.", self.send("look"))

    def test_pager_is_paged_automatically(self):
        self.login()
        out = self.mcp.call("mud_send", command="read map", wait=0.3)
        for i in (1, 16, 17, 32, 33, 40):
            self.assertIn(f"Map line {i:2d}", out)
        self.assertNotIn("SMore", out)
        self.assertIn("white painted test room", self.send("look"))   # Pager ist zu

    def test_server_closing_is_reported(self):
        self.login()
        out = self.send("quit", wait=0.5)
        self.assertIn("Saving Guest1.", out)
        self.assertIn("[Server hat die Verbindung geschlossen]", out)


class TestDefaultFilter(BridgeTest):
    """Standard: channels,tells,say"""

    def test_channels_tells_and_say_hidden(self):
        self.login()
        out = self.send("chatter")
        for secret in ("Bill", "wer ist online", "Bob enters", "geheimes Passwort",
                       "hallo zusammen", "Alice", "eingerueckt fortsetzt"):
            self.assertNotIn(secret, out)
        self.assertIn("The light brightens.", out)
        self.assertIn("[5 Nachrichten anderer Spieler ausgeblendet]", out)

    def test_player_say_hidden_npc_say_visible(self):
        self.login()
        self.send("who")                               # lernt Joe, Alice
        out = self.send("talk")
        self.assertNotIn("geheimes Gespraech", out)
        self.assertNotIn("mit Folgezeile", out)
        self.assertIn("The guard says: Welcome, stranger.", out)
        self.assertIn("The story-teller says : Once upon a time", out)
        self.assertIn("[1 Nachricht anderer Spieler ausgeblendet]", out)

    def test_own_tell_hidden(self):
        self.login()
        out = self.send("tell bob I live in Testtown")
        self.assertNotIn("Testtown", out)
        self.assertNotIn("You told", out)
        self.assertIn("[2 Nachrichten anderer Spieler ausgeblendet]", out)

    def test_tell_behind_pager_prompt_hidden(self):
        self.login()
        out = self.send("pagerprompt")
        self.assertNotIn("geheim", out)
        self.assertIn("[1 Nachricht anderer Spieler ausgeblendet]", out)

    def test_unknown_speaker_counts_as_npc(self):
        # ohne who-Liste ist Alice unbekannt und bleibt sichtbar (dokumentierte Grenze)
        self.login()
        out = self.send("talk")
        self.assertIn("Alice says: geheimes Gespraech", out)
        self.assertIn("The guard says:", out)

    def test_channel_behind_arrow_prompt_hidden(self):
        self.login()
        out = self.send("prompted")
        self.assertNotIn("cheese", out)
        self.assertNotIn("test hills", out)            # Folgezeile auch weg
        self.assertIn("Tell me the name!", out)
        self.assertIn("That's wrong.", out)
        self.assertIn("[1 Nachricht anderer Spieler ausgeblendet]", out)

    def test_channel_message_split_across_reads(self):
        self.login()
        first = self.send("split", wait=0.2)          # nur "[Jame" ist angekommen
        self.assertNotIn("Jame", first)
        rest = self.mcp.call("mud_read", wait=1.0)
        self.assertNotIn("geheim", rest)
        self.assertNotIn("son]", rest)
        self.assertIn("[1 Nachricht anderer Spieler ausgeblendet]", rest)


class TestWithoutSayFilter(BridgeTest):
    env = {"MUD_FILTER": "channels,tells"}

    def test_say_visible(self):
        # normale Saetze bleiben unberuehrt
        self.assertIn("Please select a terminal type", self.send("guest"))
        self.send("dumb")
        out = self.send("chatter")
        self.assertIn("Bob says: hallo zusammen", out)
        self.assertNotIn("geheimes Passwort", out)
        self.assertIn("The light brightens.", out)
        self.assertIn("[4 Nachrichten anderer Spieler ausgeblendet]", out)


class TestNoFilter(BridgeTest):
    env = {"MUD_FILTER": "none"}

    def test_everything_visible(self):
        self.login()
        out = self.send("chatter")
        for text in ("[Bill] wer ist online?", "[news:Bob enters the world]",
                     "Bob tells you:", "Bob says:", "[chat:Alice]"):
            self.assertIn(text, out)
        self.assertNotIn("ausgeblendet", out)


if __name__ == "__main__":
    unittest.main()
