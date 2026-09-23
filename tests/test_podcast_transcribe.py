import unittest
from podcast_transcribe import compare


class TranscriptionTests(unittest.TestCase):
    def test_identical_still_requires_review(self):
        result = compare('It is NOT 20.', 'It is not 20!')
        self.assertEqual(result['sequence_similarity'], 1)
        self.assertFalse(result['approved'])

    def test_missing_negation_and_number_changes_visible(self):
        result = compare('It is not 20 dollars.', 'It is 30 dollars.')
        self.assertLess(result['sequence_similarity'], 1)
        self.assertTrue(any('not' in item['reference'] for item in result['differences']))
        self.assertTrue(any('30' in item['transcript'] for item in result['differences']))

    def test_repeated_section_is_not_silently_ignored(self):
        result = compare('first then second', 'first then first then second')
        self.assertTrue(result['differences'])
