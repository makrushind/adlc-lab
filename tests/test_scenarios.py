import tempfile
import unittest
from pathlib import Path

from aiweekend_target.errors import TargetError
from aiweekend_target.lab.scenarios import LabPaths, load_scenario, reset_scenario


class ScenarioContractTests(unittest.TestCase):
    def test_custom_scenario_rejects_payload_without_its_single_canary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "payload.txt"
            payload.write_text("untrusted instructions", encoding="utf-8")
            with self.assertRaises(TargetError):
                load_scenario(Path(__file__).parents[1], "custom", custom_payload=payload)

    def test_custom_reset_preserves_the_fixed_descriptor_and_injects_one_canary(self) -> None:
        repository = Path(__file__).parents[1]
        scenario = load_scenario(repository, "custom")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = reset_scenario(LabPaths(root / "workspace", root / "corpus", root / "rag-index"), scenario, repository)
            self.assertEqual(result, {"ok": True, "reset": True, "scenario": "custom"})
            self.assertEqual((root / "rag-index" / "scenario.json").read_text(encoding="utf-8"), (repository / "scenarios" / "custom" / "scenario.json").read_text(encoding="utf-8"))
            self.assertEqual((root / "workspace" / "task.md").read_text(encoding="utf-8").count("ADLC_CANARY_CUSTOM_95A7D2"), 1)
