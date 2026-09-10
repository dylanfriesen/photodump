# Do these at the desktop

Three things, in order. The first two get rendering working again; the third
means you never have to do this again.

Everything else — the queue, reels, the Instagram encode, captions, email —
already works on kanto and needs nothing from you.

---

## 1. Start ComfyUI so kanto can see it

ComfyUI binds to localhost by default. It will look perfectly fine on the
desktop and be completely invisible to kanto, which is the failure we keep
hitting.

Launch it with:

```
--listen 0.0.0.0 --port 8188
```

In **ComfyUI Desktop**: Settings → Server → extra launch arguments.
If you launch from a `.bat`, add the flags to the `python main.py` line.

Check it took, **on the desktop**:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8188 | Select LocalAddress,LocalPort
```

You want **`0.0.0.0`**. If it says `127.0.0.1`, the flag did not apply and kanto
still cannot reach it.

Windows Firewall may prompt on first launch — allow it on **private** networks.

That is enough to unblock everything. Jobs in the queue will start on their own.

---

## 2. Export your LTX workflow so photodump can drive it

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

## 3. Stop having to do this in person

Two sessions have now built the kanto half of remote access and stopped at the
Windows step. `~/.ssh/desktop_ollama` exists and has never been used, because
its bootstrap was never run on this machine.

Paste this into an **Administrator** PowerShell. It uses only the OpenSSH
component already in Windows, and scopes access to the tailnet:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-ItemProperty "HKLM:\SOFTWARE\OpenSSH" DefaultShell -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force
$k="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINxPUI21NpksQXuVJNa70FPD+OYGHy4PWtk3CXbaM1g/ kanto->desktop-ollama"; $f="$env:ProgramData\ssh\administrators_authorized_keys"
Add-Content $f $k; icacls $f /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
Get-NetFirewallRule -Name OpenSSH-Server-In-TCP -EA 0 | Remove-NetFirewallRule
New-NetFirewallRule -Name sshd-tailnet -DisplayName "OpenSSH (Tailscale only)" -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -RemoteAddress 100.64.0.0/10
Restart-Service sshd; Get-NetTCPConnection -State Listen -LocalPort 22 | Select LocalAddress,LocalPort
```

Expect `0.0.0.0  22` at the end.

After that I can start ComfyUI, read its logs, reconcile the workflows and set
up an auto-start task myself — without you relaying. Key-only, tailnet-only,
and revocable with `Stop-Service sshd; Set-Service sshd -StartupType Disabled`.

The full version with a revoke script lives in `~/desktop-ssh`.

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
