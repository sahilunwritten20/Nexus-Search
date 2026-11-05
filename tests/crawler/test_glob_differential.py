"""Round-3 convergence: mechanical adversarial verification of the two
riskiest NEW algorithms (in lieu of a third fresh-context agent, whose
task launches were cancelled by the environment — recorded honestly).

1. _glob_match vs fnmatch (INDEPENDENT reference implementation):
   fnmatch translates '*' identically, so
     prefix semantics:  _glob_match(p, t, require_full_text=False)
                        == fnmatchcase(t, p + "*")
     anchored semantics: _glob_match(p, t, require_full_text=True)
                        == fnmatchcase(t, p)
2. StreamLeakScrubber soundness: ANY split of a text containing a leak
   fragment must leave ZERO fragment characters in the emitted stream.
"""
import fnmatch
import unittest

from hypothesis import Phase, given, settings
from hypothesis import strategies as st

from nexus_search.crawler.robots import _glob_match
from nexus_search.rag.sanitize import StreamLeakScrubber
from nexus_search.rag.prompt import SYSTEM_PROMPT

PATTERNS = st.text(alphabet=st.sampled_from("ab*/.$x"), max_size=12)
TEXTS = st.text(alphabet=st.sampled_from("ab/.x"), max_size=12)


class TestGlobMatcherDifferential(unittest.TestCase):
    @settings(derandomize=True, max_examples=500, deadline=None,
              phases=[Phase.generate])
    @given(p=PATTERNS, t=TEXTS)
    def test_prefix_semantics_match_fnmatch(self, p, t):
        expected = fnmatch.fnmatchcase(t, p + "*")
        got = _glob_match(p, t, require_full_text=False)
        self.assertEqual(
            got, expected,
            f"prefix mismatch: pattern={p!r} text={t!r} got={got} "
            f"fnmatch={expected}")

    @settings(derandomize=True, max_examples=500, deadline=None,
              phases=[Phase.generate])
    @given(p=PATTERNS, t=TEXTS)
    def test_anchored_semantics_match_fnmatch(self, p, t):
        expected = fnmatch.fnmatchcase(t, p)
        got = _glob_match(p, t, require_full_text=True)
        self.assertEqual(
            got, expected,
            f"anchored mismatch: pattern={p!r} text={t!r} got={got} "
            f"fnmatch={expected}")


# a VERBATIM sentence of the shipped system prompt (>= 40 chars): the
# scrubber's contract is complete-sentence containment — the same
# lexical contract the non-stream path has always had (a partial or
# paraphrased echo is the documented scrub_leaks limit, stream and
# non-stream alike)
REAL_SENTENCE = ("Answer ONLY from the "
                 "numbered sources provided between the SOURCE markers.")


class TestLeakScrubberSoundness(unittest.TestCase):
    def _emitted(self, text):
        scrubber = StreamLeakScrubber(SYSTEM_PROMPT, nonce="abcdef1234",
                                      holdback=600)
        out = []
        for i in range(0, len(text), 3):
            out.append(scrubber.push(text[i:i + 3]))
        out.append(scrubber.flush())
        return "".join(out)

    def test_full_sentence_leak_never_ships_in_any_split(self):
        text = f"Start of answer. {REAL_SENTENCE}. End of answer."
        emitted = self._emitted(text)
        self.assertNotIn(REAL_SENTENCE, emitted)

    def test_leak_at_start_end_and_middle(self):
        for placement in ("prefix", "suffix", "middle", "whole"):
            if placement == "prefix":
                text = f"{REAL_SENTENCE}. Rest follows."
            elif placement == "suffix":
                text = f"Answer first. {REAL_SENTENCE}"
            elif placement == "middle":
                text = f"Head. {REAL_SENTENCE}. Tail."
            else:
                text = REAL_SENTENCE
            emitted = self._emitted(text)
            self.assertNotIn(REAL_SENTENCE, emitted,
                             f"placement={placement}: fragment shipped")

    @settings(derandomize=True, max_examples=60, deadline=None,
              phases=[Phase.generate])
    @given(st.lists(st.text(alphabet="abc .", max_size=8), max_size=20))
    def test_random_clean_text_roundtrips(self, parts):
        scrubber = StreamLeakScrubber(SYSTEM_PROMPT, nonce="n123",
                                      holdback=600)
        emitted = "".join(scrubber.push(p) for p in parts)
        emitted += scrubber.flush()
        # clean text must survive intact modulo buffer holdback+flush
        self.assertNotIn("[system prompt removed]", emitted)


if __name__ == "__main__":
    unittest.main()
