import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import podcast_automatic as automatic
from podcast_sources import enqueue_source
from podcast_run import save,read,digest,update


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.queue=self.root/'state/podcast_queue.json'
        env=patch.dict(os.environ,{'PODCAST_SOURCE_DIR':str(self.root/'state/podcast_sources'),'PODCAST_STATE_FILE':str(self.queue),'PODCAST_STORE_BACKEND':'local'})
        env.start();self.addCleanup(env.stop)
        text='The team can build the system with context engineering and deep learning. We should test it with the users and check the results before we use it in the system.'
        self.task=enqueue_source(url='https://x.com/u/status/123',title='Title',author='Author',items=[{'type':'para','text':text}])
        self.work=self.root/'podcast-work'/self.task['id'];self.calls=[]

    def invoke(self,args):
        self.calls.append(args)
        if '--publish-ready' in args:
            update(self.queue,self.task['id'],status='published');return True
        audio=self.work/'episode.mp3';audio.write_bytes(b'audio')
        (self.work/'adapted-en.md').write_text('Captured transcript')
        save(self.work/'result.json',{'audioPath':'episode.mp3','audioSha256':digest(audio)})
        save(self.work/'quality.json',{'source_to_adapted':{'approved':False},'adapted_to_audio':{'approved':False}})
        update(self.queue,self.task['id'],status='needs_review');return True

    def test_success_prepares_strict_script_and_publishes_once(self):
        proof={'source_to_adapted':{'approved':True},'adapted_to_audio':{'approved':True}}
        with patch.object(automatic,'invoke',side_effect=self.invoke),patch.object(automatic,'probe_audio',return_value=30),patch.object(automatic,'review',return_value=proof):
            self.assertEqual(automatic.run(self.root)['status'],'published')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(read(self.queue)['tasks'][self.task['id']]['mode'],'punctuation_only')
        self.assertTrue((self.work/'capture-quality.json').exists())
        self.assertIn('not a human approval',read(self.work/'script-review.json')['evidence'])

    def test_failed_asr_never_publishes(self):
        with patch.object(automatic,'invoke',side_effect=self.invoke),patch.object(automatic,'probe_audio',return_value=30),patch.object(automatic,'review',return_value={'source_to_adapted':{'approved':True},'adapted_to_audio':{'approved':False}}):
            self.assertEqual(automatic.run(self.root)['status'],'needs_review')
        self.assertEqual(len(self.calls),1)
        self.assertFalse(read(self.work/'quality.json')['source_to_adapted']['approved'])

    def test_existing_active_task_and_user_script_are_never_approved(self):
        update(self.queue,self.task['id'],status='needs_review')
        with patch.object(automatic,'invoke') as invoke:
            self.assertEqual(automatic.run(self.root)['status'],'attention_required');invoke.assert_not_called()
        update(self.queue,self.task['id'],status='source_ready')
        self.work.mkdir(parents=True,exist_ok=True);(self.work/'approved-script.md').write_text('User script')
        with patch.object(automatic,'invoke') as invoke:
            self.assertEqual(automatic.run(self.root)['status'],'existing_evidence_requires_review');invoke.assert_not_called()
        self.assertEqual((self.work/'approved-script.md').read_text(),'User script')

    def test_capture_failure_stops_and_no_secret_output(self):
        with patch.object(automatic,'invoke',return_value=False) as invoke:
            self.assertEqual(automatic.run(self.root),{'status':'capture_failed'});invoke.assert_called_once()


if __name__=='__main__':unittest.main()
