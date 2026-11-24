import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel : serveur de test (commandes instantanées)

# ============================================================
#  BASE DE DONNÉES (simple fichier JSON)
# ============================================================
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
try:
    with open(DB_FILE, "r", encoding="utf-8") as f:
        DB = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    DB = {}


def save():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, indent=2, ensure_ascii=False)


def default_conf():
    return {
        "log_channel": None,
        "ticket_category": None,
        "staff_role": None,
        "modules": {
            "antispam": {"level": "moyen"},
            "antilink": {"level": "off"},
            "anticaps": {"level": "off"},
            "antimention": {"level": "moyen"},
            "antiraid": {"level": "off"},
        },
    }


def get_conf(guild_id: int) -> dict:
    conf = DB.setdefault(str(guild_id), default_conf())
    d = default_conf()
    for k, v in d["modules"].items():
        conf["modules"].setdefault(k, v)
    for k in ("log_channel", "ticket_category", "staff_role"):
        conf.setdefault(k, None)
    return conf


# ============================================================
#  INTENSITÉS DE PROTECTION
# ============================================================
MODULES = {
    "antispam": "Anti-spam",
    "antilink": "Anti-liens",
    "anticaps": "Anti-majuscules",
    "antimention": "Anti-mentions de masse",
    "antiraid": "Anti-raid",
}

LEVEL_LABEL = {"off": "⚫ Désactivé", "faible": "🟢 Faible", "moyen": "🟡 Moyen", "fort": "🔴 Fort"}

LEVELS = {
    "faible": {"spam": (7, 6), "dup": 4, "links": "invites", "caps": (25, 0.9), "mentions": 8, "raid": (10, 10), "timeout": 60},
    "moyen":  {"spam": (5, 5), "dup": 3, "links": "short",   "caps": (15, 0.7), "mentions": 5, "raid": (6, 10),  "timeout": 300},
    "fort":   {"spam": (4, 4), "dup": 2, "links": "all",     "caps": (10, 0.5), "mentions": 3, "raid": (4, 10),  "timeout": 900},
}

LINK_MODES = {"invites": "invitations Discord", "short": "invitations + liens raccourcis", "all": "tous les liens"}

DETAILS = {
    "antispam": lambda L: f"{L['spam'][0]} messages en {L['spam'][1]}s ou {L['dup']} messages identiques",
    "antilink": lambda L: LINK_MODES[L["links"]],
    "anticaps": lambda L: f"≥ {int(L['caps'][1] * 100)}% de majuscules (min. {L['caps'][0]} lettres)",
    "antimention": lambda L: f"{L['mentions']}+ mentions dans un seul message",
    "antiraid": lambda L: f"{L['raid'][0]} arrivées en {L['raid'][1]}s → expulsion automatique",
}

SHORTENERS = re.compile(r"(bit\.ly|tinyurl\.com|t\.co|goo\.gl|cutt\.ly|is\.gd|rebrand\.ly|shorturl\.at)/", re.I)
INVITES = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\S+", re.I)
ANY_LINK = re.compile(r"(https?://\S+|www\.\S+)", re.I)

# États en mémoire
SELECTED = {}                      # (guild_id, user_id) -> module choisi dans /reglage
MSG_LOG = defaultdict(list)        # (guild_id, user_id) -> [(timestamp, contenu)]
STRIKES = defaultdict(list)        # (guild_id, user_id) -> [timestamps]
JOIN_LOG = defaultdict(list)       # guild_id -> [timestamps]


# ============================================================
#  OUTILS
# ============================================================
async def send_log(guild: discord.Guild, conf: dict, title: str, desc: str, color: int = 0xF59E0B):
    if not conf["log_channel"]:
        return
    channel = guild.get_channel(conf["log_channel"])
    if channel is None:
        return
    try:
        await channel.send(embed=discord.Embed(title=title, description=desc, color=color,
                                               timestamp=discord.utils.utcnow()))
    except discord.HTTPException:
        pass


def is_staff(member: discord.Member, conf: dict) -> bool:
    p = member.guild_permissions
    if p.administrator or p.manage_messages:
        return True
    return bool(conf["staff_role"]) and any(r.id == conf["staff_role"] for r in member.roles)


async def punish(message: discord.Message, conf: dict, reason: str, level: str, instant: bool = False):
    L = LEVELS[level]
    try:
        await message.delete()
    except discord.HTTPException:
        pass

    key = (message.guild.id, message.author.id)
    now = time.time()
    strikes = [t for t in STRIKES[key] if now - t < 600]
    strikes.append(now)
    STRIKES[key] = strikes

    do_timeout = instant or len(strikes) >= 3
    minutes = max(1, L["timeout"] // 60)
    if do_timeout:
        text = f"⚠️ {message.author.mention}, {reason}. Tu es mis en sourdine {minutes} min."
    else:
        text = f"⚠️ {message.author.mention}, {reason}. ({len(strikes)}/3)"
    try:
        await message.channel.send(text, delete_after=6)
    except discord.HTTPException:
        pass

    if do_timeout:
        try:
            await message.author.timeout(timedelta(seconds=L["timeout"]), reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass
        STRIKES.pop(key, None)

    sanction = f"sourdine {minutes} min" if do_timeout else "message supprimé"
    await send_log(
        message.guild, conf, "🛡️ Protection déclenchée",
        f"**Membre :** {message.author.mention} ({message.author.id})\n"
        f"**Raison :** {reason}\n**Salon :** {message.channel.mention}\n**Sanction :** {sanction}",
    )


# ============================================================
#  PANNEAU /reglage
# ============================================================
def build_panel(guild: discord.Guild, user_id: int):
    conf = get_conf(guild.id)
    sel = SELECTED.get((guild.id, user_id), "antispam")

    status = "\n".join(f"**{name}** : {LEVEL_LABEL[conf['modules'][k]['level']]}" for k, name in MODULES.items())

    if sel == "all":
        detail = "Applique le niveau choisi à **tous** les modules."
        title_sel = "Tous les modules"
    else:
        lines = [f"{LEVEL_LABEL[l]} : {DETAILS[sel](LEVELS[l])}" for l in ("faible", "moyen", "fort")]
        times = " / ".join(f"{LEVELS[l]['timeout'] // 60} min" for l in ("faible", "moyen", "fort"))
        detail = "\n".join(lines) + f"\n\nSanction : message supprimé, puis sourdine ({times}) après 3 infractions."
        title_sel = MODULES[sel]

    embed = discord.Embed(title=f"⚙️ Réglages — {guild.name}", description=status, color=0x5865F2)
    embed.add_field(name="📜 Salon de logs", value=f"<#{conf['log_channel']}>" if conf["log_channel"] else "Non défini")
    embed.add_field(name="🎫 Catégorie des tickets", value=f"<#{conf['ticket_category']}>" if conf["ticket_category"] else "Non définie")
    embed.add_field(name="🛡️ Rôle staff", value=f"<@&{conf['staff_role']}>" if conf["staff_role"] else "Non défini")
    embed.add_field(name=f"🔧 Module sélectionné : {title_sel}", value=detail, inline=False)
    embed.set_footer(text="1) Choisis un module  2) Choisis son intensité")

    return embed, ReglageView(sel, conf)


async def refresh(interaction: discord.Interaction):
    embed, view = build_panel(interaction.guild, interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


class ModuleSelect(discord.ui.Select):
    def __init__(self, sel: str):
        options = [discord.SelectOption(label=name, value=k, default=(k == sel)) for k, name in MODULES.items()]
        options.append(discord.SelectOption(label="Tous les modules", value="all", default=(sel == "all")))
        super().__init__(placeholder="Module à configurer", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        SELECTED[(interaction.guild_id, interaction.user.id)] = self.values[0]
        await refresh(interaction)


class LevelSelect(discord.ui.Select):
    def __init__(self, current: Optional[str]):
        options = [
            discord.SelectOption(label="Désactivé", value="off", emoji="⚫", default=(current == "off")),
            discord.SelectOption(label="Faible", value="faible", emoji="🟢", default=(current == "faible")),
            discord.SelectOption(label="Moyen", value="moyen", emoji="🟡", default=(current == "moyen")),
            discord.SelectOption(label="Fort", value="fort", emoji="🔴", default=(current == "fort")),
        ]
        super().__init__(placeholder="Intensité du module", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        conf = get_conf(interaction.guild_id)
        sel = SELECTED.get((interaction.guild_id, interaction.user.id), "antispam")
        targets = list(MODULES.keys()) if sel == "all" else [sel]
        for k in targets:
            conf["modules"][k]["level"] = self.values[0]
        save()
        await refresh(interaction)


class LogSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.text], placeholder="📜 Choisir le salon de logs", row=2)

    async def callback(self, interaction: discord.Interaction):
        get_conf(interaction.guild_id)["log_channel"] = self.values[0].id
        save()
        await refresh(interaction)


class CategorySelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.category], placeholder="🎫 Choisir la catégorie des tickets", row=3)

    async def callback(self, interaction: discord.Interaction):
        get_conf(interaction.guild_id)["ticket_category"] = self.values[0].id
        save()
        await refresh(interaction)


class StaffSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="🛡️ Choisir le rôle staff", row=4)

    async def callback(self, interaction: discord.Interaction):
        get_conf(interaction.guild_id)["staff_role"] = self.values[0].id
        save()
        await refresh(interaction)


class ReglageView(discord.ui.View):
    def __init__(self, sel: str, conf: dict):
        super().__init__(timeout=600)
        current = None if sel == "all" else conf["modules"][sel]["level"]
        self.add_item(ModuleSelect(sel))
        self.add_item(LevelSelect(current))
        self.add_item(LogSelect())
        self.add_item(CategorySelect())
        self.add_item(StaffSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("⛔ Permission requise : Gérer le serveur.", ephemeral=True)
            return False
        return True


# ============================================================
#  TICKETS
# ============================================================
async def open_ticket(interaction: discord.Interaction):
    guild = interaction.guild
    conf = get_conf(guild.id)

    for ch in guild.text_channels:
        if ch.topic == f"ticket:{interaction.user.id}":
            return await interaction.response.send_message(f"Tu as déjà un ticket ouvert : {ch.mention}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    perms = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: perms,
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    staff = guild.get_role(conf["staff_role"]) if conf["staff_role"] else None
    if staff:
        overwrites[staff] = perms

    category = guild.get_channel(conf["ticket_category"]) if conf["ticket_category"] else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    channel = await guild.create_text_channel(
        name=f"ticket-{interaction.user.name}"[:90],
        category=category,
        topic=f"ticket:{interaction.user.id}",
        overwrites=overwrites,
    )

    embed = discord.Embed(
        title="🎫 Ticket ouvert", color=0x57F287,
        description="Explique ton problème, le staff te répondra bientôt.\nClique sur le bouton ci-dessous pour fermer le ticket.",
    )
    mention = f"{interaction.user.mention} {staff.mention if staff else ''}"
    await channel.send(content=mention, embed=embed, view=TicketCloseView())
    await send_log(guild, conf, "🎫 Ticket ouvert", f"{interaction.user.mention} a ouvert {channel.mention}", 0x57F287)
    await interaction.followup.send(f"✅ Ticket créé : {channel.mention}", ephemeral=True)


async def close_ticket(interaction: discord.Interaction):
    conf = get_conf(interaction.guild_id)
    topic = interaction.channel.topic or ""
    if not topic.startswith("ticket:"):
        return await interaction.response.send_message("Ce salon n'est pas un ticket.", ephemeral=True)

    owner_id = int(topic.split(":")[1])
    allowed = (
        interaction.user.id == owner_id
        or is_staff(interaction.user, conf)
        or interaction.user.guild_permissions.manage_channels
    )
    if not allowed:
        return await interaction.response.send_message("⛔ Seul le staff ou le créateur peut fermer ce ticket.", ephemeral=True)

    await interaction.response.send_message("🔒 Ticket fermé, suppression dans 5 secondes…")
    await send_log(interaction.guild, conf, "🔒 Ticket fermé", f"{interaction.channel.name} fermé par {interaction.user.mention}", 0xED4245)
    await asyncio.sleep(5)
    try:
        await interaction.channel.delete()
    except discord.HTTPException:
        pass


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ouvrir un ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket(interaction)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)


# ============================================================
#  BOT
# ============================================================
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True           # à activer dans le portail développeur
        intents.message_content = True   # à activer dans le portail développeur
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())
        self.tree.add_command(ticket_group)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)   # instantané (test)
        else:
            await self.tree.sync()              # global


bot = Bot()


@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")
    # Nettoyage unique : supprime les anciennes commandes enregistrées par serveur
    if not GUILD_ID:
        for g in bot.guilds:
            bot.tree.clear_commands(guild=g)
            await bot.tree.sync(guild=g)

@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ Tu n'as pas la permission d'utiliser cette commande."
    else:
        print(error)
        msg = "❌ Une erreur est survenue."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ============================================================
#  COMMANDES
# ============================================================
@bot.tree.command(name="reglage", description="Ouvre le panneau de réglages du bot")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def reglage(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("⛔ Permission requise : Gérer le serveur.", ephemeral=True)
    embed, view = build_panel(interaction.guild, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="aide", description="Liste des commandes du bot")
async def aide(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Commandes", color=0x5865F2, description="\n".join([
        "**/reglage** — Panneau de réglages (protections, logs, tickets)",
        "**/ticket panel | ajouter | fermer** — Système de tickets",
        "**/clear** · **/kick** · **/ban** · **/timeout** · **/untimeout** — Modération",
        "**/userinfo** · **/serverinfo** · **/avatar** · **/ping** — Infos",
    ]))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Affiche la latence du bot")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong ! {round(bot.latency * 1000)} ms")


@bot.tree.command(name="avatar", description="Affiche l'avatar d'un membre")
@app_commands.describe(membre="Le membre")
async def avatar(interaction: discord.Interaction, membre: Optional[discord.User] = None):
    user = membre or interaction.user
    embed = discord.Embed(title=f"Avatar de {user.name}", color=0x5865F2)
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="Infos sur un membre")
@app_commands.guild_only()
@app_commands.describe(membre="Le membre")
async def userinfo(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    m = membre or interaction.user
    roles = " ".join(r.mention for r in m.roles if r != interaction.guild.default_role) or "Aucun"
    embed = discord.Embed(title=str(m), color=0x5865F2)
    embed.set_thumbnail(url=m.display_avatar.url)
    embed.add_field(name="ID", value=str(m.id))
    embed.add_field(name="Compte créé", value=discord.utils.format_dt(m.created_at, "R"))
    embed.add_field(name="Arrivé le", value=discord.utils.format_dt(m.joined_at, "R") if m.joined_at else "Inconnu")
    embed.add_field(name="Rôles", value=roles, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Infos sur le serveur")
@app_commands.guild_only()
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Membres", value=str(g.member_count))
    embed.add_field(name="Salons", value=str(len(g.channels)))
    embed.add_field(name="Rôles", value=str(len(g.roles)))
    embed.add_field(name="Créé", value=discord.utils.format_dt(g.created_at, "R"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="clear", description="Supprime des messages")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(nombre="Nombre de messages (1 à 100)")
async def clear(interaction: discord.Interaction, nombre: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    await interaction.followup.send(f"🧹 {len(deleted)} message(s) supprimé(s).", ephemeral=True)


async def check_target(interaction: discord.Interaction, target: discord.Member) -> bool:
    if target.id == interaction.user.id:
        await interaction.response.send_message("Tu ne peux pas te cibler toi-même.", ephemeral=True)
        return False
    if interaction.guild.owner_id != interaction.user.id and target.top_role >= interaction.user.top_role:
        await interaction.response.send_message("⛔ Ce membre a un rôle supérieur ou égal au tien.", ephemeral=True)
        return False
    return True


async def mod_action(interaction: discord.Interaction, name: str, target: discord.Member, reason: str, coro):
    try:
        await coro
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Je n'ai pas les permissions (vérifie la hiérarchie de mes rôles).", ephemeral=True)
    conf = get_conf(interaction.guild_id)
    await send_log(interaction.guild, conf, f"🔨 /{name}",
                   f"**Cible :** {target.mention}\n**Par :** {interaction.user.mention}\n**Raison :** {reason}", 0xED4245)
    await interaction.response.send_message(f"✅ /{name} appliqué à **{target.name}**.")


@bot.tree.command(name="kick", description="Expulse un membre")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison"):
    if await check_target(interaction, membre):
        await mod_action(interaction, "kick", membre, raison, membre.kick(reason=raison))


@bot.tree.command(name="ban", description="Bannit un membre")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison"):
    if await check_target(interaction, membre):
        await mod_action(interaction, "ban", membre, raison, membre.ban(reason=raison))


@bot.tree.command(name="timeout", description="Met un membre en sourdine")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(minutes="Durée en minutes (max 40320)")
async def timeout(interaction: discord.Interaction, membre: discord.Member,
                  minutes: app_commands.Range[int, 1, 40320], raison: str = "Aucune raison"):
    if await check_target(interaction, membre):
        await mod_action(interaction, "timeout", membre, raison,
                         membre.timeout(timedelta(minutes=minutes), reason=raison))


@bot.tree.command(name="untimeout", description="Retire la sourdine d'un membre")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, membre: discord.Member):
    await mod_action(interaction, "untimeout", membre, "—", membre.timeout(None))


# ---- Tickets ----
ticket_group = app_commands.Group(name="ticket", description="Gestion des tickets", guild_only=True)


@ticket_group.command(name="panel", description="Envoie le panneau de création de tickets (admin)")
async def ticket_panel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("⛔ Permission requise : Gérer le serveur.", ephemeral=True)
    embed = discord.Embed(title="🎫 Support", color=0x5865F2,
                          description="Besoin d'aide ? Clique sur le bouton pour ouvrir un ticket privé avec le staff.")
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ Panneau envoyé.", ephemeral=True)


@ticket_group.command(name="ajouter", description="Ajoute un membre au ticket")
async def ticket_add(interaction: discord.Interaction, membre: discord.Member):
    conf = get_conf(interaction.guild_id)
    if not (interaction.channel.topic or "").startswith("ticket:"):
        return await interaction.response.send_message("Ce salon n'est pas un ticket.", ephemeral=True)
    if not is_staff(interaction.user, conf):
        return await interaction.response.send_message("⛔ Réservé au staff.", ephemeral=True)
    await interaction.channel.set_permissions(membre, view_channel=True, send_messages=True, read_message_history=True)
    await interaction.response.send_message(f"✅ {membre.mention} ajouté au ticket.")


@ticket_group.command(name="fermer", description="Ferme le ticket actuel")
async def ticket_close(interaction: discord.Interaction):
    await close_ticket(interaction)


# ============================================================
#  PROTECTIONS : MESSAGES
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
        return

    conf = get_conf(message.guild.id)
    if is_staff(message.author, conf):
        return

    m = conf["modules"]
    content = message.content or ""

    # Anti-spam
    lvl = m["antispam"]["level"]
    if lvl != "off":
        L = LEVELS[lvl]
        key = (message.guild.id, message.author.id)
        now = time.time()
        history = [(t, c) for t, c in MSG_LOG[key] if now - t < 15]
        history.append((now, content))
        MSG_LOG[key] = history
        burst = sum(1 for t, _ in history if now - t < L["spam"][1])
        dups = sum(1 for _, c in history if c and c == content) if content else 0
        if burst >= L["spam"][0] or dups >= L["dup"]:
            MSG_LOG.pop(key, None)
            return await punish(message, conf, "spam détecté", lvl, instant=True)

    # Anti-liens
    lvl = m["antilink"]["level"]
    if lvl != "off":
        mode = LEVELS[lvl]["links"]
        bad = bool(INVITES.search(content)) \
            or (mode != "invites" and bool(SHORTENERS.search(content))) \
            or (mode == "all" and bool(ANY_LINK.search(content)))
        if bad:
            return await punish(message, conf, "les liens ne sont pas autorisés ici", lvl)

    # Anti-majuscules
    lvl = m["anticaps"]["level"]
    if lvl != "off":
        min_letters, ratio = LEVELS[lvl]["caps"]
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", content)
        if len(letters) >= min_letters:
            upper = sum(1 for c in letters if c.isupper())
            if upper / len(letters) >= ratio:
                return await punish(message, conf, "trop de majuscules", lvl)

    # Anti-mentions de masse
    lvl = m["antimention"]["level"]
    if lvl != "off":
        count = len(message.mentions) + len(message.role_mentions)
        if count >= LEVELS[lvl]["mentions"] or (message.mention_everyone and lvl == "fort"):
            return await punish(message, conf, "trop de mentions", lvl, instant=True)


# ============================================================
#  PROTECTIONS : ANTI-RAID
# ============================================================
@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    conf = get_conf(member.guild.id)
    lvl = conf["modules"]["antiraid"]["level"]
    if lvl == "off":
        return

    joins, seconds = LEVELS[lvl]["raid"]
    now = time.time()
    arr = [t for t in JOIN_LOG[member.guild.id] if now - t < seconds]
    arr.append(now)
    JOIN_LOG[member.guild.id] = arr

    if len(arr) >= joins:
        try:
            await member.kick(reason="Anti-raid : arrivées massives")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await send_log(member.guild, conf, "🚨 Anti-raid",
                       f"{member} expulsé ({len(arr)} arrivées en {seconds}s).", 0xED4245)


# ============================================================
#  DÉMARRAGE
# ============================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ Token manquant : crée un fichier .env avec TOKEN=ton_token")
    bot.run(DISCORD_TOKEN)
