"""Application settings.

Two sources, kept separate on purpose:
- API keys come from environment variables or the project's `.env` file (never committed).
- Everything else comes from `config/settings.yaml`, which is safe to commit.

`Settings.load()` combines both into one object that the rest of the code receives.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
LOCALES_DIR = PROJECT_ROOT / "config" / "locales"


class Secrets(BaseSettings):
    """API keys, read from environment variables or `.env`. A missing key simply disables that provider."""

    model_config = SettingsConfigDict(extra="ignore")

    # Gemini accepts either variable name; the Google examples use GOOGLE_API_KEY.
    gemini_api_key: str | None = Field(None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    openai_api_key: str | None = Field(None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(None, validation_alias="ANTHROPIC_API_KEY")
    groq_api_key: str | None = Field(None, validation_alias="GROQ_API_KEY")
    ollama_api_base: str | None = Field(None, validation_alias="OLLAMA_API_BASE")  # Ollama needs no key, only an address
    google_books_api_key: str | None = Field(None, validation_alias="GOOGLE_BOOKS_API_KEY")  # optional; raises the quota

    def key_for(self, provider: str) -> str | None:
        """Return the key for a provider prefix ('gemini', 'openai', ...). Empty strings count as missing."""
        return getattr(self, f"{provider}_api_key", None) or None


class LocaleConfig(BaseModel):
    """Where the claim is priced.

    country:  ISO 3166-1 alpha-2 code, e.g. IN (India), US, GB, DE, AU.
    currency: ISO 4217 code, e.g. INR, USD, GBP, EUR, AUD.
    Each country also needs a shop list in config/locales/<country>.yaml (used by the price stage).
    """

    country: str = Field("IN", pattern=r"^[A-Z]{2}$")
    currency: str = Field("INR", pattern=r"^[A-Z]{3}$")

    @property
    def shops_file(self) -> Path:
        return LOCALES_DIR / f"{self.country}.yaml"


class IngestConfig(BaseModel):
    fps: float = Field(4.0, gt=0)          # frames kept per second of video
    max_side: int = Field(3840, gt=0)      # longest side of a saved frame, in pixels
    jpeg_quality: int = Field(92, ge=50, le=100)


class KeyframesConfig(BaseModel):
    """How the keyframe stage groups video frames into views and picks one per view."""

    # A frame starts a new view when it covers less than this share of the current view's first frame.
    new_view_overlap: float = Field(0.6, gt=0, lt=1)
    # A frame is "blurry" when its sharpness is below this share of the sweep's median sharpness.
    blurry_ratio: float = Field(0.5, gt=0, lt=1)


class CameraConfig(BaseModel):
    # Horizontal field of view, used for size estimates when a frame carries no focal length (e.g. video).
    hfov_deg: float = Field(70.0, gt=10, lt=170)


class MarkerConfig(BaseModel):
    """The optional printable scale card (see `claim marker`)."""

    dictionary: str = "DICT_4X4_50"  # OpenCV ArUco dictionary name
    marker_id: int = 0
    size_cm: float = 15.0            # printed side length of the black square


class IdentifyConfig(BaseModel):
    """How the identify stage reads spines and looks books up."""

    batch_size: int = Field(5, ge=1, le=20)   # spine images sent to the model in one call
    catalogues: list[str] = Field(default_factory=lambda: ["openlibrary", "googlebooks"])  # tried in this order


class LiveConfig(BaseModel):
    """The live sweep: what the browser sends, when the agent is warned, and the provisional count."""

    voice: str = "gemini"                 # "gemini" (Gemini Live audio) or "cascaded" (browser speech + any vision model)
    gemini_model: str = "gemini-3.8-live"
    voice_name: str = "Kore"
    frame_fps: float = Field(4.0, gt=0, le=5)        # evidence frames recorded; 4 matches ingest, so thresholds carry over
    model_fps: float = Field(1.0, gt=0, le=5)        # of those, frames forwarded to the voice model
    model_frame_side: int = 768                      # forwarded frames are shrunk to this long side
    blurry_ratio: float = Field(0.5, gt=0, lt=1)     # sharpness below this share of the recent median is blurry
    glare_max: float = Field(0.06, gt=0, lt=1)       # share of blown-out pixels above which glare is reported
    motion_max: float = Field(0.11, gt=0, lt=1)      # change between frames 1/4 s apart: slow pan <= 0.10, rushed ~0.12
    warn_every_s: float = Field(6.0, ge=1)           # the same warning is repeated at most this often
    provisional_every_s: float = Field(20.0, ge=0)   # live spine count from one frame; 0 turns it off
    max_minutes: float = Field(20.0, gt=0)


class GatewayConfig(BaseModel):
    """How model calls are retried before moving on to the next provider."""

    # True: never call a model; answer only from the cache (development and tests). `--allow-calls` overrides.
    cache_only: bool = False
    retries: int = Field(3, ge=0)        # extra attempts on rate limits and server errors
    backoff_s: float = Field(2.0, ge=0)  # wait before the first retry; doubles each time
    timeout_s: float = Field(120.0, gt=0)


class ModelsConfig(BaseModel):
    """Which provider(s) each model stage uses.

    A profile fills every stage at once; `overrides` replaces the list for single stages.
    Each list is tried in order, so later entries are fallbacks.
    """

    profile: str = "gemini"
    overrides: dict[str, list[str]] = Field(default_factory=dict)
    profiles: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    def chain(self, stage: str) -> list[str]:
        """Ordered provider specs for a model stage, e.g. ['gemini/gemini-3.5-flash', 'gemini/gemini-3.8-flash']."""
        if stage in self.overrides:
            return list(self.overrides[stage])
        profile = self.profiles.get(self.profile)
        if profile is None:
            raise KeyError(f"unknown model profile {self.profile!r}; known: {sorted(self.profiles)}")
        return list(profile.get(stage, []))


class Settings(BaseModel):
    """Everything configurable, in one object passed to every part of the system."""

    locale: LocaleConfig = Field(default_factory=LocaleConfig)
    runs_dir: Path = Path("runs")  # where sweeps are stored; relative paths are under the project root
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    keyframes: KeyframesConfig = Field(default_factory=KeyframesConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    marker: MarkerConfig = Field(default_factory=MarkerConfig)
    identify: IdentifyConfig = Field(default_factory=IdentifyConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    appraisal_threshold: float = 5000.0  # in the locale currency; anything above goes to a human appraiser
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    secrets: Secrets = Field(default_factory=lambda: Secrets(_env_file=None))

    @classmethod
    def load(cls, config_path: Path | None = None, env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
        """Read the YAML settings and the API keys. Raises a validation error that names any bad field."""
        path = config_path or DEFAULT_CONFIG
        settings = cls.model_validate(yaml.safe_load(path.read_text()) or {})
        settings.secrets = Secrets(_env_file=env_file if env_file and env_file.exists() else None)
        if not settings.runs_dir.is_absolute():
            settings.runs_dir = PROJECT_ROOT / settings.runs_dir
        return settings

    @property
    def cache_path(self) -> Path:
        """One shared cache for all sweeps, so the same model question is never paid for twice."""
        return self.runs_dir / ".cache.sqlite"
