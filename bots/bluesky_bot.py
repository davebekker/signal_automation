import os
import io
import mimetypes
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List, Set

import httpx
from PIL import Image
from atproto import Client, models
from atproto_client.exceptions import InvokeTimeoutError
from utils.tools import logger

from google import genai
from google.genai import types
import asyncio

class BlueskyBot:
    def __init__(self):
        self.handle = os.getenv("BLUESKY_HANDLE")
        self.app_password = os.getenv("BLUESKY_APP_PASSWORD")
        self.client = Client()
        self.max_chars = 300
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.gemini_client = genai.Client(api_key=self.gemini_api_key) if self.gemini_api_key else None
        # cache of followed DIDs
        self._follows_cache: Set[str] = set()
        self._follows_cache_loaded = False

    def _login(self, force: bool = False):
        """Ensure session is active. Force re-login when token is revoked/expired."""
        if force:
            self.client = Client()
            self.client.login(self.handle, self.app_password)
            return

        if not getattr(self.client, "me", None):
            self.client.login(self.handle, self.app_password)

    async def _generate_alt_text(self, image_path: str, post_text: str = "") -> str:
        """
        Generate concise, accessibility-focused alt text for a Bluesky image.
        Falls back safely if Gemini is unavailable.
        """
        if not self.gemini_client:
            return (post_text[:100] or "Image")

        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()

            mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"

            prompt = (
                "Write concise, useful alt text for this image for a Bluesky post. "
                "Describe the visible content objectively. Do not start with 'Image of'. "
                "Do not include hashtags, emojis, jokes, or speculation. "
                "Keep it under 1000 characters."
                "If the image is mainly text, include all text up to the 1000 character overall limit."
            )

            if post_text:
                prompt += f"\n\nThe accompanying post text is: {post_text}"

            response = self.gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt,
                ],
            )

            alt = (response.text or "").strip()
            alt = " ".join(alt.split())

            return alt[:1000] if alt else (post_text[:100] or "Image")

        except Exception:
            logger.exception("Failed to generate Gemini alt text")
            return (post_text[:100] or "Image")

    def _split_text(self, text: str) -> List[str]:
        """Split text into chunks < 300 chars, breaking on whole words."""
        text = (text or "").strip()

        if not text:
            return [""]

        if len(text) <= self.max_chars:
            return [text]

        posts = []
        words = text.split(" ")
        current_post = ""

        for word in words:
            if len(current_post) + len(word) + 5 > self.max_chars:
                posts.append(current_post.strip())
                current_post = word + " "
            else:
                current_post += word + " "

        if current_post:
            posts.append(current_post.strip())

        total = len(posts)
        return [f"{p} ({i+1}/{total})" for i, p in enumerate(posts)]

    def _compress_image_for_bluesky(self, image_path: str) -> tuple[bytes, int, int]:
        """Compress image to stay under Bluesky's size limit and return final dimensions."""
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            quality_steps = [85, 80, 75, 70, 65, 60, 55, 50, 45, 40]
            max_sizes = [None, 2200, 1800, 1600, 1400, 1200, 1000, 800]

            for max_dim in max_sizes:
                working = img.copy()

                if max_dim is not None and max(working.size) > max_dim:
                    working.thumbnail((max_dim, max_dim), Image.LANCZOS)

                for quality in quality_steps:
                    buf = io.BytesIO()
                    working.save(buf, format="JPEG", quality=quality, optimize=True)
                    size = buf.tell()

                    if size <= 950_000:
                        logger.info(
                            "Compressed image for Bluesky: %s bytes, size=%s, quality=%s",
                            size,
                            working.size,
                            quality,
                        )
                        return buf.getvalue(), working.width, working.height

            raise ValueError("Could not compress image below Bluesky size limit")

    def _make_reply_ref(self, reply_ref: Dict[str, str]):
        return models.AppBskyFeedPost.ReplyRef(
            root=models.ComAtprotoRepoStrongRef.Main(
                uri=reply_ref["root_uri"],
                cid=reply_ref["root_cid"],
            ),
            parent=models.ComAtprotoRepoStrongRef.Main(
                uri=reply_ref["parent_uri"],
                cid=reply_ref["parent_cid"],
            ),
        )

    def _like_parent_if_needed(self, reply_ref: Dict[str, str]) -> None:
        """Like the parent post when replying, unless it is our own post."""
        try:
            self._login()

            my_did = getattr(self.client.me, "did", None)
            parent_author_did = reply_ref.get("parent_author_did")

            if my_did and parent_author_did and parent_author_did == my_did:
                logger.info("Skipping like on own Bluesky post: %s", reply_ref.get("parent_uri"))
                return

            parent_uri = reply_ref.get("parent_uri")
            parent_cid = reply_ref.get("parent_cid")

            if not parent_uri or not parent_cid:
                logger.warning("Missing parent_uri/parent_cid; cannot like parent post")
                return

            self.client.like(parent_uri, parent_cid)
            logger.info("Liked parent Bluesky post before replying: %s", parent_uri)

        except Exception:
            logger.exception("Failed to like parent Bluesky post before replying")

    def refresh_follows_cache(self) -> Set[str]:
        """Fetch all accounts the current account follows."""
        self._login()

        follows: Set[str] = set()
        cursor = None
        actor = self.client.me.did

        while True:
            resp = self.client.get_follows(actor=actor, cursor=cursor, limit=100)
            page = getattr(resp, "follows", []) or []

            for profile in page:
                did = getattr(profile, "did", None)
                if did:
                    follows.add(did)

            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

        self._follows_cache = follows
        self._follows_cache_loaded = True

        logger.info("Loaded %s followed accounts into Bluesky follows cache", len(follows))
        return follows

    def get_followed_dids(self, refresh: bool = False) -> Set[str]:
        if refresh or not self._follows_cache_loaded:
            return self.refresh_follows_cache()
        return self._follows_cache

    async def _handle_command_once(
        self,
        text: str,
        image_path: Optional[str] = None,
        reply_ref: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        self._login()

        text = (text or "").strip()
        chunks = self._split_text(text)

        root_post = None
        parent_post = None
        posted_posts = []

        for i, chunk in enumerate(chunks):
            embed = None

            if i == 0 and image_path:
                alt_text = await self._generate_alt_text(image_path, chunk)

                img_data, width, height = await asyncio.to_thread(
                    self._compress_image_for_bluesky,
                    image_path,
                )

                upload = await asyncio.to_thread(self.client.upload_blob, img_data)

                embed = models.AppBskyEmbedImages.Main(
                    images=[
                        models.AppBskyEmbedImages.Image(
                            alt=alt_text,
                            image=upload.blob,
                            aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(
                                width=width,
                                height=height,
                            ),
                        )
                    ]
                )
                # embed = models.AppBskyEmbedImages.Main(
                #     images=[
                #         models.AppBskyEmbedImages.Image(
                #             alt=(chunk[:100] or "Image"),
                #             image=upload.blob,
                #         )
                #     ]
                # )

            outgoing_reply_to = None

            if i == 0 and reply_ref:
                logger.info("About to like parent before Bluesky reply: %s", reply_ref.get("parent_uri"))
                await asyncio.to_thread(self._like_parent_if_needed, reply_ref)
                outgoing_reply_to = self._make_reply_ref(reply_ref)

            logger.info("About to send Bluesky post chunk %s/%s", i + 1, len(chunks))
            resp = await asyncio.to_thread(
                self.client.send_post,
                text=chunk,
                reply_to=outgoing_reply_to,
                embed=embed,
            )
            logger.info("Sent Bluesky post chunk %s/%s: %s", i + 1, len(chunks), resp.uri)

            current_ref = models.ComAtprotoRepoStrongRef.Main(
                cid=resp.cid,
                uri=resp.uri,
            )

            if i == 0:
                if reply_ref:
                    root_uri = reply_ref["root_uri"]
                    root_cid = reply_ref["root_cid"]
                else:
                    root_uri = resp.uri
                    root_cid = resp.cid
                root_post = models.ComAtprotoRepoStrongRef.Main(
                    cid=root_cid,
                    uri=root_uri,
                )

            posted_posts.append(
                {
                    "uri": resp.uri,
                    "cid": resp.cid,
                    "text": chunk,
                    "root_uri": root_post.uri,
                    "root_cid": root_post.cid,
                    "parent_uri": outgoing_reply_to.parent.uri if outgoing_reply_to else None,
                    "parent_cid": outgoing_reply_to.parent.cid if outgoing_reply_to else None,
                }
            )

            parent_post = current_ref

        return {
            "ok": True,
            "message": f"Posted to Bluesky ({len(chunks)} post(s)).",
            "posts": posted_posts,
            "root_uri": root_post.uri if root_post else None,
            "root_cid": root_post.cid if root_post else None,
            "last_uri": parent_post.uri if parent_post else None,
            "last_cid": parent_post.cid if parent_post else None,
        }

    async def handle_command(
        self,
        text: str,
        image_path: Optional[str] = None,
        reply_ref: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        try:
            return await self._handle_command_once(text, image_path, reply_ref)
        except Exception as e:
            err = str(e) or e.__class__.__name__
            if "ExpiredToken" in err or "Token has been revoked" in err:
                logger.warning("Bluesky token expired/revoked; re-authenticating and retrying once")
                try:
                    self._login(force=True)
                    return await self._handle_command_once(text, image_path, reply_ref)
                except Exception as retry_e:
                    logger.exception("Bluesky retry after re-login failed")
                    e = retry_e

            logger.exception("Bluesky Error during handle_command")
            return {
                "ok": False,
                "message": f"Failed to post to Bluesky: {err}",
                "error": err,
                "posts": [],
            }

    def _list_reply_notifications_once(
        self,
        limit: int = 50,
        cursor: Optional[str] = None,
        followed_only: bool = False,
        refresh_follows: bool = False,
        include_mentions: bool = False,
    ) -> Dict[str, Any]:
        self._login()

        raw = self.client.app.bsky.notification.list_notifications(
            models.AppBskyNotificationListNotifications.Params(
                limit=limit,
                cursor=cursor,
            )
        )

        followed_dids = None
        if followed_only:
            followed_dids = self.get_followed_dids(refresh=refresh_follows)

        out = []
        for n in getattr(raw, "notifications", []) or []:
            reason = getattr(n, "reason", None)
            author = getattr(n, "author", None)
            author_did = getattr(author, "did", None)

            if reason != "reply":
                if not (include_mentions and reason == "mention"):
                    continue

            if followed_only and author_did not in followed_dids:
                continue

            out.append(n)

        return {
            "notifications": out,
            "cursor": getattr(raw, "cursor", None),
        }

    def list_reply_notifications(
        self,
        limit: int = 50,
        cursor: Optional[str] = None,
        followed_only: bool = False,
        refresh_follows: bool = False,
        include_mentions: bool = False,
    ) -> Dict[str, Any]:
        try:
            return self._list_reply_notifications_once(
                limit=limit,
                cursor=cursor,
                followed_only=followed_only,
                refresh_follows=refresh_follows,
                include_mentions=include_mentions,
            )
        except Exception as e:
            err = str(e)
            if "ExpiredToken" in err or "Token has been revoked" in err:
                logger.warning("Bluesky notification token expired/revoked; re-authenticating")
                self._login(force=True)
                return self._list_reply_notifications_once(
                    limit=limit,
                    cursor=cursor,
                    followed_only=followed_only,
                    refresh_follows=refresh_follows,
                    include_mentions=include_mentions,
                )
            raise

    def _get_posts_response(self, uri: str):
        self._login()
        # Be tolerant of SDK signature differences.
        try:
            return self.client.get_posts([uri])
        except TypeError:
            try:
                return self.client.get_posts(uris=[uri])
            except TypeError:
                return self.client.app.bsky.feed.get_posts(
                    models.AppBskyFeedGetPosts.Params(uris=[uri])
                )

    def get_post_image_urls(self, uri: str) -> List[str]:
        """Fetch a post view and return any image URLs from its embed."""
        urls: List[str] = []

        try:
            resp = self._get_posts_response(uri)
            posts = getattr(resp, "posts", []) or []
            if not posts:
                return urls

            post = posts[0]
            embed = getattr(post, "embed", None)
            if not embed:
                return urls

            images = getattr(embed, "images", None) or []
            for img in images:
                fullsize = getattr(img, "fullsize", None)
                thumb = getattr(img, "thumb", None)
                if fullsize:
                    urls.append(fullsize)
                elif thumb:
                    urls.append(thumb)

        except Exception:
            logger.exception("Failed to fetch post images for %s", uri)

        return urls

    async def download_image_from_url(self, url: str, out_dir: str = "/tmp/bluesky_images") -> Optional[str]:
        os.makedirs(out_dir, exist_ok=True)

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                ext = mimetypes.guess_extension(content_type) or ".jpg"

                parsed = urlparse(url)
                basename = os.path.basename(parsed.path) or f"image{ext}"
                if "." not in basename:
                    basename = f"{basename}{ext}"

                out_path = os.path.join(out_dir, basename)
                with open(out_path, "wb") as f:
                    f.write(resp.content)

                return out_path

        except Exception:
            logger.exception("Failed to download Bluesky image from %s", url)
            return None

    def extract_reply_notification(self, notif) -> Optional[Dict[str, Any]]:
        """Normalize a reply notification into a simpler dict."""
        try:
            author = getattr(notif, "author", None)
            record = getattr(notif, "record", None)

            text = getattr(record, "text", None)
            uri = getattr(notif, "uri", None)
            cid = getattr(notif, "cid", None)
            reason_subject = getattr(notif, "reason_subject", None)

            reply = getattr(record, "reply", None)
            root = getattr(reply, "root", None) if reply else None
            parent = getattr(reply, "parent", None) if reply else None

            return {
                "uri": uri,
                "cid": cid,
                "text": text or "",
                "author_did": getattr(author, "did", None),
                "author_handle": getattr(author, "handle", None),
                "author_display_name": getattr(author, "display_name", None),
                "reason_subject": reason_subject,
                "root_uri": getattr(root, "uri", None),
                "root_cid": getattr(root, "cid", None),
                "parent_uri": getattr(parent, "uri", None),
                "parent_cid": getattr(parent, "cid", None),
                "indexed_at": getattr(notif, "indexed_at", None),
            }
        except Exception:
            logger.exception("Failed to parse Bluesky notification")
            return None
