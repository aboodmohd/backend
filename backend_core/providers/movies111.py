from .base import ProviderProfile


PROFILE = ProviderProfile(
    name='111movies',
    worker_order=(("Desktop", False),),
    allow_media_requests=True,
    use_headed_browser=False,
    warmup_url='https://111movies.net',
    discovery_loops=8,
    discovery_delay_ms=750,
    force_interact_each_loop=True,
    extra_scroll_attempts=(1, 3, 5),
)


def build_url(target):
    if not target:
        return None
    if target['type'] == 'movie':
        return f"https://111movies.net/movie/{target['tmdb_id']}"
    if target['type'] == 'tv':
        return f"https://111movies.net/tv/{target['tmdb_id']}/{target['season']}/{target['episode']}"
    return None
