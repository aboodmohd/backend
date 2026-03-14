from .base import ProviderProfile


PROFILE = ProviderProfile(name='vidnest')


def build_url(target):
    if not target:
        return None
    if target['type'] == 'movie':
        return f"https://vidnest.fun/movie/{target['tmdb_id']}"
    if target['type'] == 'tv':
        return f"https://vidnest.fun/tv/{target['tmdb_id']}/{target['season']}/{target['episode']}"
    return None
