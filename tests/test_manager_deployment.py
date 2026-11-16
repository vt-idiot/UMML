import unittest

import umml_manager
from umml_manager.deployment import ApplyEngine, ApplyError
from umml_manager.resolver import Resolution


class DeploymentBoundaryTests(unittest.TestCase):
    def test_legacy_engine_import_resolves_to_validated_facade(self):
        from umml_manager.engine import ApplyEngine as CompatibilityApplyEngine

        self.assertIs(CompatibilityApplyEngine, ApplyEngine)
        self.assertIs(umml_manager.ApplyEngine, ApplyEngine)

    def test_stale_prepared_cache_is_rejected_by_engine(self):
        resolution = Resolution(profile="Default")
        resolution.stale_prepared.append("mod-a uses old metadata")
        engine = object.__new__(ApplyEngine)

        with self.assertRaisesRegex(ApplyError, "Stale prepared caches"):
            engine._validate_resolution(resolution)

    def test_wrong_installation_is_rejected_by_engine(self):
        resolution = Resolution(profile="Default")
        resolution.wrong_installation.append("profile belongs to steam-global")
        engine = object.__new__(ApplyEngine)

        with self.assertRaisesRegex(ApplyError, "Wrong installation"):
            engine._validate_resolution(resolution)


if __name__ == "__main__":
    unittest.main()
