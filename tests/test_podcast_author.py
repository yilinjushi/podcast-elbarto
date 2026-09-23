import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from podcast_author import prepare
from podcast_sources import enqueue_source
from podcast_run import digest,save,read,capture_input,validate_source


class AuthorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.queue=self.root/'queue.json'
        self.work=self.root/'work'
        env=patch.dict(os.environ,{'PODCAST_SOURCE_DIR':str(self.root/'sources'),'PODCAST_STATE_FILE':str(self.queue)})
        env.start();self.addCleanup(env.stop)
        self.task=enqueue_source(url='https://x.com/u/status/123',title='Title',author='Author',items=[{'type':'para','text':'Original complete source.'}])
        self.source=self.queue.parent/self.task['source_path']
        self.script=self.root/'script.md';self.script.write_bytes(b'Expert terminology stays.\r\n')
        self.review=self.root/'review.json'
        save(self.review,{'approved':True,'reviewer':'independent reader','evidence':'full source review','source_sha256':digest(self.source),'script_sha256':digest(self.script)})
        original=self.work/self.task['id'];original.mkdir(parents=True)
        save(original/'checkpoint.json',{'messageSid':'keep-original'})

    def test_new_version_preserves_source_and_old_checkpoint(self):
        new=prepare(self.task['id'],self.script,self.review,self.queue,self.work)
        self.assertTrue(new['id'].endswith('-v2'))
        self.assertEqual(new['status'],'source_ready')
        self.assertEqual(new['mode'],'punctuation_only')
        self.assertEqual(new['script_sha256'],digest(self.script))
        self.assertEqual(new['source_path'],self.task['source_path'])
        self.assertEqual(read(self.queue)['tasks'][self.task['id']]['reason'],'superseded_by_reviewed_script')
        self.assertEqual(read(self.work/self.task['id']/'checkpoint.json')['messageSid'],'keep-original')
        self.assertEqual((self.work/new['id']/'approved-script.md').read_bytes(),self.script.read_bytes())
        source_path,source=validate_source(self.queue,new)
        data=capture_input(self.work/new['id'],source_path,source,new)
        self.assertEqual(data['mode'],'punctuation_only')
        self.assertEqual(data['title'],'')
        self.assertEqual(data['source_sha256'],digest(self.source))
        self.assertNotEqual(data['text'],source['text'])

    def test_unapproved_or_stale_review_never_creates_task(self):
        for changes in ({'approved':False},{'source_sha256':'wrong'},{'script_sha256':'wrong'},{'evidence':''}):
            proof=read(self.review);proof.update(changes);save(self.review,proof)
            with self.assertRaises(ValueError):prepare(self.task['id'],self.script,self.review,self.queue,self.work)
        self.assertEqual(len(read(self.queue)['tasks']),1)

    def test_published_and_retired_are_immutable(self):
        for state in ('published','retired'):
            queue=read(self.queue);queue['tasks'][self.task['id']]['status']=state;save(self.queue,queue)
            with self.assertRaises(ValueError):prepare(self.task['id'],self.script,self.review,self.queue,self.work)

    def test_existing_destination_is_not_overwritten(self):
        target=self.work/(self.task['id'][:-1]+'2');target.mkdir()
        with self.assertRaises(ValueError):prepare(self.task['id'],self.script,self.review,self.queue,self.work)
        self.assertEqual(read(self.queue)['tasks'][self.task['id']]['status'],'source_ready')

    def test_tampered_copied_script_is_rejected_before_capture(self):
        new=prepare(self.task['id'],self.script,self.review,self.queue,self.work)
        directory=self.work/new['id'];(directory/'approved-script.md').write_text('tampered')
        source_path,source=validate_source(self.queue,new)
        with self.assertRaises(ValueError):capture_input(directory,source_path,source,new)

    def test_missing_both_script_files_cannot_fall_back_to_original(self):
        new=prepare(self.task['id'],self.script,self.review,self.queue,self.work)
        directory=self.work/new['id']
        (directory/'approved-script.md').unlink();(directory/'script-review.json').unlink()
        source_path,source=validate_source(self.queue,new)
        with self.assertRaisesRegex(ValueError,'missing or changed'):capture_input(directory,source_path,source,new)


if __name__=='__main__':unittest.main()
