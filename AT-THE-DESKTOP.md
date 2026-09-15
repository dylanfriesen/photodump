# Do these at the desktop

**2026-09-15: most of this no longer needs you at the desktop.**

- **The desktop wakes itself at 03:00** once the night-drain task is installed
  (`desktop/`). It asks kanto whether anything is queued, stays awake while the
  queue drains, and suspends again. It never suspends a PC that was already
  awake, or one someone touched during the run. kanto installs it over SSH by
  itself the next time the desktop is on: a `--auto` cron entry retries every
  15 minutes and removes itself once the install confirms. Log:
  `~/.photodump-night-drain-install.log` on kanto, `C:\ComfyUI\night-drain.log`
  on the desktop. Undo: `C:\ComfyUI\uninstall-night-drain.ps1`.
  **Unproven until the first real night.** Whether the board honours the wake
  timer depends on its sleep state. The installer prints `powercfg /a`; S3 is
  good, and Modern Standby may ignore the timer.
- **The image experiments are queued**, as sweep `upscale-hair-lettering-1`
  (7 cells x 2 seeds, 28 renders). They render on the next wake. Results are at
  `https://kanto.tail4f3755.ts.net:8452/sweeps.html#upscale-hair-lettering-1`,
  and the whole grid arrives as one email.

**Status correction, 2026-09-13:** the LTX export below is **done** —
`app/workflows/ltx_i2v.json` exists, is wired into `VIDEO_BACKENDS`, and job 49
rendered through it (`data/out/49_0_0.mp4`: 544x960 H.264 + AAC, 89 frames,
3.7s, verified with ffprobe). The section is kept for the record.

**What is actually unproven on video:** the two-pass `story_hd` (1088x1920)
path, nodes 24-30. Its only "test" was job 68, which was queued with
`video_backend: ltx` but **no `workflow`** — so `build()` resolved its lone
reference to img2img and rendered a 680x856 *still* in 11 seconds. The LTX HD
graph has never been submitted. `/api/generate` now refuses video settings
without `workflow: wan_i2v`, so that mistake cannot recur silently. Video is
parked at Dylan's request.

**Image experiments waiting for a GPU session:** see RECIPES.md, *Built
2026-09-13 for the open items*. None needs the GUI — they queue from the app.

---

## (done) Export your LTX workflow so photodump can drive it

Right now photodump can *watch* an LTX render but not *start* one. Codex wrote
progress handling for `LTXVLatentUpsampler`, `LTXVSpatioTemporalTiledVAEDecode`
and `LTXVAudioVAEDecode` — so the app understands your LTX graph, it just has no
copy of it to submit.

Give it one and the Animate task can drive LTX directly.

**In ComfyUI, with your working LTX workflow open:**

- Newer builds: **Workflow → Export (API Format)**
- Older builds: gear icon → enable **Dev mode Options** → a
  **Save (API Format)** button appears in the sidebar

> It must be **API format**. The normal "Save" export describes the canvas —
> node positions, links, UI state — and cannot be submitted to the API. The API
> format is a flat map of node id to `class_type` plus inputs. If the file has
> `"nodes"` and `"links"` arrays at the top level, it is the wrong one; the
> right one starts straight into numbered keys.

Then get the JSON to kanto, whichever is easiest:

- paste it into the next session with me, or
- save it somewhere and paste the contents, or
- drop it on the SMB share this machine already exposes

I will save it as `app/workflows/ltx_i2v.json`, add the matching entry to
`VIDEO_BACKENDS` in `app/comfy.py`, and set `VIDEO_BACKEND=ltx` in `.env`.
Preflight will then validate it against the node and name anything missing.

**Do not let me write this graph from documentation.** I would be guessing at
node names and socket indices, and your exported file is the ground truth for
the exact ComfyUI and LTX build you have installed.

While you are there, note the model filenames so `.env` can match them:

```
LTX_UNET=   # in models/unet/        e.g. LTX25-distilled-DiT-Q4_K_M.gguf
LTX_CLIP=   # in models/text_encoders/  the Gemma-4 GGUF
LTX_VAE=    # in models/vae/         ltx-2.5-video-vae-bf16.safetensors
```

---

---

## Already handled

**ComfyUI reaches kanto, and starts itself** (2026-09-10). It is launched by
`C:\ComfyUI\start-render-node.ps1`, via a `.cmd` in `shell:startup`, so it
comes up at logon without you. The script binds the **tailnet IP**
(`100.109.223.93:8188`) rather than `0.0.0.0`, deliberately — nothing on the
local network can reach it. It waits up to five minutes for that address to
appear before binding and falls back to loopback if it never does, so the
Tailscale-starts-after-ComfyUI race is already handled. Do not "fix" this to
`0.0.0.0`. It also runs an idle watchdog that drops models from VRAM once the
queue has been empty for ten minutes, so a checkpoint is not parked in memory
while you game; the server stays up and the next job pays a reload.

**SSH from kanto works** (2026-09-10). `ssh desktop` gives a PowerShell prompt.
Key-only, `PasswordAuthentication no`, sshd `Automatic` at boot.

The firewall rule `sshd-tailnet` is scoped to **kanto's tailnet IP alone**
(`100.97.103.85`), not the `100.64.0.0/10` range the setup script writes. That
matters: the tailnet ACL is default allow-all and the tailnet carries shared
nodes belonging to other accounts, so the CGNAT range was never a real
boundary. Port 22 is not reachable from the desktop's public IP.

The Windows account is **`drfxb`**, not `dylan`. That mismatch in
`~/.ssh/config` cost a session of debugging, because it fails as
`Permission denied (publickey)` — identical to the `administrators_authorized_keys`
ACL problem that `~/desktop-ssh/README.md` warns about. The ACLs were right the
whole time. Check the username first.

Revoke with `~/desktop-ssh/disable-ssh.ps1`, or
`Stop-Service sshd; Set-Service sshd -StartupType Disabled`.

---

## If ComfyUI will not start

Worth capturing rather than guessing at later:

```powershell
Get-Process | Where-Object {$_.ProcessName -match 'comfy|python'} | Select ProcessName,Id,Path
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8188,8000,8080
```

And from kanto, `curl https://kanto.tail4f3755.ts.net:8452/api/preflight` will
say exactly which node classes or model files each workflow is missing, once
ComfyUI is reachable.

Reminder for anything you download while you are there: **fp8 (e4m3fn) is
broken on RDNA4 under Windows.** Use fp16 or GGUF. The stock LTX text encoder
`umt5_xxl_fp8_e4m3fn_scaled.safetensors` will fail on your card.
