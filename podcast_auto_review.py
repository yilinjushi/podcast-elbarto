"""Conservative local automatic review. Any uncertainty leaves needs_review.

No paid service, implicit model download, similarity score, or previous human
approval is used. Exact ASR agreement is evidence, not proof against ASR errors.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from podcast_sources import atomic_write

WORDS = re.compile(r"[+\-−﹣－–—]?\s*(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)|[^\W\d_]+(?:['’][^\W\d_]+)*|[^\s.,!?;:\"“”‘’()\[\]{}…—–-]", re.UNICODE)
FUNCTION = set('the a an and or but if when because as with without in on at to from of for is are was were be been being this that these those it its you your we our they their not can will should how what who which than have has do does'.split())
SMALL = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']
CONTRACTIONS = {"don't":'do not',"doesn't":'does not',"didn't":'did not',"can't":'cannot',"won't":'will not',"isn't":'is not',"aren't":'are not',"it's":'it is',"you'll":'you will',"we'll":'we will',"they'll":'they will',"we're":'we are',"you're":'you are',"they're":'they are',"i'm":'i am',"i've":'i have',"we've":'we have',"that's":'that is'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tokens(text):
    return [re.sub(r'\s+','',x).replace('’',"'").translate(str.maketrans('−﹣－–—','-----')) for x in WORDS.findall(text)]


def prepare_source(source_path):
    source = json.loads(Path(source_path).read_text(encoding='utf-8'))
    items = source.get('items')
    if not isinstance(items,list) or not items:
        raise ValueError('source_items_required')
    if any(not isinstance(item,dict) or item.get('type') not in {'para','h1','h2','h3','li','blockquote'} for item in items):
        raise ValueError('non_text_content_requires_review')
    text = '\n\n'.join(item['text'] for item in items if item.get('text'))
    if text != source.get('text'):
        raise ValueError('source_body_mismatch')
    if re.search(r'```|~~~|!\[|<\s*(?:img|table|code)|\|[^\n]+\||^\s*(?:def |class |import |function |const |SELECT )',text,re.M|re.I):
        raise ValueError('complex_content_requires_review')
    if any(char.isalpha() and 'LATIN' not in unicodedata.name(char,'') for char in text):
        raise ValueError('non_english_requires_review')
    words = [x.lower() for x in tokens(text) if x.isalpha()]
    if len(words)<30 or sum(word in FUNCTION for word in words)/len(words)<0.18:
        raise ValueError('english_not_confident')
    # Only formatting bullets are removed. Numbered list numbers are substantive.
    return re.sub(r'(?m)^\s*[-*+]\s+(?=[A-Za-z])','',text)


def integer_words(number):
    if number<20:return [SMALL[number]]
    if number<100:return [TENS[number//10]]+([] if number%10==0 else [SMALL[number%10]])
    if number<1000:return [SMALL[number//100],'hundred']+([] if number%100==0 else integer_words(number%100))
    if number<=10000:return integer_words(number//1000)+['thousand']+([] if number%1000==0 else integer_words(number%1000))
    return None


def speech_tokens(text):
    result=[]
    for token in tokens(text.lower()):
        result.extend(CONTRACTIONS.get(token,token).split())
    # Only this unambiguous split spelling is canonicalized.
    return ['cannot' if x=='can' and i+1<len(result) and result[i+1]=='not' else x for i,x in enumerate(result) if not(x=='not' and i>0 and result[i-1]=='can')]


def variants(token):
    result=[[token]]
    if re.fullmatch(r'[+-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)',token):
        raw=token.replace(',','');sign=[]
        if raw[0] in '+-':sign=['plus' if raw[0]=='+' else 'minus'];raw=raw[1:]
        if len(raw.split('.')[0])>1 and raw.startswith('0'):return result
        parts=raw.split('.');number=int(parts[0] or '0');spoken=integer_words(number)
        if spoken:
            if len(parts)==2:spoken+=['point']+[SMALL[int(x)] for x in parts[1]]
            result.append(sign+spoken)
            if len(parts)==1 and 1000<=number<=2099 and number%100>=10:
                result.append(sign+integer_words(number//100)+integer_words(number%100))
            result.append([token.replace(',','')])
    elif token=='%':result.append(['percent'])
    return result


def exact_audio_match(adapted,transcript):
    expected=speech_tokens(adapted);actual=speech_tokens(transcript)
    positions={0}
    for token in expected:
        positions={position+len(option) for position in positions for option in variants(token) if actual[position:position+len(option)]==option}
        if not positions:return False
    return len(actual) in positions


def transcribe_local(audio_path,model):
    from faster_whisper import WhisperModel
    engine=WhisperModel(model,device='cpu',compute_type='int8',local_files_only=True)
    segments,info=engine.transcribe(str(audio_path),language='en',beam_size=5,vad_filter=False,condition_on_previous_text=False)
    if info.language!='en':raise ValueError('asr_language_not_english')
    return ' '.join(segment.text.strip() for segment in segments)


def review(source_path,adapted_path,audio_path,output_path,*,model='base.en',transcriber=None):
    quality={'status':'needs_review','source_sha256':sha(source_path),'adapted_sha256':sha(adapted_path),'audio_sha256':sha(audio_path),'source_to_adapted':{'approved':False},'adapted_to_audio':{'approved':False},'limitations':'Automatic ASR can make errors; exact agreement is not proof of flawless audio, correct voice, or faithful punctuation semantics.'}
    try:
        script=prepare_source(source_path)
        adapted=Path(adapted_path).read_text(encoding='utf-8')
        if tokens(script)!=tokens(adapted):raise ValueError('source_words_or_numbers_changed')
        quality['source_to_adapted']={'approved':True,'reviewer':'deterministic-full-text-v1','evidence':'English plain-text source; complete case-sensitive lexical and numeric token equality; only punctuation/layout ignored.'}
        transcript=(transcriber or transcribe_local)(audio_path,model)
        if not isinstance(transcript,str) or not transcript.strip():raise ValueError('asr_empty')
        quality['transcript_sha256']=hashlib.sha256(transcript.encode()).hexdigest()
        if not exact_audio_match(adapted,transcript):raise ValueError('asr_not_exact')
        quality['adapted_to_audio']={'approved':True,'reviewer':'local-faster-whisper-exact-v1','evidence':'Independent local ASR transcript matches every token in order; only fixed contraction/hyphen normalization and bounded explicit numeric readings allowed. No similarity threshold.', 'model':str(model)}
        quality['status']='ready_to_publish'
    except Exception as error:
        allowed={'source_items_required','non_text_content_requires_review','source_body_mismatch','complex_content_requires_review','non_english_requires_review','english_not_confident','source_words_or_numbers_changed','asr_empty','asr_not_exact','asr_language_not_english'}
        quality['reason']=str(error) if str(error) in allowed else 'local_review_unavailable'
    atomic_write(Path(output_path),json.dumps(quality,ensure_ascii=False,indent=2)+'\n')
    return quality


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--adapted',type=Path,required=True)
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',default='base.en')
    args=parser.parse_args()
    result=review(args.source,args.adapted,args.audio,args.output,model=args.model)
    print(json.dumps({'status':result['status'],'reason':result.get('reason')}))


if __name__=='__main__':main()
