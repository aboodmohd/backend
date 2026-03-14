import re
from backend_core.providers import get_provider_module


STREAM_HINT_KEYWORDS = [
    '.m3u8', '.mp4', '.m4v', '.ts', 'playlist', 'master', 'manifest',
    'worker', 'skylark', 'storm', 'vhls', 'vidfast', 'vidrock',
    '111movies', 'skyember', 'videostr.net', 'master.json', 'cf-master',
    'hls', '/v4/tab/'
]

STRICT_MEDIA_PATTERNS = [
    '.m3u8', '.mp4', '.m4v', '.ts', '/playlist/', 'playlist/', 'master', 'manifest',
    'master.json', 'cf-master', '/stream2/', '/vod/', '/file2/', '/proxy/file2/',
    'workers.dev/', 'videostr.net', 'vhls', '/v4/tab/', 'index.m3u8'
]

PROVIDER_ORDER = ['vidlink', 'vidfast', '111movies', 'vidnest']


def detect_provider(url):
    lowered = (url or '').lower()
    for provider in ['vidlink', 'vidfast', 'vidrock', '111movies', 'vidnest']:
        if provider in lowered:
            return provider
    return 'generic'


def parse_media_target(url):
    movie_match = re.search(r'/movie/(\d+)', url or '', re.IGNORECASE)
    if movie_match:
        return {'type': 'movie', 'tmdb_id': movie_match.group(1)}

    tv_match = re.search(r'/tv/(\d+)/(\d+)/(\d+)', url or '', re.IGNORECASE)
    if tv_match:
        return {
            'type': 'tv',
            'tmdb_id': tv_match.group(1),
            'season': tv_match.group(2),
            'episode': tv_match.group(3),
        }

    return None


def build_provider_url(provider, target):
    module = get_provider_module(provider)
    if not module or not hasattr(module, 'build_url'):
        return None
    return module.build_url(target)


def get_provider_fallback_urls(url):
    if not url:
        return []

    fallbacks = []
    lowered = url.lower()

    if 'vidnest.fun/' in lowered:
        fallbacks.append(url.replace('https://vidnest.fun/', 'https://vidlink.pro/'))
        fallbacks.append(url.replace('http://vidnest.fun/', 'https://vidlink.pro/'))

    deduped = []
    seen = set()
    for candidate in fallbacks:
        if candidate and candidate not in seen and candidate != url:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def expand_provider_urls(url, providers=None):
    target = parse_media_target(url)
    requested_provider = detect_provider(url)
    normalized_providers = []
    for provider in providers or []:
        lowered = (provider or '').strip().lower()
        if lowered and lowered not in normalized_providers:
            normalized_providers.append(lowered)

    ordered_providers = normalized_providers or [requested_provider] + [provider for provider in PROVIDER_ORDER if provider != requested_provider]

    candidates = []
    seen = set()

    for candidate_url in [url] + [build_provider_url(provider, target) for provider in ordered_providers]:
        if not candidate_url or candidate_url in seen:
            continue
        seen.add(candidate_url)
        candidates.append(candidate_url)

    return candidates
