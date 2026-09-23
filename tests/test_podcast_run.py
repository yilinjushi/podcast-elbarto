import json
import tempfile
import unittest
import os
import sys
import hashlib
import subprocess
import signal
from unittest.mock import patch
from pathlib import Path
import podcast_run
from podcast_run import reserve_budget, validate_review, resolve_audio, validate_source, validate_checkpoint, digest, save
from podcast_sources import enqueue_source


class RunnerTests(unittest.TestCase):
    def test_http_store_factory_uses_scoped_credentials(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        factory = MagicMock()
        with patch.dict(sys.modules, {'podcast_http_store':SimpleNamespace(HttpStore=factory)}), patch.dict(os.environ, {'PODCAST_SERVICE_ORIGIN':'https://podcast.example','PODCAST_STORE_TOKEN':'scoped-secret'}):
            store = podcast_run.create_store('http',Path('unused'))
        factory.assert_called_once_with('https://podcast.example','scoped-secret')
        self.assertIs(store,factory.return_value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / 'queue.json'
        self.sources = self.root / 'sources'
        env = patch.dict(os.environ, {'PODCAST_SOURCE_DIR': str(self.sources), 'PODCAST_STATE_FILE': str(self.queue)})
        env.start()
        self.addCleanup(env.stop)
        self.task = enqueue_source(url='https://x.com/u/status/123', title='Title', author='A', items=[{'type':'para','text':'Original text.'}])
        self.source_path = self.queue.parent / self.task['source_path']
        self.work_root = self.root / 'work'
        self.work = self.work_root / self.task['id']
        self.work.mkdir(parents=True)

    def checkpoint(self):
        source = json.loads(self.source_path.read_text())
        article = {'title':'Title','text': source['text'], 'source_sha256':digest(self.source_path)}
        save(self.work / 'input.json', article)
        input_hash = hashlib.sha256(json.dumps(article, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        text_hash = hashlib.sha256(source['text'].encode()).hexdigest()
        save(self.work/'segments.json', {'version':1,'sourceHash':input_hash,'ranges':[{'index':0,'start':0,'end':len(source['text']),'sha256':text_hash}]})
        segment_input = {'title':'Title','includeTitle':True,'url':'','text':source['text'],'source_sha256':text_hash}
        segment = self.work/'segment-001'
        segment.mkdir(exist_ok=True)
        save(segment/'source.json',segment_input)
        segment_hash = hashlib.sha256(json.dumps(segment_input, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        save(segment/'checkpoint.json', {'inputHash':segment_hash,'messageSid':'existing-reply','rewrite':'Rewritten text.','conversationUrl':'https://pi.ai/talk'})
        return source

    def test_migrated_absolute_audio_resolves_only_with_matching_hash(self):
        audio = self.work / 'episode.mp3'
        audio.write_bytes(b'media')
        result = {'audioPath':'C:\\old-machine\\work\\episode.mp3','audioSha256':digest(audio)}
        self.assertEqual(resolve_audio(self.work, result), audio.resolve())
        self.assertEqual(result['audioPath'], 'episode.mp3')
        result['audioSha256'] = 'wrong'
        with self.assertRaises(ValueError):
            resolve_audio(self.work, result)

    def test_audio_relative_traversal_is_rejected(self):
        outside = self.work_root / 'outside.mp3'
        outside.write_bytes(b'media')
        with self.assertRaises(ValueError):
            resolve_audio(self.work, {'audioPath':'../outside.mp3','audioSha256':digest(outside)})

    def test_source_root_url_and_actual_content_are_validated(self):
        validate_source(self.queue, self.task)
        invalid = {**self.task, 'url':'https://evil.example/status/123'}
        with self.assertRaises(ValueError):
            validate_source(self.queue, invalid)
        with self.assertRaises(ValueError):
            validate_source(self.queue, {**self.task, 'source_path':'../outside.json'})
        source = json.loads(self.source_path.read_text())
        source['text'] = 'Changed without hash update'
        save(self.source_path, source)
        with self.assertRaises(ValueError):
            validate_source(self.queue, self.task)

    def test_resume_missing_or_uncertain_checkpoint_never_invokes_browser(self):
        podcast_run.update(self.queue, self.task['id'], status='needs_review')
        arguments = ['podcast_run.py','--resume','--task', self.task['id'],'--queue',str(self.queue),'--work',str(self.work_root),'--budget',str(self.root/'budget.json')]
        with patch.object(sys, 'argv', arguments), patch.object(podcast_run, 'run_capture') as run:
            with self.assertRaises(RuntimeError):
                podcast_run.main()
            run.assert_not_called()
        source = self.checkpoint()
        checkpoint = json.loads((self.work/'segment-001/checkpoint.json').read_text())
        checkpoint.pop('messageSid')
        save(self.work/'segment-001/checkpoint.json', checkpoint)
        with self.assertRaises(ValueError):
            validate_checkpoint(self.work, self.source_path, source, self.task)

    def test_resume_existing_reply_passes_explicit_flag_and_keeps_review_gates(self):
        self.checkpoint()
        podcast_run.update(self.queue, self.task['id'], status='needs_review')
        arguments = ['podcast_run.py','--resume','--task',self.task['id'],'--queue',str(self.queue),'--work',str(self.work_root),'--budget',str(self.root/'budget.json')]
        def capture(command, **kwargs):
            self.assertEqual(command[-1], '--resume')
            self.assertEqual(Path(command[1]).name, 'pi_article.mjs')
            audio = self.work/'episode.mp3'
            audio.write_bytes(b'media')
            save(self.work/'result.json', {'audioPath':str(audio), 'audioSha256':digest(audio),'adaptedText':'Rewritten text.'})
            return type('Result', (), {'returncode':0})()
        with patch.object(sys,'argv',arguments), patch.object(podcast_run,'run_capture',side_effect=capture), patch.object(podcast_run,'probe_audio',return_value=12):
            podcast_run.main()
        quality = json.loads((self.work/'quality.json').read_text())
        self.assertFalse(quality['source_to_adapted']['approved'])
        self.assertFalse(quality['adapted_to_audio']['approved'])
        self.assertEqual(json.loads((self.work/'result.json').read_text())['audioPath'], 'episode.mp3')
        self.assertEqual(json.loads(self.queue.read_text())['tasks'][self.task['id']]['status'], 'needs_review')

    def test_both_review_gates_remain_required(self):
        path = self.source_path
        quality = {'source_sha256':digest(path),'adapted_sha256':digest(path),'audio_sha256':digest(path),'source_to_adapted':{'approved':True,'reviewer':'person','evidence':'checked'}}
        with self.assertRaisesRegex(ValueError,'both checks'):
            validate_review(path,path,path,quality)

    def test_capture_preserves_completed_process_output(self):
        result = podcast_run.run_capture([sys.executable,'-c','import sys; print("out"); print("err",file=sys.stderr)'],timeout=10,cwd=self.root)
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.strip(),'out')
        self.assertEqual(result.stderr.strip(),'err')

    def test_timeout_terminates_only_owned_process_tree_and_drains(self):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.pid = 76543
        process.communicate.side_effect = [subprocess.TimeoutExpired(['node'],1),('', '')]
        with patch.object(podcast_run.subprocess,'Popen',return_value=process) as popen:
            if os.name == 'nt':
                with patch.object(podcast_run.subprocess,'run') as terminate:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        podcast_run.run_capture(['node'],timeout=1,cwd=self.root)
                    self.assertEqual(terminate.call_args.args[0],['taskkill','/PID','76543','/T','/F'])
                    self.assertIn('creationflags',popen.call_args.kwargs)
            else:
                with patch.object(podcast_run.os,'killpg') as terminate:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        podcast_run.run_capture(['node'],timeout=1,cwd=self.root)
                    terminate.assert_called_once_with(76543,podcast_run.signal.SIGKILL)
                    self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(process.communicate.call_count,2)

    def test_linux_timeout_kills_process_group(self):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.pid = 12345
        process.communicate.side_effect = [subprocess.TimeoutExpired(['node'],1),('', '')]
        with patch.object(podcast_run.os,'name','posix'), patch.object(signal,'SIGKILL',9,create=True), patch.object(podcast_run.os,'killpg',create=True) as killpg, patch.object(podcast_run.subprocess,'Popen',return_value=process) as popen:
            with self.assertRaises(subprocess.TimeoutExpired):
                podcast_run.run_capture(['node'],timeout=1,cwd=self.root)
            killpg.assert_called_once_with(12345,signal.SIGKILL)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(process.communicate.call_count,2)

    def test_resume_rejects_manifest_without_existing_submission(self):
        source = self.checkpoint()
        (self.work/'segment-001/checkpoint.json').unlink()
        with self.assertRaisesRegex(ValueError,'existing submission'):
            validate_checkpoint(self.work,self.source_path,source,self.task)

    def test_authored_resume_uses_reviewed_script_ranges_and_mode(self):
        script=self.work/'approved-script.md';script.write_text('Technical terms remain intact.',encoding='utf-8')
        save(self.work/'script-review.json',{'approved':True,'reviewer':'reader','evidence':'full comparison','source_sha256':digest(self.source_path),'script_sha256':digest(script)})
        source=json.loads(self.source_path.read_text())
        article=podcast_run.capture_input(self.work,self.source_path,source,self.task)
        save(self.work/'input.json',article)
        text_hash=hashlib.sha256(article['text'].encode()).hexdigest()
        article_hash=hashlib.sha256(json.dumps(article,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        save(self.work/'segments.json',{'version':1,'sourceHash':article_hash,'ranges':[{'index':0,'start':0,'end':len(article['text']),'sha256':text_hash}]})
        segment=self.work/'segment-001';segment.mkdir()
        segment_input={'title':'','includeTitle':True,'url':article['url'],'text':article['text'],'source_sha256':text_hash,'mode':'punctuation_only'}
        save(segment/'source.json',segment_input)
        segment_hash=hashlib.sha256(json.dumps(segment_input,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        save(segment/'checkpoint.json',{'inputHash':segment_hash,'messageSid':'existing','rewrite':article['text'],'conversationUrl':'https://pi.ai/talk'})
        self.assertEqual(validate_checkpoint(self.work,self.source_path,source,self.task)['mode'],'punctuation_only')
        self.assertNotEqual(article['text'],source['text'])

    def test_publication_cleanup_removes_only_current_task_media(self):
        segment = self.work/'segment-001'
        segment.mkdir()
        for directory in (self.work,segment):
            for name in ('episode.mp3','episode.partial.mp3','partial.mp3','checkpoint.json','source.json'):
                (directory/name).write_bytes(b'test')
        other = self.work_root/'other-task'
        other.mkdir()
        (other/'episode.mp3').write_bytes(b'keep')
        podcast_run.cleanup_task_audio(self.work)
        for directory in (self.work,segment):
            self.assertFalse((directory/'episode.mp3').exists())
            self.assertFalse((directory/'episode.partial.mp3').exists())
            self.assertFalse((directory/'partial.mp3').exists())
            self.assertTrue((directory/'checkpoint.json').exists())
            self.assertTrue((directory/'source.json').exists())
        self.assertTrue((other/'episode.mp3').exists())

    def test_resume_rejects_range_gap_and_modified_segment_source(self):
        source = self.checkpoint()
        manifest = json.loads((self.work/'segments.json').read_text())
        manifest['ranges'][0]['start'] = 1
        save(self.work/'segments.json', manifest)
        with self.assertRaisesRegex(ValueError,'ranges'):
            validate_checkpoint(self.work,self.source_path,source,self.task)
        self.checkpoint()
        segment = json.loads((self.work/'segment-001/source.json').read_text())
        segment['text'] = 'Changed content'
        save(self.work/'segment-001/source.json',segment)
        with self.assertRaises(ValueError):
            validate_checkpoint(self.work,self.source_path,source,self.task)

    def test_budget_is_durable_and_no_refund(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'budget.json'
            self.assertTrue(reserve_budget(path, 30, 60))
            self.assertTrue(reserve_budget(path, 30, 60))
            self.assertFalse(reserve_budget(path, 30, 60))
            self.assertEqual(sum(json.loads(path.read_text()).values()), 60)

    def test_rejects_stale_review_before_publication(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'source.json'
            path.write_text('changed')
            with self.assertRaisesRegex(ValueError, 'source'):
                validate_review(path, path, path, {'source_sha256': 'old'})
