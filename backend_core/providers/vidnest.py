from .base import ProviderProfile


PROFILE = ProviderProfile(
    name='vidnest',
    allow_media_requests=True,
    use_headed_browser=True,
    warmup_url='https://vidnest.fun',
    warmup_timeout_ms=6000,
    discovery_loops=6,
    discovery_delay_ms=500,
    force_interact_each_loop=True,
    extra_scroll_attempts=(1, 2, 4),
)


def build_url(target):
    if not target:
        return None
    if target['type'] == 'movie':
        return f"https://vidnest.fun/movie/{target['tmdb_id']}"
    if target['type'] == 'tv':
        return f"https://vidnest.fun/tv/{target['tmdb_id']}/{target['season']}/{target['episode']}"
    return None
