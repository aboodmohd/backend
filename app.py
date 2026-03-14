from flask import Flask, request, jsonify, Response
import requests
import re
import os
import time
from flask_cors import CORS
from playwright.sync_api import sync_playwright
import requests.packages.urllib3
import io
import json
import uuid
import zipfile
from urllib.parse import urlencode, urljoin, quote
from backend_core.provider_config import (
    PROVIDER_ORDER,
    STREAM_HINT_KEYWORDS,
    STRICT_MEDIA_PATTERNS,
    build_provider_url,
    detect_provider,
    expand_provider_urls,
    get_provider_fallback_urls,
    parse_media_target,
)
from backend_core.providers import get_provider_profile
from backend_core.stream_utils import (
    is_challenge_page,
    is_playlist_response,
    looks_like_hls_manifest_body,
    looks_like_stream_url as base_looks_like_stream_url,
    resolve_candidate_url,
    short_url,
    should_skip_candidate,
    stream_priority,
)
from backend_core.subtitle_utils import (
    dedupe_subtitles,
    extract_subtitle_candidates_from_json_payload,
    extract_subtitles_from_m3u8,
    extract_subtitles_from_text,
    infer_subtitle_language_from_label,
    looks_like_subtitle_url,
    normalize_language_code,
    normalize_subsource_language,
    normalize_subtitle_entry,
)
requests.packages.urllib3.disable_warnings()

app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return "🚀 NOVA Backend is Running!"


SUBDL_API_KEY = "EuWRVpLf2Iyd-52MZ3n9Iyi_TX4yxL4-"
SUBSOURCE_API_KEY = "sk_2da97448424b9e7c94fbc9756291c5eac67711df44b978f43fc7ba541163a77a"
SUBTITLE_CACHE = {}
STREAM_CACHE = {}
STREAM_CACHE_TTL_SECONDS = int(os.getenv('STREAM_CACHE_TTL_SECONDS', '5400'))
PLAYWRIGHT_PROXY_SERVER = os.getenv('PLAYWRIGHT_PROXY_SERVER', '').strip()
PLAYWRIGHT_PROXY_USERNAME = os.getenv('PLAYWRIGHT_PROXY_USERNAME', '').strip()
PLAYWRIGHT_PROXY_PASSWORD = os.getenv('PLAYWRIGHT_PROXY_PASSWORD', '').strip()
PLAYWRIGHT_SESSION_COOKIES = {}


def build_stream_cache_key(url, preferred_quality, providers=None):
    normalized_providers = ','.join((provider or '').strip().lower() for provider in (providers or []) if provider)
    return f"{url}|{preferred_quality}|{normalized_providers or 'default'}"


def get_cached_stream_result(cache_key):
    cached_entry = STREAM_CACHE.get(cache_key)
    if not cached_entry:
        return None

    if cached_entry['expires_at'] <= time.time():
        STREAM_CACHE.pop(cache_key, None)
        return None

    return cached_entry['payload']


def set_cached_stream_result(cache_key, payload):
    STREAM_CACHE[cache_key] = {
        'payload': payload,
        'expires_at': time.time() + STREAM_CACHE_TTL_SECONDS,
    }


def build_playwright_proxy_settings():
    if not PLAYWRIGHT_PROXY_SERVER:
        return None

    proxy_config = {'server': PLAYWRIGHT_PROXY_SERVER}
    if PLAYWRIGHT_PROXY_USERNAME:
        proxy_config['username'] = PLAYWRIGHT_PROXY_USERNAME
    if PLAYWRIGHT_PROXY_PASSWORD:
        proxy_config['password'] = PLAYWRIGHT_PROXY_PASSWORD
    return proxy_config


def can_launch_headed_browser():
    if os.getenv('PLAYWRIGHT_FORCE_HEADED', '').strip().lower() in {'1', 'true', 'yes'}:
        return True
    return bool(os.getenv('DISPLAY', '').strip() or os.getenv('WAYLAND_DISPLAY', '').strip())


def get_session_cookie_key(provider, mode_label):
    return f"{(provider or 'generic').lower()}:{(mode_label or 'desktop').lower()}"


def get_profile_user_agent(profile, is_mobile, url):
    candidates = profile.mobile_user_agents if is_mobile else profile.desktop_user_agents
    if not candidates:
        if is_mobile:
            return "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    seed = sum(ord(ch) for ch in ((url or '') + ('mobile' if is_mobile else 'desktop')))
    return candidates[seed % len(candidates)]


def log_provider(provider, message):
    print(f"🔎 [{provider}] {message}")


def looks_like_stream_url(url):
    return base_looks_like_stream_url(url, STRICT_MEDIA_PATTERNS)


def build_proxy_url(host, target_url, referer='', cookie=''):
    lowered = (target_url or '').lower()
    proxy_name = 'stream.bin'
    if is_playlist_response(target_url):
        proxy_name = 'manifest.m3u8'
    elif any(token in lowered for token in ['cf-master', '/v4/tab/', 'playlist', 'master', 'm3u8', 'manifest', 'vhls']):
        proxy_name = 'manifest.m3u8'
    elif '.mp4' in lowered or '.m4v' in lowered:
        proxy_name = 'video.mp4'
    elif '.ts' in lowered:
        proxy_name = 'segment.ts'

    forwarded_proto = request.headers.get('X-Forwarded-Proto', request.scheme or 'http')
    proxy_url = f"{forwarded_proto}://{host}/proxy/{proxy_name}?url={quote(target_url, safe='')}"
    if referer:
        proxy_url += f"&referer={quote(referer, safe='')}"
    if cookie:
        proxy_url += f"&cookie={quote(cookie, safe='')}"
    return proxy_url


def rewrite_playlist_content(content, base_url, host, referer='', cookie=''):
    rewritten_lines = []
    uri_pattern = re.compile(r'URI="([^"]+)"')

    for raw_line in content.splitlines():
        line = raw_line.strip()

        if not line:
            rewritten_lines.append(raw_line)
            continue

        if line.startswith('#'):
            def replace_uri(match):
                absolute = urljoin(base_url, match.group(1))
                return f'URI="{build_proxy_url(host, absolute, referer, cookie)}"'

            rewritten_lines.append(uri_pattern.sub(replace_uri, raw_line))
            continue

        absolute = urljoin(base_url, line)
        rewritten_lines.append(build_proxy_url(host, absolute, referer, cookie))

    return '\n'.join(rewritten_lines)


def extract_stream_url_from_json_payload(payload, base_url=''):
    candidates = []

    def visit(value):
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.startswith('http://') or normalized.startswith('https://'):
                candidates.append(normalized)
            elif normalized.startswith('/') and base_url:
                candidates.append(urljoin(base_url, normalized))
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    prioritized = sorted(candidates, key=stream_priority, reverse=True)
    return next((candidate for candidate in prioritized if looks_like_stream_url(candidate)), None)


def probe_stream_candidate(url, headers):
    if not url:
        return False

    provider = detect_provider(url)

    probe_headers = {
        'User-Agent': headers.get('User-Agent') or headers.get('user-agent') or 'Mozilla/5.0',
        'Referer': headers.get('Referer') or headers.get('referer') or '',
        'Accept': '*/*',
        'Connection': 'keep-alive',
    }
    cookie = headers.get('Cookie') or headers.get('cookie')
    if cookie:
        probe_headers['Cookie'] = cookie

    try:
        response = requests.get(url, headers=probe_headers, stream=True, timeout=12, verify=False)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '')
        preview = ''
        try:
            preview = response.raw.read(512, decode_content=True).decode('utf-8', errors='ignore')
        except Exception:
            preview = ''
        finally:
            response.close()

        if is_playlist_response(url, content_type, preview):
            log_provider(provider, f"probe accepted playlist content-type={content_type or 'unknown'} url={short_url(url)}")
            return True

        lowered_type = content_type.lower()
        if 'html' in lowered_type and not looks_like_hls_manifest_body(preview):
            log_provider(provider, f"probe rejected html fallback url={short_url(url)}")
            return False

        if 'application/json' in lowered_type:
            body = preview
            try:
                if not body or not body.strip().startswith(('{', '[')):
                    refill = requests.get(url, headers=probe_headers, timeout=12, verify=False)
                    refill.raise_for_status()
                    body = refill.text
                payload = json.loads(body)
                embedded_url = extract_stream_url_from_json_payload(payload, url)
                if embedded_url:
                    log_provider(provider, f"probe accepted json-embedded url={short_url(embedded_url)} source={short_url(url)}")
                    return True
            except Exception:
                pass

        accepted = lowered_type.startswith('video/') or 'octet-stream' in lowered_type
        log_provider(provider, f"probe {'accepted' if accepted else 'rejected'} content-type={content_type or 'unknown'} url={short_url(url)}")
        return accepted
    except Exception as exc:
        print(f"⚠️ Probe failed for stream candidate {url[:80]}...: {exc}")
        log_provider(provider, f"probe fallback heuristic={looks_like_stream_url(url)} url={short_url(url)}")
        return looks_like_stream_url(url)


def extract_subtitle_file(archive_bytes):
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        subtitle_names = [
            name for name in archive.namelist()
            if name.lower().endswith((".srt", ".vtt")) and not name.endswith("/")
        ]

        if not subtitle_names:
            return None, None

        preferred_name = sorted(
            subtitle_names,
            key=lambda name: (0 if name.lower().endswith(".vtt") else 1, len(name))
        )[0]
        raw_bytes = archive.read(preferred_name)

        for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
            try:
                return raw_bytes.decode(encoding), preferred_name
            except UnicodeDecodeError:
                continue

        return raw_bytes.decode("utf-8", errors="ignore"), preferred_name


def cache_subtitle_content(content, filename):
    token = uuid.uuid4().hex
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'srt'
    SUBTITLE_CACHE[token] = {
        "content": content,
        "mime": "text/vtt" if extension == "vtt" else "application/x-subrip",
    }
    return token, extension


def fetch_subtitles_from_source(url, headers=None):
    if not url:
        return []

    request_headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': '*/*',
    }
    for key, value in (headers or {}).items():
        if value:
            request_headers[key] = value

    try:
        response = requests.get(url, headers=request_headers, timeout=15, verify=False)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '')
        body = response.text

        subtitles = extract_subtitles_from_text(body, url)
        if subtitles:
            return subtitles

        if is_playlist_response(url, content_type, body):
            return extract_subtitles_from_m3u8(body, url)
    except Exception as exc:
        print(f"⚠️ Subtitle source scan failed for {short_url(url)}: {exc}")

    return []


def enrich_extracted_subtitles(stream_url, stream_headers, embed_url, existing_subtitles):
    collected = list(existing_subtitles or [])
    collected.extend(fetch_subtitles_from_source(embed_url, {'Referer': embed_url}))

    source_headers = {
        'Referer': stream_headers.get('Referer') or stream_headers.get('referer') or embed_url,
        'Cookie': stream_headers.get('Cookie') or stream_headers.get('cookie') or '',
    }
    collected.extend(fetch_subtitles_from_source(stream_url, source_headers))

    return dedupe_subtitles(collected)


def fetch_subtitles_from_subsource(tmdb_id, media_type, title=None, season=None, episode=None, languages="EN,AR", limit=6):
    if not SUBSOURCE_API_KEY or not title:
        return []

    headers = {
        'X-API-Key': SUBSOURCE_API_KEY,
        'Accept': 'application/json',
    }
    media_kind = 'series' if media_type == 'tv' else 'movie'
    search_params = {
        'searchType': 'text',
        'q': title,
        'type': media_kind,
    }
    if media_type == 'tv' and season is not None:
        search_params['season'] = season

    search_response = requests.get(
        'https://api.subsource.net/api/v1/movies/search',
        headers=headers,
        params=search_params,
        timeout=20,
    )
    search_response.raise_for_status()
    search_payload = search_response.json()
    search_results = search_payload.get('data') or []

    matched = next(
        (
            item for item in search_results
            if str(item.get('tmdbId') or '') == str(tmdb_id)
            and (item.get('type') == media_kind or media_type == 'tv')
            and (media_type != 'tv' or str(item.get('season') or season or '') == str(season or ''))
        ),
        None,
    )

    if not matched and search_results:
        matched = search_results[0]

    movie_id = matched.get('movieId') if matched else None
    if not movie_id:
        return []

    collected = []
    seen = set()
    for language in [normalize_subsource_language(item) for item in languages.split(',')]:
        if not language:
            continue

        subtitle_response = requests.get(
            'https://api.subsource.net/api/v1/subtitles',
            headers=headers,
            params={
                'movieId': movie_id,
                'language': language,
                'limit': limit,
            },
            timeout=20,
        )
        subtitle_response.raise_for_status()
        subtitle_payload = subtitle_response.json()

        for item in subtitle_payload.get('data') or []:
            subtitle_id = item.get('subtitleId')
            if not subtitle_id:
                continue

            release_info = item.get('releaseInfo') or []
            release_name = release_info[0] if release_info else f"SubSource {subtitle_id}"
            dedupe_key = (language, release_name)
            if dedupe_key in seen:
                continue

            try:
                download_response = requests.get(
                    f'https://api.subsource.net/api/v1/subtitles/{subtitle_id}/download',
                    headers=headers,
                    timeout=25,
                )
                download_response.raise_for_status()
                content, filename = extract_subtitle_file(download_response.content)
                if not content or not filename:
                    continue

                token, extension = cache_subtitle_content(content, filename)
                seen.add(dedupe_key)
                collected.append({
                    'language': language.title(),
                    'languageCode': normalize_language_code(language),
                    'releaseName': release_name,
                    'url': f"{request.host_url.rstrip('/')}/subtitle/{token}",
                    'format': extension,
                    'provider': 'subsource',
                    'season': season,
                    'episode': episode,
                })
            except Exception as exc:
                print(f"⚠️ SubSource download failed for {subtitle_id}: {exc}")

            if len(collected) >= limit:
                return collected

    return collected


def fetch_subtitles_from_subdl(tmdb_id, media_type, season=None, episode=None, languages="EN,AR", limit=6):
    params = {
        "api_key": SUBDL_API_KEY,
        "tmdb_id": tmdb_id,
        "type": media_type,
        "languages": languages,
        "subs_per_page": min(limit, 30),
    }

    if media_type == 'tv':
        if season is not None:
            params["season_number"] = season
        if episode is not None:
            params["episode_number"] = episode

    response = requests.get(
        f"https://api.subdl.com/api/v1/subtitles?{urlencode(params)}",
        headers={"Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    if not payload.get('status'):
        return []

    results = []
    for item in payload.get('subtitles', []):
        relative_url = item.get('url')
        if not relative_url:
            continue

        try:
            download_url = urljoin('https://dl.subdl.com', relative_url)
            archive_response = requests.get(download_url, timeout=20)
            archive_response.raise_for_status()

            content, filename = extract_subtitle_file(archive_response.content)
            if not content or not filename:
                continue

            token, extension = cache_subtitle_content(content, filename)
            results.append({
                "language": item.get('lang') or item.get('language') or 'Unknown',
                "languageCode": item.get('language') or 'UN',
                "releaseName": item.get('release_name') or item.get('name') or filename,
                "url": f"{request.host_url.rstrip('/')}/subtitle/{token}",
                "format": extension,
                "provider": 'subdl',
            })
        except Exception as exc:
            print(f"⚠️ Subtitle fetch failed for {relative_url}: {exc}")

        if len(results) >= limit:
            break

    return results

def extract_stream_with_playwright(url, preferred_quality='Auto'):
    provider = detect_provider(url)
    profile = get_provider_profile(provider)
    log_provider(provider, f"starting extraction quality={preferred_quality}")
    worker_order = list(profile.worker_order)
    
    BLOCK_DOMAINS = ["adscore", "dtscout", "doubleclick", "analytics", "clarity", "histats", "onclick", "popunder", "exoclick", "juicyads", "popcash", "jads.co"]
    SUCCESS_KEYWORDS = STREAM_HINT_KEYWORDS

    def is_url_video(u):
        if not u: return False
        return looks_like_stream_url(u)

    def is_high_priority_stream(u):
        lowered = (u or '').lower()
        return any(token in lowered for token in ['.m3u8', 'playlist', 'master', 'manifest', '/file2/', '/stream2/'])

    def try_extraction(is_mobile=False):
        mode_label = 'Mobile' if is_mobile else 'Desktop'
        session_cookie_key = get_session_cookie_key(provider, mode_label)
        local_streams = []
        local_subtitles = []
        local_winner = {"url": None, "headers": {}}
        capture_phase = 'target'

        def should_capture_candidates():
            if not profile.warmup_url:
                return True
            return capture_phase == 'target'

        def build_cookie_header(context):
            try:
                cookies = context.cookies()
                cookie_pairs = []
                for cookie_item in cookies:
                    name = cookie_item.get('name')
                    value = cookie_item.get('value')
                    if name and value is not None:
                        cookie_pairs.append(f"{name}={value}")
                cookie_str = "; ".join(cookie_pairs)
                log_provider(provider, f"[{mode_label}] cookie snapshot count={len(cookie_pairs)}")
                return cookie_str
            except Exception as exc:
                log_provider(provider, f"[{mode_label}] cookie snapshot failed error={exc}")
                return ''

        def finalize_result(context, browser, result, success_label):
            cookie_str = build_cookie_header(context)
            if profile.reuse_session:
                try:
                    PLAYWRIGHT_SESSION_COOKIES[session_cookie_key] = context.cookies()
                except Exception:
                    pass
            if result is not None:
                result.setdefault("headers", {})["Cookie"] = cookie_str
                result["subtitles"] = local_subtitles
                result["mode"] = mode_label.lower()
                print(f"✅ [{mode_label}] {success_label} ({result['url'][:40]}...)")
                log_provider(provider, f"[{mode_label}] returning stream url={short_url(result['url'])} subtitles={len(local_subtitles)} candidates={len(local_streams)}")

            try:
                browser.close()
                log_provider(provider, f"[{mode_label}] browser closed")
            except Exception as exc:
                log_provider(provider, f"[{mode_label}] browser close failed error={exc}")

            return result

        def remember_subtitle(candidate_url, label=''):
            if not should_capture_candidates():
                return
            resolved_url = resolve_candidate_url(candidate_url, page.url if 'page' in locals() else url)
            if not resolved_url or not looks_like_subtitle_url(resolved_url):
                return
            if any(s['url'] == resolved_url for s in local_subtitles):
                return

            language_name, language_code = infer_subtitle_language_from_label(label, resolved_url)
            print(f"🗨️ Captured Subtitle: {resolved_url[:40]}...")
            local_subtitles.append({
                "url": resolved_url,
                "language": language_name,
                "languageCode": language_code,
                "provider": "embed",
                "label": label or language_name,
            })

        def remember_stream(candidate_url, headers=None, force_winner=False):
            if not should_capture_candidates():
                return False
            if not candidate_url:
                return
            if should_skip_candidate(candidate_url, url):
                return

            normalized_headers = dict(headers or {})
            if profile.warmup_url:
                target_referer = page.url if 'page' in locals() and page.url and page.url != 'about:blank' else url
                normalized_headers['Referer'] = target_referer
                normalized_headers['referer'] = target_referer

            candidate = {"url": candidate_url, "headers": normalized_headers}
            is_new = not any(existing['url'] == candidate_url for existing in local_streams)
            if is_new:
                local_streams.append(candidate)
                source_referer = candidate['headers'].get('Referer') or candidate['headers'].get('referer') or 'n/a'
                log_provider(provider, f"captured candidate url={short_url(candidate_url)} referer={short_url(source_referer, 60)}")

            current_winner_score = stream_priority(local_winner.get('url'))
            candidate_score = stream_priority(candidate_url)
            should_promote = force_winner or (preferred_quality != 'Auto' and preferred_quality.replace('p', '') in candidate_url)

            if should_promote and candidate_score >= current_winner_score:
                local_winner.update(candidate)
                log_provider(provider, f"promoted winner url={short_url(candidate_url)} force={force_winner}")

            return is_high_priority_stream(candidate_url)

        def stop_page_loading():
            try:
                if 'page' in locals() and page and not page.is_closed():
                    page.evaluate("window.stop && window.stop()")
            except Exception as exc:
                message = str(exc).lower()
                if 'execution context was destroyed' in message or 'page closed' in message or 'target page, context or browser has been closed' in message:
                    return
                log_provider(provider, f"[{mode_label}] window.stop failed error={exc}")

        try:
            with sync_playwright() as p:
                ua = get_profile_user_agent(profile, is_mobile, url)
                
                log_provider(provider, f"[{mode_label}] launching chromium")
                use_headed_browser = profile.use_headed_browser and can_launch_headed_browser()
                launch_options = {
                    'headless': not use_headed_browser,
                    'chromium_sandbox': False,
                    'timeout': 45000,
                    'args': [
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-blink-features=AutomationControlled',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-software-rasterizer',
                    ],
                }
                if use_headed_browser:
                    log_provider(provider, f"[{mode_label}] using headed browser to avoid Cloudflare challenge")
                elif profile.use_headed_browser:
                    log_provider(provider, f"[{mode_label}] headed browser unavailable, staying headless")
                proxy_settings = build_playwright_proxy_settings()
                if proxy_settings:
                    launch_options['proxy'] = proxy_settings
                    log_provider(provider, f"[{mode_label}] using upstream proxy={short_url(proxy_settings['server'], 40)}")

                browser = p.chromium.launch(
                    **launch_options
                )
                log_provider(provider, f"[{mode_label}] chromium launched")
                
                context_args = {
                    "user_agent": ua,
                    "viewport": {'width': 390, 'height': 844} if is_mobile else {'width': 1280, 'height': 720},
                    "has_touch": is_mobile,
                    "is_mobile": is_mobile,
                    "ignore_https_errors": True,
                    "locale": "en-US",
                    "timezone_id": "America/New_York",
                    "extra_http_headers": {
                        "Accept-Language": "en-US,en;q=0.9",
                        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                        "sec-ch-ua-mobile": "?1" if is_mobile else "?0",
                        "sec-ch-ua-platform": '"iOS"' if is_mobile else '"Windows"',
                        "Upgrade-Insecure-Requests": "1"
                    }
                }
                context = browser.new_context(**context_args)
                log_provider(provider, f"[{mode_label}] browser context created")
                if profile.reuse_session:
                    stored_cookies = PLAYWRIGHT_SESSION_COOKIES.get(session_cookie_key, [])
                    if stored_cookies:
                        try:
                            context.add_cookies(stored_cookies)
                            log_provider(provider, f"[{mode_label}] restored cookies count={len(stored_cookies)}")
                        except Exception as exc:
                            log_provider(provider, f"[{mode_label}] cookie restore failed error={exc}")
                context.set_default_timeout(8000 if provider == '111movies' else 10000)
                context.set_default_navigation_timeout(8000 if provider == '111movies' else 10000)
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                    Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'mimeTypes', { get: () => [1, 2, 3] });
                    window.chrome = window.chrome || { runtime: {} };
                    window.Notification = window.Notification || { permission: 'default' };
                """)
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if not profile.allow_media_requests and route.request.resource_type in ["media"]
                    or any(domain in route.request.url.lower() for domain in BLOCK_DOMAINS)
                    else route.continue_()
                )
                page = context.new_page()
                log_provider(provider, f"[{mode_label}] page created")

                def inspect_possible_stream_url(candidate_url, headers=None, label=''):
                    if not candidate_url:
                        return
                    resolved_url = resolve_candidate_url(candidate_url, page.url if 'page' in locals() else url)
                    if not resolved_url:
                        return
                    if looks_like_subtitle_url(resolved_url):
                        remember_subtitle(resolved_url, label)
                    elif looks_like_stream_url(resolved_url):
                        remember_stream(resolved_url, headers)

                def handle_request(req):
                    r_url = req.url
                    # Record subtitles (VTT/SRT)
                    if any(x in r_url.lower() for x in ['.vtt', '.srt', 'subtitle', '/sub/']):
                        remember_subtitle(r_url)

                    if any(kw in r_url.lower() for kw in SUCCESS_KEYWORDS):
                        if is_url_video(r_url):
                            if remember_stream(r_url, dict(req.headers)):
                                stop_page_loading()

                def handle_response(response):
                    try:
                        r_url = response.url
                        content_type = (response.headers or {}).get('content-type', '')
                        lowered_type = content_type.lower()

                        if 'text/vtt' in lowered_type or 'subrip' in lowered_type or looks_like_subtitle_url(r_url):
                            remember_subtitle(r_url)

                        if 'application/json' in lowered_type and any(token in r_url.lower() for token in ['subtitle', 'track', 'caption', 'player', 'source']):
                            try:
                                payload = response.json()
                                for subtitle_url, label in extract_subtitle_candidates_from_json_payload(payload):
                                    remember_subtitle(subtitle_url, label)
                            except Exception:
                                pass

                        if provider == 'vidnest' and any(token in lowered_type for token in ['text/html', 'application/json', 'javascript']):
                            try:
                                body = response.text()
                                for subtitle in extract_subtitles_from_text(body, r_url):
                                    remember_subtitle(subtitle.get('url'), subtitle.get('label', ''))
                                for match in re.findall(r'https?://[^"\'\s<>()]+', body or ''):
                                    inspect_possible_stream_url(match, {"Referer": r_url})
                            except Exception:
                                pass

                        if is_playlist_response(r_url, content_type) or content_type.lower().startswith('video/'):
                            headers = dict(response.request.headers)
                            headers['Accept'] = headers.get('Accept', '*/*')
                            if remember_stream(r_url, headers, force_winner=is_playlist_response(r_url, content_type)):
                                stop_page_loading()
                    except Exception:
                        pass

                def handle_frame_navigation(frame):
                    try:
                        frame_url = frame.url
                        if not frame_url or frame_url == 'about:blank':
                            return
                        inspect_possible_stream_url(frame_url, {"Referer": page.url})
                    except Exception:
                        pass

                def handle_frame_attached(frame):
                    try:
                        frame.wait_for_load_state('domcontentloaded', timeout=4000)
                    except Exception:
                        pass
                    try:
                        inspect_possible_stream_url(frame.url, {"Referer": page.url})
                        html = frame.content()
                        for match in re.findall(r'https?://[^"\'\s<>()]+', html or ''):
                            inspect_possible_stream_url(match, {"Referer": frame.url or page.url})
                    except Exception:
                        pass

                def handle_popup(popup):
                    try:
                        popup.wait_for_load_state('domcontentloaded', timeout=5000)
                    except Exception:
                        pass
                    try:
                        inspect_possible_stream_url(popup.url, {"Referer": page.url})
                    except Exception:
                        pass

                page.on("request", handle_request)
                page.on("response", handle_response)
                page.on("framenavigated", handle_frame_navigation)
                page.on("frameattached", handle_frame_attached)
                page.on("popup", handle_popup)

                def interact():
                    selectors = [
                        "button", ".play-button", "video", ".v-la11", "iframe", "[aria-label*='Play' i]", "main", "canvas",
                        ".fluid_control_playpause_big_circle", ".fluid_initial_play", ".fluid_initial_play_button",
                        ".fluid_button.fluid_control_playpause", ".mainplayer", ".video-container", ".fluid_video_wrapper"
                    ]
                    for frame in page.frames:
                        try:
                            for s in selectors:
                                frame.evaluate(f"""
                                    (() => {{
                                        const el = document.querySelector('{s}');
                                        if (!el) return;
                                        try {{ el.click(); }} catch (e) {{}}
                                        try {{
                                            ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'touchstart', 'touchend'].forEach((eventName) => {{
                                                el.dispatchEvent(new Event(eventName, {{ bubbles: true, cancelable: true }}));
                                            }});
                                        }} catch (e) {{}}
                                    }})()
                                """)
                        except: pass
                    try:
                        for x, y in [(640, 360), (640, 330), (640, 390), (560, 360), (720, 360)]:
                            page.mouse.click(x, y)
                    except: pass
                    try:
                        page.keyboard.press("Space")
                        page.keyboard.press("Enter")
                    except: pass

                def wait_for_player_iframe():
                    if not profile.iframe_selectors or profile.iframe_wait_timeout_ms <= 0:
                        return
                    for selector in profile.iframe_selectors:
                        try:
                            page.wait_for_selector(selector, timeout=profile.iframe_wait_timeout_ms)
                            log_provider(provider, f"[{mode_label}] iframe detected selector={selector}")
                            break
                        except Exception:
                            continue

                def wait_for_challenge_clear(label):
                    try:
                        for attempt in range(1, profile.challenge_attempts + 1):
                            if is_url_video(local_winner.get("url")) or any(is_url_video(item.get('url')) for item in local_streams):
                                log_provider(provider, f"challenge short-circuited mode={label} attempt={attempt} winner={short_url(local_winner.get('url'))}")
                                return

                            html = page.content()
                            current_page_url = page.url
                            if not is_challenge_page(html, current_page_url):
                                if attempt > 1:
                                    log_provider(provider, f"challenge cleared mode={label} attempts={attempt} url={short_url(current_page_url)}")
                                return

                            if attempt == 1:
                                print(f"🛡️ Solving Challenge ({'Mobile' if is_mobile else 'Desktop'})...")
                                log_provider(provider, f"challenge detected mode={label} url={short_url(current_page_url)}")

                            interact()
                            try:
                                page.mouse.move(320, 240)
                                page.mouse.wheel(0, 400)
                            except Exception:
                                pass
                            page.wait_for_timeout(profile.challenge_wait_ms)

                        log_provider(provider, f"challenge persisted mode={label} final_url={short_url(page.url)}")
                    except Exception as exc:
                        log_provider(provider, f"challenge wait error mode={label} error={exc}")

                try:
                    if profile.warmup_url:
                        log_provider(provider, f"[{mode_label}] warmup navigation start url={short_url(profile.warmup_url)}")
                        capture_phase = 'warmup'
                        page.goto(profile.warmup_url, wait_until='domcontentloaded', timeout=profile.warmup_timeout_ms); page.wait_for_timeout(500)
                        log_provider(provider, f"[{mode_label}] warmup navigation done url={short_url(page.url)}")
                        wait_for_challenge_clear('warmup')
                        local_streams.clear()
                        local_subtitles.clear()
                        local_winner.update({"url": None, "headers": {}})
                        log_provider(provider, f"[{mode_label}] cleared warmup candidates before target navigation")

                    capture_phase = 'target'
                    log_provider(provider, f"[{mode_label}] target navigation start url={short_url(url)}")
                    page.goto(url, wait_until='domcontentloaded', timeout=profile.target_timeout_ms)
                    log_provider(provider, f"[{mode_label}] target navigation done url={short_url(page.url)}")
                    if profile.pre_capture_wait_ms > 0:
                        page.wait_for_timeout(profile.pre_capture_wait_ms)
                    wait_for_player_iframe()
                    page.wait_for_timeout(250)
                    wait_for_challenge_clear('target')
                except: pass

                if is_url_video(local_winner["url"]):
                    log_provider(provider, f"[{mode_label}] early winner after navigation")
                    return finalize_result(context, browser, local_winner, "Success: Early Winner")

                def scan_dom_for_sources():
                    try:
                        discovered = page.evaluate("""
                            () => {
                                const found = [];
                                const attrs = ['src', 'href', 'data-src', 'data-url'];
                                const absolutize = (value) => {
                                    try {
                                        return new URL(value, window.location.href).toString();
                                    } catch (e) {
                                        return value;
                                    }
                                };
                                const pushFound = (value, label = '') => {
                                    if (!value) return;
                                    found.push({ url: absolutize(value), label });
                                };
                                for (const el of document.querySelectorAll('video, source, iframe, a, [src], [href], [data-src], [data-url]')) {
                                    for (const attr of attrs) {
                                        const value = el.getAttribute(attr);
                                        if (value && !/^javascript:/i.test(value) && !/^data:/i.test(value)) {
                                            pushFound(value, el.getAttribute('label') || el.getAttribute('srclang') || el.getAttribute('lang') || '');
                                        }
                                    }
                                }
                                for (const el of document.querySelectorAll('track[kind="subtitles"], track[kind="captions"], [data-subtitle], [data-track]')) {
                                    const value = el.getAttribute('src') || el.getAttribute('data-subtitle') || el.getAttribute('data-track') || el.getAttribute('data-src');
                                    if (value && !/^javascript:/i.test(value) && !/^data:/i.test(value)) {
                                        pushFound(value, el.getAttribute('label') || el.getAttribute('srclang') || el.getAttribute('lang') || '');
                                    }
                                }
                                const configs = [];
                                for (const key of ['__NEXT_DATA__', '__data', 'playerConfig', 'playerData', 'streamData', 'sources', 'source']) {
                                    try {
                                        const value = window[key];
                                        if (typeof value === 'string') pushFound(value);
                                        if (Array.isArray(value)) {
                                            value.forEach((item) => {
                                                if (typeof item === 'string') pushFound(item);
                                                else if (item && typeof item === 'object') {
                                                    ['file', 'src', 'url', 'playlist', 'manifest', 'hls'].forEach((prop) => pushFound(item[prop], item.label || item.srclang || ''));
                                                }
                                            });
                                        } else if (value && typeof value === 'object') {
                                            ['file', 'src', 'url', 'playlist', 'manifest', 'hls'].forEach((prop) => pushFound(value[prop]));
                                        }
                                    } catch (e) {}
                                }
                                for (const key of Object.keys(window)) {
                                    try {
                                        const value = window[key];
                                        if (value && typeof value === 'object') configs.push(value);
                                    } catch (e) {}
                                }
                                for (const value of configs) {
                                    try {
                                        if (Array.isArray(value.sources)) {
                                            value.sources.forEach((item) => {
                                                if (typeof item === 'string') pushFound(item);
                                                else if (item && typeof item === 'object') {
                                                    ['file', 'src', 'url', 'playlist', 'manifest', 'hls'].forEach((prop) => pushFound(item[prop], item.label || item.srclang || ''));
                                                }
                                            });
                                        }
                                        ['file', 'src', 'url', 'playlist', 'manifest', 'hls'].forEach((prop) => pushFound(value[prop]));
                                    } catch (e) {}
                                }
                                const html = document.documentElement?.outerHTML || '';
                                const matches = html.match(/https?:\\/\\/[^\"'\\s<>()]+/g) || [];
                                for (const item of matches) pushFound(item);
                                const encodedMatches = html.match(/[A-Za-z0-9+/=]{20,}/g) || [];
                                for (const item of encodedMatches) {
                                    try {
                                        const decoded = atob(item);
                                        if (/https?:\\/\\//i.test(decoded)) {
                                            const decodedUrls = decoded.match(/https?:\\/\\/[^"'\\s<>()]+/g) || [];
                                            decodedUrls.forEach((entry) => pushFound(entry));
                                        }
                                    } catch (e) {}
                                }
                                return found;
                            }
                        """)
                        for item in discovered:
                            candidate_url = item.get('url') if isinstance(item, dict) else item
                            label = item.get('label', '') if isinstance(item, dict) else ''
                            if looks_like_subtitle_url(candidate_url):
                                remember_subtitle(candidate_url, label)
                            elif looks_like_stream_url(candidate_url):
                                remember_stream(candidate_url, {"Referer": url})
                        for frame in page.frames:
                            try:
                                inspect_possible_stream_url(frame.url, {"Referer": page.url})
                            except Exception:
                                pass
                    except Exception:
                        pass

                interact(); page.wait_for_timeout(250)
                scan_dom_for_sources()

                if is_url_video(local_winner["url"]):
                    log_provider(provider, f"[{mode_label}] winner confirmed after DOM scan")
                    return finalize_result(context, browser, local_winner, "Success: Captured Quality")
                
                # Check discovery loop
                for attempt in range(profile.discovery_loops):
                    if is_url_video(local_winner["url"]): break
                    if any(is_url_video(item.get('url')) for item in local_streams):
                        break
                    page.wait_for_timeout(profile.discovery_delay_ms)
                    if profile.force_interact_each_loop or not local_streams:
                        interact()
                    if attempt in set(profile.extra_scroll_attempts):
                        try:
                            page.mouse.move(640, 360)
                            page.mouse.wheel(0, 300)
                        except Exception:
                            pass
                    scan_dom_for_sources()

                if not local_streams:
                    log_provider(provider, f"[{mode_label}] no streams after loop, scanning page HTML fallback")
                    m3u8_match = re.search(r'(https?://[^"\']+(?:m3u8|mp4|json|txt|playlist|master|manifest)[^"\']*)', page.content())
                    if m3u8_match and is_url_video(m3u8_match.group(1)):
                        remember_stream(m3u8_match.group(1), {"Referer": url}, force_winner=True)

                result = None
                if is_url_video(local_winner["url"]):
                    result = local_winner
                else:
                    valid_streams = [s for s in local_streams if is_url_video(s['url'])]
                    if valid_streams:
                        result = max(valid_streams, key=lambda stream: stream_priority(stream['url']))

                if not result and local_winner.get("url"):
                    result = {
                        "url": local_winner["url"],
                        "headers": {**local_winner.get("headers", {})},
                    }

                if result:
                    label = "Success: Captured Quality" if result is local_winner else "Success: Captured Valid Stream"
                    return finalize_result(context, browser, result, label)

                try:
                    browser.close()
                    log_provider(provider, f"[{mode_label}] browser closed without result")
                except Exception as exc:
                    log_provider(provider, f"[{mode_label}] browser close failed without result error={exc}")
                log_provider(provider, f"[{mode_label}] no playable stream found subtitles={len(local_subtitles)} candidates={len(local_streams)}")
        except Exception as e:
            print(f"❌ Playwright Error ({'Mobile' if is_mobile else 'Desktop'}): {e}")
            log_provider(provider, f"playwright exception mode={'mobile' if is_mobile else 'desktop'} error={e}")
        return None

    for index, (label, is_mobile) in enumerate(worker_order):
        if index == 0:
            log_provider(provider, f"launching {label.lower()} worker")
        else:
            log_provider(provider, f"{worker_order[index - 1][0].lower()} worker failed, launching {label.lower()} worker")

        result = try_extraction(is_mobile=is_mobile)
        if result and result.get('url'):
            result["success"] = True
            log_provider(provider, f"worker {label} WON with url={short_url(result['url'])}")
            return result

    log_provider(provider, "both workers finished without a direct stream")
    return {"url": None, "headers": {}, "success": False, "subtitles": []}

@app.route('/proxy')
@app.route('/proxy/<path:_name>')
def proxy(_name=None):
    url = request.args.get('url')
    if not url: return "No URL", 400
    provider = detect_provider(request.args.get('referer') or url)
    
    referer = request.args.get('referer', '')
    cookie = request.args.get('cookie', '')
    
    # 1. Build Extractor-Friendly Headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Referer': referer,
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }
    if cookie: headers['Cookie'] = cookie
    
    range_header = request.headers.get('Range', None)
    if range_header: headers['Range'] = range_header

    try:
        # 2. Fetch the stream from the provider
        req = requests.get(url, headers=headers, stream=True, timeout=20, verify=False)
        req.raise_for_status()
        
        # 3. Force Correct MIME Type (Fixes a8.W error)
        content_type = req.headers.get('Content-Type', '')
        if is_playlist_response(url, content_type):
            content_type = 'application/vnd.apple.mpegurl'
        elif '.mp4' in url.lower():
            content_type = 'video/mp4'
        elif not content_type:
            content_type = 'video/MP2T' # Default for .ts segments

        resp_headers = {
            'Content-Type': content_type,
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'bytes'
        }
        if 'Content-Range' in req.headers: resp_headers['Content-Range'] = req.headers['Content-Range']
        log_provider(provider, f"proxy fetch status={req.status_code} type={content_type or 'unknown'} url={short_url(url)}")

        if is_playlist_response(url, content_type):
            playlist_body = req.text
            rewritten_playlist = rewrite_playlist_content(playlist_body, url, request.host, referer, cookie)
            log_provider(provider, f"proxy rewrote playlist lines={len(rewritten_playlist.splitlines())} url={short_url(url)}")
            return Response(rewritten_playlist, req.status_code, resp_headers.items())

        if 'application/json' in content_type.lower():
            payload = req.json()
            embedded_url = extract_stream_url_from_json_payload(payload, url)
            if embedded_url:
                log_provider(provider, f"proxy resolved json stream target={short_url(embedded_url)} source={short_url(url)}")
                embedded_response = requests.get(embedded_url, headers=headers, stream=True, timeout=20, verify=False)
                embedded_response.raise_for_status()
                embedded_type = embedded_response.headers.get('Content-Type', '')
                embedded_headers = {
                    'Content-Type': 'application/vnd.apple.mpegurl' if is_playlist_response(embedded_url, embedded_type) else (embedded_type or 'video/MP2T'),
                    'Access-Control-Allow-Origin': '*',
                    'Cache-Control': 'no-cache',
                    'Accept-Ranges': 'bytes'
                }
                if 'Content-Range' in embedded_response.headers:
                    embedded_headers['Content-Range'] = embedded_response.headers['Content-Range']

                if is_playlist_response(embedded_url, embedded_type):
                    embedded_playlist = embedded_response.text
                    rewritten_playlist = rewrite_playlist_content(embedded_playlist, embedded_url, request.host, referer, cookie)
                    return Response(rewritten_playlist, embedded_response.status_code, embedded_headers.items())

                return Response(embedded_response.iter_content(chunk_size=256*1024), embedded_response.status_code, embedded_headers.items())

        return Response(req.iter_content(chunk_size=256*1024), req.status_code, resp_headers.items())
    except Exception as e:
        print(f"❌ Proxy Fail: {e}")
        log_provider(provider, f"proxy failure error={e} url={short_url(url)}")
        return str(e), 500


def resolve_provider_candidate(candidate_url, preferred_quality):
    provider = detect_provider(candidate_url)
    log_provider(provider, f"attempting candidate quality={preferred_quality} url={short_url(candidate_url)}")

    extraction_url = candidate_url
    extracted = extract_stream_with_playwright(extraction_url, preferred_quality)

    if not extracted.get('url'):
        for fallback_url in get_provider_fallback_urls(candidate_url):
            log_provider(provider, f"trying fallback url={short_url(fallback_url)} source={short_url(candidate_url)}")
            extracted = extract_stream_with_playwright(fallback_url, preferred_quality)
            if extracted.get('url'):
                extraction_url = fallback_url
                break

    curr_url = extracted.get('url')
    curr_headers = extracted.get('headers', {})
    extracted['subtitles'] = enrich_extracted_subtitles(curr_url, curr_headers, extraction_url, extracted.get('subtitles', []))

    is_valid_video = probe_stream_candidate(curr_url, curr_headers)
    if not is_valid_video:
        for fallback_url in get_provider_fallback_urls(candidate_url):
            if fallback_url == extraction_url:
                continue
            log_provider(provider, f"validation fallback url={short_url(fallback_url)} source={short_url(candidate_url)}")
            fallback_extracted = extract_stream_with_playwright(fallback_url, preferred_quality)
            fallback_curr_url = fallback_extracted.get('url')
            fallback_curr_headers = fallback_extracted.get('headers', {})
            if not probe_stream_candidate(fallback_curr_url, fallback_curr_headers):
                continue
            extraction_url = fallback_url
            extracted = fallback_extracted
            curr_url = fallback_curr_url
            curr_headers = fallback_curr_headers
            extracted['subtitles'] = enrich_extracted_subtitles(curr_url, curr_headers, extraction_url, fallback_extracted.get('subtitles', []))
            is_valid_video = True
            break

    if not is_valid_video:
        log_provider(provider, f"validation failed extracted={short_url(curr_url)}")
        return None

    prov_keywords = ['workers.dev', 'vidrock', 'vidfast', 'vidnest', 'vhls', 'm3u8-proxy', '111movies']
    should_proxy = any(kw in (curr_url or '').lower() for kw in prov_keywords) or any(kw in extraction_url.lower() for kw in prov_keywords)
    referer = curr_headers.get('referer', '') or curr_headers.get('Referer', '') or extraction_url
    cookie = curr_headers.get('Cookie', '') or curr_headers.get('cookie', '')

    response_payload = {
        "success": True,
        "url": build_proxy_url(request.host, curr_url, referer, cookie) if should_proxy else curr_url,
        "headers": curr_headers,
        "subtitles": extracted.get('subtitles', []),
        "provider": detect_provider(extraction_url),
        "sourceUrl": extraction_url,
    }

    if should_proxy:
        log_provider(provider, f"proxying extracted={short_url(curr_url)}")

    return response_payload


def parse_requested_providers(payload, query_args):
    providers = []

    if isinstance(payload, dict):
        requested = payload.get('providers')
        if isinstance(requested, list):
            providers.extend(requested)
        elif isinstance(requested, str):
            providers.extend(requested.split(','))

    query_value = query_args.get('providers', '')
    if query_value:
        providers.extend(query_value.split(','))

    normalized = []
    for provider in providers:
        lowered = (provider or '').strip().lower()
        if lowered in PROVIDER_ORDER and lowered not in normalized:
            normalized.append(lowered)
    return normalized[:5]


def run_provider_race(url, preferred_quality, requested_providers):
    candidate_urls = expand_provider_urls(url, requested_providers)
    attempt_details = []

    log_provider(detect_provider(url), f"race-to-stream start candidates={len(candidate_urls)} quality={preferred_quality}")

    for index, candidate_url in enumerate(candidate_urls, start=1):
        provider = detect_provider(candidate_url)
        started_at = time.time()
        log_provider(provider, f"race attempt={index}/{len(candidate_urls)} url={short_url(candidate_url)}")

        resolved_payload = resolve_provider_candidate(candidate_url, preferred_quality)
        elapsed_ms = int((time.time() - started_at) * 1000)

        attempt_record = {
            "provider": provider,
            "sourceUrl": candidate_url,
            "elapsedMs": elapsed_ms,
            "success": bool(resolved_payload),
        }
        attempt_details.append(attempt_record)

        if resolved_payload:
            resolved_payload["attempts"] = attempt_details
            resolved_payload["raceMode"] = "sequential-first-success"
            resolved_payload["requestedProviders"] = requested_providers or [item["provider"] for item in attempt_details]
            log_provider(provider, f"race winner attempt={index} elapsedMs={elapsed_ms}")
            return resolved_payload, attempt_details

        log_provider(provider, f"race miss attempt={index} elapsedMs={elapsed_ms}")

    return None, attempt_details

@app.route('/resolve', methods=['POST', 'GET'])
def resolve():
    try:
        data = {}
        preferred_quality = 'Auto'
        if request.method == 'POST':
            data = request.json or {}
            url = data.get('url')
            preferred_quality = data.get('quality', 'Auto')
        else:
            url = request.args.get('url')
            preferred_quality = request.args.get('quality', 'Auto')

        if not url:
            return jsonify({"success": False, "error": "No url provided"}), 400

        requested_provider = detect_provider(url)
        requested_providers = parse_requested_providers(data, request.args)
        log_provider(requested_provider, f"resolve request quality={preferred_quality} url={short_url(url)}")

        cache_key = build_stream_cache_key(url, preferred_quality, requested_providers)
        cached_payload = get_cached_stream_result(cache_key)
        if cached_payload:
            log_provider(requested_provider, f"cache hit quality={preferred_quality} url={short_url(url)}")
            return jsonify(cached_payload)

        resolved_payload, attempt_details = run_provider_race(url, preferred_quality, requested_providers)
        if resolved_payload:
            set_cached_stream_result(cache_key, resolved_payload)
            if len(attempt_details) > 1:
                log_provider(detect_provider(resolved_payload.get('sourceUrl') or url), f"fallback success attempts={len(attempt_details)}")
            return jsonify(resolved_payload)

        log_provider(requested_provider, f"all provider attempts failed count={len(attempt_details)}")
        return jsonify({
            "success": False,
            "error": "Failed to extract raw video. Bot protection detected.",
            "attemptedProviders": [item["provider"] for item in attempt_details],
            "attempts": attempt_details,
            "raceMode": "sequential-first-success",
        }), 400
    except Exception as e:
        print(f"🔥 Critical Resolve Error: {e}")
        log_provider(detect_provider(request.args.get('url') or ''), f"resolve exception error={e}")
        return jsonify({"success": False, "error": f"Internal Server Error: {str(e)}"}), 500


@app.route('/subtitles', methods=['GET'])
def subtitles():
    embed_url = request.args.get('embedUrl')
    tmdb_id = request.args.get('tmdbId')
    media_type = request.args.get('type', 'movie')
    season = request.args.get('season')
    episode = request.args.get('episode')
    languages = request.args.get('languages', 'EN,AR')
    title = request.args.get('title')

    if embed_url:
        try:
            subtitles = dedupe_subtitles(fetch_subtitles_from_source(embed_url, {'Referer': embed_url}))
            return jsonify({
                "success": True,
                "provider": 'embed',
                "subtitles": subtitles,
            })
        except Exception as exc:
            print(f"❌ Embed subtitle lookup failed: {exc}")
            return jsonify({"success": False, "error": str(exc), "subtitles": []}), 500

    if not tmdb_id:
        return jsonify({"success": False, "error": "tmdbId or embedUrl is required", "subtitles": []}), 400

    try:
        subtitle_results = []

        subsource_results = fetch_subtitles_from_subsource(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            season=season,
            episode=episode,
            languages=languages,
        )
        subtitle_results.extend(subsource_results)

        subdl_results = fetch_subtitles_from_subdl(
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            languages=languages,
        )
        subtitle_results.extend(subdl_results)

        deduped = []
        seen = set()
        for item in subtitle_results:
            key = (item.get('languageCode'), item.get('releaseName'), item.get('provider'))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return jsonify({
            "success": True,
            "provider": 'combined',
            "subsourceConfigured": bool(SUBSOURCE_API_KEY),
            "subtitles": deduped,
        })
    except Exception as exc:
        print(f"❌ Subtitle lookup failed: {exc}")
        return jsonify({"success": False, "error": str(exc), "subtitles": []}), 500


@app.route('/subtitle/<token>', methods=['GET'])
def subtitle_content(token):
    cached = SUBTITLE_CACHE.get(token)
    if not cached:
        return jsonify({"success": False, "error": "Subtitle not found"}), 404

    return Response(cached['content'], mimetype=cached['mime'])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
