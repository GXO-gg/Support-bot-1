import discord
from discord.ext import commands, tasks
import asyncio
import os
import yt_dlp
import logging

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config (set these in your .env or Railway environment variables) ──────────
TOKEN            = os.environ["DISCORD_TOKEN"]
GUILD_ID         = int(os.environ["GUILD_ID"])           # Your server ID
SUPPORT_VC_ID    = int(os.environ["SUPPORT_VC_ID"])      # Voice channel to sit in
NOTIFY_CHANNEL_ID= int(os.environ["NOTIFY_CHANNEL_ID"])  # Text channel for alerts
OWNER_ID         = int(os.environ["OWNER_ID"])           # Your Discord user ID

# Lofi 24/7 YouTube stream (chill waiting room vibe)
LOFI_STREAM_URL  = "https://www.youtube.com/watch?v=jfKfPfyJRdk"

# ─── YT-DLP options ────────────────────────────────────────────────────────────
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


async def get_audio_url(url: str) -> str:
    """Extract direct audio stream URL from YouTube."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return data["url"]


# ─── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ─── Voice helpers ─────────────────────────────────────────────────────────────
async def join_and_play(guild: discord.Guild):
    """Join the support VC and start streaming lofi music."""
    vc_channel = guild.get_channel(SUPPORT_VC_ID)
    if not vc_channel:
        log.error("Support VC not found. Check SUPPORT_VC_ID.")
        return

    # Connect or move if already in another channel
    voice_client = guild.voice_client
    if voice_client:
        if voice_client.channel.id != SUPPORT_VC_ID:
            await voice_client.move_to(vc_channel)
    else:
        voice_client = await vc_channel.connect()

    # Don't restart music if already playing
    if voice_client.is_playing():
        return

    log.info("Fetching lofi stream URL...")
    try:
        audio_url = await get_audio_url(LOFI_STREAM_URL)
        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=0.4)

        def after_play(error):
            if error:
                log.error(f"Playback error: {error}")
            # Auto-restart when stream ends
            asyncio.run_coroutine_threadsafe(join_and_play(guild), bot.loop)

        voice_client.play(source, after=after_play)
        log.info("Lofi stream started.")
    except Exception as e:
        log.error(f"Failed to start stream: {e}")


# ─── Reconnect loop (every 60s checks if bot is still in VC) ──────────────────
@tasks.loop(seconds=60)
async def reconnect_check():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        log.info("Not in VC — reconnecting...")
        await join_and_play(guild)
    elif not vc.is_playing():
        log.info("Stream stopped — restarting...")
        await join_and_play(guild)


# ─── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await join_and_play(guild)
    reconnect_check.start()


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Only care about joins into the support VC
    if member.bot:
        return
    if after.channel and after.channel.id == SUPPORT_VC_ID and (not before.channel or before.channel.id != SUPPORT_VC_ID):
        log.info(f"{member.display_name} joined the support VC.")
        await notify_join(member)


async def notify_join(member: discord.Member):
    embed = discord.Embed(
        title="🔔 Someone joined the support room",
        description=f"**{member.display_name}** (`{member.name}`) just entered the waiting room.",
        color=0x2ecc71,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")

    # Send to notify text channel
    notify_channel = bot.get_channel(NOTIFY_CHANNEL_ID)
    if notify_channel:
        await notify_channel.send(embed=embed)

    # DM the owner
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(embed=embed)
    except discord.Forbidden:
        log.warning("Couldn't DM owner — check DM privacy settings.")


# ─── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name="rejoin")
@commands.is_owner()
async def rejoin(ctx):
    """Force the bot to rejoin and restart the stream."""
    guild = ctx.guild
    vc = guild.voice_client
    if vc:
        await vc.disconnect()
    await join_and_play(guild)
    await ctx.send("✅ Rejoined and restarted the stream.")


@bot.command(name="volume")
@commands.is_owner()
async def volume(ctx, vol: int):
    """Set volume 0–100. Example: !volume 50"""
    vc = ctx.guild.voice_client
    if not vc or not vc.source:
        await ctx.send("❌ Not playing anything right now.")
        return
    if not 0 <= vol <= 100:
        await ctx.send("❌ Volume must be between 0 and 100.")
        return
    vc.source.volume = vol / 100
    await ctx.send(f"🔊 Volume set to {vol}%.")


@bot.command(name="stop")
@commands.is_owner()
async def stop(ctx):
    """Stop music and leave VC."""
    vc = ctx.guild.voice_client
    if vc:
        reconnect_check.stop()
        await vc.disconnect()
        await ctx.send("⏹️ Stopped and left the voice channel.")


@bot.command(name="start")
@commands.is_owner()
async def start(ctx):
    """Rejoin VC and start playing again."""
    await join_and_play(ctx.guild)
    if not reconnect_check.is_running():
        reconnect_check.start()
    await ctx.send("▶️ Started.")


# ─── Run ───────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
