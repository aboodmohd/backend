from urllib.parse import urljoin


def short_url(url, limit=90):
    if not url:
        return 'None'
    return url if len(url) <= limit else f"{url[:limit]}..."


def is_challenge_page(html, current_url=''):
    content = ((html or '') + ' ' + (current_url or '')).lower()
    checks = [
        'checking your browser', 'verify you are human', 'just a moment',
        'attention required',
        'cf-browser-verification', 'cf-challenge', 'cloudflare', '/cdn-cgi/challenge-platform/'
    ]
    return any(token in content for token in checks)


def should_skip_candidate(candidate_url, page_url=''):
    lowered = (candidate_url or '').lower()
    page_lowered = (page_url or '').lower()

    blocked_parts = [
        '/cdn-cgi/', 'cf.errors.css', 'browser-bar.png', 'cf-no-screenshot-error.png',
        '/assets/', '.css', '.js', 'hls.min.js', '/rum?', '.woff', '.woff2',
        'google-analytics.com/', 'umami.', '/api/send', 'demo-video.mp4'
    ]
    if any(part in lowered for part in blocked_parts):
        return True

    if lowered == page_lowered or lowered.rstrip('/') == page_lowered.rstrip('/'):
        return True

    return False


def stream_priority(candidate_url):
    lowered = (candidate_url or '').lower()
    score = 0

    if 'playlist' in lowered or '.m3u8' in lowered or 'manifest' in lowered:
        score += 100
    if '/playlist/' in lowered or 'hls2.vdrk.site/' in lowered or '.vdrk.site/' in lowered:
        score += 120
    if '/file2/' in lowered or '/proxy/file2/' in lowered:
        score += 95
    if 'workers.dev/' in lowered:
        score += 45
    if 'master' in lowered:
        score += 90
    if '.mp4' in lowered:
        score += 70
    if '.ts' in lowered:
        score += 40
    if 'demo-video' in lowered:
        score -= 500
    if any(part in lowered for part in ['/cdn-cgi/', '.css', '.js', '/assets/']):
        score -= 200

    return score


def looks_like_stream_url(url, strict_media_patterns):
    if not url:
        return False
    lowered = url.lower()
    return any(token in lowered for token in strict_media_patterns)


def looks_like_hls_manifest_body(body=''):
    if not body:
        return False

    body_start = (body or '')[:2048].lstrip()
    return (
        body_start.startswith('#EXTM3U')
        or '#EXTINF' in body_start
        or '#EXT-X-STREAM-INF' in body_start
        or '#EXT-X-TARGETDURATION' in body_start
        or '#EXT-X-MEDIA-SEQUENCE' in body_start
    )


def is_playlist_response(url, content_type='', body=''):
    lowered_type = (content_type or '').lower()
    lowered_url = (url or '').lower()
    has_manifest_body = looks_like_hls_manifest_body(body)

    if has_manifest_body:
        return True

    return (
        '.m3u8' in lowered_url
        or (
            any(token in lowered_url for token in ['manifest', 'playlist', 'master'])
            and ('mpegurl' in lowered_type or 'dash+xml' in lowered_type)
        )
    )


def resolve_candidate_url(candidate_url, base_url):
    if not candidate_url:
        return None
    if isinstance(candidate_url, str) and candidate_url.startswith(('http://', 'https://')):
        return candidate_url
    try:
        return urljoin(base_url, candidate_url)
    except Exception:
        return candidate_url
