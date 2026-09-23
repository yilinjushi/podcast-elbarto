"""Prepare an independently reviewed script as a new, unapproved audio version."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
from podcast_sources import queue_lock
from podcast_run import BASE, read, save, digest, validate_source, task_work, capture_input
from podcast_publish import writer_lock


def prepare(task_id, script, review, queue_path, work_root):
    script, review, queue_path, work_root = map(Path,(script,review,queue_path,work_root))
    with writer_lock(work_root/'runner-lock'), queue_lock(queue_path):
        queue = read(queue_path)
        if queue.get('schema_version') != 1:
            raise ValueError('unsupported queue')
        old = queue['tasks'][task_id]
        task_work(work_root,old)
        if old['status'] in {'published','retired','publishing','adapting_and_capturing'} or old.get('reason') == 'superseded_by_reviewed_script':
            raise ValueError('task cannot be superseded')
        source_path, source = validate_source(queue_path,old)
        proof = read(review)
        if proof.get('approved') is not True or not proof.get('reviewer') or not proof.get('evidence') or proof.get('source_sha256') != digest(source_path) or proof.get('script_sha256') != digest(script):
            raise ValueError('independent script review required')
        text = script.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('empty script')
        prefix = f"{old['article_id']}-{old['body_hash']}-v"
        versions = [int(key[len(prefix):]) for key in queue['tasks'] if key.startswith(prefix) and re.fullmatch(r'\d+',key[len(prefix):])]
        version = max(versions,default=0)+1
        new_id = prefix+str(version)
        task = {key:old[key] for key in ('article_id','body_hash','source_path','title','url','author')}
        task.update(id=new_id,processing_version=str(version),status='source_ready',created_at=datetime.now(timezone.utc).isoformat(),supersedes=task_id,mode='punctuation_only',script_sha256=proof['script_sha256'])
        destination = task_work(work_root,task)
        if new_id in queue['tasks'] or destination.exists():
            raise ValueError('destination already exists')
        destination.mkdir(parents=True,exist_ok=False)
        # Preserve the reviewed bytes, including line endings, for the hash.
        data = script.read_bytes()
        with (destination/'approved-script.md').open('xb') as stream:
            stream.write(data)
        save(destination/'script-review.json',{key:proof[key] for key in ('approved','reviewer','evidence','source_sha256','script_sha256')})
        capture_input(destination,source_path,source,task)
        old.update(status='failed',reason='superseded_by_reviewed_script',superseded_by=new_id)
        queue['tasks'][new_id] = task
        save(queue_path,queue)
        return task


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare'])
    parser.add_argument('--task',required=True)
    parser.add_argument('--script',type=Path,required=True)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--queue',type=Path,default=BASE/'state/podcast_queue.json')
    parser.add_argument('--work',type=Path,default=BASE/'podcast-work')
    args=parser.parse_args()
    task=prepare(args.task,args.script,args.review,args.queue,args.work)
    print(task['id'])


if __name__=='__main__':
    main()
