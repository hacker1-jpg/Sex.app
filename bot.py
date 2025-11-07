# bot.py — safe, non-destructive Discord bot
# Requirements: Python 3.10+, discord.py v2.x (pip install -U "discord.py")
# Use a .env or environment variable DISCORD_TOKEN to store the token.

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import sys
import traceback
from typing import Optional, List

# ----- ERROR LOGGING -----
def log_uncaught_exceptions(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    with open("errors.log", "a", encoding="utf-8") as f:
        f.write("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
        f.write("\n\n")
    print("An uncaught exception occurred. Check errors.log for details.")

sys.excepthook = log_uncaught_exceptions

def handle_async_exception(loop, context):
    msg = context.get("exception", context.get("message"))
    print(f"Caught asyncio exception: {msg}")
    with open("errors.log", "a", encoding="utf-8") as f:
        f.write(f"Asyncio exception: {msg}\n\n")

asyncio.get_event_loop().set_exception_handler(handle_async_exception)

# ----- BOT SETUP -----
intents = discord.Intents.default()
intents.message_content = False  # not needed for slash commands
intents.members = True  # required for nickname changes and member lists

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Simple cooldown maps to reduce accidental abuse (safe defaults)
USER_COOLDOWN_SECS = 20
GUILD_COOLDOWN_SECS = 5
user_last = {}
guild_last = {}

# Helpers
def is_on_user_cooldown(user_id: int) -> Optional[int]:
    last = user_last.get(user_id, 0)
    remaining = USER_COOLDOWN_SECS - (asyncio.get_event_loop().time() - last)
    return int(remaining) if remaining > 0 else None

def is_on_guild_cooldown(guild_id: int) -> Optional[int]:
    last = guild_last.get(guild_id, 0)
    remaining = GUILD_COOLDOWN_SECS - (asyncio.get_event_loop().time() - last)
    return int(remaining) if remaining > 0 else None

# ----- SAFE PREFIX COMMANDS (optional) -----
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong!")

# ----- SAFE /sendms slash command -----
@tree.command(name="sendms", description="Send up to 3 messages to this channel (safe limits and cooldowns).")
@app_commands.describe(m1="Message 1 (required)", m2="Message 2 (optional)", m3="Message 3 (optional)")
async def sendms(interaction: discord.Interaction, m1: str, m2: Optional[str] = None, m3: Optional[str] = None):
    # Cooldown checks
    uid = interaction.user.id
    gid = interaction.guild_id or 0

    user_wait = is_on_user_cooldown(uid)
    if user_wait:
        await interaction.response.send_message(f"You're doing that too often — try again in {user_wait}s.", ephemeral=True)
        return

    guild_wait = is_on_guild_cooldown(gid)
    if guild_wait:
        await interaction.response.send_message("Server is handling requests. Try again in a moment.", ephemeral=True)
        return

    # Build messages (limit to 3 and char limits)
    msgs = []
    for m in (m1, m2, m3):
        if m and m.strip():
            msgs.append(m[:2000])

    if not msgs:
        await interaction.response.send_message("No valid messages provided.", ephemeral=True)
        return

    # Acknowledge and send
    await interaction.response.send_message("Sending message(s)...", ephemeral=True)
    try:
        for i, content in enumerate(msgs):
            await interaction.channel.send(content)
            if i < len(msgs) - 1:
                await asyncio.sleep(0.7)  # gentle delay
        # update cooldowns
        user_last[uid] = asyncio.get_event_loop().time()
        guild_last[gid] = asyncio.get_event_loop().time()
        await interaction.followup.send(f"✅ Sent {len(msgs)} message(s).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to send messages in that channel.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to send messages: {e}", ephemeral=True)

# ----- SAFE changenick slash command -----
@tree.command(
    name="changenick",
    description="Change a user's nickname or change multiple members (safe, limited)."
)
@app_commands.describe(
    nickname="The new nickname to apply",
    member="(Optional) member to change. If omitted and change_many is false, command will fail.",
    change_many="If true, change multiple members (limited to a safe max)"
)
async def changenick(interaction: discord.Interaction, nickname: str, member: Optional[discord.Member] = None, change_many: bool = False):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    # Permission: require invoker to have Manage Nicknames or be server owner
    if not (interaction.user.guild_permissions.manage_nicknames or interaction.user.id == guild.owner_id):
        await interaction.response.send_message("You must have Manage Nicknames (or be server owner) to use this command.", ephemeral=True)
        return

    # Bot must have manage_nicknames
    bot_member = guild.get_member(bot.user.id)
    if bot_member is None or not bot_member.guild_permissions.manage_nicknames:
        await interaction.response.send_message("I need the Manage Nicknames permission to do this.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    targets: List[discord.Member] = []
    if change_many:
        # limit how many will be changed to avoid excessive mass edits
        MAX_CHANGE = 50
        members = [m for m in guild.members if not m.bot and m.id != bot.user.id]
        if len(members) > MAX_CHANGE:
            members = members[:MAX_CHANGE]
        targets = members
    else:
        if member is None:
            await interaction.followup.send("You must specify a member unless change_many is true.", ephemeral=True)
            return
        targets = [member]

    success = []
    failed = []

    for m in targets:
        if m.id == guild.owner_id:
            failed.append((m.display_name, "Server owner (cannot change)"))
            continue
        # role hierarchy check: bot must be higher
        if bot_member.top_role.position <= m.top_role.position:
            failed.append((m.display_name, "Role is equal or higher than bot"))
            continue
        try:
            await m.edit(nick=nickname, reason=f"Changed by {interaction.user}")
            success.append(m.display_name)
        except discord.Forbidden:
            failed.append((m.display_name, "Forbidden"))
        except Exception as e:
            failed.append((m.display_name, f"Error: {e}"))
        await asyncio.sleep(0.25)  # small delay

    out = f"✅ Done. Changed {len(success)} nickname(s). Failed: {len(failed)}.\n"
    if success:
        out += "Changed: " + ", ".join(success[:20]) + ("\n" if len(success) > 20 else "\n")
    if failed:
        out += "Failed:\n" + "\n".join(f"- {n} — {r}" for n, r in failed[:40])

    await interaction.followup.send(out)

# ----- ON READY -----
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Sync error: {e}")

# ----- RUN BOT -----
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    print("Error: DISCORD_TOKEN not found in environment variables.")
    exit(1)

bot.run(DISCORD_TOKEN)
