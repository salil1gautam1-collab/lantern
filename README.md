# Lantern

Free-tier, semi-automated, **human-in-the-loop** faceless YouTube pipeline.

> **Guardrails — non-negotiable:**
> - **Never auto-publish.** All uploads land as PRIVATE or SCHEDULED drafts. You click Publish in YouTube Studio yourself.
> - **Never automate logins or account creation.** Code only touches accounts via official APIs with OAuth tokens you provide.
> - **AI-assisted, not AI-generated.** YouTube terminates "inauthentic / mass-produced" channels. The script step requires your real input every time.
> - **Target 3–5 videos/week.** Higher daily volume increases ban risk.

---

## Active channel

`channels/india.yaml` (region IN). A US-region channel will be added later as `channels/us.yaml` — no code rewrites needed, just a config copy.

---

## Pipeline at a glance

| # | Module | What it does | Status |
|---|--------|--------------|--------|
| 1 | `research.py` | 5–10 ranked topic candidates from trend signals | not built |
| 2 | `script.py` | Original script (hook + POV + structure); `--manual` or LLM | not built |
| 3 | `voice.py` | Voiceover audio from edited script (edge-tts) | not built |
| 4 | `assemble.py` | Video assembly: voiceover + varied b-roll + (optional) captions + music + draft thumbnail | not built |
| 5 | Dashboard | FastAPI review UI — edit script/title/tags/thumbnail; click Approve to upload | not built |
| 6 | `upload.py` | Upload as PRIVATE/SCHEDULED draft after approval | not built |
| 7 | `instagram.py` | Export vertical Reels cut + caption to `output/instagram/` for **manual** posting | not built |

Modules are built one at a time. Each gets tested end-to-end before the next starts.

---

## Setup (first time)

### 1. Install prerequisites

In a regular (non-Administrator) PowerShell window:

```powershell
winget install --id Python.Python.3.12 --source winget
winget install --id Gyan.FFmpeg --source winget
winget install --id Git.Git --source winget
```

Then turn OFF the Microsoft Store Python aliases:
`Settings → Apps → Advanced app settings → App execution aliases → toggle off "python.exe" and "python3.exe"`.

Close PowerShell, open a fresh window, verify:
```powershell
python --version           # should be 3.12.x
ffmpeg -version | Select-Object -First 1
git --version
```

### 2. Get free API keys

| Key | Where to get it | Needed for |
|-----|-----------------|-----------|
| YouTube Data API v3 — **API key** | console.cloud.google.com → APIs & Services → Credentials → "Create API key" | research.py |
| Pexels | https://www.pexels.com/api/ → sign up → "Your API key" | assemble.py |
| Pixabay | https://pixabay.com/api/docs/ → log in → key shown on page | assemble.py |
| YouTube OAuth client JSON (**Desktop app**) | console.cloud.google.com → Credentials → "Create OAuth client ID" → Desktop app | upload.py |

Place the OAuth client JSON at `secrets/client_secret.json`.

### 3. Create your `.env`

```powershell
Copy-Item .env.example .env
```

Open `.env` in any editor and fill in the keys you have so far. Blank values for tiers you haven't reached yet are fine.

### 4. Virtual environment + Python deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## DO MANUALLY — DO NOT AUTOMATE

These steps are intentionally off-limits to code, by design:

- **Creating YouTube/Google accounts**, channels, or any sign-in flow.
- **Creating Instagram accounts** or any post / DM / story upload to Instagram. The Instagram module only *exports* a vertical cut + caption for you to copy-paste manually.
- **Clicking the final "Publish" button** in YouTube Studio. The upload module only places drafts.
- **AdSense application.** One AdSense account per individual, applied for manually once eligibility is met (1000 subs + 4000 watch hours in the past 12 months, **or** 10M Shorts views in the past 90 days).
- **Final answer on the "Was this content altered or made with AI?" question** in YouTube Studio. The upload module sets the flag if the script was LLM-assisted, but you double-check it on Publish.

---

## Security & account hygiene

- All secrets live in `.env` only. Never hardcoded, never passed in command-line args, never in URLs.
- `secrets/` is gitignored. OAuth client JSON and saved refresh tokens never enter git history.
- LLM output (when enabled) is sanitized and length-checked before it enters upload metadata.
- Per-video provenance records (`records/`) capture asset licenses at download time + whether AI was used + human-approval timestamp.
- To revoke OAuth access at any time: https://myaccount.google.com/permissions

---

## Status

- [x] Scaffold + initial config
- [ ] `research.py` (module 1)
- [ ] `script.py` (module 2)
- [ ] `voice.py` (module 3)
- [ ] `assemble.py` (module 4)
- [ ] Review dashboard (module 5)
- [ ] `upload.py` (module 6)
- [ ] `instagram.py` (module 7)
- [ ] Backup script
