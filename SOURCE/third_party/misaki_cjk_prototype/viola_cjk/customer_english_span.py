"""Reconstructed inactive bridge to the exact retained English customer source.

New source after the workspace reset; historical bridge receipts do not qualify
these bytes. No raw callback or equality-to-spelling heuristic is accepted.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from types import CodeType

_ENGLISH_SHA256 = "b333138ac21703a61d9e05e4f19967b4823df24133a55ed924a601c6598055eb"


def _find_code(code, qualname):
    for value in code.co_consts:
        if isinstance(value, CodeType):
            if value.co_qualname == qualname:
                return value
            nested = _find_code(value, qualname)
            if nested is not None:
                return nested
    return None


class CustomerEnglishBridge:
    def __init__(self, tokenizer, locale="en-us"):
        from voice.customer_pronunciation import CustomerTokenizer

        if type(tokenizer) is not CustomerTokenizer:
            raise ValueError("English spans require the exact customer tokenizer")
        if not isinstance(locale, str) or not CustomerTokenizer.supports_locale(locale):
            raise ValueError("English span locale is unsupported")
        path = Path(inspect.getfile(CustomerTokenizer))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _ENGLISH_SHA256:
            raise ValueError("English source differs from the retained source binding")
        # Compile for comparison only. Never execute a reconstructed code object.
        module_code = compile(raw, str(path), "exec", dont_inherit=True, optimize=sys.flags.optimize)
        expected = _find_code(module_code, "CustomerTokenizer.phonemize")
        if expected is None or CustomerTokenizer.phonemize.__code__ != expected:
            raise ValueError("English pronunciation implementation differs from its source")
        self._method = CustomerTokenizer.phonemize
        self._tokenizer = tokenizer
        self.locale = locale.lower().replace("_", "-")
        self.vocab = dict(tokenizer.vocab)

    def phonemize(self, source):
        from voice.customer_pronunciation import CustomerTokenizer

        if (
            type(self._tokenizer) is not CustomerTokenizer
            or CustomerTokenizer.phonemize is not self._method
            or self._tokenizer.vocab != self.vocab
        ):
            raise ValueError("English span ownership or vocabulary changed")
        phones = self._method(self._tokenizer, source, lang=self.locale, norm=False)
        if not isinstance(phones, str) or not phones or any(c not in self.vocab for c in phones):
            raise ValueError("English span phonemes are incomplete")
        return {"source": source, "locale": self.locale, "phonemes": phones}
