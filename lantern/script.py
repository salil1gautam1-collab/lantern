"""Script generator — manual template or LLM-assisted draft.

Default mode is **manual ($0)**: writes a structured markdown template, opens
it in the configured editor, waits for the operator to write and save, then
records provenance.

LLM mode is opt-in via `.env` (LLM_PROVIDER + LLM_API_KEY + LLM_MODEL). On any
LLM failure it silently falls back to the manual template — the run never
crashes out of "no script produced".
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT, ChannelConfig, EnvConfig
from .records import write_record

log = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe slug. Lowercase, dashes, no punctuation."""
    s = re.sub(r"[^\w\s-]", "", text.lower().strip())
    s = re.sub(r"[-\s]+", "-", s)
    return s[:max_len].strip("-") or "untitled"


def _resolve_editor(configured: str | None) -> list[str]:
    """Return the editor command as a list ready for subprocess.run."""
    if configured:
        # Special-case VS Code so it actually waits for the file to be closed
        if configured.lower() in ("code", "code.cmd"):
            return [configured, "--wait"]
        return [configured]
    if os.name == "nt":
        return ["notepad"]
    if shutil.which("code"):
        return ["code", "--wait"]
    return [os.environ.get("EDITOR", "vi")]


def _open_in_editor(path: Path, configured: str | None) -> None:
    """Open path in editor and BLOCK until the editor closes."""
    cmd = _resolve_editor(configured) + [str(path)]
    print(f"\nOpening editor: {' '.join(cmd)}")
    print("(Write your script. Save. Then close the editor to continue.)\n")
    subprocess.run(cmd, check=False)


def _build_template(topic: str, channel: ChannelConfig) -> str:
    """Manual-mode markdown template. Editorial prompts are HTML comments
    so they don't show up in voice synthesis later (whisper/edge-tts strip them).
    """
    sc = channel.script
    return f"""# {topic}

> **Channel:** {channel.name} ({channel.slug}, region {channel.region}, language {channel.language})
> **Niche:** {channel.niche}
> **Date:** {datetime.now().strftime("%Y-%m-%d")}
> **Length target:** ~{sc.word_count_target} words ({sc.word_count_min}-{sc.word_count_max})

---

## Hook (first 15 seconds — open with tension, not summary)

<!--
The viewer decides in 15 seconds whether to keep watching.
DO NOT open with "Today we'll talk about..." or "In this video...".
DO open with: a specific moment, a question, a contradiction, a sharp observation.
Example: "Three a.m. The phone rings. Your mother is in the hospital.
What does Marcus Aurelius have to say to you right now?"
-->



## Angle / POV (1-2 lines — what's YOUR unique stance here?)

<!--
This is the single most important line of the whole script.
Is the stance "ancient wisdom is harder than it looks"?
"This teaching is misunderstood"? "Most advice on this is wrong"?
Without an angle, you have a Wikipedia entry, not a Lantern video.
This is also what protects the channel from YouTube's "inauthentic content"
takedown — your distinct angle is the human signal.
-->



## Body (~800-1300 words — clear progression, no bullets)

<!--
Build through 3-5 movements. Each movement = a beat, not a heading.
Anchor in concrete: specific incidents, specific people, specific moments.
Avoid abstractions. Avoid "we" — speak to one person.
This script will be read aloud — write to be HEARD, not skimmed.
Vary sentence length. Use contractions where they sound natural.
-->



## Close (final 30 seconds — leave a question, not a moral)

<!--
Don't summarize. Don't moralize. End on a question or an open observation
that lingers. The viewer should sit for a moment after the video ends.
-->



---

## YouTube metadata (we'll polish in the dashboard, but seed here)

**Title (60-70 chars, hook in title):**


**Description (first 2 lines visible in thumbnail context):**


**Tags (comma-separated, 6-10):**

"""


# Default LLM system prompt — operator can paste their own later and we'll
# wire it through .env or a channel field.
DEFAULT_LLM_SYSTEM_PROMPT = """You are writing a 6-10 minute YouTube voiceover script for the channel "{channel_name}".

The channel's niche is: {niche}
The channel draws on these wisdom traditions: {themes}.
Region: {region}, narrated in: {language}.

Hard rules — these protect the channel from YouTube's "inauthentic content" enforcement:

1. OPEN WITH TENSION, not summary. The first 15 seconds must hook a viewer
   who is one click from scrolling. Use a specific moment, a contradiction,
   a question. NEVER open with "Today we'll talk about..." or "In this video...".

2. HAVE A DISTINCT POINT OF VIEW. Don't summarize the tradition. Take a stance.
   Examples: "this teaching is harder than it looks", "most people misread this",
   "this is the part nobody tells you". Without a POV, the script is a Wikipedia
   entry and the channel will be flagged as mass-produced AI content.

3. VARY STRUCTURE per script. Do NOT use a fill-in-the-blank template
   (intro / three points / outro). Each script should feel like a single
   piece of writing, not a slot-filler.

4. WRITE TO BE HEARD, NOT SKIMMED. No headings, no bullets, no lists.
   Natural prose only. Vary sentence length. Use contractions where they
   sound natural.

5. ANCHOR IN CONCRETE DETAIL. Specific people, specific situations, specific
   moments. Avoid abstraction. Avoid "we" — speak to one listener.

6. END ON A QUESTION OR OPEN OBSERVATION, not a moral. The viewer should
   sit with the script for a moment after it ends.

7. LENGTH: roughly {word_target} words ({word_min}-{word_max}).

Output format: pure prose, no headings, no markdown. The first paragraph IS the hook.
The script begins immediately — no preamble, no "Here's your script:".
"""


def _build_llm_prompt(topic: str, channel: ChannelConfig) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the LLM call."""
    sc = channel.script
    system = DEFAULT_LLM_SYSTEM_PROMPT.format(
        channel_name=channel.name,
        niche=channel.niche,
        themes=", ".join(channel.themes),
        region=channel.region,
        language=channel.language,
        word_target=sc.word_count_target,
        word_min=sc.word_count_min,
        word_max=sc.word_count_max,
    )
    user = f'Topic for this script: "{topic}"'
    return system, user


def _llm_draft(topic: str, channel: ChannelConfig, env: EnvConfig) -> str | None:
    """Try to generate a script via LLM. Returns text or None on any failure."""
    if env.llm_provider == "none" or not env.llm_api_key:
        log.info("LLM not configured — manual mode.")
        return None

    if env.llm_provider != "gemini":
        log.warning(
            "LLM_PROVIDER='%s' not yet implemented in script.py. Falling back to manual.",
            env.llm_provider,
        )
        return None

    try:
        import google.generativeai as genai  # type: ignore[import-not-found]
    except ImportError:
        log.error(
            "LLM_PROVIDER=gemini requires the google-generativeai package. "
            "Install with: pip install google-generativeai"
        )
        return None

    try:
        genai.configure(api_key=env.llm_api_key)
        system_prompt, user_prompt = _build_llm_prompt(topic, channel)
        model = genai.GenerativeModel(env.llm_model, system_instruction=system_prompt)
        response = model.generate_content(user_prompt)
        return response.text
    except Exception as e:  # noqa: BLE001
        log.error("Gemini call failed: %s — falling back to manual template.", e)
        return None


def run_script(
    channel: ChannelConfig,
    env: EnvConfig,
    topic: str,
    use_llm: bool,
    no_edit: bool,
) -> None:
    """Create a script file for `topic` and (unless --no-edit) open the editor."""
    scripts_dir = REPO_ROOT / "output" / "scripts" / channel.slug
    scripts_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    slug = _slugify(topic)
    path = scripts_dir / f"{ts}_{slug}.md"

    provenance = "manual"
    if use_llm:
        print(f"Attempting LLM draft via provider='{env.llm_provider}' model='{env.llm_model}'...")
        llm_text = _llm_draft(topic, channel, env)
        if llm_text:
            path.write_text(llm_text, encoding="utf-8")
            provenance = f"ai-drafted ({env.llm_provider}/{env.llm_model})"
            print(f"LLM draft written: {path.relative_to(REPO_ROOT)}")
        else:
            path.write_text(_build_template(topic, channel), encoding="utf-8")
            provenance = "manual (llm fallback)"
            print(f"LLM unavailable; manual template written: {path.relative_to(REPO_ROOT)}")
    else:
        path.write_text(_build_template(topic, channel), encoding="utf-8")
        print(f"Manual template written: {path.relative_to(REPO_ROOT)}")

    if not no_edit:
        _open_in_editor(path, channel.script.editor)
        if provenance.startswith("ai-drafted"):
            provenance = provenance.replace("ai-drafted", "ai-drafted, human-edited")

    final_text = path.read_text(encoding="utf-8")
    word_count = len(final_text.split())

    record_path = write_record(
        channel_slug=channel.slug,
        kind="script",
        payload={
            "topic": topic,
            "script_path": str(path.relative_to(REPO_ROOT)),
            "provenance": provenance,
            "word_count_approx": word_count,
            "llm_provider": env.llm_provider if use_llm else None,
            "llm_model": env.llm_model if use_llm else None,
            "no_edit": no_edit,
        },
    )

    print(f"\nScript saved:  {path.relative_to(REPO_ROOT)}")
    print(f"Word count:    ~{word_count} (target {channel.script.word_count_target})")
    print(f"Provenance:    {provenance}")
    print(f"Record:        {record_path.relative_to(REPO_ROOT)}")
