export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/now-playing') {
      return new Response('Not found', { status: 404 });
    }

    if (!env.LASTFM_API_KEY) {
      return json({ status: 'error', error: 'LASTFM_API_KEY is not configured' }, 500);
    }

    const params = new URLSearchParams({
      method: 'user.getrecenttracks',
      user: 'yy675908',
      api_key: env.LASTFM_API_KEY,
      format: 'json',
      limit: '1',
      extended: '1',
      cb: Date.now().toString(),
    });

    try {
      const response = await fetch(`https://ws.audioscrobbler.com/2.0/?${params}`, {
        headers: {
          'User-Agent': 'yy675908-now-playing/1.1',
          'Cache-Control': 'no-cache, no-store',
          Pragma: 'no-cache',
        },
        cf: { cacheTtl: 0, cacheEverything: false },
      });
      const payload = await response.json();

      if (!response.ok || payload.error) {
        return json({
          status: 'error',
          error: payload.message || `Last.fm returned HTTP ${response.status}`,
        }, 502);
      }

      const track = payload?.recenttracks?.track?.[0];
      if (!track) {
        return json({ status: 'ok', is_playing: false, track: null });
      }

      const images = Array.isArray(track.image) ? track.image : [];
      const image = [...images].reverse().find((item) => item?.['#text'])?.['#text'] || '';
      const isPlaying = track?.['@attr']?.nowplaying === 'true';

      return json({
        status: 'ok',
        user: 'yy675908',
        checked_at: new Date().toISOString(),
        is_playing: isPlaying,
        track: track.name || '',
        artist: track.artist?.name || track.artist?.['#text'] || '',
        album: track.album?.['#text'] || '',
        image,
        url: track.url || '',
        published: isPlaying ? 'Scrobbling now' : track.date?.['#text'] || '',
      });
    } catch (error) {
      return json({ status: 'error', error: String(error) }, 502);
    }
  },
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store, no-cache, must-revalidate, max-age=0',
      pragma: 'no-cache',
      expires: '0',
      'access-control-allow-origin': '*',
    },
  });
}
