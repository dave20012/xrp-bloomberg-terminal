import unittest

from app_utils import is_safe_url
from xrpl_utils import CLASSIC_ADDRESS_RE, parse_account_input


class TestXrplUtils(unittest.TestCase):
    def test_classic_address_regex_accepts_valid(self):
        self.assertTrue(CLASSIC_ADDRESS_RE.match("rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"))

    def test_parse_address_with_colon_tag(self):
        addr, tag, notes = parse_account_input("rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe:123")
        self.assertEqual(addr, "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe")
        self.assertEqual(tag, 123)
        self.assertFalse(notes)

    def test_parse_address_query_tag(self):
        addr, tag, notes = parse_account_input("rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe?dt=45")
        self.assertEqual(addr, "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe")
        self.assertEqual(tag, 45)
        self.assertFalse(notes)

    def test_parse_rejects_x_address(self):
        addr, tag, notes = parse_account_input("XVLhUMDd2P1w6vMXJ6zGuvvUNvms2xohACt1LrBkXaGTWRK")
        self.assertIsNone(addr)
        self.assertIn("X-address", " ".join(notes))

    def test_parse_rejects_invalid_tag(self):
        addr, tag, notes = parse_account_input("rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe", explicit_tag=-1)
        self.assertIsNone(tag)
        self.assertIn("non-negative", " ".join(notes))

    def test_safe_url_blocks_insecure_and_private_hosts(self):
        self.assertFalse(is_safe_url("http://localhost:3000"))
        self.assertFalse(is_safe_url("https://127.0.0.1"))
        self.assertTrue(is_safe_url("https://data.ripple.com/v2"))


if __name__ == "__main__":
    unittest.main()
