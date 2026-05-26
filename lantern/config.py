"""Configuration loader for Lantern.

Reads `.env` and `channels/<slug>.yaml`, returns validated config objects.
Shared by every pipeline module.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent


class ResearchConfig(BaseModel):
    pytrends_geo: str = "IN"
    youtube_region_code: str = "IN"
    candidates_per_run: int = 8
    query_modifiers: list[str] = Field(
        default_factory=lambda: ["wisdom", "for hard times", "teachings"]
    )
    exclude_shorts: bool = True
    youtube_duration: str = "medium"  # any | short | medium | long
    min_video_seconds: int = 90
    exclude_non_latin_titles: bool = True  # drop Devanagari/CJK/etc. titles when language=en


class ScriptConfig(BaseModel):
    editor: str | None = None              # None = auto: 'notepad' on Windows
    word_count_target: int = 1300          # ~7-8 min at typical narration speed
    word_count_min: int = 900              # ~6 min floor
    word_count_max: int = 1700             # ~10 min ceiling


class VoiceConfig(BaseModel):
    primary: str = "en-IN-PrabhatNeural"   # warm Indian-English male
    rate: str = "+0%"                       # edge-tts rate adjustment, e.g. "-10%", "+15%"
    volume: str = "+0%"                     # edge-tts volume adjustment


class AssembleConfig(BaseModel):
    resolution_width: int = 1920
    resolution_height: int = 1080
    fps: int = 30
    clip_seconds_avg: int = 6              # how long each b-roll segment plays
    clips_per_query: int = 5                # how many results to fetch per search query
    music_volume: float = 0.10              # mix at 10% behind voice
    music_enabled: bool = True              # uses random track from assets/music/ if present
    ledger_keep_recent: int = 50           # how many recently-used assets to avoid in dedupe


class UploadConfig(BaseModel):
    privacy_status: str = "private"        # private | unlisted | public — KEEP PRIVATE
    category_id: str = "22"                 # 22 = People & Blogs, 27 = Education
    default_language: str = "en"
    default_tags: list[str] = Field(
        default_factory=lambda: [
            "practical wisdom",
            "philosophy",
            "life advice",
        ]
    )


class ChannelConfig(BaseModel):
    name: str
    slug: str
    region: str
    niche: str
    themes: list[str]
    language: str = "en"
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    script: ScriptConfig = Field(default_factory=ScriptConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    assemble: AssembleConfig = Field(default_factory=AssembleConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)


class EnvConfig(BaseModel):
    youtube_api_key: str | None = None
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    youtube_oauth_client_json: str = "secrets/client_secret.json"
    youtube_token_cache: str = "secrets/youtube_token.json"
    llm_provider: str = "none"
    llm_api_key: str | None = None
    llm_model: str = "gemini-2.0-flash"
    active_channel: str = "india"
    stock_cache_max_gb: int = 5


def load_env(env_path: Path | None = None) -> EnvConfig:
    """Load `.env` from repo root and return a validated EnvConfig."""
    env_path = env_path or (REPO_ROOT / ".env")
    load_dotenv(env_path)
    return EnvConfig(
        youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
        pexels_api_key=os.getenv("PEXELS_API_KEY") or None,
        pixabay_api_key=os.getenv("PIXABAY_API_KEY") or None,
        youtube_oauth_client_json=os.getenv(
            "YOUTUBE_OAUTH_CLIENT_JSON", "secrets/client_secret.json"
        ),
        youtube_token_cache=os.getenv(
            "YOUTUBE_TOKEN_CACHE", "secrets/youtube_token.json"
        ),
        llm_provider=os.getenv("LLM_PROVIDER", "none"),
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL", "gemini-2.0-flash"),
        active_channel=os.getenv("ACTIVE_CHANNEL", "india"),
        stock_cache_max_gb=int(os.getenv("STOCK_CACHE_MAX_GB", "5")),
    )


def load_channel(slug: str) -> ChannelConfig:
    """Load and validate `channels/<slug>.yaml`."""
    path = REPO_ROOT / "channels" / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Channel config not found: {path}. "
            f"Available: {sorted(p.stem for p in (REPO_ROOT / 'channels').glob('*.yaml'))}"
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ChannelConfig(**data)
