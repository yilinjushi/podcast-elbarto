import json
from pathlib import Path
import tempfile
import unittest
from podcast_auto_review import review,exact_audio_match,prepare_source,sha,tokens


class AutoReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.text='The team can build the system with context engineering and deep learning. We should test it with the users and check the results before we use it in the system. In 2022 we had 10,000 examples.'
        self.source=self.root/'source.json';self.adapted=self.root/'adapted.md';self.audio=self.root/'episode.mp3';self.output=self.root/'quality.json'
        self.source.write_text(json.dumps({'text':self.text,'items':[{'type':'para','text':self.text}]}));self.adapted.write_text(self.text);self.audio.write_bytes(b'test-media')

    def test_exact_local_transcript_passes_bound_hashes(self):
        result=review(self.source,self.adapted,self.audio,self.output,transcriber=lambda *_:self.text.replace('2022','twenty twenty two').replace('10,000','ten thousand'))
        self.assertEqual(result['status'],'ready_to_publish')
        self.assertTrue(result['source_to_adapted']['approved']);self.assertTrue(result['adapted_to_audio']['approved'])
        self.assertEqual(result['audio_sha256'],sha(self.audio))

    def test_word_change_and_audio_omission_never_pass(self):
        self.adapted.write_text(self.text.replace('context engineering','engineering'))
        result=review(self.source,self.adapted,self.audio,self.output,transcriber=lambda *_:self.text)
        self.assertFalse(result['source_to_adapted']['approved'])
        self.adapted.write_text(self.text)
        result=review(self.source,self.adapted,self.audio,self.output,transcriber=lambda *_:self.text.replace('deep learning','learning'))
        self.assertFalse(result['adapted_to_audio']['approved'])

    def test_images_code_and_non_english_stop(self):
        for kind in ('image','code','table'):
            self.source.write_text(json.dumps({'text':self.text,'items':[{'type':kind,'text':self.text}]}))
            with self.assertRaises(ValueError):prepare_source(self.source)
        self.source.write_text(json.dumps({'text':'中文内容','items':[{'type':'para','text':'中文内容'}]}))
        with self.assertRaises(ValueError):prepare_source(self.source)

    def test_missing_local_asr_model_never_inherits_approval(self):
        def missing(*_):raise RuntimeError('private path token')
        result=review(self.source,self.adapted,self.audio,self.output,transcriber=missing)
        self.assertEqual(result['status'],'needs_review')
        self.assertFalse(result['adapted_to_audio']['approved'])
        self.assertNotIn('private path',self.output.read_text())

    def test_numeric_signs_decimals_and_no_similarity_tolerance(self):
        self.assertTrue(exact_audio_match('We have -12.5% and 2022 examples.','We have minus twelve point five percent and two thousand twenty two examples.'))
        self.assertFalse(exact_audio_match('We have -12.5%.','We have twelve point five percent.'))
        self.assertFalse(exact_audio_match('We have 12 examples.','We have thirteen examples.'))
        self.assertFalse(exact_audio_match('We have context engineering.','We have engineering.'))

    def test_spaced_signs_and_leading_decimals_cannot_disappear(self):
        for source,transcript in [('- 12','twelve'),('.5','five'),('5','.5'),('12','- 12'),('- .5','zero point five'),('− 12','twelve'),('﹣12','twelve'),('－12','twelve'),('–12','twelve'),('— 12','twelve')]:
            with self.subTest(source=source,transcript=transcript):
                self.assertFalse(exact_audio_match(source,transcript))
        self.assertEqual(tokens('- 12'),['-12'])
        self.assertEqual(tokens('.5'),['.5'])
        self.assertTrue(exact_audio_match('- 12','minus twelve'))
        self.assertTrue(exact_audio_match('− .5','minus zero point five'))

    def test_source_adapted_numeric_changes_fail_before_asr(self):
        for original,changed in [('- 12','12'),('.5','5'),('5','.5'),('−12','12')]:
            text=self.text+' The measured value is '+original+'.'
            self.source.write_text(json.dumps({'text':text,'items':[{'type':'para','text':text}]}))
            self.adapted.write_text(self.text+' The measured value is '+changed+'.')
            result=review(self.source,self.adapted,self.audio,self.output,transcriber=lambda *_:self.adapted.read_text())
            self.assertFalse(result['source_to_adapted']['approved'])


if __name__=='__main__':unittest.main()
