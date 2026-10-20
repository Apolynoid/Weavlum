import unittest

from server import netinfo


class NetInfoTests(unittest.TestCase):
    def test_candidate_ips_filters_local_only(self):
        ips = netinfo.candidate_ips()
        for ip in ips:
            self.assertFalse(ip.startswith(("127.", "169.254.", "0.")), ip)

    def test_candidate_ips_returns_list(self):
        self.assertIsInstance(netinfo.candidate_ips(), list)


if __name__ == "__main__":
    unittest.main()
