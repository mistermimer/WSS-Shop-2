import os, sqlite3, logging
from urllib.parse import urlparse
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
DATABASE_PATH = os.getenv('DATABASE_PATH', 'wss_feedback.sqlite3')
GUILD_ID = os.getenv('GUILD_ID', '').strip()
DEFAULT_RAID_CHANNEL_ID = 1504635802594967583
DEFAULT_BOOST_CHANNEL_ID = 1476283095341596762
GOLD = discord.Colour.from_rgb(230, 170, 35)
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wss.jpg')
ANONYMITY_NOTE = 'Client identity protected'
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('wss-feedback')


def connect():
    con = sqlite3.connect(DATABASE_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY, moderation_channel_id INTEGER,
            raid_channel_id INTEGER, boost_channel_id INTEGER)''')
        cols = {r['name'] for r in con.execute('PRAGMA table_info(settings)')}
        for col in ('moderation_channel_id', 'raid_channel_id', 'boost_channel_id'):
            if col not in cols:
                con.execute(f'ALTER TABLE settings ADD COLUMN {col} INTEGER')
        con.execute('''CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, category TEXT NOT NULL DEFAULT 'boost',
            booster TEXT NOT NULL, service TEXT NOT NULL, rating INTEGER NOT NULL,
            comment TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', review_message_id INTEGER,
            published_message_id INTEGER, decided_by INTEGER, decided_at TEXT)''')
        cols = {r['name'] for r in con.execute('PRAGMA table_info(feedback)')}
        migrations = {
            'category': "ALTER TABLE feedback ADD COLUMN category TEXT NOT NULL DEFAULT 'boost'",
            'status': "ALTER TABLE feedback ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
            'review_message_id': 'ALTER TABLE feedback ADD COLUMN review_message_id INTEGER',
            'published_message_id': 'ALTER TABLE feedback ADD COLUMN published_message_id INTEGER',
            'decided_by': 'ALTER TABLE feedback ADD COLUMN decided_by INTEGER',
            'decided_at': 'ALTER TABLE feedback ADD COLUMN decided_at TEXT',
            'image_url': 'ALTER TABLE feedback ADD COLUMN image_url TEXT',
        }
        for col, sql in migrations.items():
            if col not in cols:
                con.execute(sql)

        # Remove the legacy one-review-per-user constraint while preserving old records.
        table_sql_row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='feedback'"
        ).fetchone()
        table_sql = (table_sql_row['sql'] or '').replace(' ', '').replace('\\n', '').lower() if table_sql_row else ''
        if 'unique(guild_id,user_id)' in table_sql:
            con.execute('ALTER TABLE feedback RENAME TO feedback_old')
            con.execute('''CREATE TABLE feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, category TEXT NOT NULL DEFAULT 'boost',
                booster TEXT NOT NULL, service TEXT NOT NULL, rating INTEGER NOT NULL,
                comment TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', review_message_id INTEGER,
                published_message_id INTEGER, decided_by INTEGER, decided_at TEXT,
                image_url TEXT)''')
            con.execute('''INSERT INTO feedback
                (id,guild_id,user_id,category,booster,service,rating,comment,created_at,
                 status,review_message_id,published_message_id,decided_by,decided_at,image_url)
                SELECT id,guild_id,user_id,category,booster,service,rating,comment,created_at,
                       status,review_message_id,published_message_id,decided_by,decided_at,image_url
                FROM feedback_old''')
            con.execute('DROP TABLE feedback_old')


def channels_for(guild_id):
    with connect() as con:
        row = con.execute('SELECT * FROM settings WHERE guild_id=?', (guild_id,)).fetchone()
    if not row:
        return None, DEFAULT_RAID_CHANNEL_ID, DEFAULT_BOOST_CHANNEL_ID
    return (row['moderation_channel_id'], row['raid_channel_id'] or DEFAULT_RAID_CHANNEL_ID,
            row['boost_channel_id'] or DEFAULT_BOOST_CHANNEL_ID)


def get_review(review_id):
    with connect() as con:
        return con.execute('SELECT * FROM feedback WHERE id=?', (review_id,)).fetchone()


def valid_image_url(value):
    value = (value or '').strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return value


def review_embed(row, preview=False):
    rating = max(1, min(5, int(row['rating'])))
    category = 'RAID FEEDBACK' if row['category'] == 'raid' else 'BOOST FEEDBACK'
    created_at = datetime.fromisoformat(row['created_at'])
    embed = discord.Embed(
        title=f'⭐ WSS SERVICE — {category}',
        description=(
            f'👤 **From:** Anonymous *({ANONYMITY_NOTE})*\n'
            f'🛡️ **Booster:** {discord.utils.escape_markdown(row["booster"])}\n'
            f'⭐ **Rating:** {"⭐" * rating}{"☆" * (5-rating)} **{rating}/5**\n'
            f'⚔️ **Service:** {discord.utils.escape_markdown(row["service"])}\n\n'
            f'💬 **Comment**\n{discord.utils.escape_markdown(row["comment"])}'
        ),
        colour=GOLD,
        timestamp=created_at,
    )
    image_url = valid_image_url(row['image_url'] if 'image_url' in row.keys() else None)
    if image_url:
        embed.set_thumbnail(url=image_url)
    embed.set_footer(text='🏆 WSS BOOST & SERVICE • ' + ('Awaiting approval' if preview else category.title()))
    if os.path.isfile(LOGO_PATH):
        embed.set_author(name='WSS SERVICE — Feedback', icon_url='attachment://wss.jpg')
    else:
        embed.set_author(name='WSS SERVICE — Feedback')
    return embed


def logo_file():
    """Return a fresh logo attachment when the original WSS image is present."""
    if os.path.isfile(LOGO_PATH):
        return discord.File(LOGO_PATH, filename='wss.jpg')
    return None


async def remove_from_moderation_queue(interaction, review_id, result_label):
    """Delete a processed moderation post; if Discord refuses, hide the review card and log why."""
    message = interaction.message
    if message is None:
        log.error('No interaction message available to remove review %s from queue', review_id)
        return
    try:
        await message.delete()
        log.info('Removed review %s from moderation queue (%s)', review_id, result_label)
    except discord.NotFound:
        log.info('Moderation message for review %s was already deleted', review_id)
    except discord.Forbidden:
        log.exception('Discord denied deletion of moderation message for review %s; check bot permissions', review_id)
        try:
            await message.edit(
                content=f'✅ Review #{review_id} {result_label} — removed from the active queue.',
                embed=None,
                attachments=[],
                view=None,
            )
        except discord.HTTPException:
            log.exception('Could not replace processed moderation message for review %s', review_id)
    except discord.HTTPException:
        log.exception('Could not delete moderation message for review %s', review_id)
        try:
            await message.edit(
                content=f'✅ Review #{review_id} {result_label} — removed from the active queue.',
                embed=None,
                attachments=[],
                view=None,
            )
        except discord.HTTPException:
            log.exception('Could not replace processed moderation message for review %s', review_id)


class DecisionView(discord.ui.View):
    def __init__(self, review_id):
        super().__init__(timeout=None)
        self.review_id = review_id
        self.approve.custom_id = f'wss:approve:{review_id}'
        self.reject.custom_id = f'wss:reject:{review_id}'

    async def interaction_check(self, interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message('Only staff with **Manage Server** permission can decide.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Publish review', style=discord.ButtonStyle.success, emoji='✅')
    async def approve(self, interaction, button):
        row = get_review(self.review_id)
        if not row or row['status'] != 'pending':
            await interaction.response.send_message('This review has already been processed.', ephemeral=True)
            return
        _, raid_id, boost_id = channels_for(row['guild_id'])
        target_id = raid_id if row['category'] == 'raid' else boost_id
        channel = interaction.client.get_channel(int(target_id))
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(int(target_id))
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                channel = None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(f"Can't access destination channel `{target_id}`. Check bot permissions.", ephemeral=True)
            return
        try:
            logo = logo_file()
            if logo:
                post = await channel.send(embed=review_embed(row), file=logo)
            else:
                post = await channel.send(embed=review_embed(row))
            for emoji in ('❤️', '🔥', '👍'):
                try:
                    await post.add_reaction(emoji)
                except discord.HTTPException:
                    break
        except discord.HTTPException:
            await interaction.response.send_message('Could not publish. Check channel permissions.', ephemeral=True)
            return
        with connect() as con:
            con.execute("UPDATE feedback SET status='approved', decided_by=?, decided_at=?, published_message_id=? WHERE id=? AND status='pending'",
                        (interaction.user.id, datetime.now(timezone.utc).isoformat(), post.id, self.review_id))
        # Acknowledge the click, then remove the entire moderation post.
        await interaction.response.defer()
        await remove_from_moderation_queue(interaction, self.review_id, 'published')

    @discord.ui.button(label='Reject review', style=discord.ButtonStyle.danger, emoji='🗑️')
    async def reject(self, interaction, button):
        row = get_review(self.review_id)
        if not row or row['status'] != 'pending':
            await interaction.response.send_message('This review has already been processed.', ephemeral=True)
            return
        with connect() as con:
            con.execute("UPDATE feedback SET status='rejected', decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                        (interaction.user.id, datetime.now(timezone.utc).isoformat(), self.review_id))
        # Remove rejected item from the private moderation queue too.
        await interaction.response.defer()
        await remove_from_moderation_queue(interaction, self.review_id, 'rejected')


class ReviewModal(discord.ui.Modal, title='WSS SERVICE — Leave Feedback'):
    rating = discord.ui.TextInput(label='Rating (1–5)', placeholder='5', min_length=1, max_length=1)
    booster = discord.ui.TextInput(label='Booster name', placeholder='Who provided the service?', max_length=80)
    service = discord.ui.TextInput(label='Service / raid', placeholder='e.g. BT, MH, DST, 9/9 Diamond', max_length=100)
    comment = discord.ui.TextInput(label='Your feedback', placeholder='Tell us how it went...',
                                   style=discord.TextStyle.paragraph, min_length=3, max_length=1000)
    image_url = discord.ui.TextInput(label='Screenshot / Image URL (optional)',
                                     placeholder='Paste a direct image link (https://...)',
                                     required=False, max_length=500)

    def __init__(self, category):
        super().__init__()
        self.category = category

    async def on_submit(self, interaction):
        if not interaction.guild:
            await interaction.response.send_message('Please use this form inside the WSS server.', ephemeral=True)
            return
        try:
            rating = int(str(self.rating.value).strip())
        except ValueError:
            rating = 0
        if rating not in range(1, 6):
            await interaction.response.send_message('Rating must be from 1 to 5.', ephemeral=True)
            return
        mod_id, _, _ = channels_for(interaction.guild.id)
        mod_channel = interaction.guild.get_channel(int(mod_id)) if mod_id else None
        if not isinstance(mod_channel, discord.TextChannel):
            await interaction.response.send_message('Moderation channel is not configured. Ask staff to run `/feedback_setup`.', ephemeral=True)
            return
        image_url = valid_image_url(str(self.image_url.value or '').strip())
        if str(self.image_url.value or '').strip() and not image_url:
            await interaction.response.send_message('Image URL must start with https:// or http://. Leave it empty if you do not want to add an image.', ephemeral=True)
            return
        now = datetime.now(timezone.utc).isoformat()
        with connect() as con:
            cur = con.execute('''INSERT INTO feedback(guild_id,user_id,category,booster,service,rating,comment,created_at,status,image_url)
                VALUES(?,?,?,?,?,?,?,?,'pending',?)''',
                (interaction.guild.id, interaction.user.id, self.category, str(self.booster.value).strip(),
                 str(self.service.value).strip(), rating, str(self.comment.value).strip(), now, image_url))
            review_id = cur.lastrowid
        row = get_review(review_id)
        try:
            logo = logo_file()
            # The moderation card contains the submitter identity for staff only.
            # Public reviews still use review_embed(), which always shows Anonymous.
            moderation_embed = review_embed(row, True)
            display_name = getattr(interaction.user, 'global_name', None) or interaction.user.name
            moderation_embed.add_field(
                name='🔒 Submitter (staff only)',
                value=(
                    f'**Discord:** {discord.utils.escape_markdown(display_name)} '
                    f'({interaction.user.mention})\n'
                    f'**Username:** `{discord.utils.escape_markdown(interaction.user.name)}`\n'
                    f'**User ID:** `{interaction.user.id}`'
                ),
                inline=False,
            )
            send_kwargs = {
                'content': f"📝 **New anonymous {'raid' if self.category == 'raid' else 'boost'} review awaiting approval** · Review `#{review_id}`",
                'embed': moderation_embed,
                'view': DecisionView(review_id),
            }
            if logo:
                send_kwargs['file'] = logo
            msg = await mod_channel.send(**send_kwargs)
            with connect() as con:
                con.execute('UPDATE feedback SET review_message_id=? WHERE id=?', (msg.id, review_id))
        except discord.HTTPException:
            with connect() as con:
                con.execute("DELETE FROM feedback WHERE id=? AND status='pending'", (review_id,))
            await interaction.response.send_message('Could not send to moderation. Please contact staff.', ephemeral=True)
            return
        await interaction.response.send_message('Thanks! Your review is waiting for WSS staff approval.', ephemeral=True)


class ChoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Raid Feedback', style=discord.ButtonStyle.primary, emoji='⚔️', custom_id='wss:choose_raid')
    async def raid(self, interaction, button):
        await interaction.response.send_modal(ReviewModal('raid'))

    @discord.ui.button(label='Boost Feedback', style=discord.ButtonStyle.success, emoji='⭐', custom_id='wss:choose_boost')
    async def boost(self, interaction, button):
        await interaction.response.send_modal(ReviewModal('boost'))


class WSSBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(ChoiceView())
        # Re-register moderation buttons for reviews that were pending when the bot stopped.
        try:
            with connect() as con:
                pending_ids = [r['id'] for r in con.execute("SELECT id FROM feedback WHERE status='pending'").fetchall()]
            for review_id in pending_ids:
                self.add_view(DecisionView(review_id))
        except sqlite3.Error:
            log.exception('Could not restore pending moderation views')
        if GUILD_ID.isdigit():
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot = WSSBot(command_prefix='!', intents=discord.Intents.default())

@bot.event
async def on_ready():
    init_db()
    log.info('Logged in as %s', bot.user)

@bot.tree.command(name='feedback_setup', description='Set review channels and post the customer choice panel')
@app_commands.describe(moderation_channel='Private staff channel for approval', raid_channel='Channel for approved raid reviews', boost_channel='Channel for approved boost reviews')
@app_commands.checks.has_permissions(manage_guild=True)
async def feedback_setup(interaction: discord.Interaction, moderation_channel: discord.TextChannel,
                         raid_channel: discord.TextChannel, boost_channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message('Use this command in your server.', ephemeral=True)
        return
    with connect() as con:
        con.execute('''INSERT INTO settings(guild_id,moderation_channel_id,raid_channel_id,boost_channel_id)
            VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET moderation_channel_id=excluded.moderation_channel_id,
            raid_channel_id=excluded.raid_channel_id, boost_channel_id=excluded.boost_channel_id''',
            (interaction.guild.id, moderation_channel.id, raid_channel.id, boost_channel.id))
    embed = discord.Embed(title='WSS SERVICE', description=(
        '## ⭐ Leave Feedback\nHad a raid or boost with WSS? Choose the type below.\n\n'
        '⚔️ **Raid Feedback** — reviews about raids.\n⭐ **Boost Feedback** — reviews about boosting services.\n\n'
        'Your Discord name will not appear in the public review. Every submission is checked by WSS staff.\n\n'
        f'Client identity protected.'), colour=GOLD)
    embed.add_field(name='Review includes', value='⭐ Rating • 🛡️ Booster • ⚔️ Service • 💬 Comment • 🖼️ Optional image link', inline=False)
    embed.set_footer(text='WSS BOOST & SERVICE')
    await interaction.channel.send(embed=embed, view=ChoiceView())
    await interaction.response.send_message(f'Setup complete.\nModeration: {moderation_channel.mention}\nRaid reviews: {raid_channel.mention}\nBoost reviews: {boost_channel.mention}\nPanel posted in {interaction.channel.mention}.', ephemeral=True)

@bot.tree.command(name='feedback_stats', description='Show feedback statistics')
@app_commands.checks.has_permissions(manage_guild=True)
async def feedback_stats(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message('Use this command in your server.', ephemeral=True)
        return
    with connect() as con:
        row = con.execute("SELECT COUNT(*) total, SUM(status='approved') approved, SUM(status='pending') pending, AVG(CASE WHEN status='approved' THEN rating END) average FROM feedback WHERE guild_id=?", (interaction.guild.id,)).fetchone()
    await interaction.response.send_message(f"**WSS Feedback Stats**\nSubmissions: **{row['total'] or 0}**\nPublished: **{row['approved'] or 0}**\nPending: **{row['pending'] or 0}**\nAverage published rating: **{float(row['average'] or 0):.2f}/5**", ephemeral=True)

@feedback_setup.error
@feedback_stats.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = 'You need **Manage Server** permission to use this command.'
    else:
        log.exception('Slash command failed', exc_info=error)
        message = 'Something went wrong. Check the bot console.'
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

if __name__ == '__main__':
    init_db()
    if not TOKEN:
        raise RuntimeError('DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.')
    bot.run(TOKEN)
