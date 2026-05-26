"""Lantern CLI — subcommands for each pipeline module."""

from __future__ import annotations

import logging

import click

from .config import load_channel, load_env


@click.group()
@click.option(
    "--channel",
    default=None,
    help="Channel slug to operate on. Defaults to ACTIVE_CHANNEL in .env, else 'india'.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Verbose logging (INFO level instead of WARNING).",
)
@click.pass_context
def cli(ctx: click.Context, channel: str | None, verbose: bool) -> None:
    """Lantern — free-tier, human-in-the-loop faceless YouTube pipeline."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    env = load_env()
    slug = channel or env.active_channel
    ch = load_channel(slug)
    ctx.ensure_object(dict)
    ctx.obj["env"] = env
    ctx.obj["channel"] = ch


@cli.command()
@click.pass_context
def research(ctx: click.Context) -> None:
    """Surface ranked topic candidates from pytrends + YouTube Data API."""
    env = ctx.obj["env"]
    if not env.youtube_api_key:
        raise click.ClickException(
            "YOUTUBE_API_KEY missing from .env. research.py needs it."
        )
    from .research import run_research

    run_research(ctx.obj["channel"], env)


@cli.command()
@click.option("--topic", required=True, help="The topic for this video.")
@click.option(
    "--llm",
    is_flag=True,
    help="Generate first draft via LLM_PROVIDER from .env. Manual fallback if unavailable.",
)
@click.option(
    "--no-edit",
    is_flag=True,
    help="Write the template/draft and exit without opening an editor.",
)
@click.pass_context
def script(ctx: click.Context, topic: str, llm: bool, no_edit: bool) -> None:
    """Generate (or open) a script for the chosen topic."""
    from .script import run_script

    run_script(
        channel=ctx.obj["channel"],
        env=ctx.obj["env"],
        topic=topic,
        use_llm=llm,
        no_edit=no_edit,
    )


@cli.command()
@click.option(
    "--script",
    "script_path_str",
    type=click.Path(exists=True),
    default=None,
    help="Path to script .md file. Defaults to the latest script in output/scripts/<channel>/.",
)
@click.pass_context
def voice(ctx: click.Context, script_path_str: str | None) -> None:
    """Render voiceover audio from a script .md using edge-tts."""
    from pathlib import Path

    from .voice import run_voice

    p = Path(script_path_str) if script_path_str else None
    run_voice(ctx.obj["channel"], ctx.obj["env"], p)


@cli.command()
@click.option(
    "--voice-audio",
    "voice_audio_str",
    type=click.Path(exists=True),
    default=None,
    help="Path to voiceover .mp3. Defaults to latest in output/voiceover/<channel>/.",
)
@click.pass_context
def assemble(ctx: click.Context, voice_audio_str: str | None) -> None:
    """Compose voiceover + b-roll + music into a draft .mp4."""
    from pathlib import Path

    env = ctx.obj["env"]
    if not env.pexels_api_key and not env.pixabay_api_key:
        raise click.ClickException(
            "Both PEXELS_API_KEY and PIXABAY_API_KEY missing from .env. "
            "Assemble needs at least one stock source."
        )
    from .assemble import run_assemble

    p = Path(voice_audio_str) if voice_audio_str else None
    run_assemble(ctx.obj["channel"], env, p)


if __name__ == "__main__":
    cli()
