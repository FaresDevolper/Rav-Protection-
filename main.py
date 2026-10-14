import os
import re
import asyncio
import json
import time
import logging
import unicodedata
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rav-shield")

TOKEN = os.getenv("TOKEN")
CONFIG_FILE = "config.json"

# ضع ايدي قناة السجل هنا (او اتركه 0 وحدده بالامر تحديد_اللوج او من متغير LOG_CHANNEL_ID في Render)
DEFAULT_LOG_CHANNEL_ID = 1559930208050487550
MAX_TIMEOUT_MINUTES = 40320
RED = 0xE74C3C
GREEN = 0x2ECC71
PURPLE = 0x9B59B6

# تعبير منتظم (Regex) لالتقاط الروابط بكافة أنواعها
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+|www\.(?:[-\w.]|(?:%[\da-fA-F]{2}))+')

# ------------------------------------------------------------------
# Banned words and phrases (قذف وسب الاعراض)
# ------------------------------------------------------------------
BASE_WORDS = [
    # single words
    "شرموطة", "شرموط", "شراميط", "قحبة", "قحاب", "عاهرة", "عاهر",
    "زانية", "زاني", "ديوث", "ديوثة", "منيوك", "منيوكة", "متناك", "متناكة",
    "معرص", "عرص", "لوطي", "كسمك", "كسامك", "كسختك", "كسمكم",
    "كس", "زب", "خول", "نيك", "بكس",
    # accusing the origin
    "ابن الحرام", "ولد الحرام", "بنت الحرام",
    "ابن زنا", "ولد زنا", "بنت زنا", "ابن الزنا", "ولد الزنا", "بنت الزنا",
    "ابن الزانية", "بنت الزانية", "ولد الزانية",
    "ابن الشرموطة", "بنت الشرموطة", "ولد الشرموطة",
    "ابن القحبة", "بنت القحبة", "ولد القحبة",
    "ابن العاهرة", "ابن المتناكة", "ابن الديوث",
    # family honor
    "امك زانية", "امك شرموطة", "امك قحبة", "امك عاهرة", "امك تتناك", "امك فاجرة",
    "اختك زانية", "اختك شرموطة", "اختك قحبة", "اختك عاهرة", "اختك تتناك",
    "زوجتك شرموطة", "زوجتك قحبة", "حرمتك شرموطة", "حرمتك قحبة",
    "مرتك شرموطة", "مرتك قحبة",
    "اخوك ديوث", "ابوك ديوث",
    "كس امك", "كس اختك", "كس عرضك",
    "نيك امك", "انيك امك", "نيك اختك", "انيك اختك", "نيك عرضك", "انيك عرضك",
]

PREFIXES = {"و", "ف", "ب", "ل", "ك", "يا", "ويا", "فيا"}


# ------------------------------------------------------------------
# Text normalization
# ------------------------------------------------------------------
STRIP = re.compile(
    "[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640"
    "\u200b-\u200f\u202a-\u202e\u2060\ufeff]"
)
CHAR_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي",
    "ک": "ك", "ی": "ي", "گ": "ك", "پ": "ب", "چ": "ج", "ڤ": "ف",
})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = STRIP.sub("", text)
    text = text.translate(CHAR_MAP)
    text = re.sub(r"(.)\1+", r"\1", text)
    return text


def merge_spaced(tokens):
    out, buf = [], []
    for t in tokens:
        if len(t) == 1:
            buf.append(t)
            continue
        if buf:
            out.extend(["".join(buf)] if len(buf) >= 3 else buf)
            buf = []
        out.append(t)
    if buf:
        out.extend(["".join(buf)] if len(buf) >= 3 else buf)
    return out


def tokenize(text: str):
    return merge_spaced(re.findall(r"[\u0621-\u064A]+", normalize(text)))


def fmt_duration(m: int) -> str:
    if m % 1440 == 0:
        return f"{m // 1440} يوم"
    if m % 60 == 0:
        return f"{m // 60} ساعة"
    return f"{m} دقيقة" if (m > 10 or m == 1) else f"{m} دقائق"


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
class Config:
    KEYS = ("enabled", "timeout_minutes", "log_channel_id",
            "escalate", "custom_words")

    def __init__(self):
        self.enabled = True
        self.timeout_minutes = int(os.getenv("TIMEOUT_MINUTES", "10"))
        self.log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0") or DEFAULT_LOG_CHANNEL_ID)
        self.escalate = os.getenv("ESCALATE", "false").lower() == "true"
        self.custom_words = []
        self.load()

    def load(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for key in self.KEYS:
            if key in data:
                setattr(self, key, data[key])

    def save(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({k: getattr(self, k) for k in self.KEYS}, f,
                          ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("could not save config: %s", e)


cfg = Config()


# ------------------------------------------------------------------
# Matcher
# ------------------------------------------------------------------
class Matcher:
    def __init__(self):
        self.phrases = []
        self.long_phrases = []
        self.rebuild()

    def rebuild(self):
        seen = set()
        self.phrases, self.long_phrases = [], []
        for raw in BASE_WORDS + list(cfg.custom_words):
            toks = tuple(tokenize(raw))
            if not toks or toks in seen:
                continue
            seen.add(toks)
            self.phrases.append(toks)
            joined = "".join(toks)
            if len(joined) >= 6:
                self.long_phrases.append((joined, toks))

    @staticmethod
    def _first_match(token: str, word: str) -> bool:
        if token == word:
            return True
        return token.endswith(word) and token[:-len(word)] in PREFIXES

    def find(self, text: str):
        tokens = tokenize(text)
        if not tokens:
            return None
        for phrase in self.phrases:
            n = len(phrase)
            for i in range(len(tokens) - n + 1):
                if not self._first_match(tokens[i], phrase[0]):
                    continue
                if n == 1 or tuple(tokens[i + 1:i + n]) == phrase[1:]:
                    return " ".join(phrase)
        squashed = "".join(tokens)
        for joined, toks in self.long_phrases:
            if joined in squashed:
                return " ".join(toks)
        return None


matcher = Matcher()
strikes = defaultdict(deque)


def register_strike(guild_id: int, user_id: int) -> int:
    q = strikes[(guild_id, user_id)]
    now = time.time()
    while q and now - q[0] > 86400:
        q.popleft()
    q.append(now)
    return len(q)


def is_exempt(member: discord.Member) -> bool:
    p = member.guild_permissions
    return (member.guild.owner_id == member.id or p.administrator
            or p.manage_messages or p.moderate_members)


# ------------------------------------------------------------------
# Web server for Render and UptimeRobot
# ------------------------------------------------------------------
async def start_web():
    async def health(_):
        return web.Response(text="Rav Shield is running")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()


# ------------------------------------------------------------------
# Slash commands
# ------------------------------------------------------------------
def admin_only(func):
    func = app_commands.checks.has_permissions(administrator=True)(func)
    func = app_commands.guild_only()(func)
    func = app_commands.default_permissions(administrator=True)(func)
    return func


async def reply(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(text, ephemeral=True)


@app_commands.command(name="تحقق", description="عرض حالة بوت الحماية")
@admin_only
async def cmd_status(interaction: discord.Interaction):
    log_ch = f"<#{cfg.log_channel_id}>" if cfg.log_channel_id else "غير محددة"
    embed = discord.Embed(title="حالة بوت الحماية", color=PURPLE)
    embed.add_field(name="الحالة", value="شغال" if cfg.enabled else "متوقف")
    embed.add_field(name="مدة التايم", value=fmt_duration(cfg.timeout_minutes))
    embed.add_field(name="قناة اللوج", value=log_ch)
    embed.add_field(name="دبل عقوبة", value="شغال" if cfg.escalate else "متوقف")
    embed.add_field(name="عدد الكلمات والعبارات", value=str(len(matcher.phrases)))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="تشغيل_او_ايقاف_الحماية", description="تشغيل او ايقاف الحماية")
@admin_only
async def cmd_toggle(interaction: discord.Interaction):
    cfg.enabled = not cfg.enabled
    cfg.save()
    await reply(interaction, "تم تشغيل الحماية" if cfg.enabled else "تم ايقاف الحماية")


@app_commands.command(name="تغيير_مدة_التايم", description="تغيير مدة التايم اوت بالدقائق")
@app_commands.describe(minutes="عدد الدقائق مثل 10 او 30")
@app_commands.rename(minutes="الدقائق")
@admin_only
async def cmd_duration(interaction: discord.Interaction,
                       minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES]):
    cfg.timeout_minutes = minutes
    cfg.save()
    await reply(interaction, f"تم تغيير مدة التايم الى {fmt_duration(minutes)}")


@app_commands.command(name="تحديد_اللوج", description="تحديد قناة اللوج")
@app_commands.describe(channel="القناة")
@app_commands.rename(channel="القناة")
@admin_only
async def cmd_setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg.log_channel_id = channel.id
    cfg.save()
    await reply(interaction, f"تم تحديد قناة اللوج {channel.mention}")


@app_commands.command(name="دبل_عقوبة", description="تشغيل او ايقاف مضاعفة العقوبة للمكرر خلال 24 ساعة")
@admin_only
async def cmd_escalate(interaction: discord.Interaction):
    cfg.escalate = not cfg.escalate
    cfg.save()
    await reply(interaction, "تم تشغيل دبل عقوبة" if cfg.escalate else "تم ايقاف دبل عقوبة")


@app_commands.command(name="اضافة_كلمة", description="اضافة كلمة او عبارة ممنوعة")
@app_commands.describe(word="الكلمة او العبارة")
@app_commands.rename(word="الكلمة")
@admin_only
async def cmd_addword(interaction: discord.Interaction, word: str):
    if not tokenize(word):
        return await reply(interaction, "الكلمة غير صالحة")
    word = word.strip()
    if word in cfg.custom_words:
        return await reply(interaction, "الكلمة مضافة من قبل")
    cfg.custom_words.append(word)
    cfg.save()
    matcher.rebuild()
    await reply(interaction, "تمت الاضافة")


@app_commands.command(name="حذف_كلمة", description="حذف كلمة او عبارة من القائمة المضافة")
@app_commands.describe(word="الكلمة او العبارة")
@app_commands.rename(word="الكلمة")
@admin_only
async def cmd_removeword(interaction: discord.Interaction, word: str):
    target = tokenize(word)
    for w in list(cfg.custom_words):
        if tokenize(w) == target:
            cfg.custom_words.remove(w)
            cfg.save()
            matcher.rebuild()
            return await reply(interaction, "تم الحذف")
    await reply(interaction, "الكلمة غير موجودة في القائمة المضافة")


@app_commands.command(name="عرض_الكلمات", description="عرض الكلمات التي اضفتها")
@admin_only
async def cmd_listwords(interaction: discord.Interaction):
    if not cfg.custom_words:
        return await reply(interaction, "لا توجد كلمات مضافة")
    await reply(interaction, "\n".join(cfg.custom_words)[:1900])


COMMANDS = [
    cmd_status, cmd_toggle, cmd_duration, cmd_setlog,
    cmd_escalate, cmd_addword, cmd_removeword, cmd_listwords,
]


# ------------------------------------------------------------------
# Bot
# ------------------------------------------------------------------
class ProtectBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.recent = {}

    async def setup_hook(self):
        for command in COMMANDS:
            self.tree.add_command(command)
        await self.tree.sync()
        await start_web()

    async def on_ready(self):
        await self.change_presence(
            activity=discord.Streaming(name="Rav Shield", url="https://www.twitch.tv/discord")
        )
        log.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message):
        await self.inspect(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self.inspect(after)

    async def inspect(self, message: discord.Message):
        if not cfg.enabled or not message.guild or message.author.bot or not message.content:
            return
        member = message.author
        if not isinstance(member, discord.Member) or is_exempt(member):
            return

        # 1. فحص إرسال الروابط ومعاقبة المرسل فوراً
        if URL_REGEX.search(message.content):
            await self.punish_link(message, member)
            return

        # 2. فحص القذف والسب
        hit = matcher.find(message.content)
        if hit:
            await self.punish(message, member, hit)

    async def punish_link(self, message: discord.Message, member: discord.Member):
        guild = message.guild
        minutes = cfg.timeout_minutes
        duration = fmt_duration(minutes)

        # حذف رسالة الرابط فوراً
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        timed_out = False
        try:
            await member.timeout(timedelta(minutes=minutes), reason="إرسال روابط ممنوعة")
            timed_out = True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not timeout %s for link", member)

        if timed_out:
            desc = f"{member.mention} تم منعك من الكتابة لمدة {duration} بسبب إرسال روابط"
        else:
            desc = f"{member.mention} تم حذف رسالتك بسبب إرسال روابط"
        
        notice = discord.Embed(title="حماية السيرفر", description=desc, color=RED)
        notice.set_footer(text="إرسال الروابط ممنوع في هذا السيرفر")
        asyncio.create_task(self.announce(message.channel, member, notice, timed_out))

        if timed_out:
            try:
                await member.send(
                    f"تم منعك من الكتابة في {guild.name} لمدة {duration} بسبب إرسال روابط\n"
                    "نرجو الالتزام بقوانين السيرفر"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # تسجيل المخالفة في اللوج
        if cfg.log_channel_id:
            channel = guild.get_channel(cfg.log_channel_id)
            if channel:
                embed = discord.Embed(title="سجل الحماية (رابط ممنوع)", color=RED, timestamp=discord.utils.utcnow())
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name="العضو", value=f"{member.mention}\n{member.id}")
                embed.add_field(name="القناة", value=message.channel.mention)
                embed.add_field(name="العقوبة", value=duration if timed_out else "فشل الاسكات")
                content = discord.utils.escape_markdown(message.content)[:1000]
                embed.add_field(name="نص الرسالة", value=content, inline=False)
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

    async def punish(self, message: discord.Message, member: discord.Member, hit: str):
        guild = message.guild
        key = (guild.id, member.id)
        now = time.time()

        # إزالة استثناء الـ 15 ثانية المؤقت حتى يتم حذف الرسالة وإعطاء التايم أوت بشكل دائم وفوري
        self.recent[key] = now

        count = register_strike(guild.id, member.id)
        minutes = cfg.timeout_minutes
        if cfg.escalate:
            minutes = min(minutes * (2 ** (min(count, 10) - 1)), MAX_TIMEOUT_MINUTES)
        duration = fmt_duration(minutes)

        # حذف رسالة القذف فوراً
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        timed_out = False
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"قذف: {hit}")
            timed_out = True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not timeout %s", member)

        if timed_out:
            desc = f"{member.mention} تم منعك من الكتابة لمدة {duration} بسبب القذف والسب"
        else:
            desc = f"{member.mention} تم حذف رسالتك بسبب القذف والسب"
        notice = discord.Embed(title="حماية السيرفر", description=desc, color=RED)
        notice.set_footer(text="القذف ممنوع في هذا السيرفر")
        asyncio.create_task(self.announce(message.channel, member, notice, timed_out))

        if timed_out:
            try:
                await member.send(
                    f"تم منعك من الكتابة في {guild.name} لمدة {duration} بسبب القذف والسب\n"
                    "نرجو الالتزام بقوانين السيرفر"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        await self.send_log(guild, member, message, hit, duration, timed_out, count)

    async def announce(self, channel, member, embed, timed_out):
        notice = None
        try:
            notice = await channel.send(embed=embed)
        except discord.HTTPException:
            pass

        await asyncio.sleep(10)

        if notice is not None:
            try:
                await notice.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        if timed_out:
            text = f"{member.mention} تم اعطائك تايم اوت بسبب وقوعك باحدى مخالفات السيرفر"
        else:
            text = f"{member.mention} تم حذف رسالتك بسبب وقوعك باحدى مخالفات السيرفر"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass

    async def send_log(self, guild, member, message, hit, duration, timed_out, count):
        if not cfg.log_channel_id:
            return
        channel = guild.get_channel(cfg.log_channel_id)
        if channel is None:
            return
        embed = discord.Embed(title="سجل الحماية", color=RED, timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="العضو", value=f"{member.mention}\n{member.id}")
        embed.add_field(name="القناة", value=message.channel.mention)
        embed.add_field(name="الكلمة المكتشفة", value=hit)
        embed.add_field(name="العقوبة", value=duration if timed_out else "فشل الاسكات (تحقق من صلاحيات ورتبة البوت)")
        embed.add_field(name="عدد المخالفات خلال 24 ساعة", value=str(count))
        content = discord.utils.escape_markdown(message.content)[:1000]
        embed.add_field(name="نص الرسالة", value=content, inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


bot = ProtectBot()


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "هذا الامر للادارة فقط"
    else:
        log.exception("command error", exc_info=error)
        msg = "حصل خطا غير متوقع"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TOKEN is missing")
    bot.run(TOKEN)
