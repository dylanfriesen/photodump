"""Headless-browser checks for the web UI, against tools/ui_fixture.py data.

Run through tools/ui_check.sh, which starts an isolated instance and Chromium.
Ported from the 53 checks Codex ran during the September v2 design port (they
lived only in /tmp), plus the sweep page, reel options and carousel export.

Drives Chromium over the DevTools protocol with the image's own `websockets`
sync client, because kanto has no playwright/puppeteer and headless Chromium's
--virtual-time-budget never settles against the app's polling timers.
"""
import base64
import json
import sys
import time
import urllib.request
import warnings
from pathlib import Path

from websockets.sync.client import connect

DEBUG = sys.argv[1]           # e.g. http://127.0.0.1:9231
APP = sys.argv[2]             # e.g. http://127.0.0.1:8099
SHOTS = Path(sys.argv[3]) if len(sys.argv) > 3 else None

req = urllib.request.Request(f"{DEBUG}/json/new?about:blank", method="PUT")
target = json.load(urllib.request.urlopen(req))
# One flat script, so the connection is held open for its whole life rather
# than through a `with` block; websockets 17 warns about that on every send.
warnings.filterwarnings("ignore", message="connect\\(\\) must be used as a context manager")
ws = connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=30)
seq = 0
errors: list = []
passed = failed = 0


def call(method, params=None):
    global seq
    seq += 1
    me = seq
    ws.send(json.dumps({"id": me, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv(timeout=60))
        if r.get("method") == "Runtime.exceptionThrown":
            d = r["params"]["exceptionDetails"]
            errors.append((d.get("exception") or {}).get("description") or d.get("text"))
        if r.get("id") == me:
            if "error" in r:
                raise RuntimeError(r["error"])
            return r.get("result", {})


def evaluate(js):
    r = call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in r:
        raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"])
    return r.get("result", {}).get("value")


def check(name, js):
    global passed, failed
    try:
        result = evaluate(js)
    except Exception as e:          # a thrown check is a failed check, not a crash
        result = f"threw: {str(e)[:200]}"
    ok = result is True
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"  -> {result!r}"), flush=True)


def viewport(width, height=1000):
    call("Emulation.setDeviceMetricsOverride",
         {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 901})


def shot(name):
    if SHOTS:
        data = call("Page.captureScreenshot", {"format": "png"})["data"]
        (SHOTS / f"{name}.png").write_bytes(base64.b64decode(data))


def goto(path, settle=3.0):
    call("Page.navigate", {"url": f"{APP}{path}"})
    time.sleep(settle)


def wait_for(js, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if evaluate(js) is True:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


call("Runtime.enable")
call("Page.enable")
viewport(1440)
goto("/")

print("studio")
check("boot", "!!CONFIG && IMAGES.length===5 && CR_REF_ROWS.length===3")
check("favicon is the lime mark", "document.querySelector('link[rel=icon]').href.endsWith('/favicon.svg')")
check("header links to sweeps", "document.querySelector('header a.head-link').getAttribute('href')==='/sweeps.html'")
check("sampler in create", "$('sampler').closest('.task').dataset.task==='create'")
check("no-reference plan", "$('cr-plan').textContent.includes('text to image') && $('cr-ip-row').hidden")
check("single reference composition", "(()=>{document.querySelector('[data-pick=\"1\"]').click();return !$('cr-denoise-wrap').hidden && !$('cr-clean-wrap').hidden && createValues().workflow==='img2img'})()")
check("two-pass controls", "(()=>{$('cr-stylematch').click();$('cr-p2-add').value='warm colours';return !$('cr-p2-row').hidden && createValues().second_pass && createValues().second_pass_prompt_add==='warm colours'})()")
check("multi-reference payload matches plan", "(()=>{document.querySelector('[data-pick=\"2\"]').click();return $('cr-keep').disabled && $('cr-clean-wrap').hidden && $('cr-hires-wrap').hidden && createValues().workflow==='ipadapter_multi' && !('second_pass' in createValues())})()")
check("capability blocks generation", "(()=>{NODE_CAPS.has_ipadapter=false;syncCreate();return CREATE_BLOCKED && $('btn-create').disabled && !$('cr-warn').hidden})()")
check("capability recovers", "(()=>{NODE_CAPS.has_ipadapter=true;syncCreate();return !CREATE_BLOCKED && !$('btn-create').disabled})()")
check("count stepper", "(()=>{$('cr-count').parentElement.querySelector('[data-step=\"1\"]').click();return $('cr-qty').textContent.includes('×'+$('cr-count').value)})()")
check("shared sampler retains values", "(()=>{$('steps').value=37;switchTask('fuse');const ok=$('sampler').closest('.task').dataset.task==='fuse' && $('steps').value==='37';switchTask('create');return ok && $('steps').value==='37'})()")
check("style-match queue grouped", "[...document.querySelectorAll('.smgroup')].some(g=>g.querySelectorAll('.job').length===2)")
check("create queue label", "[...document.querySelectorAll('.jmeta')].some(e=>e.textContent.includes('create'))")
check("lightbox count reads 'of'", "(()=>{openLightbox(IMAGES[0]);return $('lb-pos').textContent==='1 of 5'})()")
check("lightbox paging", "(()=>{stepLightbox(1);return $('lb-pos').textContent==='2 of 5'})()")
check("input arrows stay in input", "(()=>{const key=CURRENT.key;$('an-prompt').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));return CURRENT.key===key})()")
check("create settings reuse", "(()=>{openLightbox(IMAGES.find(i=>i.id===1));$('lb-reuse').click();return $('cr-prompt').value==='A quiet illustrated portrait' && $('steps').value==='40' && $('cr-denoise').value==='0.45' && CR_REFS.join()==='1' && $('cr-stylematch').checked && CR_CLEAN[1].length===1})()")
check("preview plan", "(()=>{$('btn-cr-preview').click();return !$('cr-preview-box').hidden && $('cr-preview').textContent.includes('two passes each')})()")
check("negative added once on reuse", "$('cr-negative').value==='text, watermark'")
check("gallery media retained", "(async()=>{await refreshGallery();const first=$('grid').firstElementChild;await refreshGallery();return first===$('grid').firstElementChild})()")
check("animation and delivery exclusive", "(()=>{openLightbox(IMAGES[0]);$('lb-deliver').click();$('lb-animate').click();return $('deliver-box').hidden && !$('animate-box').hidden})()")
check("model nested text selects", "(()=>{$('an-backend').querySelector('[data-value=ltx] b').click();return $('an-backend').value==='ltx' && $('an-go').textContent.includes('LTX')})()")
check("model keyboard selects", "(()=>{$('an-backend').querySelector('[data-value=ltx]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));return $('an-backend').value==='wan'})()")
check("six canvases", "$('an-size').querySelectorAll('[data-value]').length===6")
check("animate real API payload", "(async()=>{$('an-backend').value='ltx';$('an-prompt').value='Slow camera drift';await $('an-go').onclick({currentTarget:$('an-go')});const r=await api('/api/jobs');const p=JSON.parse(r.body[0].params);return p.video_backend==='ltx' && p.video_size==='story_540' && p.workflow==='wan_i2v'})()")
check("create real API payload", "(async()=>{closeLb();openLightbox(IMAGES.find(i=>i.id===1));$('lb-reuse').click();await $('btn-create').onclick();const r=await api('/api/jobs');const p=JSON.parse(r.body[0].params);return p.second_pass && p.steps===40 && p.clean_regions.length===1})()")

print("queue")
evaluate('closeLb();switchTab("queue")')
check("schedule popover", "(()=>{const b=document.querySelector('[data-hold]');b.click();return !$('hold-pop').hidden && HOLD_JOB===+b.dataset.hold})()")
check("local schedule reaches API in UTC", "(async()=>{const id=HOLD_JOB;const d=new Date(Date.now()+3600000);await schedule(id,d);const r=await api('/api/jobs');return r.body.find(j=>j.id===id).not_before.slice(0,16)===toUtc(d)})()")
check("hold job", "(async()=>{const id=HOLD_JOB;await $('hold-pop').onclick({target:document.querySelector('[data-hold-pause]')});const r=await api('/api/jobs');return r.body.find(j=>j.id===id).status==='paused'})()")
check("unknown progress", "(()=>{renderProgress({job_id:999,percent:null,elapsed:12,stage:'loading',estimated:true});return !$('node-prog').hidden && $('prog-pct').textContent==='—' && !document.querySelector('.node-rule').hasAttribute('aria-valuenow')})()")
check("measured progress", "(()=>{renderProgress({job_id:999,percent:42,elapsed:12,stage:'sampling',estimated:false});return document.querySelector('.node-rule').classList.contains('measured') && $('node-fill').style.width==='42%'})()")
check("estimated progress", "(()=>{renderProgress({job_id:999,percent:43,elapsed:13,stage:'sampling',estimated:true});return document.querySelector('.node-rule').classList.contains('estimated') && $('prog-stage').textContent.includes('estimate')})()")
evaluate("renderProgress(null);closeHold()")

print("create clean-up pad, swipe, keyboard")
check("clean preview loads", "(async()=>{closeLb();switchScreen('studio');switchTask('create');CR_REFS=[1];$('cr-mode').value='img2img';syncCreate();CR_CLEAN[1]=[[.1,.8,.6,.1]];paintCleanBoxes();await $('btn-clean-preview').onclick({currentTarget:$('btn-clean-preview')});return !!CLEAN_PREVIEW && $('cr-clean-title').textContent==='filled result'})()")
check("back to boxes", "(async()=>{await $('btn-clean-preview').onclick({currentTarget:$('btn-clean-preview')});return !CLEAN_PREVIEW && !$('btn-clean-clear').hidden})()")
check("failed preview recovers", "(async()=>{const f=fetch;window.fetch=(url,opts)=>String(url).endsWith('clean-preview')?Promise.reject(new Error('offline')):f(url,opts);await $('btn-clean-preview').onclick({currentTarget:$('btn-clean-preview')});window.fetch=f;return !CLEAN_PREVIEW && !$('btn-clean-preview').disabled && $('toasts').textContent.includes('Could not load')})()")
check("swipe pages", "(()=>{openLightbox(IMAGES[0]);const img=$('lb-img');img.dispatchEvent(new PointerEvent('pointerdown',{pointerId:7,pointerType:'touch',isPrimary:true,clientX:280,clientY:200,bubbles:true}));img.dispatchEvent(new PointerEvent('pointerup',{pointerId:7,pointerType:'touch',isPrimary:true,clientX:80,clientY:210,bubbles:true}));return CURRENT.id===IMAGES[1].id})()")
check("vertical swipe does not page", "(()=>{const key=CURRENT.key;const img=$('lb-img');img.dispatchEvent(new PointerEvent('pointerdown',{pointerId:7,pointerType:'touch',isPrimary:true,clientX:280,clientY:200,bubbles:true}));img.dispatchEvent(new PointerEvent('pointerup',{pointerId:7,pointerType:'touch',isPrimary:true,clientX:200,clientY:400,bubbles:true}));return CURRENT.key===key})()")
check("reference keyboard control", "(()=>{closeLb();const before=CR_REFS.length;const fig=document.querySelector('[data-pick=\"2\"]');fig.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return CR_REFS.length===before+1 && fig.getAttribute('aria-pressed')==='true'})()")

print("reel and carousel")
evaluate("closeLb();$('toasts').replaceChildren();switchScreen('studio');switchTask('reel');SELECTED=['image:3','image:1','image:5'];syncSelection();")
check("cover options follow the cut order", "$('re-cover').options.length===4 && $('re-cover').options[3].textContent==='shot 3'")
check("hook reveals its duration", "(()=>{$('re-hook').value='what if';$('re-hook').dispatchEvent(new Event('input'));return !$('re-hook-row').hidden})()")
check("track offset hidden without music", "$('re-audio-start-row').hidden")
check("cover pick survives a shorter selection", "(()=>{$('re-cover').value='1';SELECTED=['image:3','image:1'];syncSelection();return $('re-cover').value==='1'})()")
check("reel real API payload", "(async()=>{$('re-cover').value='0';await buildReel($('btn-reel'));const r=await api('/api/jobs');const p=JSON.parse(r.body[0].params);return p.workflow==='reel' && p.hook==='what if' && p.cover_shot===0 && p.hook_seconds===2.5})()")
check("carousel real API payload", "(async()=>{switchTask('reel');SELECTED=['image:5','image:3'];syncSelection();$('re-carousel-target').value='square';await $('btn-carousel').onclick({currentTarget:$('btn-carousel')});const r=await api('/api/jobs');const p=JSON.parse(r.body[0].params);return p.workflow==='carousel' && p.target==='square' && p.shots[0].id===5})()")
check("carousel labelled in the queue", "(async()=>{await refreshJobs();return [...document.querySelectorAll('.jmeta')].some(e=>e.textContent.includes('carousel'))})()")
print("  (waiting for the reel and carousel to build locally)")
check("reel and carousel build with the node offline", "(async()=>{for(let i=0;i<90;i++){const r=await api('/api/jobs');const mine=r.body.filter(j=>['reel','carousel'].includes(JSON.parse(j.params).workflow));if(mine.length===2&&mine.every(j=>j.status==='done'))return true;if(mine.some(j=>j.status==='failed'))return 'failed: '+mine.map(j=>j.error).join(' | ');await new Promise(z=>setTimeout(z,1000));}return 'timed out'})()")
check("reel landed with its cover", "(async()=>{const r=await api('/api/images');const n=r.body.map(i=>i.filename);return n.some(f=>/_reel\\.mp4$/.test(f)) && n.some(f=>/_reel_cover\\.jpg$/.test(f)) && n.filter(f=>/_slide\\d\\d_ig\\.jpg$/.test(f)).length===2})()")

print("layout")
evaluate("SELECTED=[];syncSelection();switchTask('create');switchTab('gallery')")
for width in [390, 768, 1440]:
    viewport(width)
    for screen in ["studio", "queue", "gallery", "refs"]:
        evaluate(f"closeLb();switchScreen({json.dumps(screen)})")
        time.sleep(.3)
        check(f"no horizontal overflow {width} {screen}", "document.documentElement.scrollWidth<=window.innerWidth")
        if screen in ("studio", "queue"):
            shot(f"{width}-{screen}")
    evaluate("switchScreen('studio');switchTask('reel')")
    time.sleep(.3)
    check(f"reel panel fits {width}", "document.documentElement.scrollWidth<=window.innerWidth")
    shot(f"{width}-reel")
    evaluate("switchTask('create');openLightbox(IMAGES[0]);$('lb-animate').click()")
    check(f"lightbox fits {width}", "document.documentElement.scrollWidth<=window.innerWidth && document.querySelector('.lb-frame').getBoundingClientRect().right<=innerWidth")
    shot(f"{width}-detail")
check("network failures recoverable", "(async()=>{const f=fetch;window.fetch=()=>Promise.reject(new Error('offline'));const r=await api('/api/status');window.fetch=f;return !r.ok && r.status===0})()")

print("sweeps page")
viewport(1440)
goto("/sweeps.html")
check("sweep loads with both cells", "document.querySelectorAll('.cell').length===2")
check("reference shown", "!!document.querySelector('.refcard img[src^=\"/refs/\"]')")
check("finished cell scored, waiting cell not", "(()=>{const rows=[...document.querySelectorAll('table.rank tbody tr')].map(r=>r.textContent);return rows.some(t=>t.includes('plain')&&/\\d\\.\\d{3}/.test(t)) && rows.some(t=>t.includes('pixel')&&t.includes('—'))})()")
check("pass 1 and pass 2 side by side", "document.querySelectorAll('.cell')[0].querySelectorAll('figure img').length===2")
check("waiting slot says why", "document.querySelectorAll('.cell')[1].textContent.includes('waits for the desktop')")
check("progress counts real renders", "document.querySelector('.summary p').textContent.includes('2 of 4')")
check("zoom compares with the reference", "(()=>{document.querySelector('.cell figure img').click();return !$('zoom').hidden && $('zoom-ref').src.includes('/refs/')})()")
check("escape closes zoom", "(()=>{document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));return $('zoom').hidden})()")
shot("1440-sweeps")
viewport(390)
time.sleep(.5)
check("sweeps fit 390", "document.documentElement.scrollWidth<=window.innerWidth")
shot("390-sweeps")

print(f"\nbrowser errors: {errors or 'none'}")
print(f"{passed} passed, {failed} failed")
ws.close()
sys.exit(1 if failed or errors else 0)
