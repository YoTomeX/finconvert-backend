import unittest

from converter_web import build_mt940, format_account_for_25


class Mt940BuildTests(unittest.TestCase):
    def test_format_account_for_25_for_pl_iban(self):
        self.assertEqual(
            format_account_for_25("PL12 3456 7890 1234 5678 9012 3456"),
            "/PL12345678901234567890123456",
        )

    def test_build_mt940_contains_required_tags(self):
        transactions = [
            ("260401", "-125,00", "Oplata za prowadzenie rachunku", "0401", "N775"),
            ("260402", "3000,00", "Wplyw od kontrahenta", "0402", "N524"),
        ]
        mt = build_mt940(
            account="PL12345678901234567890123456",
            saldo_pocz="1000,00",
            saldo_konc="3875,00",
            transactions=transactions,
            num_20="260401120000",
            num_28C="00001",
            open_d="260401",
            close_d="260402",
        )

        self.assertIn(":20:260401120000", mt)
        self.assertIn(":25:/PL12345678901234567890123456", mt)
        self.assertIn(":28C:00001", mt)
        self.assertIn(":60F:C260401PLN1000,00", mt)
        self.assertIn(":62F:C260402PLN3875,00", mt)

        lines_61 = [line for line in mt.splitlines() if line.startswith(":61:")]
        lines_86 = [line for line in mt.splitlines() if line.startswith(":86:")]
        self.assertEqual(len(lines_61), 2)
        self.assertEqual(len(lines_86), 2)


if __name__ == "__main__":
    unittest.main()
