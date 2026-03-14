import re
from urllib.parse import urljoin


def normalize_subsource_language(language):
    mapping = {
        'EN': 'english',
        'AR': 'arabic',
        'ENGLISH': 'english',
        'ARABIC': 'arabic',
    }
    return mapping.get((language or '').strip().upper())


def normalize_language_code(language_name):
    lowered = (language_name or '').strip().lower()
    if lowered == 'english':
        return 'EN'
    if lowered == 'arabic':
        return 'AR'
    return (language_name or 'UN')[:2].upper()


def infer_subtitle_language_from_url(url):
    lowered = (url or '').lower()
    if any(token in lowered for token in ['arabic', '/ar/', '_ar', '-ar', '.ar.', 'lang=ar']):
        return 'Arabic', 'AR'
    if any(token in lowered for token in ['english', '/en/', '_en', '-en', '.en.', 'lang=en']):
        return 'English', 'EN'
    return 'Default', 'UN'


def infer_subtitle_language_from_code(code):
    normalized = (code or '').strip().lower().replace('_', '-').split('-')[0]
    mapping = {
        'en': ('English', 'EN'),
        'ar': ('Arabic', 'AR'),
        'es': ('Spanish', 'ES'),
        'fr': ('French', 'FR'),
        'de': ('German', 'DE'),
        'it': ('Italian', 'IT'),
        'pt': ('Portuguese', 'PT'),
        'tr': ('Turkish', 'TR'),
        'ru': ('Russian', 'RU'),
        'hi': ('Hindi', 'HI'),
        'id': ('Indonesian', 'ID'),
        'ms': ('Malay', 'MS'),
        'th': ('Thai', 'TH'),
        'vi': ('Vietnamese', 'VI'),
        'ko': ('Korean', 'KO'),
        'ja': ('Japanese', 'JA'),
        'zh': ('Chinese', 'ZH'),
        'pl': ('Polish', 'PL'),
        'nl': ('Dutch', 'NL'),
        'sv': ('Swedish', 'SV'),
        'no': ('Norwegian', 'NO'),
        'da': ('Danish', 'DA'),
        'fi': ('Finnish', 'FI'),
        'uk': ('Ukrainian', 'UK'),
        'fa': ('Persian', 'FA'),
        'he': ('Hebrew', 'HE'),
    }
    return mapping.get(normalized)


def infer_subtitle_language_from_label(label, fallback_url=''):
    lowered = (label or '').strip().lower()
    code_guess = infer_subtitle_language_from_code(lowered)
    if code_guess:
        return code_guess
    if any(token in lowered for token in ['arabic', ' arabic', ' ar', '[ar]', '(ar)', 'ara']):
        return 'Arabic', 'AR'
    if any(token in lowered for token in ['english', ' eng', ' en', '[en]', '(en)']):
        return 'English', 'EN'
    if any(token in lowered for token in ['spanish', ' esp', ' es', '[es]', '(es)', 'spa']):
        return 'Spanish', 'ES'
    if any(token in lowered for token in ['french', ' fr', '[fr]', '(fr)', 'fre', 'fra']):
        return 'French', 'FR'
    if any(token in lowered for token in ['german', ' de', '[de]', '(de)', 'ger', 'deu']):
        return 'German', 'DE'
    return infer_subtitle_language_from_url(fallback_url)


def looks_like_subtitle_url(url):
    lowered = (url or '').lower()
    if any(lowered.endswith(ext) for ext in ['.js', '.mjs', '.css', '.map']):
        return False
    if '/ui/subtitles/' in lowered or '/utils/subtitle' in lowered or '/options/defaults/' in lowered:
        return False
    return any(token in lowered for token in ['.vtt', '.srt', '.ass', '.ssa', 'subtitle', '/sub/', 'captions', 'texttrack'])


def normalize_subtitle_entry(candidate_url, label='', provider='embed'):
    resolved_url = (candidate_url or '').strip()
    if not resolved_url:
        return None

    language_name, language_code = infer_subtitle_language_from_label(label, resolved_url)
    return {
        'url': resolved_url,
        'language': language_name,
        'languageCode': language_code,
        'provider': provider,
        'label': label or language_name,
    }


def dedupe_subtitles(items):
    deduped = []
    seen = set()

    for item in items or []:
        if not item or not item.get('url'):
            continue

        key = item['url'].strip()
        if key in seen:
            continue

        seen.add(key)
        deduped.append(item)

    return deduped


def extract_subtitles_from_text(content, base_url=''):
    if not content:
        return []

    candidates = []
    absolute_pattern = re.compile(r'https?://[^"\'\s<>()]+?(?:\.vtt|\.srt|\.ass|\.ssa)(?:\?[^"\'\s<>()]*)?', re.IGNORECASE)
    relative_pattern = re.compile(r'(["\'])(/[^"\'\s<>()]+?(?:\.vtt|\.srt|\.ass|\.ssa)(?:\?[^"\'\s<>()]*)?)\1', re.IGNORECASE)
    track_pattern = re.compile(
        r'<track[^>]+src=["\']([^"\']+)["\'][^>]*?(?:label=["\']([^"\']*)["\'])?[^>]*?>',
        re.IGNORECASE,
    )
    loose_track_pattern = re.compile(
        r'(?:file|src|url)\s*[:=]\s*["\']([^"\']+?(?:\.vtt|\.srt|\.ass|\.ssa)(?:\?[^"\']*)?)["\'][^\n\r{}]*?(?:label|language|lang|srclang)?\s*[:=]?\s*["\']?([^"\',}\]]*)',
        re.IGNORECASE,
    )

    for match in absolute_pattern.finditer(content):
        candidates.append(normalize_subtitle_entry(match.group(0), provider='embed'))

    if base_url:
        for match in relative_pattern.finditer(content):
            candidates.append(normalize_subtitle_entry(urljoin(base_url, match.group(2)), provider='embed'))

    for match in track_pattern.finditer(content):
        candidate_url = resolve_candidate_url(match.group(1), base_url)
        label = match.group(2) or ''
        if candidate_url:
            candidates.append(normalize_subtitle_entry(candidate_url, label=label, provider='embed'))

    for match in loose_track_pattern.finditer(content):
        candidate_url = resolve_candidate_url(match.group(1), base_url)
        label = (match.group(2) or '').strip(' :,-')
        if candidate_url:
            candidates.append(normalize_subtitle_entry(candidate_url, label=label, provider='embed'))

    return dedupe_subtitles(candidates)


def extract_subtitles_from_m3u8(content, manifest_url):
    if not content or '#EXTM3U' not in content:
        return []

    subtitles = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith('#EXT-X-MEDIA:') or 'TYPE=SUBTITLES' not in line.upper():
            continue

        uri_match = re.search(r'URI="([^"]+)"', line, re.IGNORECASE)
        if not uri_match:
            continue

        label_match = re.search(r'NAME="([^"]+)"', line, re.IGNORECASE)
        language_match = re.search(r'LANGUAGE="([^"]+)"', line, re.IGNORECASE)
        candidate_url = urljoin(manifest_url, uri_match.group(1))
        label = (label_match.group(1) if label_match else '') or (language_match.group(1) if language_match else '')
        subtitles.append(normalize_subtitle_entry(candidate_url, label=label, provider='embed'))

    return dedupe_subtitles(subtitles)


def extract_subtitle_candidates_from_json_payload(payload):
    candidates = []

    def visit(value, context_label=''):
        if isinstance(value, dict):
            url_value = None
            for key in ['file', 'src', 'url', 'track', 'subtitle', 'subtitleUrl']:
                candidate = value.get(key)
                if isinstance(candidate, str) and looks_like_subtitle_url(candidate):
                    url_value = candidate
                    break

            if url_value:
                label = value.get('label') or value.get('language') or value.get('lang') or context_label
                candidates.append((url_value, label or ''))

            for key, nested in value.items():
                next_label = context_label
                if key.lower() in ['label', 'language', 'lang', 'name'] and isinstance(nested, str):
                    next_label = nested
                visit(nested, next_label)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, context_label)

    visit(payload)
    return candidates


def resolve_candidate_url(candidate_url, base_url):
    if not candidate_url:
        return None
    if isinstance(candidate_url, str) and candidate_url.startswith(('http://', 'https://')):
        return candidate_url
    try:
        return urljoin(base_url, candidate_url)
    except Exception:
        return candidate_url
