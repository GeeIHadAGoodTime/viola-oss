"""Pure pinned-frontend controls; no model, speech runtime or installation."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest
from _romance_vendor.base import ProsodyInfo as P
from romance_coverage import CoverageError, CoveredRomance, lower_units

ROOT = Path(__file__).resolve().parents[1]
VOCAB = json.loads((ROOT / "source/third_party/kokoro_onnx/src/kokoro_onnx/config.json").read_text())["vocab"]


def check_trace(text, result):
    spans = result["source_spans"]
    assert "".join(s["source"] for s in spans) == text
    assert spans[0]["start"] == 0 and spans[-1]["end"] == len(text)
    assert all(s["source"] == text[s["start"] : s["end"]] for s in spans)
    assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:]))
    mappings = result["phone_mappings"]
    assert "".join(m["model_text"] for m in mappings) == result["phonemes"]
    assert all(m["model_text"] == result["phonemes"][m["model_start"] : m["model_end"]] for m in mappings)
    assert all(a["model_end"] == b["model_start"] for a, b in zip(mappings, mappings[1:]))
    assert not set(result["phonemes"]) - set(VOCAB)
    assert not result["release_eligible"] and not result["phonological_or_acoustic_qualification"]
    assert not result["customer_profile_activated"]


@pytest.mark.parametrize(
    "unit,expected", [("rr", "r"), ("y_vowel", "y"), ("tʃ", "ʧ"), ("dʒ", "ʤ"), ("ã", "ã"), ("ũ", "ũ"), ("ɛ̃", "ɛ̃")]
)
def test_semantic_units_are_lowered_as_units_not_arbitrary_characters(unit, expected):
    phones, trace = lower_units([unit], [P(0, 0, 1)], VOCAB)
    assert phones == expected
    assert trace[0]["unit"] == unit and trace[0]["prosody"]["a3"] == 1


def test_trill_is_distinct_from_tap_and_double_character_join():
    route = CoveredRomance("es", VOCAB)
    tap, trill = route.phonemize("pero"), route.phonemize("perro")
    assert tap["phonemes"] == "pˈeɾo"
    assert trill["phonemes"] == "pˈero"
    assert "rr" in trill["upstream_tokens"] and "rr" not in trill["phonemes"]


def test_french_rounded_vowel_is_not_u_and_not_piper_pua():
    route = CoveredRomance("fr", VOCAB)
    rounded, back = route.phonemize("tu"), route.phonemize("tout")
    assert rounded["phonemes"] == "tˈy" and back["phonemes"] == "tˈu"
    assert "y_vowel" in rounded["upstream_tokens"]


@pytest.mark.parametrize(
    "tokens,prosody,expected",
    [
        (["t", "y_vowel"], [P(0, 0, 2), P(0, 2, 2)], "tˈy"),
        (["t", "u"], [P(0, 0, 2), P(0, 1, 2)], "tˌu"),
        (["p", "ˈ", "e", "rr", "o"], [P(0, 0, 4), P(0, 2, 4), P(0, 2, 4), P(0, 0, 4), P(0, 0, 4)], "pˈero"),
    ],
)
def test_stress_is_preserved_without_doubling_explicit_markers(tokens, prosody, expected):
    phones, mappings = lower_units(tokens, prosody, VOCAB)
    assert phones == expected
    assert [m["prosody"] for m in mappings] == [asdict(p) for p in prosody]
    assert phones.count("ˈ") + phones.count("ˌ") == 1


@pytest.mark.parametrize("unit", ["\ue01d", "\ue01e", "\U000f0000", "ɫ", "Spotify", "rrr", "t͡ʃ", "uː", "_", "aɾ"])
def test_pua_unknown_and_unsupported_units_reject_even_with_character_membership(unit):
    expanded = {**VOCAB, **{c: 1000 + i for i, c in enumerate(unit)}}
    with pytest.raises(CoverageError):
        lower_units([unit], [P(0, 0, 1)], expanded)


@pytest.mark.parametrize("prosody", [[P(1, 2, 1)], [P(0, 3, 1)], [P(0, 2, -1)], [P(0, True, 1)], [None], [], None])
def test_malformed_or_unrepresented_prosody_rejects(prosody):
    with pytest.raises(CoverageError):
        lower_units(["a"], prosody, VOCAB)


@pytest.mark.parametrize(
    "tokens,prosody",
    [
        (["ˈ"], [P(0, 2, 1)]),
        (["ˈ", "p"], [P(0, 2, 1), P(0, 2, 1)]),
        (["ˈ", "a"], [P(0, 2, 1), P(0, 0, 1)]),
        (["ˈ", "ˈ", "a"], [P(0, 2, 1), P(0, 2, 1), P(0, 2, 1)]),
        (["p"], [P(0, 2, 1)]),
        ([" "], [P(0, 2, 0)]),
    ],
)
def test_stress_must_have_consistent_nucleus_and_boundary(tokens, prosody):
    with pytest.raises(CoverageError):
        lower_units(tokens, prosody, VOCAB)


@pytest.mark.parametrize(
    "locale,text",
    [
        ("es", "Hola mundo."),
        ("es", "café canción pingüino"),
        ("es", "  CAFE\u0301\t mundo!"),
        ("fr", "Bonjour le monde."),
        ("fr", "les amis"),
        ("fr", "l’ami"),
        ("fr", "aujourd'hui"),
        ("fr", "tu tout rue roue"),
        ("fr", "un bon vin"),
        ("fr", "«bonjour»"),
        ("pt-br", "Olá mundo."),
        ("pt-br", "bom pão vinho"),
        ("pt-br", "dia noite"),
        ("pt-br", "Brasil azul"),
        ("pt-pt", "Olá mundo."),
        ("pt-pt", "dia noite"),
    ],
)
def test_actual_source_prosody_context_and_complete_raw_span_coverage(locale, text):
    route = CoveredRomance(locale, VOCAB)
    result = route.phonemize(text)
    tokens, prosody = route._call(result["frontend_text"])
    assert result["upstream_tokens"] == tokens
    assert result["upstream_prosody"] == [asdict(p) for p in prosody]
    check_trace(text, result)


def test_french_liaison_and_elision_keep_whole_phrase_context():
    route = CoveredRomance("fr", VOCAB)
    linked = route.phonemize("les amis")
    blocked = route.phonemize("les, amis")
    assert "z" in linked["upstream_tokens"] and "z" not in blocked["upstream_tokens"]
    elision = route.phonemize("l’ami")
    assert elision["source_spans"][0]["source"] == "l’ami"
    assert elision["source_spans"][0]["frontend_tokens"] == ["lami"]
    assert elision["phonemes"] == route.phonemize("l'ami")["phonemes"]


@pytest.mark.parametrize("locale", ["es", "fr", "pt-br", "pt-pt"])
@pytest.mark.parametrize(
    "text",
    [
        "hello 123.45 € 世界",
        "$1.5",
        "bonjour €",
        "a1b",
        "foo_bar",
        "🙂",
        "café\nbonjour",
        "A\x00B",
        "\ue01d",
        "漢字",
        "αλφα",
    ],
)
def test_numbers_currency_mixed_scripts_and_control_gaps_never_disappear(locale, text):
    route = CoveredRomance(locale, VOCAB)
    route._call = lambda _: (_ for _ in ()).throw(AssertionError("Uncovered source reached frontend"))
    with pytest.raises(CoverageError):
        route.phonemize(text)


@pytest.mark.parametrize("locale", ["es", "fr", "pt-br", "pt-pt"])
@pytest.mark.parametrize("text", ["h", "h ami", "ami h"])
def test_a_fully_silent_word_cannot_disappear(locale, text):
    with pytest.raises(CoverageError):
        CoveredRomance(locale, VOCAB).phonemize(text)


def test_european_dark_l_is_reported_instead_of_mapped_to_plain_l_or_w():
    with pytest.raises(CoverageError, match="ɫ"):
        CoveredRomance("pt-pt", VOCAB).phonemize("Brasil azul")


def test_missing_word_wrong_word_count_and_punctuation_omission_reject():
    route = CoveredRomance("fr", VOCAB)
    for replacement in [(["a"], [P(0, 2, 1)]), (["a", " ", "i"], [P(0, 2, 2), P(0, 0, 0), P(0, 2, 1)])]:
        route._call = lambda _, value=replacement: value
        with pytest.raises(CoverageError):
            route.phonemize("ami ici.")


def test_exact_locale_and_vocabulary_are_bound_per_instance():
    original = dict(VOCAB)
    route = CoveredRomance("fr", original)
    original.pop("y")
    assert route.phonemize("tu")["phonemes"] == "tˈy"
    with pytest.raises(AttributeError):
        route.locale = "es"
    for locale in [None, [], True, "en", "es-es", "frankenstein", "pt"]:
        with pytest.raises(CoverageError):
            CoveredRomance(locale, VOCAB)
    reduced = dict(VOCAB)
    reduced.pop("y")
    with pytest.raises(CoverageError):
        CoveredRomance("fr", reduced).phonemize("tu")


def test_parallel_instances_preserve_per_locale_source_and_prosody():
    inputs = [("es", "perro"), ("fr", "tu"), ("pt-br", "bom pão"), ("pt-pt", "dia")] * 8
    routes = {lang: CoveredRomance(lang, VOCAB) for lang, _ in inputs}
    expected = [routes[lang].phonemize(text) for lang, text in inputs]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda item: routes[item[0]].phonemize(item[1]), inputs))
    assert actual == expected
    assert not ({"onnxruntime", "numpy", "phonemizer", "espeakng_loader", "piper_plus_g2p"} & set(sys.modules))


@pytest.mark.parametrize("locale", ["fr", "pt-br", "pt-pt"])
@pytest.mark.parametrize("text", ["qa", "piñata", "ami qa"])
def test_internal_unknown_graphemes_never_disappear(locale, text):
    with pytest.raises(CoverageError, match="grapheme"):
        CoveredRomance(locale, VOCAB).phonemize(text)


def test_known_spanish_enye_and_silent_h_rules_are_not_rejected():
    route = CoveredRomance("es", VOCAB)
    assert "ɲ" in route.phonemize("piñata")["phonemes"]
    assert route.phonemize("hola")["phonemes"] == route.phonemize("ola")["phonemes"]
