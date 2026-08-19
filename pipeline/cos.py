"""Image bytes -> Tencent COS -> a public cdn.bookmepk.com URL.

In:  a remote image URL (Agoda's CDN).
Out: a public URL on Bookme's own CDN, or None if the image is unusable.

The brief is explicit that Bookme hosts the actual bytes rather than hotlinking
a competitor's CDN, so every image is downloaded and re-uploaded. Object keys
are the MD5 of the IMAGE BYTES, which makes upload idempotent: the same picture
re-fetched lands on the same key, so a re-run overwrites nothing, duplicates
nothing, and an object orphaned by a crashed run is silently reused rather than
leaked.
"""
import hashlib
import mimetypes
import os
import threading
import time

import requests

from . import config

config.load_env()

BUCKET = os.getenv("S3_BUCKET")
PREFIX = (os.getenv("S3_PREFIX") or "").strip("/")
ENDPOINT = os.getenv("S3_ENDPOINT_URL") or None
REGION = os.getenv("AWS_REGION")
PUBLIC = os.getenv("S3_PUBLIC_URL")

MIME_TYPE = os.getenv("ATTACHMENT_MIME_TYPE", "image")
ATTACHMENT_SIZE = os.getenv("ATTACHMENT_SIZE", "MED")
ATTACHABLE_TYPE = os.getenv("ATTACHABLE_TYPE", "App\\Models\\Hotels\\Room")
ATTACHMENT_CATEGORY = os.getenv("ATTACHMENT_CATEGORY", "room-image")

# Agoda serves images from its own CDN with no auth, but a bare requests UA gets
# 403s on some edges -- reuse the one the rest of the project already presents.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_client = None
_seen = set()          # keys this process has already confirmed present
# run.py mirrors a hotel's images concurrently (see mirror_all_images) -- the
# download+upload themselves need no coordination (independent URLs, and COS
# keys are content-addressed so a genuine double-upload is harmless), but two
# threads racing on `key in _seen` / `_seen.add(key)` is a real, if narrow,
# check-then-act race. The cost of losing it is only a redundant head/put
# call, never wrong data, but the lock is one line and removes the doubt.
_seen_lock = threading.Lock()


def client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("s3", region_name=REGION, endpoint_url=ENDPOINT)
    return _client


def public_url(key):
    return PUBLIC.format(bucket=BUCKET, region=REGION, key=key)


def _ext(url, content_type):
    """Extension for the stored object. Content-Type is authoritative -- an
    Agoda image URL often ends in a size token, not a file extension."""
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    tail = os.path.splitext(url.split("?")[0])[1].lower()
    return tail if tail in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"


# A dead link is a fact about the image; a refused connection is a fact about
# the network. Retrying the first is pointless, and NOT retrying the second
# turns a thirty-second wifi drop into a permanently pictureless room.
NET_TRIES = 4
BASE_DELAY = 2


def _fetch(url, session, timeout):
    """Download one image. Returns (body, content_type) or None if the image is
    genuinely unusable. Raises on transport failure, so the caller can tell a
    404 apart from an outage instead of recording both as "no picture".
    """
    get = (session or requests).get
    r = get(url, headers={"User-Agent": _UA}, timeout=timeout)
    if 400 <= r.status_code < 500:
        return None                       # gone, forbidden -- a fact about the
                                          # image; retrying cannot change it
    r.raise_for_status()                  # 5xx / redirect loops -> transient
    body, ctype = r.content, r.headers.get("Content-Type", "")
    if len(body) < config.MIN_IMAGE_BYTES or not body.startswith(
            (b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF")):
        return None                       # too small, or not actually an image
    return body, ctype


def mirror(url, session=None, timeout=30):
    """Download one image and store it. Returns its public URL, or None.

    None means the image is UNUSABLE -- a dead link, an HTML error page served
    with an image content-type, a 200-byte placeholder. All things an upstream
    CDN does routinely, none of which should reach the site.

    None does NOT mean "the network was down". Transport failures and 5xx are
    retried with exponential backoff first, and only a failure that outlives
    the whole budget returns None. Before this, a single dropped packet
    returned None indistinguishably from a dead link, so a brief outage
    published a hotel's rooms with no pictures at all -- recoverable via the
    needs_image_backfill path, but only after a full re-run of that hotel.
    """
    got, last = None, None
    for i in range(NET_TRIES):
        try:
            got = _fetch(url, session, timeout)
            break                         # decided: usable, or definitely not
        except Exception as e:            # transport, 5xx, truncated body
            last = e
            if i < NET_TRIES - 1:
                time.sleep(BASE_DELAY * (2 ** i))
    else:
        print(f"  image fetch gave up after {NET_TRIES} tries "
              f"({type(last).__name__}): {url[:70]}")
        return None
    if got is None:
        return None
    body, ctype = got

    key = f"{PREFIX}/{hashlib.md5(body).hexdigest()}{_ext(url, ctype)}"
    with _seen_lock:
        already = key in _seen
    if already:
        return public_url(key)
    c = client()
    # The STORE half needs its own retry: COS can be reachable when the source
    # CDN is not, and vice versa. Losing the upload after paying for the
    # download is the most wasteful failure available here.
    for i in range(NET_TRIES):
        try:
            try:
                c.head_object(Bucket=BUCKET, Key=key)   # already stored by any run
            except Exception:
                c.put_object(Bucket=BUCKET, Key=key, Body=body,
                             ContentType=ctype.split(";")[0] or "image/jpeg")
            break
        except Exception as e:
            if i == NET_TRIES - 1:
                print(f"  COS upload gave up after {NET_TRIES} tries "
                      f"({type(e).__name__}): {key}")
                return None
            time.sleep(BASE_DELAY * (2 ** i))
    # Marked done only AFTER the object provably exists. Two threads racing on
    # the identical URL can both miss `_seen` and both reach here -- that's a
    # redundant HEAD/PUT, not a bug, because the key is content-addressed and
    # a same-key PUT is idempotent. What must never happen is a caller getting
    # a "done" URL back before the object is actually there.
    with _seen_lock:
        _seen.add(key)
    return public_url(key)


if __name__ == "__main__":
    # Round-trip one real image: it must upload, be publicly readable, and a
    # second mirror() of the same bytes must return the identical key.
    assert BUCKET and PREFIX and PUBLIC, "COS settings missing from .env"
    # A real room image as Agoda serves them (via the bstatic CDN), query
    # string and all -- the shape _ext has to cope with.
    src = ("https://q-xx.bstatic.com/xdata/images/hotel/max1024x768/397874591.jpg"
           "?k=7c8b54ab4d3215ec2a558fe166e3d7309156f1b3db6e728d91e8f3ebf7f86d35&o=")
    u1 = mirror(src)
    assert u1, f"mirror returned nothing for {src}"
    _seen.clear()
    assert mirror(src) == u1, "content-addressing is not idempotent"
    assert requests.get(u1, timeout=30).ok, f"uploaded object not public: {u1}"
    print(f"OK: {src.rsplit('/', 1)[-1]} -> {u1}")

    # run.py's mirror_all_images() calls this from several threads at once.
    # Regression test for a real bug caught during that change: an earlier
    # version marked `_seen` BEFORE the upload completed, so a second thread
    # racing on the identical URL could get back a URL for an object that did
    # not exist yet. Every thread's returned URL must be immediately fetchable.
    from concurrent.futures import ThreadPoolExecutor
    _seen.clear()
    with ThreadPoolExecutor(max_workers=8) as ex:
        urls = list(ex.map(lambda _: mirror(src), range(8)))
    assert all(u == u1 for u in urls), f"racing threads returned different urls: {urls}"
    assert all(requests.get(u, timeout=30).ok for u in urls), \
        "a racing thread returned a URL before the object was actually there"
    print(f"OK: 8 concurrent mirror() calls on the same URL all returned "
          f"{u1!r}, all immediately fetchable")
