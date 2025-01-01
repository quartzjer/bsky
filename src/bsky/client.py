import os, re, logging
from atproto import AsyncClient
from typing import Optional
from datetime import datetime


def sanitize(field: str) -> str:
    field = re.sub(r'\.bsky\.social$', '', field)
    field = field.replace('.', '_')
    field = field.replace(' ', '_')
    # strip out anything but letters, digits, and underscores
    base = re.sub(r'[^A-Za-z0-9_]', '', field)
    base = re.sub(r'_+$', '', base)
    if not base:
        base = "_nohandle"
    if base[0].isdigit():
        base = "_" + base
    return base[:16]

class Author:
    def __init__(self, did: str, handle: str, display_name: Optional[str] = None):
        self.did = did
        self.handle = handle
        self.display_name = display_name
        self.nick = sanitize(display_name or handle)

class AT:
    def __init__(self):
        self.client = AsyncClient()
        self.handle = os.getenv('BSKY_HANDLE')
        self.password = os.getenv('BSKY_APP_PASSWORD')
        self.profile = None
        self.cursor = None
        self.seen_posts = set()
        self.oldest = None
        self.posts = []
        self.initialized = False

    async def initialize(self):
        self.profile = await self.client.login(self.handle, self.password)
        # load initial timeline
        data = await self.client.get_timeline(limit=100)
        self.timeline = data.feed
        for fv in self.timeline:
            self.add_fv(fv)
        self.initialized = True

    def add_fv(self, fv):
        post = self.add_post(fv.post)
        if post and fv.reason:
            post._reason = fv.reason
        return post

    def add_post(self, post):
        if post.cid in self.seen_posts:
            return None
        post._at = datetime.fromisoformat(post.indexed_at.replace('Z', '+00:00'))
        if self.oldest is None or post._at < self.oldest:
            self.oldest = post._at
        self.posts.append(post)
        self.seen_posts.add(post.cid)
        return post

    async def sync_timeline(self):
        cursor = None
        new_posts = []

        while True:
            try:
                timeline = await self.client.get_timeline(limit=1, cursor=cursor)
                cursor = None

                for fv in timeline.feed:
                    # only continue if it was a new post
                    post = self.add_fv(fv)
                    if post and self.oldest < post._at:
                        cursor = timeline.cursor
                        new_posts.append(post)
                    else:
                        cursor = None

            except Exception:
                logging.exception("Error checking timeline")
                return []

            if not cursor:
                break

        return new_posts

    async def sync_post(self, uri: str):
        try:
            response = await self.client.get_posts([uri])
            if response and response.posts:
                return response.posts[0]
            return None
        except Exception:
            logging.exception("Error checking timeline")
            return None

    def get_author(self, post) -> Author:
        # Get author from either repost reason or post author
        author = getattr(getattr(post, '_reason', None), 'by', None) or post.author
        return Author(
            did=author.did,
            handle=author.handle,
            display_name=getattr(author, 'display_name', None)
        )

    async def format_post(self, post):
        lines = []

        # handle replies based on context
        reply_ok = False
        if post.record.reply:
            if hasattr(post.record.reply.parent, 'cid'):
                parent_cid = post.record.reply.parent.cid
                if parent_cid in self.seen_posts:
                    reply_ok = True
            if not reply_ok:
                parent = await self.sync_post(post.record.reply.parent.uri)
                if parent:
                    self.add_post(parent)
                    parent_formatted = self.format_record(parent.record)
                    lines.append(f"↩ {parent.author.display_name} (@{parent.author.handle}):")
                    lines.extend(f" | {line}" for line in parent_formatted)

        logging.debug("Post data: %s", post.model_dump_json())

        formatted_lines = self.format_record(post.record)
        formatted_lines.extend(self.format_links(post, formatted_lines))
        formatted_lines.extend(self.format_embed(post.embed, post.uri))
    
        # Handle reposts showing original author and indented
        if hasattr(post, '_reason') and hasattr(post._reason, 'by'):
            lines.append(f"↻ {post.author.display_name} (@{post.author.handle}):")
            lines.extend(f" | {line}" for line in formatted_lines)
        else:
            lines.extend(formatted_lines)
            if reply_ok:
                lines[0] = f"↪ {lines[0]}"

        return lines

    def format_links(self, post, lines):
        links = set()
        
        record = post.record
        if hasattr(record, 'facets') and record.facets:
            for facet in record.facets:
                if facet.py_type == 'app.bsky.richtext.facet':
                    for feature in facet.features:
                        if feature.py_type == 'app.bsky.richtext.facet#link':
                            links.add(feature.uri)

        if post.embed:
            if 'external' in post.embed.py_type:
                links.add(post.embed.external.uri)

        return [f"🔗 {link}" for link in links if not any(link in line for line in lines)]

    def format_record(self, record):
        if not record.text:
            return []
        
        lines = re.split(r'\r\n|\r|\n', record.text.strip())
        lines = [line for line in lines if line.strip()]
        return lines

    def format_embed(self, e, uri):
        if not e:
            return []
        
        if e.py_type == 'app.bsky.embed.images#view':
            lines = []
            for x in e.images:
                alt_text = f"{x.alt.replace('\n',' ').strip()} " if getattr(x, 'alt', None) else ""
                alt_text = alt_text[:47] + "..." if len(alt_text) > 50 else alt_text
                lines.append(f"📷 {alt_text}{x.fullsize or x.thumb} ")
            return lines
        elif e.py_type == 'app.bsky.embed.record#view':
            if hasattr(e.record, 'value'):
                formatted_lines = self.format_record(e.record.value)
                formatted_lines.extend(self.format_embed(e.record.value.embed, e.record.uri))
                lines = [f"💬 {e.record.author.display_name} (@{e.record.author.handle}):"]
                lines.extend(f" | {line}" for line in formatted_lines)
                return lines
        elif e.py_type == 'app.bsky.embed.video#view':
            alt_text = f"{e.alt.replace('\n',' ').strip()} " if getattr(e, 'alt', None) else ""
            did_match = re.search(r'did:plc:[^/]+', uri)
            if did_match and e.cid:
                did = did_match.group(0)
                video_url = f"https://atproto-browser.vercel.app/blob/{did}/{e.cid}"
                return [f"🎥 {alt_text}{video_url}"]
            return []

        return []