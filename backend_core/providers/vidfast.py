from .base import ProviderProfile


PROFILE = ProviderProfile(name='vidfast', worker_order=(("Mobile", True), ("Desktop", False)))


def build_url(target):
    if not target:
        return None
    if target['type'] == 'movie':
        return f"https://vidfast.pro/movie/{target['tmdb_id']}?autoPlay=true"
    if target['type'] == 'tv':
        return f"https://vidfast.pro/tv/{target['tmdb_id']}/{target['season']}/{target['episode']}?autoPlay=true"
    return None
