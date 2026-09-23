import {readFile,writeFile,mkdir,rename,rm} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';

const hash = data => createHash('sha256').update(data).digest('hex');
const words = text => text.trim().split(/\s+/).filter(Boolean).length;
async function readJson(file) { try{return JSON.parse(await readFile(file,'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;} }
async function atomicJson(file,value){await writeFile(`${file}.tmp`,JSON.stringify(value,null,2));await rename(`${file}.tmp`,file);}

// Every character belongs to exactly one segment. Paragraph boundaries, never a
// character slice, decide splits. maxChars is an experiment guard, not a Pi limit.
export function splitArticle(text,{maxChars=6000,targetWords=400}={}) {
  if(typeof text!=='string'||!text.trim())throw new Error('Empty article');
  if(!Number.isInteger(maxChars)||maxChars<1||!Number.isInteger(targetWords)||targetWords<1)throw new Error('Invalid segment limits');
  const blocks=[];let start=0;
  for(const match of text.matchAll(/\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*/g)) {const end=match.index+match[0].length;blocks.push({start,end});start=end;}
  if(start<text.length)blocks.push({start,end:text.length});
  const groups=[];let current=null;
  for(const block of blocks){
    const blockText=text.slice(block.start,block.end);
    if(blockText.length>maxChars)throw new Error(`PI_PARAGRAPH_TOO_LONG: needs_review at characters ${block.start}-${block.end}; no text was truncated`);
    if(current && (block.end-current.start>maxChars || words(text.slice(current.start,block.end))>targetWords)) {groups.push(current);current=null;}
    current=current?{start:current.start,end:block.end}:{...block};
  }
  if(current)groups.push(current);
  return groups.map((range,index)=>({index,...range,text:text.slice(range.start,range.end),sha256:hash(text.slice(range.start,range.end))}));
}

function run(command,args,options={}){const r=spawnSync(command,args,{encoding:'utf8',windowsHide:true,maxBuffer:2*1024*1024,...options});if(r.error||r.status!==0)throw new Error(`${path.basename(command)} failed: ${r.error?.message||r.stderr}`);return r.stdout;}
export async function joinAudio(results,outputDir,{ffmpeg=process.env.FFMPEG||'ffmpeg',ffprobe=process.env.FFPROBE||'ffprobe'}={}){
  const intermediates=[];
  try{
    for(let i=0;i<results.length;i++){
      const name=`join-${String(i).padStart(4,'0')}.wav`;intermediates.push(path.join(outputDir,name));
      run(ffmpeg,['-y','-v','error','-xerror','-i',results[i].audioPath,'-ac','1','-ar','44100','-c:a','pcm_s16le',intermediates.at(-1)]);
    }
    const listPath=path.join(outputDir,'concat-inputs.txt');intermediates.push(listPath);
    await writeFile(listPath,results.map((_,i)=>`file 'join-${String(i).padStart(4,'0')}.wav'`).join('\n'));
    const partial=path.join(outputDir,'episode.partial.mp3');
    run(ffmpeg,['-y','-v','error','-xerror','-f','concat','-safe','1','-i',listPath,'-c:a','libmp3lame','-b:a','128k',partial]);
    run(ffmpeg,['-v','error','-xerror','-i',partial,'-f','null','-']);
    const durationSeconds=Number(JSON.parse(run(ffprobe,['-v','error','-show_entries','format=duration','-of','json',partial])).format.duration);
    const expected=results.reduce((sum,r)=>sum+r.durationSeconds,0);
    if(!Number.isFinite(durationSeconds)||Math.abs(durationSeconds-expected)>Math.max(2,results.length*.2))throw new Error('Combined duration does not match segment durations');
    const audioPath=path.join(outputDir,'episode.mp3');await rename(partial,audioPath);
    const bytes=await readFile(audioPath);
    return {audioPath,durationSeconds,bytes:bytes.length,audioSha256:hash(bytes),contentType:'audio/mpeg',decoded:true};
  }finally{for(const file of intermediates)await rm(file,{force:true});}
}

export async function generateArticle(article,outputDir,options={}){
  const segments=splitArticle(article.text,options);
  await mkdir(outputDir,{recursive:true});
  const sourceHash=hash(JSON.stringify(article));
  const manifestPath=path.join(outputDir,'segments.json');
  let manifest=await readJson(manifestPath);
  if(options.resume&&!manifest)throw new Error('PI_RESUME_MANIFEST_MISSING: no new article was submitted');
  if(manifest&&(manifest.sourceHash!==sourceHash||JSON.stringify(manifest.ranges)!==JSON.stringify(segments.map(({index,start,end,sha256})=>({index,start,end,sha256})))))throw new Error('Source or segment plan changed; refuse to reuse this output directory');
  if(!manifest){manifest={version:1,sourceHash,source_sha256:article.source_sha256||options.inputSha256||sourceHash,mode:article.mode||'spoken_copyedit',scriptSha256:hash(article.text),status:'capturing',qualityStatus:'needs_review',ranges:segments.map(({index,start,end,sha256})=>({index,start,end,sha256}))};await atomicJson(manifestPath,manifest);}
  const cleanupCompleted=async()=>{
    const sync=options.sync || (process.env.PODCAST_DURABLE_SYNC==='true'?(await import('./pi_capture.mjs')).durableSync:null);
    if(sync)await sync();
    for(const segment of segments)await rm(path.join(outputDir,`segment-${String(segment.index+1).padStart(3,'0')}`,'episode.mp3'),{force:true});
    if(sync)await sync();
  };
  const previousResult=await readJson(path.join(outputDir,'result.json'));
  if(previousResult){
    let completeBytes;try{completeBytes=await readFile(path.join(outputDir,'episode.mp3'));}catch(e){if(e.code!=='ENOENT')throw e;}
    if(completeBytes){if(previousResult.sourceSha256!==hash(article.text)||previousResult.audioSha256!==hash(completeBytes))throw new Error('Completed article integrity check failed');if(article.mode==='punctuation_only'){if(previousResult.textValidation?.passed!==true)throw new Error('PI_TEXT_CHANGED: completed article lacks validation');(await import('./pi_capture.mjs')).assertPunctuationOnly(article,previousResult.adaptedText);}await cleanupCompleted();return {...previousResult,audioPath:path.join(outputDir,'episode.mp3')};}
  }
  const capture=options.capture || (await import('./pi_capture.mjs')).generatePiAudio;
  const results=[];
  for(const segment of segments){
    const dir=path.join(outputDir,`segment-${String(segment.index+1).padStart(3,'0')}`);await mkdir(dir,{recursive:true});
    const input={title:segment.index===0?article.title||'':'',includeTitle:segment.index===0&&article.includeTitle!==false,url:article.url||'',text:segment.text,source_sha256:segment.sha256};
    if(article.mode!==undefined)input.mode=article.mode;
    const sourceFile=path.join(dir,'source.json');
    const previousInput=await readJson(sourceFile);
    if(previousInput&&JSON.stringify(previousInput)!==JSON.stringify(input))throw new Error('Segment source changed');
    if(!previousInput)await atomicJson(sourceFile,input);
    let result=await readJson(path.join(dir,'result.json'));
    if(result){
      let actual;
      try{actual=await readFile(path.join(dir,'episode.mp3'));}catch(error){if(error.code!=='ENOENT')throw error;}
      if(result.sourceSha256!==segment.sha256||(actual&&hash(actual)!==result.audioSha256))throw new Error('Existing segment result failed integrity verification; needs_review');
      if(actual)result={...result,audioPath:path.join(dir,'episode.mp3')};
      else{
        const saved=await readJson(path.join(dir,'checkpoint.json'));
        if(!saved?.messageSid||!saved?.rewrite)throw new Error('PI_MEDIA_MISSING: result exists without confirmed checkpoint; refusing to resubmit');
        result=null;
      }
    }
    if(!result){
      const checkpoint=await readJson(path.join(dir,'checkpoint.json'));
      // Resume the article: only untouched segments may be first-submitted.
      // A checkpoint forces capture's fetch-only path, including uncertain-stop.
      result=await capture(input,dir,{...options,resume:!!checkpoint});
    }
    if(article.mode==='punctuation_only'&&result.textValidation?.passed!==true)throw new Error('PI_TEXT_CHANGED: segment lacks successful exact-word validation');
    if(article.mode==='punctuation_only')(await import('./pi_capture.mjs')).assertPunctuationOnly(input,result.adaptedText||result.rewrite);
    results.push(result);
    manifest.completedSegments=results.length;await atomicJson(manifestPath,manifest);
    options.onProgress?.({completed:results.length,total:segments.length,durationSeconds:result.durationSeconds});
  }
  const adaptedText=results.map(r=>r.adaptedText||r.rewrite).join('\n\n');
  const completeTextValidation=article.mode==='punctuation_only'?(await import('./pi_capture.mjs')).assertPunctuationOnly(article,adaptedText):null;
  const audio=await (options.join||joinAudio)(results,outputDir,options);
  const result={status:'complete',...audio,title:article.title||'',sourceUrl:article.url||'',source_sha256:manifest.source_sha256,sourceSha256:hash(article.text),mode:article.mode||'spoken_copyedit',promptVersions:[...new Set(results.map(r=>r.promptVersion||'legacy-full-adaptation-v1'))],textValidation:completeTextValidation?{...completeTextValidation,segments:results.map(r=>r.textValidation)}:null,adaptedText,rewrite:adaptedText,segments:segments.length,textExtractionVersion:Math.min(...results.map(r=>r.textExtractionVersion||1)),qualityStatus:'needs_review',qualityNote:'Segment mapping and audio decoding checked. Full source-to-rewrite fidelity, images, voice consistency, and whole-episode listening still require review. No publishing approval.'};
  await atomicJson(path.join(outputDir,'result.json'),result);
  await writeFile(path.join(outputDir,'adapted-en.md'),adaptedText);
  manifest.status='captured';await atomicJson(manifestPath,manifest);
  // Exact generated segment paths only. Keep reply checkpoints and source text
  // so missing final media can be reconstructed without repeating submissions.
  await cleanupCompleted();
  return result;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
  try{const raw=await readFile(process.argv[2]);const article=JSON.parse(raw.toString('utf8'));const result=await generateArticle(article,path.resolve(process.argv[3]||'pi-article-output'),{inputSha256:hash(raw),resume:process.argv.includes('--resume'),onProgress:p=>console.log(JSON.stringify({status:'segment_complete',...p}))});console.log(JSON.stringify({status:result.status,audioPath:result.audioPath,durationSeconds:result.durationSeconds,qualityStatus:result.qualityStatus}));}
  catch(error){console.error(JSON.stringify({status:'needs_review',error:error.message}));process.exitCode=1;}
}
