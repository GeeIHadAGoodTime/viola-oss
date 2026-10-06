# Representable Hindi rule subset

`HindiRepresentableWord(vocabulary).phonemize(word)` accepts a bounded Devanagari
word only when its complete mapped and postprocessed stream is representable.
The unchanged pinned map/post rules supply the conversion. A small source grammar
rejects stranded or duplicate vowel marks, nasal signs and virama misuse. Raw
Latin, unknown characters, digits, punctuation and currency are never passed
through or silently removed.

Nukta forms retain original code-point ownership even when one precomposed source
character decomposes into two units. Aspiration, affricates, represented nasal
symbols and vowel length remain in the output. Chandrabindu/length metathesis
moves their separate ownership with each symbol. Deleted inherent schwas and
virama are recorded as explicit rewrite events; all original code points remain
in the mapped consumption record.

The entire word is rejected for the unqualified ɦ, breathy, voiceless-rhotic and
syllabic features, even if a caller supplies extra vocabulary symbols. Any अं
sequence is rejected because the pinned mapping loses its independent vowel.
Terminal anusvara is rejected because the pinned fallback does not qualify the
nasal-vowel distinction there. These are known boundaries, not a list of every
linguistic defect.

Acceptance means pinned-rule representation, not certified Hindi pronunciation.
In particular, the retained schwa rules have unresolved cluster/nasal cases.
Fixtures such as पंख -> pəŋkʰə and संपर्क -> səmprkə deliberately record the
existing rule stream and ownership; they must not be described as pronunciation
goldens. Lexical schwa deletion, conjunct pronunciation, loanwords, stress and
audio quality remain open. No lexical model or external dependency is introduced.
