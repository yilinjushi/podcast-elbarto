"""Local, independent ASR evidence. This command never approves publication."""
import argparse
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re

from podcast_publish import atomic, probe_audio


def words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace('’', "'"))


def compare(expected, actual):
    reference, heard = words(expected), words(actual)
    matcher = SequenceMatcher(None, reference, heard, autojunk=False)
    differences = []
    for operation, a, b, c, d in matcher.get_opcodes():
        if operation != 'equal':
            differences.append({'operation': operation, 'reference_word_start': a,
                                'reference': ' '.join(reference[a:b]),
                                'transcript': ' '.join(heard[c:d])})
    return {'reference_words': len(reference), 'transcript_words': len(heard),
            'sequence_similarity': matcher.ratio(), 'differences': differences,
            'approved': False,
            'warning': 'ASR can mishear names, numbers and punctuation. Similarity is not a fidelity approval.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('--adapted', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='base.en')
    parser.add_argument('--model-cache')
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    duration = probe_audio(args.audio)
    # No prompt containing the rewrite: independent transcription must not be led
    # toward the expected words. All audio processing stays on this machine.
    model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=8,
                         download_root=args.model_cache)
    segments, information = model.transcribe(str(args.audio), language='en', beam_size=5,
                                             condition_on_previous_text=False)
    records = [{'start': segment.start, 'end': segment.end, 'text': segment.text}
               for segment in segments]
    transcript = '\n'.join(segment['text'].strip() for segment in records)
    reference = args.adapted.read_text(encoding='utf-8')
    report = {'audio_sha256': hashlib.sha256(args.audio.read_bytes()).hexdigest(),
              'adapted_sha256': hashlib.sha256(args.adapted.read_bytes()).hexdigest(),
              'model': args.model, 'engine': 'faster-whisper', 'duration': duration,
              'segments': records, 'transcript': transcript, 'comparison': compare(reference, transcript),
              'status': 'needs_review'}
    atomic(args.output, json.dumps(report, ensure_ascii=False, indent=2).encode())
    print(json.dumps({'status': 'needs_review', 'duration': duration,
                      'segments': len(records), 'similarity': report['comparison']['sequence_similarity']}))


if __name__ == '__main__':
    main()
