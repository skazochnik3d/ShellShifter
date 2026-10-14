import {test} from 'node:test';
import assert from 'node:assert/strict';
import {deflateSync} from 'node:zlib';
import worker, {ReportQuota, sanitizePng, validateReport, issueBody, hmac} from './worker.mjs';

const origin = 'https://shellshifter-reports.example.workers.dev';
const secret = 'a'.repeat(64);
const report = () => ({schema:1, request_id:crypto.randomUUID(), title:'Unfold problem',
  description:'A UV shell stays bent after unfolding.', consent_public:true, screenshot:null,
  environment:{shellshifter:'1.4.237',maya:'2027',maya_api:'20270200',os:'Windows 11',python:'3.13.9',qt:'6.8.3'}});
function crc(bytes) {
  let c = 0xffffffff;
  for (const x of bytes) { c ^= x; for (let i=0;i<8;i++) c = (c&1) ? 0xedb88320^(c>>>1) : c>>>1; }
  return (c^0xffffffff)>>>0;
}
function chunk(kind, data) {
  const body = Buffer.concat([Buffer.from(kind),data]), result = Buffer.alloc(body.length+8);
  result.writeUInt32BE(data.length); body.copy(result,4); result.writeUInt32BE(crc(body),result.length-4);
  return result;
}
function png({width=2,height=2,bomb=false,metadata=false}={}) {
  const header = Buffer.alloc(13); header.writeUInt32BE(width); header.writeUInt32BE(height,4);
  header[8]=8; header[9]=6;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),
    ...(metadata ? [chunk('tEXt',Buffer.from('Comment\0private scene path'))] : []),
    chunk('IDAT',deflateSync(Buffer.alloc(bomb ? 100000 : (width*4+1)*height))),chunk('IEND',Buffer.alloc(0))]);
}
class Storage {
  data = new Map(); tail = Promise.resolve();
  get = async key => structuredClone(this.data.get(key));
  put = async (key,value) => {this.data.set(key,structuredClone(value));};
  transaction(fn) { const job=this.tail.then(()=>fn(this)); this.tail=job.catch(()=>{}); return job; }
}
function environment() {
  const images=new Map(), storage=new Storage();
  const env = {REPORTS_ENABLED:'true',PUBLIC_ORIGIN:origin,GITHUB_TOKEN:'test-only-credential',LINK_SECRET:secret,
    INTAKE_LIMIT:{limit:async()=>({success:true})},IMAGE_LIMIT:{limit:async()=>({success:true})},
    IMAGES:{put:async(k,v)=>images.set(k,v),delete:async k=>images.delete(k),
      get:async k=>images.has(k) ? {body:images.get(k)} : null},
    images,storage};
  const actor=new ReportQuota({storage},env);
  env.QUOTA={idFromName:x=>x,get:()=>({fetch:r=>actor.fetch(r)})};
  return env;
}
function request(value, ip='198.51.100.1', extra={}) {
  return new Request(origin+'/v1/reports',{method:'POST',headers:{'Content-Type':'application/json',
    'CF-Connecting-IP':ip,...extra},body:JSON.stringify(value)});
}
async function mockGithub(fn, action) {
  const original=globalThis.fetch; globalThis.fetch=fn;
  try { return await action(); } finally { globalThis.fetch=original; }
}
const created = () => new Response(JSON.stringify({html_url:'https://github.com/skazochnik3d/ShellShifter/issues/42'}),{status:201});

test('requires explicit publication consent, known versions and bounded fields',()=>{
  assert.equal(validateReport(report()).environment.maya,'2027');
  for (const mutation of [r=>r.consent_public=false,r=>r.title='',r=>r.description='short',
    r=>r.environment.maya='2020',r=>r.environment.os='x'.repeat(101),r=>r.request_id='../bad']) {
    const r=report();mutation(r);assert.throws(()=>validateReport(r));
  }
});
test('accepts PNG pixels and strips text metadata',async()=>{
  const original=png({metadata:true}), safe=await sanitizePng(original.toString('base64'));
  assert.ok(!Buffer.from(safe).includes(Buffer.from('private scene path')));
  assert.ok(safe.length<original.length);
});
test('rejects scripts, CRC tampering, trailing bytes and oversized dimensions',async()=>{
  const corrupt=png();corrupt[35]^=1;
  for (const value of [Buffer.from('<svg onload="alert(1)"/>'),corrupt,
    Buffer.concat([png(),Buffer.from('EXE')]),png({width:2000})]) {
    await assert.rejects(()=>sanitizePng(value.toString('base64')));
  }
});
test('rejects decompression bombs without allocating unbounded output',async()=>{
  await assert.rejects(()=>sanitizePng(png({bomb:true}).toString('base64')));
});
test('renders report text literally and neutralizes mentions',()=>{
  const r=report();r.description='<script>evil</script> ``` @victim https://evil.example';
  const body=issueBody(r,null);assert.ok(body.includes('````text'));assert.ok(!body.includes('@victim'));
  assert.ok(!body.includes('test-only-credential'));
});
test('disabled service performs no GitHub call',async()=>{
  await mockGithub(()=>assert.fail('unexpected network'),async()=>{
    const res=await worker.fetch(request(report()),{REPORTS_ENABLED:'false'});assert.equal(res.status,503);
  });
});
test('health exposes only service state, never credentials',async()=>{
  const res=await worker.fetch(new Request(origin+'/health'),environment());
  assert.deepEqual(await res.json(),{schema:1,enabled:true});
});
test('sends screenshot, fixed repository and server-only credential; repeated request is cached',async()=>{
  const env=environment(), r=report();r.screenshot=png({metadata:true}).toString('base64');
  let calls=0, body;
  await mockGithub(async(url,options)=>{
    calls++;assert.equal(url,'https://api.github.com/repos/skazochnik3d/ShellShifter/issues');
    assert.equal(options.headers.Authorization,'Bearer test-only-credential');body=JSON.parse(options.body);
    return created();
  },async()=>{
    for (let n=0;n<2;n++) {const res=await worker.fetch(request(r),env);assert.equal(res.status,201);}
  });
  assert.equal(calls,1);assert.equal(env.images.size,1);
  const link=/!\[Screenshot\]\(([^)]+)\)/.exec(body.body)[1];
  const image=await worker.fetch(new Request(link,{headers:{'CF-Connecting-IP':'198.51.100.2'}}),env);
  assert.equal(image.status,200);assert.equal(image.headers.get('Content-Type'),'image/png');
  assert.equal(image.headers.get('X-Content-Type-Options'),'nosniff');
  const bad=new URL(link);bad.searchParams.set('key','a'.repeat(64));
  assert.equal((await worker.fetch(new Request(bad,{headers:{'CF-Connecting-IP':'198.51.100.2'}}),env)).status,404);
});
test('same ID with different content cannot create a second issue',async()=>{
  const env=environment(),r=report();
  await mockGithub(created,async()=>{
    assert.equal((await worker.fetch(request(r),env)).status,201);r.title='Changed title';
    assert.equal((await worker.fetch(request(r),env)).status,409);
  });
});
test('three reports per IP; twenty globally despite IP rotation',async()=>{
  const env=environment();let calls=0;
  await mockGithub(()=>{calls++;return created();},async()=>{
    for(let n=0;n<3;n++) assert.equal((await worker.fetch(request(report()),env)).status,201);
    assert.equal((await worker.fetch(request(report()),env)).status,429);
    for(let n=0;n<17;n++) assert.equal((await worker.fetch(request(report(),'198.51.100.'+(n+10)),env)).status,201);
    assert.equal((await worker.fetch(request(report(),'198.51.100.250'),env)).status,429);
  });assert.equal(calls,20);
});
test('concurrent retry reserves only one GitHub operation',async()=>{
  const env=environment(),r=report();let calls=0;
  await mockGithub(async()=>{calls++;await new Promise(resolve=>setTimeout(resolve,20));return created();},async()=>{
    const responses=await Promise.all([worker.fetch(request(r),env),worker.fetch(request(r),env)]);
    assert.deepEqual(responses.map(x=>x.status).sort(),[201,409]);
  });assert.equal(calls,1);
});
test('ambiguous GitHub timeout does not duplicate an issue on retry',async()=>{
  const env=environment(),r=report();r.screenshot=png().toString('base64');let calls=0;
  await mockGithub(()=>{calls++;throw new Error('SECRET MUST NOT APPEAR');},async()=>{
    for(let n=0;n<2;n++) {
      const res=await worker.fetch(request(r),env);assert.equal(res.status,503);
      assert.deepEqual(await res.json(),{error:'unconfirmed'});
    }
  });assert.equal(calls,1);assert.equal(env.images.size,1);
});
test('definite GitHub rejection cleans uploaded screenshot and hides error body',async()=>{
  const env=environment(),r=report();r.screenshot=png().toString('base64');
  await mockGithub(()=>new Response('sensitive upstream diagnostic',{status:403}),async()=>{
    const res=await worker.fetch(request(r),env);assert.equal(res.status,503);
    assert.deepEqual(await res.json(),{error:'upstream'});
  });assert.equal(env.images.size,0);
});
test('definite rejection can recover but every retry consumes a hard quota attempt',async()=>{
  const env=environment(),r=report();let calls=0;
  await mockGithub(()=>{calls++;return new Response('rejected',{status:403});},async()=>{
    for(let n=0;n<3;n++) assert.equal((await worker.fetch(request(r),env)).status,503);
    assert.equal((await worker.fetch(request(r),env)).status,429);
  });assert.equal(calls,3);
});
test('GitHub 5xx is ambiguous and cannot repeat the POST',async()=>{
  const env=environment(),r=report();r.screenshot=png().toString('base64');let calls=0;
  await mockGithub(()=>{calls++;return new Response('uncertain',{status:502});},async()=>{
    for(let n=0;n<2;n++) assert.deepEqual(await (await worker.fetch(request(r),env)).json(),{error:'unconfirmed'});
  });assert.equal(calls,1);assert.equal(env.images.size,1);
});
test('rejects foreign Origin, forged forwarded IP and malformed inputs',async()=>{
  const env=environment();
  await mockGithub(()=>assert.fail('unexpected network'),async()=>{
    assert.equal((await worker.fetch(request(report(),'198.51.100.1',{Origin:'https://evil.example'}),env)).status,400);
    const noEdge=request(report());noEdge.headers.delete('CF-Connecting-IP');noEdge.headers.set('X-Forwarded-For','1.2.3.4');
    assert.equal((await worker.fetch(noEdge,env)).status,400);
    assert.equal((await worker.fetch(new Request(origin+'/v1/reports',{method:'POST',
      headers:{'Content-Type':'application/json','CF-Connecting-IP':'198.51.100.1'},body:'{'}),env)).status,400);
  });
});
test('limits request bodies even without Content-Length',async()=>{
  const env=environment();
  const r=new Request(origin+'/v1/reports',{method:'POST',headers:{'Content-Type':'application/json',
    'CF-Connecting-IP':'198.51.100.1'},body:'x'.repeat(3*1024*1024+1)});
  assert.equal((await worker.fetch(r,env)).status,413);
});
test('intake limiter rejects before decoding/uploading a body',async()=>{
  const env=environment();env.INTAKE_LIMIT.limit=async()=>({success:false});
  assert.equal((await worker.fetch(request(report()),env)).status,429);assert.equal(env.images.size,0);
});
test('HMAC capability binds the exact image path',async()=>{
  assert.notEqual(await hmac(secret,'/images/one.png'),await hmac(secret,'/images/two.png'));
});
