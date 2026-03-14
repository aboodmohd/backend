from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    worker_order: tuple = (("Desktop", False), ("Mobile", True))
    allow_media_requests: bool = False
    use_headed_browser: bool = False
    reuse_session: bool = False
    warmup_url: str | None = None
    warmup_timeout_ms: int = 8000
    target_timeout_ms: int = 10000
    discovery_loops: int = 2
    discovery_delay_ms: int = 250
    force_interact_each_loop: bool = False
    extra_scroll_attempts: tuple = field(default_factory=tuple)
    challenge_attempts: int = 8
    challenge_wait_ms: int = 250
    desktop_user_agents: tuple = field(default_factory=tuple)
    mobile_user_agents: tuple = field(default_factory=tuple)
