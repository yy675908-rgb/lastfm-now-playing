import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path


class RecentTrackParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_recent_section = False
        self.section_depth = 0
        self.in_first_row = False
        self.row_depth = 0
        self.row_found = False
        self.row_classes = set()
        self.current_field = None
        self.fields = {
            'track': [],
            'artist': [],
            'album': [],
            'timestamp': [],
        }
        self.track_url = ''
        self.image = ''

    @staticmethod
    def _attrs(attrs):
        return {key: value or '' for key, value in attrs}

    def handle_starttag(self, tag, attrs):
        attrs = self._attrs(attrs)
        classes = set(attrs.get('class', '').split())

        if tag == 'section' and attrs.get('id') == 'recent-tracks-section':
            self.in_recent_section = True
            self.section_depth = 1
            return

        if self.in_recent_section and tag == 'section':
            self.section_depth += 1

        if self.in_recent_section and not self.row_found and tag == 'tr' and 'chartlist-row' in classes:
            self.in_first_row = True
            self.row_depth = 1
            self.row_classes = classes
            return

        if self.in_first_row:
            if tag == 'tr':
                self.row_depth += 1

            if tag == 'td':
                if 'chartlist-name' in classes:
                    self.current_field = 'track'
                elif 'chartlist-artist' in classes:
                    self.current_field = 'artist'
                elif 'chartlist-album' in classes:
                    self.current_field = 'album'
                elif 'chartlist-timestamp' in classes:
                    self.current_field = 'timestamp'

            if tag == 'a' and self.current_field == 'track' and not self.track_url:
                href = attrs.get('href', '')
                if href:
                    self.track_url = urllib.parse.urljoin('https://www.last.fm', href)

            if tag == 'img' and not self.image:
                self.image = attrs.get('src') or attrs.get('data-src') or ''

    def handle_endtag(self, tag):
        if self.in_first_row:
            if tag == 'td':
                self.current_field = None
            if tag == 'tr':
                self.row_depth -= 1
                if self.row_depth <= 0:
                    self.in_first_row = False
                    self.row_found = True

        if self.in_recent_section and tag == 'section':
            self.section_depth -= 1
            if self.section_depth <= 0:
                self.in_recent_section = False

    def handle_data(self, data):
        if self.in_first_row and self.current_field:
            value = re.sub(r'\s+', ' ', data).strip()
            if value:
                self.fields[self.current_field].append(value)

    def result(self):
        def clean(name):
            values = self.fields[name]
            compact = []
            for value in values:
                if not compact or value != compact[-1]:
                    compact.append(value)
            return ' '.join(compact).strip()

        timestamp = clean('timestamp')
        return {
            'track': clean('track'),
            'artist': clean('artist'),
            'album': clean('album'),
            'timestamp': timestamp,
            'url': self.track_url,
            'image': self.image,
            'is_playing': (
                'chartlist-row--now-scrobbling' in self.row_classes
                or 'scrobbling now' in timestamp.lower()
            ),
        }


def request_bytes(url, accept):
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
            'Accept': accept,
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_api(user, api_key):
    query = urllib.parse.urlencode({
        'method': 'user.getrecenttracks',
        'user': user,
        'api_key': api_key,
        'format': 'json',
        'limit': 1,
        'extended': 1,
    })
    endpoint = f'https://ws.audioscrobbler.com/2.0/?{query}'
    payload = json.loads(request_bytes(endpoint, 'application/json').decode('utf-8'))
    if 'error' in payload:
        raise RuntimeError(f"Last.fm API {payload.get('error')}: {payload.get('message', 'unknown error')}")

    tracks = payload.get('recenttracks', {}).get('track', [])
    if not tracks:
        raise RuntimeError('Last.fm API returned no tracks')

    item = tracks[0]
    artist_value = item.get('artist', {})
    album_value = item.get('album', {})
    images = item.get('image', []) or []
    image = ''
    for candidate in reversed(images):
        value = candidate.get('#text', '') if isinstance(candidate, dict) else ''
        if value:
            image = value
            break

    is_playing = item.get('@attr', {}).get('nowplaying') == 'true'
    date_value = item.get('date', {}) if isinstance(item.get('date'), dict) else {}

    return {
        'source': endpoint,
        'source_type': 'lastfm_api',
        'is_playing': is_playing,
        'track': item.get('name', ''),
        'artist': artist_value.get('name') or artist_value.get('#text', '') if isinstance(artist_value, dict) else str(artist_value),
        'album': album_value.get('#text', '') if isinstance(album_value, dict) else str(album_value),
        'image': image,
        'url': item.get('url', ''),
        'published': 'Scrobbling now' if is_playing else date_value.get('#text', ''),
    }


def scrape_profile(user):
    profile_url = f"https://www.last.fm/user/{urllib.parse.quote(user)}"
    payload = request_bytes(profile_url, 'text/html,application/xhtml+xml')
    parser = RecentTrackParser()
    parser.feed(payload.decode('utf-8', errors='replace'))
    parsed = parser.result()
    if not parser.row_found or not parsed['track']:
        raise RuntimeError('Could not locate the first recent-track row on Last.fm profile')
    parsed.update({
        'source': profile_url,
        'source_type': 'lastfm_profile',
        'published': parsed.pop('timestamp'),
    })
    return parsed


def text_of(node, name):
    if node is None:
        return ''
    for child in node:
        if child.tag.split('}')[-1].lower() == name.lower():
            return (child.text or '').strip()
    return ''


def scrape_rss(feed_url):
    root = ET.fromstring(request_bytes(feed_url, 'application/rss+xml,application/xml,text/xml,*/*'))
    item = next((node for node in root.iter() if node.tag.split('}')[-1].lower() == 'item'), None)
    if item is None:
        raise RuntimeError('RSS feed contains no track item')

    raw_title = text_of(item, 'title')
    artist = ''
    track = raw_title
    for sep in (' – ', ' — ', ' - '):
        if sep in raw_title:
            artist, track = raw_title.split(sep, 1)
            break

    description = re.sub(r'<[^>]+>', ' ', text_of(item, 'description'))
    description = re.sub(r'\s+', ' ', unescape(description)).strip()
    image = ''
    for child in item:
        local = child.tag.split('}')[-1].lower()
        if local in {'enclosure', 'thumbnail', 'content'} and child.attrib.get('url'):
            image = child.attrib['url'].strip()
            break

    return {
        'source': feed_url,
        'source_type': 'xiffy_rss_fallback',
        'is_playing': False,
        'track': track.strip(),
        'artist': artist.strip(),
        'album': text_of(item, 'album'),
        'image': image,
        'url': text_of(item, 'link'),
        'published': text_of(item, 'pubDate') or text_of(item, 'date'),
        'description': description,
    }


def main():
    config = json.loads(Path('config.json').read_text(encoding='utf-8'))
    user = config['lastfm_user']
    result = {
        'status': 'error',
        'user': user,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'is_playing': False,
        'track': '',
        'artist': '',
        'album': '',
        'image': '',
        'url': '',
        'published': '',
    }

    errors = []
    api_key = os.getenv('LASTFM_API_KEY', '').strip()

    if api_key:
        try:
            result.update(fetch_api(user, api_key))
            result['status'] = 'ok'
        except Exception as exc:
            errors.append(f'api: {type(exc).__name__}: {exc}')

    if result['status'] != 'ok':
        try:
            result.update(scrape_profile(user))
            result['status'] = 'ok'
        except Exception as exc:
            errors.append(f'profile: {type(exc).__name__}: {exc}')

    if result['status'] != 'ok':
        try:
            result.update(scrape_rss(config['feed_url']))
            result['status'] = 'ok'
        except Exception as exc:
            errors.append(f'rss: {type(exc).__name__}: {exc}')

    if not api_key:
        errors.append('api: LASTFM_API_KEY is not configured; live status may be delayed')

    if errors:
        result['warnings'] = errors

    Path('now-playing.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


if __name__ == '__main__':
    main()
