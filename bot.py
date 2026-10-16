"""Dual-client bot:
  1. userbot (client): reads news channels and user direct/forwarded news
  2. bot client (bot): delivers videos directly in the bot chat with instant interactive buttons
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

load_dotenv()
import renderer, textutil  # noqa: E402

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
CHANNELS = [c.strip().lstrip("@") for c in os.environ["CHANNELS"].split(",") if c.strip()]
OUT_DIR = Path(os.getenv("OUTPUT_DIR", "output")); OUT_DIR.mkdir(exist_ok=True)
CACHE_DIR = OUT_DIR / "cache"; CACHE_DIR.mkdir(exist_ok=True)
# Final renders are uploaded to Telegram and are not kept on the host after a restart.
for stale_render in OUT_DIR.glob("*.mp4"):
    try:
        stale_render.unlink()
    except OSError as e:
        print("Could not remove stale render:", stale_render, e)
SEEN_FILE = Path("processed.json")

# Two clients (cloud version: sessions live in environment variables, not in files):
# userbot watches private/public channels
USER_SESSION_STRING = os.environ.get("USER_SESSION_STRING", "").strip()
BOT_SESSION_STRING = os.environ.get("BOT_SESSION_STRING", "").strip()
user_client = TelegramClient(StringSession(USER_SESSION_STRING), API_ID, API_HASH)
# bot_client delivers videos in the bot's private chat and handles button callbacks
bot_client = TelegramClient(StringSession(BOT_SESSION_STRING), API_ID, API_HASH)


def start_health_server():
    """Tiny web page so free hosts (and UptimeRobot) see the app as alive."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"smi-bot is running")

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    port = int(os.getenv("PORT", "7860"))
    srv = HTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("health server on port", port)

seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
JOBS_FILE = OUT_DIR / "jobs.json"
MAX_SAVED_JOBS = max(1, int(os.getenv("MAX_SAVED_JOBS", "30")))
try:
    JOBS = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else {}
except Exception:
    JOBS = {}  # job_id -> { "video", "photos", "headline", "caption", "link", "bg_cache" }


def save_jobs():
    # Keep the latest jobs and their re-render assets bounded on persistent hosts.
    old_jobs = list(JOBS)[:-MAX_SAVED_JOBS]
    for k in old_jobs:
        JOBS.pop(k, None)
        cache_dir = CACHE_DIR / k
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        for path in OUT_DIR.iterdir():
            if path.is_file() and path.name.startswith(f"{k}_") and path.suffix == ".mp4":
                try:
                    path.unlink()
                except OSError as e:
                    print("Could not remove old render:", path, e)
        # Base render filename does not have the position suffix.
        base_video = OUT_DIR / f"{k}.mp4"
        if base_video.exists():
            try:
                base_video.unlink()
            except OSError as e:
                print("Could not remove old render:", base_video, e)
    JOBS_FILE.write_text(json.dumps(JOBS, ensure_ascii=False), encoding="utf-8")


def mark(key):
    seen.add(key)
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def bold_first_text(msg):
    """Text of the first bold entity that starts the post (the channel's headline), if any."""
    from telethon.tl.types import MessageEntityBold
    t = msg.raw_text or ""
    for e in (msg.entities or []):
        if isinstance(e, MessageEntityBold) and e.length > 8:
            u = t.encode("utf-16-le")
            return u[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
    return None


_owner = None


async def get_owner():
    """Resolve the owner as an entity the BOT can message (the owner must have pressed Start on the bot once)."""
    global _owner
    if _owner is None:
        try:
            _owner = await bot_client.get_input_entity(OWNER_CHAT_ID)
        except Exception:
            _owner = OWNER_CHAT_ID
    return _owner


async def start_bot_client():
    """Reuse a stored bot session when present; otherwise log in with the bot token."""
    if BOT_SESSION_STRING:
        await bot_client.start()
    else:
        await bot_client.start(bot_token=BOT_TOKEN)


async def send_result(video: Path, headline: str, caption: str, link: str, job_id: str = None, reply_to_msg_id: int = None, show_adjust: bool = True):
    """Deliver video directly through the Bot in the bot chat with the Adjust button attached."""
    caption_text = f"{headline}\n\nSource: {link}"[:1000]

    buttons = None
    if job_id and show_adjust:
        buttons = [[Button.inline("📐 Adjust Headline Position", data=f"adj:{job_id}".encode("utf-8"))]]

    owner = await get_owner()
    from telethon.tl.types import DocumentAttributeVideo
    try:
        vw, vh, vdur = renderer.probe(video)
        sent_msg = await bot_client.send_file(
            owner,
            str(video),
            caption=caption_text,
            mime_type="video/mp4",
            force_document=False,
            attributes=[DocumentAttributeVideo(duration=int(round(vdur)), w=vw, h=vh, supports_streaming=True)],
            supports_streaming=True,
            buttons=buttons,
            reply_to=reply_to_msg_id
        )

        if caption:
            for i in range(0, len(caption), 4000):
                await bot_client.send_message(owner, caption[i:i + 4000])

        return sent_msg
    finally:
        # The delivered MP4 is disposable; Telegram has its own uploaded copy.
        try:
            Path(video).unlink(missing_ok=True)
        except OSError as e:
            print("Could not remove sent render:", video, e)


async def process(messages, link: str, card_pos: str = None, reply_to_msg_id: int = None, allow_adjust: bool = True):
    """messages: list of Telethon messages (one post, or an album)."""
    text_msg = next((m for m in messages if (m.raw_text or "").strip()), messages[0])
    text = text_msg.raw_text or ""
    headline, body = textutil.split_headline_body(text, bold_first_text(text_msg))
    if not headline:
        print("no text, skipping", link); return

    job_id = f"{messages[0].chat_id}_{messages[0].id}".replace("-", "m")
    job_dir = CACHE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    photos, video = [], None
    for i, m in enumerate(messages):
        if m.video or (m.document and (m.document.mime_type or "").startswith("video/")):
            vpath = str(job_dir / f"v_{i}.mp4")
            if not os.path.exists(vpath):
                await m.download_media(file=vpath)
            video = video or vpath
        elif m.photo:
            ppath = str(job_dir / f"p_{i}.jpg")
            if not os.path.exists(ppath):
                await m.download_media(file=ppath)
            photos.append(ppath)

    if not video and not photos:
        print("post has no photo/video, skipping", link); return

    pos_suffix = f"_{card_pos}" if card_pos else ""
    out = OUT_DIR / f"{job_id}{pos_suffix}.mp4"
    bg_cache = str(job_dir / "bg.mp4")

    print("rendering", link, f"[pos={card_pos}]", "|", headline[:60])
    if video:
        await asyncio.to_thread(renderer.render_video, video, headline, out, card_pos=card_pos, bg_cache_path=bg_cache)
    else:
        await asyncio.to_thread(renderer.render_images, photos, headline, out, card_pos=card_pos, bg_cache_path=bg_cache)

    caption = await asyncio.to_thread(textutil.paraphrase, body, headline)

    if allow_adjust:
        JOBS[job_id] = {
            "video": video,
            "photos": photos,
            "headline": headline,
            "caption": caption,
            "link": link,
            "bg_cache": bg_cache
        }
        save_jobs()

    await send_result(out, headline, caption, link, job_id=job_id, reply_to_msg_id=reply_to_msg_id, show_adjust=allow_adjust)
    print("sent to bot chat:", out)


def link_for(entity, msg_id):
    u = getattr(entity, "username", None)
    return f"https://t.me/{u}/{msg_id}" if u else f"(channel {entity.id}, post {msg_id})"


DIRECTIONS = {"up", "down", "left", "right", "up_left", "up_right", "down_left", "down_right", "center"}


def _job_state(job):
    """Where the headline card currently is (saved after every move; first time read from the first render)."""
    if job.get("pos"):
        return job["pos"]
    try:
        m = json.loads(Path(str(job.get("bg_cache")) + ".json").read_text())
        return {"x": m.get("card_x"), "y": m.get("card_y")}
    except Exception:
        return {}


async def re_render_job(job_id: str, direction: str):
    job = JOBS.get(job_id)
    if not job:
        await bot_client.send_message(await get_owner(), "⚠️ Post data not found. Send the post/link to me again.")
        return

    if direction in DIRECTIONS:
        new_state, moved = await asyncio.to_thread(renderer.move_card, job["headline"], _job_state(job), direction)
        if not moved:
            await bot_client.send_message(await get_owner(), "↔️ The headline is already at the limit in that direction.")
            return
        job["pos"] = new_state
        save_jobs()
        card_pos = (new_state["x"], new_state["y"])
        out = OUT_DIR / f"{job_id}_{new_state['x']}x{new_state['y']}.mp4"
    else:  # old buttons from earlier messages (top, upper_mid, ...)
        card_pos = direction
        out = OUT_DIR / f"{job_id}_{direction}.mp4"

    print(f"Re-rendering {job_id}: {direction} -> {card_pos}")
    status = await bot_client.send_message(await get_owner(), f"⏳ Moving headline **{direction.replace('_', '-').upper()}**...")

    bg_cache = job.get("bg_cache")
    if bg_cache and os.path.exists(bg_cache):
        await asyncio.to_thread(renderer.re_render_fast, bg_cache, job["headline"], out, card_pos=card_pos)
    elif job["video"]:
        await asyncio.to_thread(renderer.render_video, job["video"], job["headline"], out, card_pos=card_pos)
    else:
        await asyncio.to_thread(renderer.render_images, job["photos"], job["headline"], out, card_pos=card_pos)

    try:
        await send_result(out, job["headline"], "", job["link"], job_id=job_id)
        await status.delete()
    except Exception as e:
        await status.edit(f"❌ Failed to send re-rendered video: {e}")
        raise
    print("sent re-rendered to bot chat:", out)


def setup_bot_handlers():
    """Register native Telethon callback query handlers on the bot client."""
    @bot_client.on(events.CallbackQuery())
    async def on_callback(event):
        data = event.data.decode("utf-8")
        if event.sender_id != OWNER_CHAT_ID:
            return

        if data.startswith("adj:"):
            job_id = data.split(":", 1)[1]
            await event.answer("Each tap moves the headline one step. Center resets.")
            def pb(label, p):
                return Button.inline(label, data=f"setp:{job_id}:{p}".encode("utf-8"))
            pos_buttons = [
                [pb("↖️ Up-Left", "up_left"), pb("⬆️ Up", "up"), pb("↗️ Up-Right", "up_right")],
                [pb("⬅️ Left", "left"), pb("🎯 Center", "center"), pb("➡️ Right", "right")],
                [pb("↙️ Down-Left", "down_left"), pb("⬇️ Down", "down"), pb("↘️ Down-Right", "down_right")],
            ]
            await bot_client.send_message(
                await get_owner(),
                "📍 **Move the headline** - each tap nudges it one step from where it is now (you can tap several times). 🎯 Center resets it.",
                buttons=pos_buttons,
                reply_to=event.message_id
            )

        elif data.startswith("setp:"):
            _, job_id, pos_preset = data.split(":", 2)
            await event.answer("Moving...")
            async def _run_job():
                try:
                    await re_render_job(job_id, pos_preset)
                except Exception as e:
                    print("ERROR re-rendering:", e)
                    await bot_client.send_message(await get_owner(), f"❌ Re-render failed: {e}")
            asyncio.create_task(_run_job())

    @bot_client.on(events.NewMessage(chats=[OWNER_CHAT_ID]))
    async def on_bot_msg(event):
        """Allow user to directly forward news or links inside the bot chat."""
        m = event.message
        text = m.raw_text or ""
        link_match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", text)

        if m.forward:
            print("Received forwarded news inside bot chat...")
            src_chat = await m.forward.get_chat()
            link = f"https://t.me/{getattr(src_chat, 'username', '')}/{m.forward.channel_post or m.id}"
            if m.grouped_id:
                grp = [x async for x in bot_client.iter_messages(event.chat_id, min_id=m.id - 12, max_id=m.id + 12)
                       if x.grouped_id == m.grouped_id]
                messages = sorted(grp, key=lambda x: x.id) if grp else [m]
            else:
                messages = [m]
            await process(messages, link, reply_to_msg_id=m.id)

        elif link_match:
            ch_name, msg_id = link_match.group(1), int(link_match.group(2))
            print(f"User sent link to {ch_name}/{msg_id}")
            try:
                target_msg = await user_client.get_messages(ch_name, ids=msg_id)
                if target_msg:
                    if target_msg.grouped_id:
                        grp = [x async for x in user_client.iter_messages(ch_name, min_id=msg_id - 12, max_id=msg_id + 12)
                               if x.grouped_id == target_msg.grouped_id]
                        messages = sorted(grp, key=lambda x: x.id) if grp else [target_msg]
                    else:
                        messages = [target_msg]
                    await process(messages, f"https://t.me/{ch_name}/{msg_id}", reply_to_msg_id=m.id)
            except Exception as e:
                print("Could not fetch message by link:", e)

        elif m.media and (m.photo or m.video):
            await process([m], "Direct upload", reply_to_msg_id=m.id)


async def run_poll_once():
    """Process a bounded backlog for one ephemeral GitHub Actions runner."""
    state_file = Path("workflow_state.json")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    except Exception as e:
        raise RuntimeError(f"Could not read {state_file}: {e}") from e
    cursors = state.setdefault("cursors", {})
    failures = state.setdefault("failures", {})
    max_posts = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "10")))

    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise SystemExit("USER_SESSION_STRING is missing or no longer valid. Run gen_session.py again and update the repository secret.")
    await start_bot_client()

    def save_state():
        temp_file = state_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(state_file)

    try:
        for channel in CHANNELS:
            entity = await user_client.get_entity(channel)
            channel_key = getattr(entity, "username", None) or str(entity.id)

            # Start at the latest post on the first workflow run to avoid rendering old channel history.
            if channel_key not in cursors:
                latest = await user_client.get_messages(entity, limit=1)
                cursors[channel_key] = latest[0].id if latest else 0
                save_state()
                print(f"Initialized {channel_key} at message {cursors[channel_key]}; new posts will be processed on later runs.")
                continue

            cursor = int(cursors[channel_key])
            incoming = [m async for m in user_client.iter_messages(entity, min_id=cursor, limit=500, reverse=True)]
            grouped = {}
            for message in incoming:
                group_key = f"album:{message.grouped_id}" if message.grouped_id else f"message:{message.id}"
                grouped.setdefault(group_key, []).append(message)
            batches = sorted(grouped.values(), key=lambda group: min(m.id for m in group))

            handled = 0
            for messages in batches:
                if handled >= max_posts:
                    print(f"Reached MAX_POSTS_PER_RUN={max_posts} for {channel_key}; remaining posts stay queued.")
                    break
                messages.sort(key=lambda m: m.id)
                batch_id = max(m.id for m in messages)
                retry_key = f"{channel_key}:{batch_id}"
                has_media = any(m.photo or m.video or (m.document and (m.document.mime_type or "").startswith("video/")) for m in messages)

                if has_media:
                    try:
                        await process(messages, link_for(entity, messages[0].id), allow_adjust=False)
                    except Exception as e:
                        attempts = int(failures.get(retry_key, 0)) + 1
                        failures[retry_key] = attempts
                        save_state()
                        print(f"ERROR processing {retry_key} (attempt {attempts}/3): {e!r}")
                        if attempts < 3:
                            raise
                        try:
                            await bot_client.send_message(
                                await get_owner(),
                                f"I skipped channel post {link_for(entity, messages[0].id)} after 3 failed attempts. Check the GitHub Actions log."
                            )
                        except Exception as notify_error:
                            print("Could not send failure notice:", repr(notify_error))
                        failures.pop(retry_key, None)

                cursors[channel_key] = batch_id
                save_state()
                handled += 1

            print(f"Checked {channel_key}: cursor is {cursors[channel_key]}.")
    finally:
        await bot_client.disconnect()
        await user_client.disconnect()


async def run(test_last):
    start_health_server()
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise SystemExit("USER_SESSION_STRING is missing or no longer valid. Run gen_session.py again and update the secret.")
    await start_bot_client()
    setup_bot_handlers()
    try:
        me = await bot_client.get_me()
        await bot_client.send_message(await get_owner(), f"✅ @{me.username} is online. Videos will be delivered here.")
    except Exception as e:
        print(f"\n!! The BOT cannot message OWNER_CHAT_ID={OWNER_CHAT_ID}: {e}\n"
              "   Open your bot in Telegram, press Start, and check OWNER_CHAT_ID (run get_chat_id.py).")
        raise

    entities = [await user_client.get_entity(c) for c in CHANNELS]

    if test_last:
        for ent in entities:
            posts = [m async for m in user_client.iter_messages(ent, limit=test_last * 10) if m.media]
            done = set()
            for m in posts:
                key = m.grouped_id or m.id
                if key in done:
                    continue
                done.add(key)
                grp = [m] if not m.grouped_id else sorted(
                    [x async for x in user_client.iter_messages(ent, min_id=m.id - 12, max_id=m.id + 12)
                     if x.grouped_id == m.grouped_id], key=lambda x: x.id)
                try:
                    await process(grp, link_for(ent, grp[0].id))
                except Exception as e:
                    print("ERROR processing post (skipped, bot keeps running):", repr(e))
                if len(done) >= test_last:
                    break
        print("\n✅ Test-last complete! Bot is running and ready for 'Adjust Position' clicks inside your bot chat. Press Ctrl+C to stop.")
        await bot_client.run_until_disconnected()
        return

    # 1. Listen for new posts from tracked channels
    @user_client.on(events.Album(chats=entities))
    async def on_album(ev):
        key = f"{ev.chat_id}:{ev.messages[0].id}"
        if key in seen: return
        mark(key)
        try:
            await process(list(ev.messages), link_for(await ev.get_chat(), ev.messages[0].id))
        except Exception as e:
            print("ERROR in album:", e)

    @user_client.on(events.NewMessage(chats=entities))
    async def on_msg(ev):
        if ev.message.grouped_id:
            return
        key = f"{ev.chat_id}:{ev.message.id}"
        if key in seen: return
        mark(key)
        try:
            await process([ev.message], link_for(await ev.get_chat(), ev.message.id))
        except Exception as e:
            print("ERROR in post:", e)

    print("Watching channels:", ", ".join(CHANNELS), "- Press Ctrl+C to stop")
    # Run both clients
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-last", type=int, default=0)
    ap.add_argument("--poll-once", action="store_true", help="Process new channel posts and exit (for scheduled runners).")
    args = ap.parse_args()
    asyncio.run(run_poll_once() if args.poll_once else run(args.test_last))
