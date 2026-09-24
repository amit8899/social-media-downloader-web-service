import yt_dlp
import json
import os
import re
import tempfile

def _format_size(size_bytes):
    if not size_bytes or size_bytes <= 0:
        return ''
    gb = size_bytes / 1073741824.0
    if gb >= 1.0:
        return f'{gb:.2f} GB'
    mb = size_bytes / 1048576.0
    if mb >= 1.0:
        return f'{mb:.1f} MB'
    kb = size_bytes / 1024.0
    if kb >= 1.0:
        return f'{kb:.1f} KB'
    return f'{size_bytes} B'


# Hot-patch yt-dlp's facebook extractor with updated DASH / delivery fragment parser
try:
    import facebook_updated
    import yt_dlp.extractor.facebook as _fb_mod
    for _attr in dir(facebook_updated):
        if _attr.startswith('Facebook') and _attr.endswith('IE'):
            setattr(_fb_mod, _attr, getattr(facebook_updated, _attr))
except Exception:
    pass

# Hot-patch yt-dlp's instagram extractor to support photo stories, photo carousels, and image posts
try:
    import yt_dlp.extractor.instagram as _ig_mod
    from yt_dlp.utils import traverse_obj, float_or_none

    _orig_ig_extract_product_media = _ig_mod.InstagramBaseIE._extract_product_media

    def _patched_ig_extract_product_media(self, product_media):
        media_id = product_media.get('code') or _ig_mod._pk_to_id(product_media.get('pk'))
        vcodec = product_media.get('video_codec')
        dash_manifest_raw = product_media.get('video_dash_manifest')
        videos_list = product_media.get('video_versions')
        raw_pk = str(product_media.get('pk') or '')

        thumbnails = [{
            'url': thumbnail.get('url'),
            'width': thumbnail.get('width'),
            'height': thumbnail.get('height'),
        } for thumbnail in traverse_obj(product_media, ('image_versions2', 'candidates')) or []]

        # If this is an image (no video stream)
        if not (dash_manifest_raw or videos_list):
            best_img_url = None
            best_w, best_h = None, None
            if thumbnails:
                best_thumb = max(thumbnails, key=lambda t: (t.get('width') or 0) * (t.get('height') or 0))
                best_img_url = best_thumb.get('url')
                best_w = best_thumb.get('width')
                best_h = best_thumb.get('height')
            if not best_img_url:
                best_img_url = product_media.get('display_url') or product_media.get('thumbnail_src')
                if not thumbnails and best_img_url:
                    thumbnails = [{'url': best_img_url}]

            if not best_img_url:
                return {}

            formats = [{
                'format_id': 'image',
                'url': best_img_url,
                'width': best_w,
                'height': best_h,
                'ext': 'jpg',
                'vcodec': 'none',
                'acodec': 'none',
            }]
            return {
                'id': media_id,
                'pk': raw_pk,
                'url': best_img_url,
                'thumbnail': best_img_url,
                'ext': 'jpg',
                'vcodec': 'none',
                'acodec': 'none',
                'formats': formats,
                'thumbnails': thumbnails,
                'is_video': False,
            }

        res = _orig_ig_extract_product_media(self, product_media)
        if res:
            res['pk'] = raw_pk
            res['is_video'] = True
        return res

    _ig_mod.InstagramBaseIE._extract_product_media = _patched_ig_extract_product_media
    print("[EXTRACT_VIDEO] Hot-patched InstagramBaseIE._extract_product_media for photo/story support", flush=True)
except Exception as _ig_patch_err:
    print(f"[EXTRACT_VIDEO] Failed to hot-patch InstagramBaseIE: {_ig_patch_err}", flush=True)

# Hot-patch yt-dlp's pornhub extractor to support modern player layout (CLIPS_DATA and flashvars mediaDefinitions) and prevent 410 Gone
try:
    import yt_dlp.extractor.pornhub as _ph_mod
    from yt_dlp.utils import int_or_none, str_to_int, url_or_none, clean_html, ExtractorError

    def _patched_ph_real_extract(self, url):
        mobj = self._match_valid_url(url)
        host = mobj.group('host') or 'pornhub.com'
        video_id = mobj.group('id')

        import urllib.request
        import ssl

        webpage = None
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                f'https://www.{host}/view_video.php?viewkey={video_id}',
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Cookie': 'platform=pc; age_verified=1',
                }
            )
            with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
                webpage = resp.read().decode('utf-8', errors='ignore')
                print(f"[EXTRACT_VIDEO] Direct urllib fetch succeeded for {video_id}, len={len(webpage)}", flush=True)
        except urllib.error.HTTPError as http_err:
            try:
                webpage = http_err.read().decode('utf-8', errors='ignore')
                print(f"[EXTRACT_VIDEO] Extracted body from urllib HTTPError {http_err.code}, len={len(webpage)}", flush=True)
            except Exception:
                pass
            if not webpage:
                try:
                    self._set_cookie(host, 'platform', 'pc')
                    self._set_cookie(host, 'age_verified', '1')
                    webpage = self._download_webpage(
                        f'https://www.{host}/view_video.php?viewkey={video_id}',
                        video_id, 'Downloading webpage')
                except Exception as fallback_err:
                    print(f"[EXTRACT_VIDEO] Fallback _download_webpage failed: {fallback_err}", flush=True)
        except Exception as dl_err:
            print(f"[EXTRACT_VIDEO] Direct urllib fetch failed: {dl_err}, trying self._download_webpage...", flush=True)
            try:
                self._set_cookie(host, 'platform', 'pc')
                self._set_cookie(host, 'age_verified', '1')
                webpage = self._download_webpage(
                    f'https://www.{host}/view_video.php?viewkey={video_id}',
                    video_id, 'Downloading webpage')
            except Exception as fallback_err:
                print(f"[EXTRACT_VIDEO] Fallback _download_webpage failed: {fallback_err}", flush=True)

        error_msg = self._html_search_regex(
            (r'(?s)<div[^>]+class=(["\'])(?:(?!\1).)*\b(?:removed|userMessageSection)\b(?:(?!\1).)*\1[^>]*>(?P<error>.+?)</div>',
             r'(?s)<section[^>]+class=["\']noVideo["\'][^>]*>(?P<error>.+?)</section>'),
            webpage, 'error message', default=None, group='error')
        if error_msg:
            error_msg = re.sub(r'\s+', ' ', error_msg)
            raise ExtractorError(f'PornHub said: {error_msg}', expected=True, video_id=video_id)

        title = self._html_search_meta(
            'twitter:title', webpage, default=None) or self._html_search_regex(
            (r'(?s)<h1[^>]+class=["\']title["\'][^>]*>(?P<title>.+?)</h1>',
             r'<div[^>]+data-video-title=(["\'])(?P<title>(?:(?!\1).)+)\1',
             r'shareTitle["\']\s*[=:]\s*(["\'])(?P<title>(?:(?!\1).)+)\1'),
            webpage, 'title', default=None, group='title')

        media_definitions = []
        duration = None
        thumbnail = None

        # 1. Parse CLIPS_DATA (modern player layout)
        m_clips = re.search(r'var\s+CLIPS_DATA\s*=\s*({.+?});', webpage)
        if m_clips:
            try:
                clips_data = json.loads(m_clips.group(1))
                if not title:
                    title = clips_data.get('videoTitle')
                duration = int_or_none(clips_data.get('videoDuration'))
                thumbnail = clips_data.get('posterUrl')
                defs = clips_data.get('mediaDefinition') or clips_data.get('mediaDefinitions') or []
                if isinstance(defs, list):
                    media_definitions.extend(defs)
            except Exception as e:
                print(f"[EXTRACT_VIDEO] Error parsing CLIPS_DATA: {e}", flush=True)

        # 2. Parse flashvars (legacy player layout)
        m_flash = re.search(r'var\s+flashvars_\d+\s*=\s*({.+?});', webpage)
        if m_flash:
            try:
                flashvars = json.loads(m_flash.group(1))
                if not title:
                    title = flashvars.get('video_title')
                if not duration:
                    duration = int_or_none(flashvars.get('video_duration'))
                if not thumbnail:
                    thumbnail = flashvars.get('image_url')
                defs = flashvars.get('mediaDefinitions') or flashvars.get('mediaDefinition') or []
                if isinstance(defs, list):
                    media_definitions.extend(defs)
            except Exception as e:
                print(f"[EXTRACT_VIDEO] Error parsing flashvars: {e}", flush=True)

        # 3. Direct regex fallback for mediaDefinition
        if not media_definitions:
            m_def = re.search(r'"mediaDefinition"\s*:\s*(\[\s*\{.+?\}\s*\])', webpage)
            if m_def:
                try:
                    media_definitions = json.loads(m_def.group(1))
                except Exception:
                    pass

        formats = []
        formats_set = set()

        for d in media_definitions:
            if not isinstance(d, dict):
                continue
            video_url = url_or_none(d.get('videoUrl'))
            if not video_url:
                continue
            if '/video/get_media' in video_url:
                continue

            q = d.get('quality')
            h = int_or_none(d.get('height') or q)
            fmt_type = str(d.get('format') or '').lower()

            if 'm3u8' in video_url or fmt_type == 'hls':
                try:
                    m3u8_fmts = self._extract_m3u8_formats(
                        video_url, video_id, 'mp4', entry_protocol='m3u8_native',
                        m3u8_id=f'{h}p' if h else 'hls', fatal=False)
                    for mf in m3u8_fmts:
                        mf_url = mf.get('url')
                        if mf_url and mf_url not in formats_set:
                            formats_set.add(mf_url)
                            if h and not mf.get('height'):
                                mf['height'] = h
                            formats.append(mf)
                except Exception:
                    pass
                if video_url not in formats_set:
                    formats_set.add(video_url)
                    formats.append({
                        'url': video_url,
                        'format_id': f'{h}p-hls' if h else 'hls',
                        'height': h,
                        'ext': 'mp4',
                        'protocol': 'm3u8_native',
                    })
            else:
                if video_url not in formats_set:
                    formats_set.add(video_url)
                    formats.append({
                        'url': video_url,
                        'format_id': f'{h}p' if h else 'http',
                        'height': h,
                        'ext': 'mp4',
                    })

        uploader = self._html_search_regex(
            r'(?s)From:&nbsp;.+?<(?:a\b[^>]+\bhref=["\']/(?:(?:user|channel)s|model|pornstar)/|span\b[^>]+\bclass=["\']username)[^>]+>(.+?)<',
            webpage, 'uploader', default=None)

        return {
            'id': video_id,
            'uploader': uploader,
            'title': title or f'PornHub_{video_id}',
            'thumbnail': thumbnail,
            'duration': duration,
            'formats': formats,
            'age_limit': 18,
            'is_video': True,
        }

    _ph_mod.PornHubIE._real_extract = _patched_ph_real_extract
    print("[EXTRACT_VIDEO] Hot-patched PornHubIE._real_extract for modern CLIPS_DATA & HLS support", flush=True)
except Exception as _ph_patch_err:
    print(f"[EXTRACT_VIDEO] Failed to hot-patch PornHubIE: {_ph_patch_err}", flush=True)


def _write_cookie_file(cookies_str, domain):
    """Write a Netscape cookie file from a raw 'k=v; k2=v2' string."""
    if not cookies_str or not cookies_str.strip():
        return None

    domain_map = {
        'youtube':   ['.youtube.com', 'youtube.com', '.google.com', 'google.com'],
        'instagram': ['.instagram.com', 'instagram.com'],
        'facebook':  ['.facebook.com', 'facebook.com', '.fb.com', 'fb.com'],
        'twitter':   ['.twitter.com', 'twitter.com', '.x.com', 'x.com'],
        'tiktok':    ['.tiktok.com', 'tiktok.com'],
    }
    domains = domain_map.get(domain, ['.' + domain + '.com', domain + '.com'])

    lines = [
        '# Netscape HTTP Cookie File\n',
        '# https://curl.haxx.se/rfc/cookie_spec.html\n'
    ]
    for pair in cookies_str.split(';'):
        pair = pair.strip()
        if '=' not in pair:
            continue
        name, _, value = pair.partition('=')
        name, value = name.strip(), value.strip()
        if not name:
            continue
        for d in domains:
            include_sub = 'TRUE' if d.startswith('.') else 'FALSE'
            lines.append('{}\t{}\t/\tTRUE\t2147483647\t{}\t{}\n'.format(
                d, include_sub, name, value))

    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tmp.writelines(lines)
    tmp.close()
    return tmp.name


def _resolve_redirect(url):
    """Resolve HTTP 301/302 redirects without downloading body (useful for fb.watch and facebook.com/share/*)."""
    try:
        import urllib.request
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirectHandler)
        # Use Facebook crawler user-agent for Facebook URLs to bypass HTTP 400 Bad Request
        if 'facebook.com' in url or 'fb.watch' in url or 'fb.com' in url:
            ua = 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'
        else:
            ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'

        req = urllib.request.Request(url, headers={'User-Agent': ua})
        try:
            with opener.open(req, timeout=6) as resp:
                loc = resp.headers.get('Location')
                if loc:
                    return loc
                resolved = resp.geturl()
                if resolved:
                    return resolved
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get('Location')
                if loc:
                    return loc
    except Exception as e:
        print(f"[EXTRACT_VIDEO] Redirect resolution error: {e}")
    return url


def _is_video_entry(entry, eu=''):
    if not entry:
        return False
    if entry.get('is_video') is False:
        return False
    ext = (entry.get('ext') or '').lower()
    if ext in ('mp4', 'mkv', 'webm', 'mov', 'flv', 'm4v', '3gp'):
        return True
    if ext in ('jpg', 'jpeg', 'png', 'webp', 'heic'):
        return False
    if entry.get('is_video') is True:
        return True
    if entry.get('_type') == 'video':
        return True
    vc = entry.get('vcodec')
    if vc not in (None, 'none', '', 'null'):
        return True
    fmts = entry.get('formats') or []
    has_video_codec = False
    for f in fmts:
        f_ext = (f.get('ext') or '').lower()
        f_vc = f.get('vcodec')
        if f_vc not in (None, 'none', '', 'null') or f_ext in ('mp4', 'mkv', 'webm'):
            has_video_codec = True
            break
    if has_video_codec:
        return True
    lower_u = (eu or entry.get('url') or '').lower()
    if '.mp4' in lower_u or 'video' in lower_u:
        return True
    return False


def extract_video(video_url, cookies_str=None, platform=None):
    """
    Extract a direct stream URL from any yt-dlp-supported platform.

    Args:
        video_url:   The full URL to download from.
        cookies_str: Optional raw cookie string (k=v; k2=v2) from user's session.
        platform:    Optional hint: 'youtube' | 'instagram' | 'facebook' | 'twitter' | 'tiktok'

    Returns:
        JSON string with keys: urls (list), url (first), title, thumbnail,
        thumbnails (list), size_mb, mime_type, is_video, error
    """
    cookie_file = None
    try:
        # Normalize Facebook URLs (share/v/, share/r/, share/p/, fb.watch)
        if 'facebook.com' in video_url or 'fb.watch' in video_url or 'fb.com' in video_url:
            if 'fb.watch' in video_url or '/share/' in video_url:
                print(f"[EXTRACT_VIDEO] Resolving Facebook share/redirect URL: {video_url}")
                video_url = _resolve_redirect(video_url)
                print(f"[EXTRACT_VIDEO] Resolved Facebook URL: {video_url}")

        # Normalize YouTube URLs (clean tracking params like ?si=...)
        if 'youtube.com' in video_url or 'youtu.be' in video_url:
            m = re.search(r'(?:youtu\.be/|youtube\.com/(?:watch\?.*?v=|embed/|shorts/))([A-Za-z0-9_-]{11})', video_url)
            if m:
                video_url = f'https://www.youtube.com/watch?v={m.group(1)}'

        # Auto-detect platform from URL if not provided
        u = video_url.lower()
        if platform is None:
            if 'youtube.com' in u or 'youtu.be' in u:
                platform = 'youtube'
            elif 'instagram.com' in u:
                platform = 'instagram'
            elif 'facebook.com' in u or 'fb.watch' in u or 'fb.com' in u:
                platform = 'facebook'
            elif 'twitter.com' in u or 'x.com' in u:
                platform = 'twitter'
            elif 'tiktok.com' in u:
                platform = 'tiktok'
            else:
                platform = 'generic'

        print(f"[EXTRACT_VIDEO] Platform: {platform} | URL: {video_url} | Has cookies: {bool(cookies_str)}", flush=True)

        # Write cookies if provided (skip for YouTube to avoid bot verification triggers)
        if cookies_str and cookies_str.strip() and platform != 'youtube':
            cookie_file = _write_cookie_file(cookies_str, platform)

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }

        # For non-YouTube platforms, use standard desktop headers
        if platform != 'youtube':
            ydl_opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }

        # For YouTube: use android client.
        # android talks directly to the mobile Innertube API and returns complete progressive MP4 formats (video + audio),
        # which can be downloaded seamlessly by Android's DownloadManager without 403 Forbidden errors.
        if platform == 'youtube':
            ydl_opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['android'],
                    'player_skip': ['webpage', 'configs'],
                    'formats': ['missing_pot'],
                }
            }
        elif platform == 'facebook':
            # For Facebook: allow best video + audio or best standalone format or image
            ydl_opts['format'] = 'bestvideo*+bestaudio/best[ext=mp4]/best/bestvideo/bestaudio/image/all'
        else:
            # For other platforms, request best mp4/webm/best or image
            ydl_opts['format'] = 'best[ext=mp4]/best[ext=webm]/best/bestvideo/bestaudio/image/all'

        # For non-Facebook platforms, attach cookiefile if present.
        # For Facebook, we intentionally attempt extraction WITHOUT cookies first
        # to fetch full public video titles and rich metadata, falling back to cookies only if needed.
        if cookie_file and platform != 'facebook':
            ydl_opts['cookiefile'] = cookie_file

        def _do_extract(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                if platform == 'facebook':
                    try:
                        import facebook_updated
                        for ie_cls in (
                            facebook_updated.FacebookIE,
                            facebook_updated.FacebookReelIE,
                            facebook_updated.FacebookPluginsVideoIE,
                            facebook_updated.FacebookRedirectURLIE,
                            facebook_updated.FacebookAdsIE,
                        ):
                            ydl.add_info_extractor(ie_cls(ydl))
                        print("[EXTRACT_VIDEO] Registered updated Facebook extractors", flush=True)
                    except Exception as fb_err:
                        print(f"[EXTRACT_VIDEO] Could not register updated Facebook extractors: {fb_err}", flush=True)
                return ydl.extract_info(video_url, download=False)

        info = None
        if platform == 'facebook':
            # Facebook: First try WITHOUT cookies to retrieve rich public metadata and titles.
            # If it fails or returns no formats (private/friends-only content), retry WITH cookies.
            print("[EXTRACT_VIDEO] Facebook: attempting extraction WITHOUT cookies first...", flush=True)
            try:
                info = _do_extract(ydl_opts)
                if not (info and (info.get('url') or info.get('formats') or info.get('entries'))):
                    raise ValueError("No video formats found in public Facebook extraction")
                print("[EXTRACT_VIDEO] Facebook: public extraction succeeded!", flush=True)
            except Exception as fb_pub_err:
                if cookie_file:
                    print(f"[EXTRACT_VIDEO] Facebook: public extraction failed ({fb_pub_err}). Retrying WITH cookies for private content...", flush=True)
                    ydl_opts_fb_cookie = dict(ydl_opts)
                    ydl_opts_fb_cookie['cookiefile'] = cookie_file
                    info = _do_extract(ydl_opts_fb_cookie)
                else:
                    raise fb_pub_err
        elif platform == 'instagram':
            # For Instagram:
            # Public web extraction yields rich DASH manifests with 1080p (1920p) video.
            # Authenticated mobile extraction (with cookies) provides reliable 720p (1280p) progressive stream.
            if cookie_file and '/stories/' not in video_url:
                try:
                    info = _do_extract(ydl_opts)
                except Exception as auth_err:
                    print(f"[EXTRACT_VIDEO] Instagram auth extraction failed ({auth_err}). Retrying without cookies...", flush=True)
                    ydl_opts_no_cookie = dict(ydl_opts)
                    ydl_opts_no_cookie.pop('cookiefile', None)
                    info = _do_extract(ydl_opts_no_cookie)
                else:
                    # If auth extraction succeeded, augment with public DASH formats for 1920p quality if available
                    curr_max_h = max([f.get('height') or 0 for f in (info.get('formats') or [])], default=0)
                    if curr_max_h < 1400:
                        try:
                            ydl_opts_pub = dict(ydl_opts)
                            ydl_opts_pub.pop('cookiefile', None)
                            pub_info = _do_extract(ydl_opts_pub)
                            if pub_info and pub_info.get('formats'):
                                existing_fids = {f.get('format_id') for f in (info.get('formats') or [])}
                                for pf in pub_info.get('formats', []):
                                    if pf.get('format_id') not in existing_fids and pf.get('url'):
                                        info['formats'].append(pf)
                                print(f"[EXTRACT_VIDEO] Instagram: successfully augmented formats with public DASH streams", flush=True)
                        except Exception as pub_aug_err:
                            print(f"[EXTRACT_VIDEO] Instagram: public DASH augmentation skipped: {pub_aug_err}", flush=True)
            else:
                try:
                    info = _do_extract(ydl_opts)
                except Exception as first_err:
                    if cookie_file:
                        print(f"[EXTRACT_VIDEO] Extraction with cookies failed ({first_err}). Retrying WITHOUT cookies...", flush=True)
                        ydl_opts_no_cookie = dict(ydl_opts)
                        ydl_opts_no_cookie.pop('cookiefile', None)
                        info = _do_extract(ydl_opts_no_cookie)
                    else:
                        raise first_err
        else:
            try:
                info = _do_extract(ydl_opts)
            except Exception as first_err:
                # If YouTube extraction failed (e.g. webpage bot challenge), retry with player_skip: ['webpage'] as fallback
                if platform == 'youtube':
                    print(f"[EXTRACT_VIDEO] YouTube extraction without player_skip failed ({first_err}). Retrying with player_skip: ['webpage']...", flush=True)
                    ydl_opts_skip = dict(ydl_opts)
                    ydl_opts_skip['extractor_args'] = {
                        'youtube': {
                            'player_client': ['android'],
                            'player_skip': ['webpage'],
                            'formats': ['missing_pot'],
                        }
                    }
                    try:
                        info = _do_extract(ydl_opts_skip)
                    except Exception as second_err:
                        print(f"[EXTRACT_VIDEO] YouTube fallback extraction also failed: {second_err}", flush=True)
                        if cookie_file:
                            print("[EXTRACT_VIDEO] Retrying WITHOUT cookies...", flush=True)
                            ydl_opts_no_cookie = dict(ydl_opts_skip)
                            ydl_opts_no_cookie.pop('cookiefile', None)
                            info = _do_extract(ydl_opts_no_cookie)
                        else:
                            raise first_err
                elif cookie_file:
                    print(f"[EXTRACT_VIDEO] Extraction with cookies failed ({first_err}). Retrying WITHOUT cookies...", flush=True)
                    ydl_opts_no_cookie = dict(ydl_opts)
                    ydl_opts_no_cookie.pop('cookiefile', None)
                    info = _do_extract(ydl_opts_no_cookie)
                else:
                    raise first_err

        if not info:
            return json.dumps({
                'urls': [],
                'url': '',
                'title': '',
                'thumbnail': '',
                'thumbnails': [],
                'size_mb': '0',
                'mime_type': 'video/mp4',
                'is_video': True,
                'error': 'Failed to retrieve media details',
            })

        # Extract target story ID if present in the URL (e.g. instagram.com/stories/username/123456789)
        target_id = ''
        m_story = re.search(r'/stories/[^/]+/(\d+)', video_url)
        if m_story:
            target_id = m_story.group(1)

        # Handle playlists / carousels (entries list)
        entries = info.get('entries') or []
        if entries:
            urls, thumbs, mimes = [], [], []
            items = []
            has_any_video = False
            for idx, entry in enumerate(entries):
                if not entry:
                    continue
                eu = entry.get('url') or ''
                entry_fmts = entry.get('formats') or []
                if not eu and entry_fmts:
                    for f in reversed(entry_fmts):
                        if f.get('url'):
                            eu = f['url']
                            break

                ev = _is_video_entry(entry, eu)
                if ev:
                    has_any_video = True

                et = entry.get('thumbnail') or ''
                if not et:
                    entry_thumbs = entry.get('thumbnails') or []
                    if entry_thumbs and isinstance(entry_thumbs, list):
                        for t in reversed(entry_thumbs):
                            if isinstance(t, dict) and t.get('url'):
                                et = t['url']
                                break
                if not et and not ev:
                    et = eu

                raw_pk = str(entry.get('pk') or '')
                raw_id = str(entry.get('id') or '')
                entry_id = raw_pk if raw_pk else (raw_id if raw_id else str(idx + 1))
                entry_fs = entry.get('filesize') or entry.get('filesize_approx') or 0
                entry_size_mb = _format_size(entry_fs)

                if eu:
                    urls.append(eu)
                    thumbs.append(et)
                    mime_str = 'video/mp4' if ev else 'image/jpeg'
                    mimes.append(mime_str)
                    items.append({
                        'id': entry_id,
                        'pk': raw_pk,
                        'url': eu,
                        'thumbnail': et,
                        'is_video': ev,
                        'mime_type': mime_str,
                        'title': entry.get('title') or '',
                        'size_mb': entry_size_mb,
                    })

            first_url = urls[0] if urls else ''
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            size_mb = _format_size(filesize) if filesize > 0 else '0'

            return json.dumps({
                'urls': urls,
                'url': first_url,
                'title': info.get('title') or '',
                'thumbnail': thumbs[0] if thumbs else '',
                'thumbnails': thumbs,
                'mimes': mimes,
                'items': items,
                'target_id': target_id,
                'duration': info.get('duration') or 0,
                'size_mb': size_mb,
                'mime_type': mimes[0] if mimes else ('video/mp4' if has_any_video else 'image/jpeg'),
                'is_video': has_any_video,
                'error': '',
            })

        # Single item
        formats = info.get('formats') or []
        stream_url = info.get('url') or ''

        # If direct 'url' is not populated, find the best working format
        if not stream_url:
            # 1. Look for progressive video format (has both video and audio codec, not m3u8)
            for f in reversed(formats):
                f_url = f.get('url') or ''
                proto = str(f.get('protocol') or '').lower()
                if 'm3u8' in proto or '.m3u8' in f_url.lower() or (f.get('ext') or '').lower() == 'm3u8':
                    continue
                if f_url and f.get('vcodec') not in (None, 'none') and f.get('acodec') not in (None, 'none'):
                    stream_url = f_url
                    break

            # 2. Fallback to any non-m3u8 format with a valid URL
            if not stream_url:
                for f in reversed(formats):
                    f_url = f.get('url') or ''
                    proto = str(f.get('protocol') or '').lower()
                    if 'm3u8' in proto or '.m3u8' in f_url.lower() or (f.get('ext') or '').lower() == 'm3u8':
                        continue
                    if f_url:
                        stream_url = f_url
                        break

            # 3. Fallback to any format with a valid URL (including HLS)
            if not stream_url:
                for f in reversed(formats):
                    f_url = f.get('url') or ''
                    if f_url:
                        stream_url = f_url
                        break

            # 4. Fallback to thumbnail / thumbnails if it's an image
            if not stream_url:
                stream_url = info.get('thumbnail') or ''
                if not stream_url and info.get('thumbnails'):
                    for t in reversed(info.get('thumbnails') or []):
                        if isinstance(t, dict) and t.get('url'):
                            stream_url = t['url']
                            break

        is_video = _is_video_entry(info, stream_url)

        title = info.get('title') or ''
        # If title is empty or generic 'Video'/'Facebook Video', improve with description or id
        if not title or title.strip().lower() in ('video', 'facebook video', 'facebook', 'instagram'):
            desc = info.get('description') or ''
            if desc and desc.strip():
                first_line = desc.strip().split('\n')[0].strip()
                if first_line:
                    title = first_line[:60]
            if not title or title.strip().lower() in ('video', 'facebook video', 'facebook'):
                video_id = info.get('id') or ''
                title = f"{platform.capitalize()}_{video_id}" if video_id else f"{platform.capitalize()}_Download"

        if not is_video and title.startswith('Video by '):
            title = 'Photo by ' + title[len('Video by '):]

        thumbnail = info.get('thumbnail') or ''
        if not thumbnail and stream_url and not is_video:
            thumbnail = stream_url

        filesize = info.get('filesize') or info.get('filesize_approx') or 0
        dur = info.get('duration') or 0
        if not filesize and dur > 0:
            tbr = info.get('tbr') or 0
            if tbr and tbr > 0:
                filesize = int((float(tbr) * 1000 / 8) * dur)
        size_mb = _format_size(filesize) if filesize > 0 else '0'
        ext = info.get('ext') or ('mp4' if is_video else 'jpg')
        mime_type = ('video/' + ext) if is_video else 'image/jpeg'

        parsed_formats = []
        for f in formats:
            if not f.get('url'):
                continue
            fid = str(f.get('format_id', '')).lower()
            vc = str(f.get('vcodec') or 'none').lower()
            ac = str(f.get('acodec') or 'none').lower()

            # Skip VP9, AV1, and WebM formats because Android MediaMuxer and basic decoders cannot handle them
            if 'vp09' in vc or 'vp9' in vc or 'av01' in vc or 'av1' in vc or '-av1' in fid or (f.get('ext') or '').lower() == 'webm':
                continue

            # Handle HLS (.m3u8) streams:
            protocol = str(f.get('protocol') or '').lower()
            f_url = f.get('url') or ''
            is_hls = 'm3u8' in protocol or '.m3u8' in f_url.lower() or (f.get('ext') or '').lower() == 'm3u8'
            if is_hls:
                f['ext'] = 'mp4'
                if vc in ('none', '') and ac in ('none', ''):
                    vc = 'h264'
                    ac = 'aac'

            has_v = vc not in ('none', '')
            has_a = ac not in ('none', '')
            h = f.get('height') or 0

            # Special handling for Instagram progressive videos and DASH audio:
            # In yt-dlp's Instagram extractor, native video_versions do not set 'acodec',
            # but they ALWAYS have both video and audio. Their format_id does not start with 'dash'.
            if platform == 'instagram' or 'instagram.com' in f.get('url', ''):
                if not fid.startswith('dash'):
                    has_v = True
                    has_a = True
                elif fid.startswith('dash-audio') or fid.endswith('-audio') or (f.get('ext') or '').lower() in ('m4a', 'aac'):
                    if not has_v:
                        has_a = True

            # Special handling for Facebook/generic progressive format indicators
            if fid == 'hd':
                has_v = True
                has_a = True
                h = h or 720
            elif fid == 'sd':
                has_v = True
                has_a = True
                h = h or 480
            elif not has_v and not has_a:
                if (f.get('ext') or '').lower() == 'mp4' and f.get('video_ext') == 'mp4':
                    has_v = True
                    has_a = True
                else:
                    continue

            fmt_ext = (f.get('ext') or 'mp4').upper()
            if has_v:
                if h >= 2000:
                    ql = f"4K ({h}p)"
                elif h >= 1300:
                    ql = f"2K ({h}p)"
                elif h >= 900:
                    ql = f"Full HD ({h}p)"
                elif h >= 650:
                    ql = f"HD ({h}p)"
                elif h >= 400:
                    ql = f"SD ({h}p)"
                elif h in (360, 338):
                    ql = "360p"
                elif h > 0:
                    ql = f"{h}p"
                else:
                    ql = "HD"
            else:
                ql = "Audio"

            abr_raw = f.get('abr')
            abr_clean = ''
            if abr_raw:
                try:
                    abr_clean = str(int(round(float(abr_raw))))
                except Exception:
                    abr_clean = str(abr_raw)

            f_size = f.get('filesize') or f.get('filesize_approx') or 0
            if not f_size and dur > 0:
                tbr = f.get('tbr') or ((f.get('vbr') or 0) + (f.get('abr') or 0))
                if tbr and tbr > 0:
                    f_size = int((float(tbr) * 1000 / 8) * dur)
                elif h >= 2000:
                    f_size = int((12000 * 1000 / 8) * dur)
                elif h >= 1300:
                    f_size = int((6000 * 1000 / 8) * dur)
                elif h >= 900:
                    f_size = int((3500 * 1000 / 8) * dur)
                elif h >= 650:
                    f_size = int((2000 * 1000 / 8) * dur)
                elif h >= 400:
                    f_size = int((1000 * 1000 / 8) * dur)
                elif h > 0:
                    f_size = int((600 * 1000 / 8) * dur)
                elif not has_v and has_a:
                    f_size = int((128 * 1000 / 8) * dur)
            f_size_mb = _format_size(f_size)
            parsed_formats.append({
                'format_id': str(f.get('format_id', '')),
                'url': f['url'],
                'has_video': has_v,
                'has_audio': has_a,
                'height': h,
                'ext': fmt_ext,
                'vcodec': vc,
                'quality_label': ql,
                'filesize': f_size,
                'size_mb': f_size_mb,
                'abr': abr_clean,
            })

        # ── Filter & Deduplicate Formats ──────────────────────────────────────
        # 1. Separate video and audio formats
        raw_video_fmts = [f for f in parsed_formats if f.get('has_video')]
        raw_audio_fmts = [f for f in parsed_formats if not f.get('has_video') and f.get('has_audio')]

        # 2. Pick single best audio format (prefer M4A/MP3, highest bitrate/filesize)
        def _audio_sort_key(f):
            is_m4a = 1 if (f.get('ext') or '').upper() in ('M4A', 'MP3') else 0
            return (is_m4a, f.get('filesize', 0))

        raw_audio_fmts.sort(key=_audio_sort_key, reverse=True)
        best_audio = raw_audio_fmts[:1] if raw_audio_fmts else []
        best_audio_url = best_audio[0]['url'] if best_audio else ''

        # Fallback: if no standalone audio stream exists, use audio from any progressive format that has audio
        # (VideoAudioMuxer extracts only the audio/ track from it, so any progressive MP4 can supply the audio stream)
        if not best_audio_url:
            for v in raw_video_fmts:
                if v.get('has_audio') and v.get('url'):
                    best_audio_url = v['url']
                    break

        # 3. Attach best_audio_url to any video format that lacks audio (enabling post-download muxing)
        for v in raw_video_fmts:
            if not v.get('has_audio') and best_audio_url:
                v['audio_url'] = best_audio_url

        # 4. Filter out very low resolutions (< 300p like 144p, 240p) if better qualities exist
        high_video_fmts = [f for f in raw_video_fmts if f.get('height', 0) >= 300 or f.get('height', 0) == 0]
        if high_video_fmts:
            raw_video_fmts = high_video_fmts

        # 5. Sort video: prefer has_any_audio (muxable or progressive), height desc, H.264/AVC codec (for MediaMuxer compatibility), native audio, MP4, then filesize
        def _video_sort_key(f):
            has_any_audio = 1 if (f.get('has_audio') or f.get('audio_url')) else 0
            vc_str = (f.get('vcodec') or '').lower()
            is_h264 = 1 if ('avc' in vc_str or 'h264' in vc_str) else 0
            native_audio = 1 if f.get('has_audio') else 0
            is_mp4 = 1 if (f.get('ext') or '').upper() == 'MP4' else 0
            return (has_any_audio, f.get('height', 0), is_h264, native_audio, is_mp4, f.get('filesize', 0))

        raw_video_fmts.sort(key=_video_sort_key, reverse=True)

        # 6. Deduplicate video formats: keep single best format per resolution height, cap at top 6
        dedup_videos = []
        seen_heights = set()
        for f in raw_video_fmts:
            h = f.get('height', 0)
            if h not in seen_heights:
                seen_heights.add(h)
                dedup_videos.append(f)
            if len(dedup_videos) >= 6:
                break

        # 7. If no standalone audio format exists (e.g. YouTube adaptive audio blocked),
        # provide an audio download option from the progressive video+audio stream
        if not best_audio and dedup_videos:
            for v in dedup_videos:
                if v.get('has_audio') and v.get('url'):
                    audio_entry = dict(v)
                    audio_entry['format_id'] = str(v.get('format_id', '')) + '-audio'
                    audio_entry['has_video'] = False
                    audio_entry['has_audio'] = True
                    audio_entry['ext'] = 'M4A'
                    audio_entry['quality_label'] = 'Audio'
                    audio_entry['stream_type'] = 'Audio only'
                    dur = info.get('duration') or 0
                    if dur > 0:
                        est_bytes = int((128 * 1000 / 8) * dur)
                        audio_entry['filesize'] = est_bytes
                        audio_entry['size_mb'] = _format_size(est_bytes)
                    else:
                        audio_entry['filesize'] = 0
                        audio_entry['size_mb'] = ''
                    best_audio = [audio_entry]
                    break

        filtered_formats = dedup_videos + best_audio

        print(f"[EXTRACT_VIDEO] Success: returning {len(filtered_formats)} clean formats (from {len(parsed_formats)} raw) for '{title}'")

        raw_pk = str(info.get('pk') or '')
        raw_id = str(info.get('id') or '')
        item_id = raw_pk if raw_pk else (raw_id if raw_id else '')
        single_item = [{
            'id': item_id,
            'pk': raw_pk,
            'url': stream_url,
            'thumbnail': thumbnail,
            'is_video': is_video,
            'mime_type': mime_type,
            'title': title,
            'size_mb': size_mb,
        }]

        return json.dumps({
            'urls': [stream_url] if stream_url else [],
            'url': stream_url,
            'title': title,
            'thumbnail': thumbnail,
            'thumbnails': [thumbnail] if thumbnail else [],
            'size_mb': size_mb,
            'mime_type': mime_type,
            'is_video': is_video,
            'duration': info.get('duration') or 0,
            'formats': filtered_formats,
            'items': single_item,
            'target_id': target_id,
            'error': '',
        })

    except Exception as e:
        print(f"[EXTRACT_VIDEO] Exception raised: {e}")
        return json.dumps({
            'urls': [],
            'url': '',
            'title': '',
            'thumbnail': '',
            'thumbnails': [],
            'size_mb': '0',
            'mime_type': 'video/mp4',
            'is_video': True,
            'error': str(e),
        })
    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except Exception:
                pass
