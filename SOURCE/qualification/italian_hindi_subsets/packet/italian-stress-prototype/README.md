# Explicit Italian accents

`ItalianExplicitStress(vocabulary).phonemize(word)` accepts one bounded word
with one explicitly written acute/grave accent. It preserves the vowel's open or
closed quality represented in the pinned map and inserts the model stress token
immediately before that nucleus. It does not guess lexical stress or choose an
accent for unmarked text. Only represented mapping entries are accepted, so an
acute a remains rejected by the pinned map.

Lowercase, uppercase and title case have identical NFC/NFD behavior. The case
predicate uses a normalized view; every source span still indexes the original
string. True mixed case, unknown characters, punctuation, spaces, digits,
currency symbols and unsupported combining marks reject the whole word. Silent
mapped h remains in the consumption record. The result records mapped spans and
per-output-code-point source ownership, including the explicit stress marker.

The upstream map has an unconditional `sc -> ʃ` rule. A local traced correction
protects sc from the subsequent c palatalization rules, uses sk in hard contexts
and sch, and uses ʃ before e/i. An explicitly stressed i remains vocalic: scìa
produces ʃˈia. Unaccented sci before another vowel is rejected, since this subset
does not resolve the lexical/spelling-only i distinction. The retained upstream
data is unchanged.

The small sc distinction is supported by Treccani's entries on
[digraphs](https://www.treccani.it/enciclopedia/digramma_%28Enciclopedia-dell%27Italiano%29/)
and the [stressed i in scìa](https://www.treccani.it/magazine/lingua_italiana/domande_e_risposte/grammatica/grammatica_112.html).
The tests qualify those distinctions and tracing, not all sounds in their words.
Other retained rule outputs, including glide resolution, intervocalic consonant
length, s/z variants, affrication and lexical exceptions, remain unqualified.
General Italian readiness requires a separately validated frontend and acoustic
evidence.
