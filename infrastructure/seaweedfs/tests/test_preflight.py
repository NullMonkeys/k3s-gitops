import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("preflight", Path(__file__).parents[1] / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def test_capacity_cannot_be_split_across_disks(self):
        budgets = [{"node": "a", "disk": "data", "bytes": 40 * preflight.GIB},
                   {"node": "b", "disk": "data", "bytes": 40 * preflight.GIB}]
        with self.assertRaisesRegex(ValueError, "Insufficient capacity"):
            preflight.allocate(budgets, {"data": 64 * preflight.GIB})

    def test_all_claims_fit_without_reusing_reserved_capacity(self):
        budgets = [{"node": "a", "disk": "data", "bytes": 66 * preflight.GIB}]
        with self.assertRaisesRegex(ValueError, "Insufficient capacity"):
            preflight.allocate(budgets, preflight.CLAIMS)
        self.assertEqual(budgets[0]["bytes"], 66 * preflight.GIB)
        budgets[0]["bytes"] = 67 * preflight.GIB
        self.assertEqual(len(preflight.allocate(budgets, preflight.CLAIMS)), 3)

    def disk_fixture(self):
        conditions = [{"type": "Ready", "status": "True"},
                      {"type": "Schedulable", "status": "True"}]
        nodes = {"items": [{"metadata": {"name": "node"}, "spec": {},
                            "status": {"conditions": conditions,
                                       "nodeInfo": {"operatingSystem": "linux", "architecture": "arm64"}}}]}
        longhorn = {"items": [{"metadata": {"name": "node"},
                              "spec": {"allowScheduling": True, "disks": {
                                  "data": {"allowScheduling": True, "storageReserved": 5 * preflight.GIB}}},
                              "status": {"conditions": conditions, "diskStatus": {"data": {
                                  "conditions": conditions, "storageMaximum": 100 * preflight.GIB,
                                  "storageAvailable": 90 * preflight.GIB,
                                  "storageScheduled": 40 * preflight.GIB}}}}]}
        return nodes, longhorn

    def test_sparse_volumes_do_not_allow_overprovisioning(self):
        nodes, longhorn = self.disk_fixture()
        self.assertEqual(preflight.storage_budgets(nodes, longhorn)[0]["bytes"], 40 * preflight.GIB)

    def test_actual_free_space_preserves_twenty_percent(self):
        nodes, longhorn = self.disk_fixture()
        longhorn["items"][0]["status"]["diskStatus"]["data"]["storageAvailable"] = 25 * preflight.GIB
        self.assertEqual(preflight.storage_budgets(nodes, longhorn)[0]["bytes"], 5 * preflight.GIB)

    def test_evicted_disk_is_not_a_candidate(self):
        nodes, longhorn = self.disk_fixture()
        longhorn["items"][0]["spec"]["disks"]["data"]["evictionRequested"] = True
        self.assertEqual(preflight.storage_budgets(nodes, longhorn), [])

    def test_unsupported_architecture_is_rejected(self):
        nodes, longhorn = self.disk_fixture()
        nodes["items"][0]["status"]["nodeInfo"]["architecture"] = "ppc64le"
        with self.assertRaisesRegex(ValueError, "amd64 or arm64"):
            preflight.storage_budgets(nodes, longhorn)

    def test_empty_or_anonymous_s3_configuration_is_rejected(self):
        for identities in ([], [{"name": "anonymous"}], [{"name": "admin", "actions": ["Admin"]}]):
            with self.subTest(identities=identities), self.assertRaises(ValueError):
                preflight.validate_identities({"identities": identities})

    def test_authenticated_administrator_is_accepted(self):
        preflight.validate_identities({"identities": [{"name": "admin", "actions": ["Admin"],
            "credentials": [{"accessKey": "test-access", "secretKey": "test-secret"}]}]})

    def test_disabled_administrator_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "administrative"):
            preflight.validate_identities({"identities": [{"name": "admin", "disabled": True,
                "actions": ["Admin"], "credentials": [{"accessKey": "test", "secretKey": "test"}]}]})


if __name__ == "__main__":
    unittest.main()
