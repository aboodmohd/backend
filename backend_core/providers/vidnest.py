from .base import ProviderProfile


PROFILE = ProviderProfile(
    name='vidnest',
    allow_media_requests=True,
    use_headed_browser=True,
    reuse_session=True,
    warmup_url=None,
    discovery_loops=6,
    discovery_delay_ms=500,
    force_interact_each_loop=True,
    extra_scroll_attempts=(1, 2, 4),
    challenge_attempts=12,
    challenge_wait_ms=700,
    pre_capture_wait_ms=8000,
    iframe_wait_timeout_ms=8000,
    iframe_selectors=(
        'iframe',
        'iframe[src*="embed"]',
        'iframe[src*="player"]',
        'iframe[src*="videostr"]',
    ),
    desktop_user_agents=(
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    ),
    mobile_user_agents=(
        'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    ),
)


def build_url(target):
    if not target:
        return None
    if target['type'] == 'movie':
        return f"https://vidnest.fun/movie/{target['tmdb_id']}"
    if target['type'] == 'tv':
        return f"https://vidnest.fun/tv/{target['tmdb_id']}/{target['season']}/{target['episode']}"
    return None
