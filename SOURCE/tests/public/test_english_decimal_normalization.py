"""Exact plain-decimal normalization; native audio and broader identifiers are separate."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from decimal import localcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def _formatter():
    log = types.ModuleType("core.logging_config")
    log.get_logger = lambda name: types.SimpleNamespace(debug=lambda *args, **kwargs: None)
    spec = importlib.util.spec_from_file_location("_decimal_formatter_contract", ROOT / "voice/synthesis/text_normalizer.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"core.logging_config": log}):
        spec.loader.exec_module(module)
    config = types.SimpleNamespace(
        tts_pronunciation_overrides={},
        tts_brand_dict_enabled=True,
        tts_acronym_dict_enabled=True,
        tts_prosody_hints_enabled=False,
    )
    return module, module.SpeechFormatter(summarize=False, config=config)


class EnglishDecimalNormalization(unittest.TestCase):
    def setUp(self):
        self.module, self.formatter = _formatter()

    def test_exact_fraction_sign_and_trailing_zeros(self):
        cases = {
            "0.5": "zero point five",
            "-1.25": "minus one point two five",
            ".5": "zero point five",
            "-.5": "minus zero point five",
            "+1.05": "plus one point zero five",
            "−0.005": "minus zero point zero zero five",
            "0.000": "zero point zero zero zero",
            "-0.00": "minus zero point zero zero",
            "001.050": "one point zero five zero",
            "2026.1999": "two thousand twenty six point one nine nine nine",
            "123.4567890": "one hundred twenty three point four five six seven eight nine zero",
            "0.1234567890": "zero point one two three four five six seven eight nine zero",
        }
        for source, expected in cases.items():
            for locale in (None, "en", "en-us", "en-gb", "EN_US"):
                with self.subTest(source=source, locale=locale):
                    self.assertEqual(self.formatter.format(f"Value {source}.", language=locale), f"Value {expected}.")

    def test_all_fraction_digits_survive_decimal_context_changes(self):
        fraction = "12345678901234567890"
        expected = " ".join(self.module._DIGIT_NAMES[int(digit)] for digit in fraction)
        for precision in (1, 3, 6, 28, 80):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                result = self.formatter.format("Value -12345678901234567." + fraction + ".")
                self.assertTrue(result.startswith("Value minus twelve quadrillion"))
                self.assertEqual(result.split(" point ", 1)[1], expected + ".")

    def test_long_fraction_has_no_implicit_twenty_digit_truncation(self):
        fraction = "1234567890" * 5
        expected = " ".join(self.module._DIGIT_NAMES[int(digit)] for digit in fraction)
        self.assertEqual(self.formatter.format("0." + fraction), "zero point " + expected)

    def test_unsupported_decimals_remain_complete_without_partial_rewrites(self):
        for source in ("1000000000000000000000.2026", "9" * 150 + ".1234567890", "0." + "5" * 150):
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format("Value " + source + "."), "Value " + source + ".")

    def test_existing_currency_percent_time_phone_and_ordinal_contracts(self):
        cases = {
            "$1.5": "one dollar and fifty cents",
            "$12.345": "twelve point three four five dollars",
            "$0.01": "one cent",
            "$12.000": "twelve dollars",
            "7.5%": "seven point five percent",
            "15%": "fifteen percent",
            "2:30 PM": "two thirty PM",
            "21st in 2026": "twenty-first in twenty twenty-six",
        }
        phone = "five five five, one two three, four five six seven"
        cases.update({value: phone for value in ("555-123-4567", "555.123.4567", "(555) 123-4567", "5551234567")})
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format(source), expected)
        self.assertEqual(
            self.formatter.format("Value 0.5; call 555.123.4567 at 2:30 PM for $1.5."),
            "Value zero point five; call " + phone + " at two thirty PM for one dollar and fifty cents.",
        )

    def test_decimal_matcher_abstains_from_existing_identifier_and_currency_paths(self):
        # These retain the pre-existing normalizer path; this patch does not
        # claim that every inherited identifier pronunciation is correct.
        for source in (
            "v1.25", "build_1.25", "1.25kg", "1.2.3.4", "555.123.4567", "1.25.txt",
            "abc/1.25", "abc:1.25", "1.25/file", "1,234.56", "1.25e3", "A1.25",
            "$1.5", "£1.25", "€1.25", "1.25€", "₹1.25", "7.5%", "1.25-2.5",
            "1.25@example.com", "@1.25", "#1.25", "₩1.25", "1.25₽",
        ):
            with self.subTest(source=source):
                self.assertIsNone(self.module._DECIMAL_RE.search(source))

    def test_other_locales_preserve_exact_decimals_and_units(self):
        source = "Value -1.25 +0.005 0.12345678901234567890 $1.5 7.5% 555.123.4567"
        for locale in ("fr", "hi", "it", "pt", "ja", "unknown", ""):
            with self.subTest(locale=locale):
                self.assertEqual(self.formatter.format(source, language=locale), source)

    def test_degree_sign_is_spoken_before_phonemization(self):
        english = {
            "A high of 68°F, a low of 52°F.": (
                "A high of sixty eight degrees Fahrenheit, a low of fifty two degrees Fahrenheit."
            ),
            "It is 20 °C.": "It is twenty degrees Celsius.",
            "68.5°F": "sixty eight point five degrees Fahrenheit",
            "turn 90° left": "turn ninety degrees left",
            "52°Fahrenheit": "fifty two degrees Fahrenheit",
        }
        for source, expected in english.items():
            for locale in (None, "en-us", "en-gb"):
                with self.subTest(source=source, locale=locale):
                    self.assertEqual(self.formatter.format(source, language=locale), expected)
        self.assertEqual(
            self.formatter.format("Máxima de 68°F.", language="es"), "Máxima de sesenta y ocho grados Fahrenheit."
        )
        self.assertEqual(self.formatter.format("Hace 20 °C.", language="es-MX"), "Hace veinte grados Celsius.")
        self.assertEqual(self.formatter.format("68°F, 20 °C, 90°", language="zh"), "68华氏度, 20摄氏度, 90度")
        for locale in ("ja", "fr"):
            with self.subTest(locale=locale):
                self.assertEqual(self.formatter.format("68°F", language=locale), "68°F")

    def test_spanish_numbers_are_spelled_for_the_letter_only_route(self):
        cases = {
            "Hace 20 grados.": "Hace veinte grados.",
            "Son las 11:45.": "Son las once y cuarenta y cinco.",
            "Es la 1:05, luego las 21:00.": "Es la una y cinco, luego las veintiuna en punto.",
            "Máxima de 68°F, 16% de lluvia.": "Máxima de sesenta y ocho grados Fahrenheit, dieciséis por ciento de lluvia.",
            "0, 15, 21, 29, 31, 100, 101, 115, 500, 999.": (
                "cero, quince, veintiuno, veintinueve, treinta y uno, cien, ciento uno, ciento quince, "
                "quinientos, novecientos noventa y nueve."
            ),
            "1000, 2000, 21001, 31000, 101000, 999999.": (
                "mil, dos mil, veintiún mil uno, treinta y un mil, ciento un mil, "
                "novecientos noventa y nueve mil novecientos noventa y nueve."
            ),
            "1000000 y 2500000": "un millón y dos millones quinientos mil",
            "En 2026": "En dos mil veintiséis",
            "1.250 pesos, 21 000 personas": "mil doscientos cincuenta pesos, veintiún mil personas",
            "3,5 o 68.5 o 0.001": "tres coma cinco o sesenta y ocho coma cinco o cero coma cero cero uno",
            "Un 0,25% y 7%.": "Un cero coma veinticinco por ciento y siete por ciento.",
            "$5 o 12,50 €": "cinco dólares o doce coma cincuenta euros",
            "-3 y +2, del 10-20": "menos tres y más dos, del diez a veinte",
            "1º lugar, 3er día, la 2ª vez, el 7º.": "primer lugar, tercer día, la segunda vez, el séptimo.",
            "Llama al 414-555-1234.": "Llama al cuatro uno cuatro cinco cinco cinco uno dos tres cuatro.",
        }
        for source, expected in cases.items():
            for locale in ("es", "es-MX", "ES_us"):
                with self.subTest(source=source, locale=locale):
                    self.assertEqual(self.formatter.format(source, language=locale), expected)

    def test_spanish_counts_agree_with_the_noun_that_follows(self):
        cases = {
            "101 mesas, 21 personas y 200 casas.": "ciento una mesas, veintiuna personas y doscientas casas.",
            "21 días, 1 grado y 1 día.": "veintiún días, un grado y un día.",
            "1 persona, 1 hora y 1 minuto.": "una persona, una hora y un minuto.",
            "1 canción, 1 mano, 1 mapa.": "una canción, una mano, un mapa.",
            "31 semanas, 500 millas, 41 fotos.": "treinta y una semanas, quinientas millas, cuarenta y una fotos.",
            "1 alarma, 21 tormentas, 1 libro, 21 libros.": "una alarma, veintiuna tormentas, un libro, veintiún libros.",
            "3 notificaciones, 201 ciudades.": "tres notificaciones, doscientas una ciudades.",
            "201 000 personas, 21 000 euros": "doscientas un mil personas, veintiún mil euros",
            "$1 y $21, 1 €": "un dólar y veintiún dólares, un euro",
            # Standalone counts, verbs, pronouns, decimals and percents keep "uno".
            "Tengo 21. Quedan 1 o 2.": "Tengo veintiuno. Quedan uno o dos.",
            "En 2021 hubo 21 tormentas.": "En dos mil veintiuno hubo veintiuna tormentas.",
            "En 2021 llega. Los 21 estamos.": "En dos mil veintiuno llega. Los veintiuno estamos.",
            "1,5 horas, 21% de 101.": "uno coma cinco horas, veintiuno por ciento de ciento uno.",
            "Son las 21:00 y la 1:00.": "Son las veintiuna en punto y la una en punto.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format(source, language="es"), expected)

    def test_spanish_ranges_and_english_ordinal_suffixes(self):
        cases = {
            "del 10-20 de octubre": "del diez a veinte de octubre",
            "de 10 – 20 minutos": "de diez a veinte minutos",
            "el 21st de octubre, el 1st y el 3rd": "el veintiuno de octubre, el uno y el tres",
            "El 22nd y el 4TH.": "El veintidós y el cuatro.",
            # ISO dates and phone numbers are not ranges.
            "2026-10-23": "dos mil veintiséis-diez-veintitrés",
            "Llama al 414-555-1234.": "Llama al cuatro uno cuatro cinco cinco cinco uno dos tres cuatro.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format(source, language="es"), expected)

    def test_mandarin_symbols_are_spoken_around_route_numbers(self):
        cases = {
            "现在是11:45。": "现在是11点45分。",
            "现在是11:05，明天8:00。": "现在是11点零5分，明天8点。",
            "降雨概率16%，0.5%。": "降雨概率百分之16，百分之0.5。",
            "今天是2026年10月23日。": "今天是2026年10月23日。",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.formatter.format(source, language="zh"), expected)

    def test_decimal_conversion_never_uses_float(self):
        with patch("builtins.float", side_effect=AssertionError("Float conversion prohibited")):
            self.assertEqual(self.formatter.format("Value 1.2500."), "Value one point two five zero zero.")

    def test_markdown_header_cleans_before_plain_decimal_conversion(self):
        self.assertEqual(self.formatter.format("#1.25"), "one point two five")
        self.assertEqual(self.formatter.format("Tag #1.25"), "Tag #one.twenty five")

    def test_english_url_and_summary_controls_remain_present(self):
        self.assertEqual(self.formatter.format("See https://example.com/1.25?secret=fixture"), "See (link sent in chat)")
        self.assertEqual(
            self.module.SpeechFormatter(config=self.formatter._config).format("First. Second. Third. Fourth."),
            "First. Second. Third. I'll send the full details in chat.",
        )


if __name__ == "__main__":
    unittest.main()
