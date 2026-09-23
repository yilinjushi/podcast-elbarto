"""One conservative automatic episode per invocation; no implicit resume."""
import json
import os
from pathlib import Path
import subprocess
import sys

from podcast_sources import atomic_write, queue_lock
from podcast_run import BASE, read, save, digest, validate_source, task_work, resolve_audio
from podcast_publish import writer_lock, probe_audio
from podcast_auto_review import prepare_source, review


def invoke(arguments):
    try:
        result=subprocess.run([sys.executable,str(BASE/'podcast_run.py'),*arguments],cwd=BASE,capture_output=True,text=True,timeout=3900)
        return result.returncode==0
    except (OSError,subprocess.SubprocessError):
        return False


def run(root=None):
    root=Path(root or os.environ.get('PODCAST_ROOT') or BASE).resolve()
    queue_path=root/'state/podcast_queue.json';work_root=root/'podcast-work'
    task=None
    try:
        # Separate orchestration lock: runner itself acquires runner-lock.
        with writer_lock(work_root/'automatic-lock'):
            with queue_lock(queue_path):
                queue=read(queue_path)
                if queue.get('schema_version')!=1:raise ValueError()
                tasks=queue['tasks']
                active={'needs_login','needs_review','adapting_and_capturing','captured','checking','publishing','ready_to_publish','retry_wait'}
                if any(t.get('status') in active for t in tasks.values()):return {'status':'attention_required'}
                pending=[t for t in tasks.values() if t.get('status')=='source_ready']
                if not pending:return {'status':'idle'}
                task=min(pending,key=lambda t:(t['created_at'],t['id']))
                work=task_work(work_root,task)
                source_path,_=validate_source(queue_path,task)
                # Do not replace any old user/reviewer/capture evidence.
                if task.get('mode') or (work.exists() and any(work.iterdir())):
                    return {'status':'existing_evidence_requires_review'}
                script=prepare_source(source_path)
                work.mkdir(parents=True,exist_ok=True)
                script_path=work/'approved-script.md'
                atomic_write(script_path,script)
                script_hash=digest(script_path)
                save(work/'script-review.json',{'approved':True,'reviewer':'deterministic-original-text-format-only-v1','evidence':'Complete original plain-English text preserved; only Markdown bullet formatting removed. This is an automated lexical check, not a human approval.','source_sha256':digest(source_path),'script_sha256':script_hash})
                task.update(mode='punctuation_only',script_sha256=script_hash)
                save(queue_path,queue)
            args=['--queue',str(queue_path),'--work',str(work_root),'--budget',str(root/'state/podcast_budget.json'),'--task',task['id']]
            if not invoke(args):return {'status':'capture_failed'}
            current=read(queue_path)['tasks'][task['id']]
            if current['status']!='needs_review':return {'status':'capture_not_ready'}
            if (work/'quality.json').exists():
                previous=read(work/'quality.json')
                if any(previous.get(gate,{}).get('approved') for gate in ('source_to_adapted','adapted_to_audio')):
                    return {'status':'existing_approval_requires_review'}
            result=read(work/'result.json');audio=resolve_audio(work,result)
            duration=probe_audio(audio)
            quality=review(source_path,work/'adapted-en.md',audio,work/'automatic-quality.json',model=os.environ.get('PODCAST_ASR_MODEL','base.en'))
            quality['ffprobe']={'duration':duration}
            save(work/'automatic-quality.json',quality)
            if not all(quality.get(gate,{}).get('approved') is True for gate in ('source_to_adapted','adapted_to_audio')):
                return {'status':'needs_review'}
            # Preserve the capture-generated unapproved evidence under a fixed name.
            if (work/'quality.json').exists():
                if (work/'capture-quality.json').exists():return {'status':'existing_evidence_requires_review'}
                atomic_write(work/'capture-quality.json',(work/'quality.json').read_text(encoding='utf-8'))
            save(work/'quality.json',quality)
            backend=os.environ.get('PODCAST_STORE_BACKEND','s3')
            if backend not in {'s3','http','local'}:return {'status':'invalid_store_backend'}
            if not invoke([*args,'--publish-ready','--store',backend]):return {'status':'publication_failed'}
            status=read(queue_path)['tasks'][task['id']]['status']
            return {'status':status if status in {'published','retired'} else 'publication_not_confirmed'}
    except Exception:
        if task:
            try:
                with queue_lock(queue_path):
                    queue=read(queue_path)
                    current=queue['tasks'][task['id']]
                    if current['status']=='source_ready':
                        current.update(status='needs_review',reason='automatic_source_validation_failed')
                        save(queue_path,queue)
            except Exception:pass
        return {'status':'needs_review'}


def main():
    result=run()
    print(json.dumps(result))
    return 0 if result['status'] in {'published','retired','idle'} else 1


if __name__=='__main__':sys.exit(main())
