import unittest

from scraper import (
    clean_abstract,
    extract_doi,
    extract_pii,
    normalize_inline,
    normalize_space,
    rebuild_openalex_abstract,
    split_authors,
    strip_jats,
)


class ParsingTests(unittest.TestCase):
    def test_normalize_space_preserves_paragraphs(self):
        value = " First   sentence.\n\n Second\n sentence. "
        self.assertEqual(normalize_space(value), "First sentence.\n\nSecond sentence.")

    def test_normalize_inline(self):
        self.assertEqual(normalize_inline("SiameseNet\n + \nclassification"), "SiameseNet + classification")

    def test_clean_abstract_removes_heading(self):
        self.assertEqual(clean_abstract("Abstract\nA useful result."), "A useful result.")

    def test_extract_doi(self):
        text = "Read https://doi.org/10.1016/j.artint.2026.104621."
        self.assertEqual(extract_doi(text), "10.1016/j.artint.2026.104621")

    def test_extract_pii(self):
        url = "https://www.sciencedirect.com/science/article/pii/S0004370226001475"
        self.assertEqual(extract_pii(url), "S0004370226001475")

    def test_split_authors(self):
        self.assertEqual(split_authors("Ada Lovelace, Alan Turing"), ["Ada Lovelace", "Alan Turing"])

    def test_rebuild_openalex_abstract(self):
        inverted = {"World": [1], "Hello": [0], ".": [2]}
        self.assertEqual(rebuild_openalex_abstract(inverted), "Hello World .")

    def test_strip_jats(self):
        self.assertEqual(strip_jats("<jats:p>A &amp; B.</jats:p>"), "A & B.")


if __name__ == "__main__":
    unittest.main()
