import json
import unittest
from pathlib import Path

from jarvis import expand_voice_commands, normalize


class VoiceCommandTests(unittest.TestCase):
    def test_config_generates_exactly_9000_unique_commands(self):
        config_path = Path(__file__).resolve().parents[1] / "comenzi.json"
        entries = json.loads(config_path.read_text(encoding="utf-8"))
        commands = {normalize(phrase): args for phrase, args in entries.items()}
        targets = [(phrase, args) for phrase, args in commands.items() if phrase.startswith("deschide ")]

        expanded = expand_voice_commands(commands)

        self.assertEqual(len(expanded), 9000)
        self.assertEqual(len(expanded), len(set(expanded)))
        self.assertEqual(len(targets), 60)
        online_targets = [(phrase, args) for phrase, args in targets if args[0] == "explorer.exe"
                          and len(args) > 1 and args[1].startswith("https://")]
        self.assertEqual(len(online_targets), 40)
        self.assertEqual(len(targets) - len(online_targets), 20)
        self.assertEqual(expanded["deschide youtube"], ["explorer.exe", "https://www.youtube.com"])
        self.assertEqual(expanded["intra pe youtube"], expanded["deschide youtube"])
        self.assertEqual(expanded["deschide calculatorul"], ["calc.exe"])
        self.assertIn("verifica microfonul", expanded)

        for phrase, args in targets:
            target = phrase[len("deschide "):]
            online = args[0] == "explorer.exe" and len(args) > 1 and args[1].startswith("https://")
            forms = (
                (f"deschide {target}", f"pornește {target}", f"lansează {target}",
                 f"intră pe {target}", f"du-mă pe {target}", f"mergi pe {target}")
                if online
                else (f"deschide {target}", f"pornește {target}", f"lansează {target}")
            )
            for form in forms:
                self.assertEqual(expanded[normalize(form)], args)


if __name__ == "__main__":
    unittest.main()
