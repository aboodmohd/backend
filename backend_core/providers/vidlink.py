from .base import ProviderProfile


PROFILE = ProviderProfile(name='vidlink')


def build_url(target):
    if not target:
        return None
    if target['type'] == 'movie':
        return f"https://vidlink.pro/movie/{target['tmdb_id']}"
    if target['type'] == 'tv':
        return f"https://vidlink.pro/tv/{target['tmdb_id']}/{target['season']}/{target['episode']}"
    return None
