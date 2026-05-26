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


if __name__ == "__main__":
    cli()
