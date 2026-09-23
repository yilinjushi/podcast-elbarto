"""Single-task Pi capture runner; publication requires independent review evidence."""
import argparse
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from pathlib import Path
import subprocess
import signal
from datetime import datetime, timezone

from podcast_sources import atomic_write, queue_lock
from podcast_publish import probe_audio, writer_lock, Publisher, LocalStore, S3Store

BASE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def create_store(backend, local_root):
    if backend == 'local':
        return LocalStore(local_root)
    if backend == 'http':
        from podcast_http_store import HttpStore
        return HttpStore(os.environ['PODCAST_SERVICE_ORIGIN'], os.environ['PODCAST_STORE_TOKEN'])
    if backend == 's3':
        return S3Store(os.environ['PODCAST_BUCKET'], 'podcast', os.environ['PODCAST_S3_ENDPOINT'])
    raise ValueError('unsupported store backend')


def task_work(root, task):
    if not re.fullmatch(r'x-\d+-[a-f0-9]{64}-v\d+', task['id']):
        raise ValueError('unsafe task identity')
    work = (root / task['id']).resolve()
    if not work.is_relative_to(root.resolve()):
        raise ValueError('task directory outside work root')
    return work


def cleanup_task_audio(work):
    """Remove only known media names inside this already-validated task."""
    work = work.resolve()
    directories = [work]
    for candidate in work.iterdir():
        if re.fullmatch(r'segment-\d{3,}', candidate.name):
            if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)() or not candidate.resolve().is_relative_to(work):
                raise ValueError('segment cleanup path outside task')
            if candidate.is_dir():
                directories.append(candidate)
    for directory in directories:
        for name in ('episode.mp3', 'episode.partial.mp3', 'partial.mp3'):
            path = directory / name
            if path.is_symlink() or not path.resolve().is_relative_to(work):
                raise ValueError('audio cleanup path outside task')
    for directory in directories:
        for name in ('episode.mp3', 'episode.partial.mp3', 'partial.mp3'):
            (directory / name).unlink(missing_ok=True)


def validate_source(queue_path, task):
    source_path = (queue_path.parent / task['source_path']).resolve()
    root = Path(os.environ.get('PODCAST_SOURCE_DIR') or BASE / 'state/podcast_sources').resolve()
    if not source_path.is_relative_to(root):
        raise ValueError('source outside configured source root')
    source = read(source_path)
    metadata = source.get('metadata', {})
    parsed = urlsplit(task['url'])
    status = re.fullmatch(r'/[^/]+/status/(\d+)/?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname not in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'} or not status or parsed.username or parsed.password:
        raise ValueError('invalid source URL')
    if task['article_id'] != 'x-' + status.group(1) or source.get('article_id') != task['article_id'] or metadata.get('url') != task['url']:
        raise ValueError('source identity mismatch')
    expected_hash = hashlib.sha256(json.dumps({'title': metadata.get('title'), 'items': source.get('items')}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    expected_text = '\n\n'.join(x['text'] for x in source['items'] if x.get('type') != 'image' and x.get('text'))
    if source.get('body_hash') != task['body_hash'] or expected_hash != task['body_hash'] or source.get('text') != expected_text or not expected_text.strip() or metadata.get('title') != task['title']:
        raise ValueError('source version mismatch')
    return source_path, source


def resolve_audio(work, captured):
    raw = captured.get('audioPath', '')
    if not raw:
        raise ValueError('capture audio path missing')
    # Legacy results used an absolute path. Relocate only the known media name
    # and require its recorded hash, never follow an external path or symlink.
    normalized = raw.replace('\\', '/')
    old_absolute = normalized.startswith('/') or bool(re.match(r'^[A-Za-z]:/', normalized))
    if old_absolute:
        if normalized.rsplit('/', 1)[-1] != 'episode.mp3':
            raise ValueError('unrecognized legacy audio path')
        candidate = work / 'episode.mp3'
    else:
        candidate = work / normalized
    audio = candidate.resolve()
    if not audio.is_relative_to(work.resolve()) or not audio.is_file():
        raise ValueError('audio outside task directory or missing')
    if not captured.get('audioSha256') or digest(audio) != captured['audioSha256']:
        raise ValueError('capture audio hash mismatch')
    captured['audioPath'] = audio.relative_to(work.resolve()).as_posix()
    return audio


def capture_input(work, source_path, source, task):
    script = work / 'approved-script.md'
    review_path = work / 'script-review.json'
    if task.get('mode') == 'punctuation_only':
        if not script.is_file() or not review_path.is_file() or not task.get('script_sha256') or digest(script) != task['script_sha256']:
            raise ValueError('required authored script missing or changed')
    elif task.get('mode') is not None:
        raise ValueError('unsupported task mode')
    if script.exists() or review_path.exists():
        if any(p.is_symlink() or not p.resolve().is_relative_to(work.resolve()) for p in (script,review_path)):
            raise ValueError('reviewed script path escaped task')
        review = read(review_path)
        if review.get('approved') is not True or not review.get('reviewer') or not review.get('evidence') or review.get('source_sha256') != digest(source_path) or review.get('script_sha256') != digest(script):
            raise ValueError('authored script review mismatch')
        text = script.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('empty authored script')
        return {'title':'', 'text':text, 'source_sha256':digest(source_path), 'url':task['url'], 'mode':'punctuation_only'}
    return {'title':task['title'], 'text':source['text'], 'url':task['url'], 'source_sha256':digest(source_path)}


def validate_checkpoint(work, source_path, source, task):
    input_data = read(work / 'input.json')
    wanted = capture_input(work,source_path,source,task)
    if any(input_data.get(key) != value for key,value in wanted.items() if key != 'url') or ('url' in input_data and input_data['url'] != task['url']):
        raise ValueError('checkpoint input does not match source')
    expected = hashlib.sha256(json.dumps(input_data, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    manifest = read(work / 'segments.json')
    if manifest.get('version') != 1 or manifest.get('sourceHash') != expected:
        raise ValueError('segment manifest does not match input')
    cursor = 0
    existing = False
    utf16_text = input_data['text'].encode('utf-16-le')
    text_length = len(utf16_text) // 2
    for index, segment in enumerate(manifest.get('ranges', [])):
        end = segment.get('end')
        if segment.get('index') != index or segment.get('start') != cursor or not isinstance(end, int) or not cursor < end <= text_length:
            raise ValueError('invalid segment ranges')
        text = utf16_text[cursor * 2:end * 2].decode('utf-16-le')
        if hashlib.sha256(text.encode()).hexdigest() != segment.get('sha256'):
            raise ValueError('segment content hash mismatch')
        directory = work / f'segment-{index + 1:03d}'
        if not directory.resolve().is_relative_to(work.resolve()):
            raise ValueError('segment directory outside task')
        checkpoint_path = directory / 'checkpoint.json'
        result_path = directory / 'result.json'
        if checkpoint_path.exists():
            existing = True
            checkpoint = read(checkpoint_path)
            segment_input = read(directory / 'source.json')
            wanted_input = {'title': input_data.get('title', '') if index == 0 else '', 'includeTitle': index == 0, 'url':input_data.get('url', ''), 'text':text, 'source_sha256':segment['sha256']}
            if input_data.get('mode'):
                wanted_input['mode'] = input_data['mode']
            hashed_input = hashlib.sha256(json.dumps(segment_input, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            if segment_input != wanted_input or checkpoint.get('inputHash') != hashed_input or not checkpoint.get('messageSid') or not checkpoint.get('rewrite'):
                raise ValueError('checkpoint has no confirmed existing reply; resubmission prohibited')
            conversation = urlsplit(checkpoint.get('conversationUrl', ''))
            if conversation.scheme != 'https' or conversation.hostname != 'pi.ai' or conversation.username or conversation.password:
                raise ValueError('invalid checkpoint conversation URL')
        elif result_path.exists():
            existing = True
            result = read(result_path)
            if result.get('sourceSha256') != segment['sha256']:
                raise ValueError('segment result source mismatch')
            resolve_audio(directory, result)
        cursor = end
    if cursor != text_length or not existing:
        raise ValueError('resume requires a complete manifest and existing submission checkpoint')
    return input_data


def update(queue_path, task_id, **changes):
    with queue_lock(queue_path):
        queue = read(queue_path)
        queue['tasks'][task_id].update(changes)
        save(queue_path, queue)


def reserve_budget(path, minutes, limit):
    """Conservative reservations are never refunded after a crash or short run."""
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    with queue_lock(path):
        ledger = read(path) if path.exists() else {}
        reserved = ledger.get(month, 0)
        if reserved + minutes > limit:
            return False
        ledger[month] = reserved + minutes
        save(path, ledger)
    return True


def run_capture(command, *, timeout, cwd):
    """Own the capture's entire process group, including durable-sync children."""
    kwargs = {'cwd': cwd, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            # PID is obtained only from the process created immediately above.
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=30)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise subprocess.TimeoutExpired(command, timeout) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def validate_review(source, adapted_path, audio_path, quality):
    if quality.get('source_sha256') != digest(source) or quality.get('adapted_sha256') != digest(adapted_path):
        raise ValueError('review does not match source and adapted text')
    if quality.get('audio_sha256') != digest(audio_path):
        raise ValueError('review does not match audio')
    for gate in ['source_to_adapted', 'adapted_to_audio']:
        check = quality.get(gate, {})
        if check.get('approved') is not True or not check.get('evidence') or not check.get('reviewer'):
            raise ValueError('needs_review: both checks need approval, evidence and reviewer')
    return probe_audio(audio_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', type=Path, default=BASE / 'state/podcast_queue.json')
    parser.add_argument('--work', type=Path, default=BASE / 'podcast-work')
    parser.add_argument('--budget', type=Path, default=BASE / 'state/podcast_budget.json')
    parser.add_argument('--minutes', type=int, default=30)
    parser.add_argument('--monthly-minutes', type=int, default=1200)
    parser.add_argument('--task')
    parser.add_argument('--resume', action='store_true', help='Resume a checkpointed article without resubmitting existing segments')
    parser.add_argument('--publish-ready', action='store_true')
    parser.add_argument('--store', choices=['local', 's3', 'http'], default='local')
    parser.add_argument('--store-root', type=Path, default=BASE / 'podcast-store')
    args = parser.parse_args()
    if args.resume and (not args.task or args.publish_ready):
        parser.error('--resume requires --task and cannot combine with --publish-ready')
    if not 1 <= args.minutes <= 60 or not 1 <= args.monthly_minutes <= 1600:
        parser.error('invalid free-budget reservation')
    args.work.mkdir(parents=True, exist_ok=True)
    with writer_lock(args.work / 'runner-lock'):
        if args.publish_ready:
            if not args.task:
                parser.error('--publish-ready requires --task')
            queue = read(args.queue)
            task = queue['tasks'][args.task]
            try:
                work = task_work(args.work, task)
                source, _ = validate_source(args.queue, task)
                captured = read(work / 'result.json')
                audio = resolve_audio(work, captured)
                validate_review(source, work / 'adapted-en.md', audio, read(work / 'quality.json'))
            except (ValueError, KeyError, OSError, TypeError, RuntimeError):
                update(args.queue, task['id'], status='needs_review', reason='publication_quality_validation_failed')
                raise RuntimeError('Publication blocked: source, audio and both review gates must validate') from None
            store = create_store(args.store, args.store_root)
            publisher = Publisher(store, args.work / 'publisher', os.environ['PODCAST_BASE_URL'], max_bytes=int(os.environ.get('PODCAST_MAX_BYTES', '1000000000')))
            published = publisher.publish(read(work / 'episode.json'), audio, work / 'quality.json')
            update(args.queue, task['id'], status=published['status'])
            # Remove only captured audio managed by this task; retain text and evidence.
            if published['status'] in {'published', 'retired'}:
                cleanup_task_audio(work)
            print(json.dumps({'status': published['status'], 'id': task['article_id']}))
            return
        with queue_lock(args.queue):
            queue = read(args.queue)
            if queue.get('schema_version') != 1:
                raise ValueError('unsupported queue')
            # One incomplete capture at a time; reviews do not silently disappear.
            active = [t for t in queue['tasks'].values() if t['status'] in {'adapting_and_capturing', 'needs_review', 'captured', 'needs_login'}]
            if active and not args.resume:
                print(json.dumps({'status': active[0]['status'], 'task': active[0]['id']}))
                return
            eligible = {'adapting_and_capturing', 'needs_review', 'captured', 'needs_login', 'retry_wait', 'failed', 'quota_wait'} if args.resume else {'source_ready', 'quota_wait'}
            if args.resume and any(t['id'] != args.task for t in active):
                raise ValueError('another task requires attention before resume')
            choices = [t for t in queue['tasks'].values() if t['status'] in eligible and (not args.task or t['id'] == args.task)]
            if not choices:
                if args.resume:
                    raise ValueError('requested task is not resumable')
                print('{"status":"idle"}')
                return
            task = min(choices, key=lambda t: (t['created_at'], t['id']))
        try:
            work = task_work(args.work, task)
            source_path, source = validate_source(args.queue, task)
            prepared_input = capture_input(work,source_path,source,task)
            if args.resume:
                validate_checkpoint(work, source_path, source, task)
            elif (work / 'checkpoint.json').exists() or (work / 'segments.json').exists():
                raise ValueError('existing checkpoint requires explicit --resume')
        except (ValueError, KeyError, OSError, TypeError):
            update(args.queue, task['id'], status='needs_review' if args.resume else 'failed', reason='checkpoint_or_source_validation_failed')
            raise RuntimeError('Task source/checkpoint validation failed; no content submitted') from None
        if not reserve_budget(args.budget, args.minutes, args.monthly_minutes):
            update(args.queue, task['id'], status='quota_wait')
            print('{"status":"quota_wait"}')
            return
        work.mkdir(parents=True, exist_ok=True)
        update(args.queue, task['id'], status='adapting_and_capturing')
        if not args.resume:
            save(work / 'input.json', prepared_input)
        try:
            command = ['node', str(BASE / 'podcast/pi_article.mjs'), str(work / 'input.json'), str(work)]
            if args.resume:
                command.append('--resume')
            result = run_capture(command, timeout=args.minutes * 60, cwd=BASE / 'podcast')
            if result.returncode:
                # Do not print browser exceptions that may contain session addresses.
                state = 'needs_login' if any(marker in result.stderr for marker in ('PI_NEEDS_ATTENTION', 'PI_NEEDS_LOGIN')) else 'needs_review'
                update(args.queue, task['id'], status=state, reason='capture_failed_inspect_result_no_auto_resubmit')
                raise RuntimeError('Pi capture incomplete; inspect task result, do not blindly resubmit')
            captured = read(work / 'result.json')
            audio_path = resolve_audio(work, captured)
            save(work / 'result.json', captured)
            adapted = work / 'adapted-en.md'
            atomic_write(adapted, captured['adaptedText'])
            duration = probe_audio(audio_path)
            save(work / 'episode.json', {'id': task['article_id'], 'title': task['title'], 'source_url': task['url'], 'description': task['author']})
            save(work / 'quality.json', {
                'source_sha256': digest(source_path), 'adapted_sha256': digest(adapted),
                'audio_sha256': digest(audio_path), 'ffprobe': {'duration': duration},
                'source_to_adapted': {'approved': False, 'reviewer': '', 'evidence': ''},
                'adapted_to_audio': {'approved': False, 'reviewer': '', 'evidence': ''},
                'status': 'needs_review',
            })
            update(args.queue, task['id'], status='needs_review', work_path=task['id'], reason='independent_quality_checks_required')
            print(json.dumps({'status': 'needs_review', 'task': task['id']}))
        except subprocess.TimeoutExpired:
            update(args.queue, task['id'], status='needs_review', reason='capture_timeout_no_auto_resubmit')
            raise RuntimeError('Pi capture timeout; inspect existing reply before retry') from None
        except (ValueError, KeyError, OSError, TypeError):
            update(args.queue, task['id'], status='needs_review', reason='capture_result_validation_failed_no_auto_resubmit')
            raise RuntimeError('Capture result validation failed; inspect existing reply before retry') from None
        except RuntimeError:
            # A failed decoder or local verification must not strand the queue
            # in an apparently running state. Preserve explicit login failures.
            if read(args.queue)['tasks'][task['id']]['status'] == 'adapting_and_capturing':
                update(args.queue, task['id'], status='needs_review', reason='capture_verification_failed_no_auto_resubmit')
            raise


if __name__ == '__main__':
    main()
