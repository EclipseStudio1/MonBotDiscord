import asyncio
import copy
import io
import json
import math
import os
import platform
import re
import time
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher
from threading import Thread
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel : serveur de test (commandes instantanées)
BOT_START = time.time()

# ============================================================
#  MINI SERVEUR WEB (pour Render)
# ============================================================
web = Flask(__name__)


@web.route("/")
def home():
    return "Bot en ligne"


def run_web():
    port = int(os.environ.get("PORT", 10000))
    web.run(host="0.0.0.0", port=port)


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


# ---------- Styles par défaut ----------
# Variables des tickets : {user} {server} {number}
DEFAULT_STYLE = {
    "panel_title": "🎫 Centre d'aide",
    "panel_description": (
        "Une question, un problème ou une demande ?\n\n"
        "Clique sur le bouton ci-dessous pour ouvrir un **ticket privé** "
        "avec notre équipe.\n\n"
        "> 🕐 Nous répondons dès que possible.\n"
        "> 🔒 Seuls toi et le staff voyez le ticket."
    ),
    "panel_color": "#5865F2",
    "panel_image": "",
    "panel_thumbnail": "",
    "panel_footer": "{server} • Support",
    "button_label": "Ouvrir un ticket",
    "button_emoji": "🎫",
    "ticket_title": "🎫 Ticket #{number}",
    "ticket_description": (
        "Bienvenue {user} 👋\n\n"
        "Explique ton problème avec un maximum de détails, "
        "un membre du staff va te répondre.\n\n"
        "**Boutons ci-dessous :**\n"
        "🙋 Prendre en charge *(staff)*\n"
        "🔒 Fermer le ticket"
    ),
    "ticket_color": "#57F287",
    "ticket_image": "",
}

# Variables bienvenue : {user} {user.name} {server} {count} {created}
#                       {inviter} {inviter.name} {inviter.total}
DEFAULT_WELCOME = {
    "enabled": False,
    "channel": None,
    "autorole": None,
    "content": "{user}",
    "title": "👋 Bienvenue sur {server} !",
    "description": (
        "Bienvenue {user} ! Nous sommes maintenant **{count}** membres 🎉\n\n"
        "📨 **Invité par :** {inviter} ({inviter.total} invitations)\n"
        "📅 **Compte créé :** {created}\n\n"
        "Pense à lire le règlement et à te présenter !"
    ),
    "color": "#5865F2",
    "footer": "{server} • Membre n°{count}",
    "image": "",
    "thumbnail": "avatar",   # avatar | serveur | aucune | URL
    "dm_enabled": False,
    "dm_title": "👋 Bienvenue sur {server} !",
    "dm_description": "Merci de nous avoir rejoints, **{user.name}** ! Lis le règlement et amuse-toi bien 🎉",
    "leave_enabled": False,
    "leave_channel": None,
    "leave_title": "👋 {user.name} nous a quittés",
    "leave_description": "Au revoir **{user.name}**… Nous sommes maintenant **{count}** membres.",
    "leave_color": "#ED4245",
}

# Variables classement : {rank} {user} {total} {regular} {left} {fake} {bonus} {server} {page} {pages}
DEFAULT_INV_STYLE = {
    "title": "🏆 Classement des invitations",
    "description": "Les meilleurs recruteurs de **{server}** !\n*Les départs et les comptes suspects sont déduits.*",
    "color": "#F59E0B",
    "line": "{rank} {user} — **{total}** invitations\n┕ ✅ {regular} · 🚪 {left} · ⚠️ {fake} · 🎁 {bonus}",
    "footer": "Page {page}/{pages} • {server}",
    "image": "",
    "thumbnail": "serveur",
}

DEFAULT_INV_CONF = {"enabled": True, "min_age_days": 7, "similar_check": True, "similarity": 80}


def default_conf():
    return {
        "log_channel": None,
        "ticket_category": None,
        "staff_role": None,
        "ticket_counter": 0,
        "ticket_style": dict(DEFAULT_STYLE),
        "welcome": dict(DEFAULT_WELCOME),
        "invites": {
            "users": {},      # inviter_id -> {regular, left, fake, bonus}
            "members": {},    # member_id -> {inviter, code, status, left, ts, reasons}
            "config": dict(DEFAULT_INV_CONF),
            "style": dict(DEFAULT_INV_STYLE),
        },
        "warns": {},          # member_id -> [{reason, mod, ts}]
        "modules": {
            "antispam": {"level": "moyen"},
            "antilink": {"level": "off"},
            "anticaps": {"level": "off"},
            "antimention": {"level": "moyen"},
            "antiraid": {"level": "off"},
        },
    }


def merge_defaults(conf: dict, default: dict):
    for k, v in default.items():
        if k not in conf:
            conf[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(conf[k], dict):
            merge_defaults(conf[k], v)


_MERGED = set()


def get_conf(guild_id: int) -> dict:
    key = str(guild_id)
    conf = DB.get(key)
    if conf is None:
        conf = DB[key] = default_conf()
        _MERGED.add(guild_id)
    elif guild_id not in _MERGED:
        merge_defaults(conf, default_conf())
        _MERGED.add(guild_id)
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
INVITE_CACHE = {}                  # guild_id -> {code: (uses, inviter_id)}
INVITE_DELETED = defaultdict(list)  # guild_id -> [(code, inviter_id, timestamp)]
INVITE_LOCKS = defaultdict(asyncio.Lock)


# ============================================================
#  OUTILS GÉNÉRAUX
# ============================================================
def fmt(text: str, mapping: Optional[dict] = None, **kw) -> str:
    """Remplace {variable} sans planter sur les autres accolades."""
    data = dict(mapping or {})
    data.update(kw)
    for k, v in data.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def parse_color(value: str, fallback: int = 0x5865F2) -> int:
    value = (value or "").strip().lstrip("#")
    try:
        if len(value) == 6:
            return int(value, 16)
    except ValueError:
        pass
    return fallback


def valid_url(value: str) -> bool:
    return bool(value) and value.startswith(("http://", "https://"))


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d} j")
    if h:
        parts.append(f"{h} h")
    parts.append(f"{m} min")
    return " ".join(parts)


async def log_embed(guild: discord.Guild, conf: dict, embed: discord.Embed, file: Optional[discord.File] = None):
    if not conf["log_channel"]:
        return
    channel = guild.get_channel(conf["log_channel"])
    if channel is None:
        return
    try:
        await channel.send(embed=embed, file=file)
    except discord.HTTPException:
        pass


async def send_log(guild: discord.Guild, conf: dict, title: str, desc: str,
                   color: int = 0xF59E0B, file: Optional[discord.File] = None):
    await log_embed(guild, conf, discord.Embed(title=title, description=desc, color=color,
                                               timestamp=discord.utils.utcnow()), file)


async def try_dm(user: discord.abc.User, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


def is_staff(member: discord.Member, conf: dict) -> bool:
    p = member.guild_permissions
    if p.administrator or p.manage_messages:
        return True
    return bool(conf["staff_role"]) and any(r.id == conf["staff_role"] for r in member.roles)


async def deny_admin(interaction: discord.Interaction) -> bool:
    """True si l'utilisateur n'a pas 'Gérer le serveur' (et envoie le refus)."""
    if interaction.user.guild_permissions.manage_guild:
        return False
    await interaction.response.send_message("⛔ Permission requise : **Gérer le serveur**.", ephemeral=True)
    return True


class FieldsModal(discord.ui.Modal):
    """Modal générique : édite des clés d'un dictionnaire (puis sauvegarde)."""

    def __init__(self, title: str, store: dict, fields: list, on_done):
        super().__init__(title=title[:45])
        self.store = store
        self.on_done = on_done
        self.inputs = []
        for key, label, long, maxlen, required in fields:
            ti = discord.ui.TextInput(
                label=label[:45],
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                default=str(store.get(key, "") or "")[:maxlen],
                max_length=maxlen,
                required=required,
            )
            self.add_item(ti)
            self.inputs.append((key, ti))

    async def on_submit(self, interaction: discord.Interaction):
        for key, ti in self.inputs:
            self.store[key] = ti.value.strip()
        save()
        await self.on_done(interaction)


class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int, on_confirm):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ce n'est pas ton menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmer", emoji="✅", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_confirm(interaction)

    @discord.ui.button(label="Annuler", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Action annulée.", view=None)


# ============================================================
#  PROTECTIONS (punition automatique)
# ============================================================
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
#  AIDE INTERACTIVE
# ============================================================
HELP = {
    "mod": ("🛡️", "Modération", "Sanctionne, avertis et contrôle ton serveur.", [
        ("/kick membre [raison]", "Expulse un membre (avec MP automatique)"),
        ("/ban membre [raison] [supprimer_messages]", "Bannit un membre, et efface ses messages récents (0-7 j)"),
        ("/unban id [raison]", "Débannit un utilisateur grâce à son ID"),
        ("/timeout membre minutes [raison]", "Met un membre en sourdine"),
        ("/untimeout membre", "Retire la sourdine"),
        ("/warn membre [raison]", "Avertit un membre — 3 avertissements = sourdine auto de 1 h"),
        ("/warns membre", "Affiche l'historique des avertissements"),
        ("/clearwarns membre [numero]", "Supprime un ou tous les avertissements"),
        ("/clear nombre [membre]", "Supprime des messages (option : ceux d'un seul membre)"),
        ("/lock [salon] · /unlock [salon]", "Verrouille / déverrouille un salon"),
        ("/slowmode secondes [salon]", "Règle le mode lent (0 = désactivé)"),
        ("/nick membre [pseudo]", "Change ou réinitialise le pseudo d'un membre"),
        ("/role ajouter | retirer", "Donne ou retire un rôle à un membre"),
    ]),
    "info": ("ℹ️", "Informations", "Toutes les infos détaillées sur le serveur et les membres.", [
        ("/userinfo [membre]", "Profil complet : badges, dates, rôles, permissions, invitations, avertissements"),
        ("/serverinfo", "Fiche complète du serveur : membres, salons, boosts, sécurité…"),
        ("/roleinfo role", "Détails d'un rôle : membres, permissions, position"),
        ("/channelinfo [salon]", "Détails d'un salon"),
        ("/roles", "Liste des rôles avec le nombre de membres"),
        ("/membercount", "Répartition des membres (humains / bots)"),
        ("/avatar [membre] · /banner [membre]", "Avatar / bannière d'un membre"),
        ("/servericon", "Icône et bannière du serveur"),
        ("/botinfo · /ping", "État du bot, latence, uptime"),
    ]),
    "util": ("🧰", "Utilitaires", "Outils pour animer ton serveur.", [
        ("/embed [salon]", "Crée un embed personnalisé via un formulaire"),
        ("/sondage question option1 option2 …", "Crée un sondage avec réactions (jusqu'à 5 choix)"),
    ]),
    "ticket": ("🎫", "Tickets", "Support privé avec le staff, entièrement stylisable.", [
        ("/ticket style", "Personnalise le panneau et le message d'accueil (aperçu en direct)"),
        ("/ticket panel", "Envoie le panneau d'ouverture dans le salon"),
        ("/ticket ajouter membre", "Ajoute un membre au ticket (staff)"),
        ("/ticket fermer", "Ferme le ticket (une transcription est envoyée dans les logs)"),
    ]),
    "invites": ("📨", "Invitations", "Suivi des invitations avec détection des faux comptes.", [
        ("/invites stats [membre]", "Statistiques complètes + derniers invités"),
        ("/invites top", "Classement paginé des invitations"),
        ("/invites qui membre", "Découvre qui a invité un membre"),
        ("/invites style", "Personnalise le message du classement"),
        ("/invites config", "Règle l'anti-fake (âge du compte, pseudos similaires…)"),
        ("/invites ajouter | retirer", "Ajoute / retire des invitations bonus"),
        ("/invites reset [membre]", "Réinitialise les invitations"),
    ]),
    "welcome": ("👋", "Bienvenue", "Messages d'arrivée et de départ, rôle automatique.", [
        ("/bienvenue config", "Panneau de configuration complet avec aperçu"),
        ("/bienvenue tester", "Envoie un message de bienvenue test dans ce salon"),
    ]),
    "config": ("⚙️", "Configuration", "Protections automatiques et réglages généraux.", [
        ("/reglage", "Anti-spam, anti-liens, anti-majuscules, anti-mentions, anti-raid, logs, staff"),
    ]),
}


def help_embed(key: str) -> discord.Embed:
    if key == "home":
        total = sum(len(v[3]) for v in HELP.values())
        embed = discord.Embed(
            title=f"📖 Centre d'aide — {bot.user.name if bot.user else 'Bot'}",
            description=(
                "Bienvenue ! Ce bot gère la **modération**, les **tickets**, les **invitations**, "
                "la **bienvenue** et plein d'outils.\n\n"
                "👇 **Choisis une catégorie dans le menu** pour voir le détail des commandes."
            ),
            color=0x5865F2,
        )
        for k, (emoji, name, desc, cmds) in HELP.items():
            embed.add_field(name=f"{emoji} {name}", value=f"{desc}\n`{len(cmds)} commande(s)`", inline=True)
        embed.set_footer(text=f"{total} commandes • Astuce : tape / pour voir toutes les commandes")
    else:
        emoji, name, desc, cmds = HELP[key]
        body = "\n\n".join(f"`{usage}`\n└ {d}" for usage, d in cmds)
        embed = discord.Embed(title=f"{emoji} {name}", description=f"*{desc}*\n\n{body}", color=0x5865F2)
        embed.set_footer(text="[ ] = optionnel • Certaines commandes demandent des permissions")
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self, current: str = "home"):
        options = [discord.SelectOption(label="Accueil", value="home", emoji="🏠", default=(current == "home"))]
        for k, (emoji, name, desc, _) in HELP.items():
            options.append(discord.SelectOption(label=name, value=k, emoji=emoji, description=desc[:100], default=(k == current)))
        super().__init__(placeholder="📂 Choisis une catégorie…", options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        await interaction.response.edit_message(embed=help_embed(key), view=HelpView(interaction.user.id, key))


class HelpView(discord.ui.View):
    def __init__(self, user_id: int, current: str = "home"):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(HelpSelect(current))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ouvre ton propre menu avec **/aide** 😉", ephemeral=True)
            return False
        return True


# ============================================================
#  TICKETS : STYLE (embeds personnalisables)
# ============================================================
def build_panel_embed(style: dict, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=fmt(style["panel_title"], server=guild.name)[:256],
        description=fmt(style["panel_description"], server=guild.name)[:4000],
        color=parse_color(style["panel_color"]),
    )
    if valid_url(style["panel_image"]):
        embed.set_image(url=style["panel_image"])
    if valid_url(style["panel_thumbnail"]):
        embed.set_thumbnail(url=style["panel_thumbnail"])
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if style["panel_footer"]:
        embed.set_footer(text=fmt(style["panel_footer"], server=guild.name)[:2000])
    return embed


def build_ticket_embed(style: dict, guild: discord.Guild, user: discord.abc.User, number: int) -> discord.Embed:
    kw = dict(user=user.mention, server=guild.name, number=f"{number:04d}")
    embed = discord.Embed(
        title=fmt(style["ticket_title"], **kw)[:256],
        description=fmt(style["ticket_description"], **kw)[:4000],
        color=parse_color(style["ticket_color"], 0x57F287),
        timestamp=discord.utils.utcnow(),
    )
    if valid_url(style["ticket_image"]):
        embed.set_image(url=style["ticket_image"])
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"{guild.name} • Ticket #{number:04d}")
    return embed


def style_preview(interaction: discord.Interaction):
    s = get_conf(interaction.guild_id)["ticket_style"]
    return [build_panel_embed(s, interaction.guild), build_ticket_embed(s, interaction.guild, interaction.user, 1)]


STYLE_HELP = (
    "🎨 **Style des tickets** — aperçu du panneau (haut) et du message d'accueil (bas).\n"
    "Variables : `{user}` `{server}` `{number}` · Couleurs au format `#5865F2`"
)


async def refresh_style(interaction: discord.Interaction):
    await interaction.response.edit_message(content=STYLE_HELP, embeds=style_preview(interaction), view=StyleView())


class StyleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("⛔ Permission requise : Gérer le serveur.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Texte du panneau", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def b_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = get_conf(interaction.guild_id)["ticket_style"]
        await interaction.response.send_modal(FieldsModal("Texte du panneau", s, [
            ("panel_title", "Titre", False, 100, True),
            ("panel_description", "Description", True, 2000, True),
            ("panel_color", "Couleur (hex)", False, 7, True),
            ("panel_footer", "Pied de page", False, 100, False),
        ], refresh_style))

    @discord.ui.button(label="Images & bouton", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def b_extra(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = get_conf(interaction.guild_id)["ticket_style"]
        await interaction.response.send_modal(FieldsModal("Images & bouton", s, [
            ("panel_image", "Grande image (URL, vide = aucune)", False, 400, False),
            ("panel_thumbnail", "Petite image (URL, vide = icône serveur)", False, 400, False),
            ("button_label", "Texte du bouton", False, 40, True),
            ("button_emoji", "Émoji du bouton", False, 10, False),
        ], refresh_style))

    @discord.ui.button(label="Message du ticket", emoji="💬", style=discord.ButtonStyle.primary, row=0)
    async def b_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = get_conf(interaction.guild_id)["ticket_style"]
        await interaction.response.send_modal(FieldsModal("Message du ticket", s, [
            ("ticket_title", "Titre", False, 100, True),
            ("ticket_description", "Description", True, 2000, True),
            ("ticket_color", "Couleur (hex)", False, 7, True),
            ("ticket_image", "Image (URL, vide = aucune)", False, 400, False),
        ], refresh_style))

    @discord.ui.button(label="Envoyer le panneau ici", emoji="📤", style=discord.ButtonStyle.success, row=1)
    async def b_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        await post_panel(interaction.channel, interaction.guild)
        await interaction.response.send_message("✅ Panneau envoyé dans ce salon.", ephemeral=True)

    @discord.ui.button(label="Réinitialiser", emoji="♻️", style=discord.ButtonStyle.danger, row=1)
    async def b_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        get_conf(interaction.guild_id)["ticket_style"] = dict(DEFAULT_STYLE)
        save()
        await refresh_style(interaction)


async def post_panel(channel: discord.abc.Messageable, guild: discord.Guild):
    style = get_conf(guild.id)["ticket_style"]
    embed = build_panel_embed(style, guild)
    try:
        await channel.send(embed=embed, view=TicketPanelView(style))
    except discord.HTTPException:
        fallback = dict(style, button_emoji="")
        await channel.send(embed=embed, view=TicketPanelView(fallback))


# ============================================================
#  TICKETS : OUVERTURE / FERMETURE
# ============================================================
async def open_ticket(interaction: discord.Interaction):
    guild = interaction.guild
    conf = get_conf(guild.id)

    for ch in guild.text_channels:
        if ch.topic == f"ticket:{interaction.user.id}":
            return await interaction.response.send_message(f"Tu as déjà un ticket ouvert : {ch.mention}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    conf["ticket_counter"] += 1
    number = conf["ticket_counter"]
    save()

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
        name=f"ticket-{number:04d}",
        category=category,
        topic=f"ticket:{interaction.user.id}",
        overwrites=overwrites,
    )

    embed = build_ticket_embed(conf["ticket_style"], guild, interaction.user, number)
    mention = f"{interaction.user.mention} {staff.mention if staff else ''}"
    await channel.send(content=mention, embed=embed, view=TicketControlView())
    await send_log(guild, conf, "🎫 Ticket ouvert", f"{interaction.user.mention} a ouvert {channel.mention} (#{number:04d})", 0x57F287)
    await interaction.followup.send(f"✅ Ticket créé : {channel.mention}", ephemeral=True)


async def make_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%d/%m/%Y %H:%M")
        text = m.content or ("[embed]" if m.embeds else "")
        if m.attachments:
            text += " " + " ".join(a.url for a in m.attachments)
        if text.strip():
            lines.append(f"[{ts}] {m.author} : {text}")
    return "\n".join(lines) or "(ticket vide)"


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

    embed = discord.Embed(description="🔒 **Ticket fermé** — suppression dans 5 secondes…", color=0xED4245)
    await interaction.response.send_message(embed=embed)

    transcript = await make_transcript(interaction.channel)
    file = discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"{interaction.channel.name}.txt")
    await send_log(
        interaction.guild, conf, "🔒 Ticket fermé",
        f"**Ticket :** {interaction.channel.name}\n**Créateur :** <@{owner_id}>\n**Fermé par :** {interaction.user.mention}",
        0xED4245, file=file,
    )
    await asyncio.sleep(5)
    try:
        await interaction.channel.delete()
    except discord.HTTPException:
        pass


class TicketPanelView(discord.ui.View):
    """Panneau public (bouton d'ouverture). Le texte/émoji du bouton vient du style."""

    def __init__(self, style: Optional[dict] = None):
        super().__init__(timeout=None)
        style = style or DEFAULT_STYLE
        self.open_btn.label = style["button_label"] or "Ouvrir un ticket"
        self.open_btn.emoji = style["button_emoji"] or None

    @discord.ui.button(label="Ouvrir un ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket(interaction)


class TicketControlView(discord.ui.View):
    """Boutons dans le ticket : prendre en charge + fermer."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Prendre en charge", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        conf = get_conf(interaction.guild_id)
        if not is_staff(interaction.user, conf):
            return await interaction.response.send_message("⛔ Réservé au staff.", ephemeral=True)
        button.disabled = True
        button.label = f"Pris par {interaction.user.display_name}"[:80]
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=discord.Embed(
            description=f"🙋 {interaction.user.mention} prend en charge ce ticket.", color=0x57F287))

    @discord.ui.button(label="Fermer le ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)


# ============================================================
#  INVITATIONS : SUIVI + ANTI-FAKE
# ============================================================
RANK_ICON = {1: "🥇", 2: "🥈", 3: "🥉"}
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def inv_user(conf: dict, uid: int) -> dict:
    u = conf["invites"]["users"].setdefault(str(uid), {})
    for k in ("regular", "left", "fake", "bonus"):
        u.setdefault(k, 0)
    return u


def inv_total(u: dict) -> int:
    return u.get("regular", 0) + u.get("bonus", 0) - u.get("left", 0)


def invite_total(conf: dict, uid) -> int:
    if not uid:
        return 0
    return inv_total(conf["invites"]["users"].get(str(uid), {}))


def ranked(conf: dict):
    out = []
    for uid, u in conf["invites"]["users"].items():
        if any(u.get(k, 0) for k in ("regular", "bonus", "left", "fake")):
            out.append((int(uid), u, inv_total(u)))
    out.sort(key=lambda x: (-x[2], -x[1].get("regular", 0)))
    return out


def norm_variants(s: str) -> set:
    s = (s or "").lower()
    a = re.sub(r"[^a-zà-ÿ]", "", s)
    b = re.sub(r"[^a-zà-ÿ]", "", s.translate(LEET))
    return {v for v in (a, b) if len(v) >= 3}


def name_similarity(a: str, b: str) -> float:
    best = 0.0
    for x in norm_variants(a):
        for y in norm_variants(b):
            if x == y:
                return 1.0
            if min(len(x), len(y)) >= 4 and (x in y or y in x):
                best = max(best, 0.9)
            best = max(best, SequenceMatcher(None, x, y).ratio())
    return best


def suspicious_reasons(member: discord.Member, inviter: Optional[discord.Member], cfg: dict) -> list:
    reasons = []
    age = (discord.utils.utcnow() - member.created_at).days
    if cfg["min_age_days"] and age < cfg["min_age_days"]:
        reasons.append(f"compte créé il y a {age} j (minimum {cfg['min_age_days']} j)")
    if inviter and cfg["similar_check"]:
        mine = [x for x in (member.name, member.display_name, member.global_name) if x]
        theirs = [x for x in (inviter.name, inviter.display_name, inviter.global_name) if x]
        best = max((name_similarity(x, y) for x in mine for y in theirs), default=0.0)
        if best * 100 >= cfg["similarity"]:
            reasons.append(f"pseudo similaire à l'inviteur ({int(best * 100)} %)")
        if member.avatar and inviter.avatar and member.avatar.key == inviter.avatar.key:
            reasons.append("même avatar que l'inviteur")
    return reasons


async def snapshot_invites(guild: discord.Guild):
    try:
        invs = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return
    INVITE_CACHE[guild.id] = {i.code: (i.uses or 0, i.inviter.id if i.inviter else None) for i in invs}


async def find_used_invite(guild: discord.Guild):
    """Retourne (code, inviter_id) de l'invitation utilisée, ou None."""
    old = INVITE_CACHE.get(guild.id, {})
    try:
        invs = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None
    new = {i.code: (i.uses or 0, i.inviter.id if i.inviter else None) for i in invs}
    INVITE_CACHE[guild.id] = new

    for code, (uses, inviter) in new.items():
        if uses > old.get(code, (0, None))[0]:
            return code, inviter

    # Invitation supprimée (limite d'utilisations atteinte, expirée…)
    now = time.time()
    gone = {c: old[c][1] for c in old if c not in new}
    for code, inviter, ts in INVITE_DELETED[guild.id]:
        if now - ts < 20:
            gone[code] = inviter
    INVITE_DELETED[guild.id] = [x for x in INVITE_DELETED[guild.id] if now - x[2] < 20]
    if len(gone) == 1:
        code, inviter = next(iter(gone.items()))
        return code, inviter
    return None


async def track_join(member: discord.Member, conf: dict) -> dict:
    inv = conf["invites"]
    info = {"inviter": None, "code": None, "status": "unknown", "reasons": []}
    if not inv["config"]["enabled"]:
        return info
    guild = member.guild

    async with INVITE_LOCKS[guild.id]:
        used = await find_used_invite(guild)

    prev = inv["members"].get(str(member.id))
    if used:
        code, inviter_id = used
        info["code"], info["inviter"] = code, inviter_id
        if inviter_id:
            inviter_member = guild.get_member(inviter_id)
            reasons = ["s'est invité lui-même"] if inviter_id == member.id \
                else suspicious_reasons(member, inviter_member, inv["config"])
            if prev and prev.get("inviter") == inviter_id and prev.get("status") == "regular" and prev.get("left"):
                # Retour d'un membre déjà compté : l'invitation est rendue
                u = inv_user(conf, inviter_id)
                u["left"] = max(0, u["left"] - 1)
                info["status"] = "regular"
            elif prev:
                info["status"] = "rejoin"          # déjà compté une fois : pas recompté
            elif reasons:
                info["status"] = "fake"
                info["reasons"] = reasons
                inv_user(conf, inviter_id)["fake"] += 1
            else:
                info["status"] = "regular"
                inv_user(conf, inviter_id)["regular"] += 1
    elif prev:
        info["status"] = "rejoin"

    inv["members"][str(member.id)] = {
        "inviter": info["inviter"], "code": info["code"], "status": info["status"],
        "left": False, "ts": int(time.time()), "reasons": info["reasons"],
    }
    save()
    return info


def track_leave(member: discord.Member, conf: dict) -> Optional[dict]:
    rec = conf["invites"]["members"].get(str(member.id))
    if not rec:
        return None
    if rec.get("status") == "regular" and not rec.get("left") and rec.get("inviter"):
        inv_user(conf, rec["inviter"])["left"] += 1     # -1 invitation
    rec["left"] = True
    save()
    return rec


STATUS_TEXT = {
    "regular": "✅ Valide", "fake": "⚠️ Suspecte (non comptée)", "rejoin": "🔁 Retour (non recomptée)",
    "unknown": "❔ Inconnue", "vanity": "🔗 Lien personnalisé",
}


def status_icon(rec: dict) -> str:
    if rec.get("status") == "fake":
        return "⚠️"
    if rec.get("left"):
        return "🚪"
    if rec.get("status") == "rejoin":
        return "🔁"
    return "✅"


def build_leaderboard(guild: discord.Guild, conf: dict, page: int, viewer_id: Optional[int] = None):
    st = conf["invites"]["style"]
    data = ranked(conf)
    sample = False
    if not data and viewer_id:
        data = [(viewer_id, {"regular": 14, "left": 2, "fake": 3, "bonus": 0}, 12)]
        sample = True
    per = 10
    pages = max(1, math.ceil(len(data) / per))
    page = max(0, min(page, pages - 1))

    lines = []
    for i, (uid, u, total) in enumerate(data[page * per:(page + 1) * per], start=page * per + 1):
        lines.append(fmt(
            st["line"], rank=RANK_ICON.get(i, f"`#{i}`"), user=f"<@{uid}>", total=total,
            regular=u.get("regular", 0), left=u.get("left", 0), fake=u.get("fake", 0),
            bonus=u.get("bonus", 0), server=guild.name,
        ))
    body = "\n".join(lines) or "*Aucune invitation pour le moment.*"
    if sample:
        body += "\n\n*(exemple d'aperçu — le vrai classement apparaîtra avec les invitations)*"

    embed = discord.Embed(
        title=fmt(st["title"], server=guild.name)[:256],
        description=(fmt(st["description"], server=guild.name) + "\n\n" + body)[:4096],
        color=parse_color(st["color"], 0xF59E0B),
    )
    users = conf["invites"]["users"].values()
    embed.add_field(
        name="📊 Serveur",
        value=(f"✅ **{sum(u.get('regular', 0) for u in users)}** valides · "
               f"🚪 **{sum(u.get('left', 0) for u in users)}** parties · "
               f"⚠️ **{sum(u.get('fake', 0) for u in users)}** suspectes"),
        inline=False,
    )
    if viewer_id:
        for pos, (uid, u, total) in enumerate(ranked(conf), start=1):
            if uid == viewer_id:
                embed.add_field(name="📍 Ton classement", value=f"**#{pos}** — {total} invitations", inline=False)
                break
    embed.set_footer(text=fmt(st["footer"], server=guild.name, page=page + 1, pages=pages)[:2000])
    if valid_url(st["image"]):
        embed.set_image(url=st["image"])
    th = st["thumbnail"]
    if valid_url(th):
        embed.set_thumbnail(url=th)
    elif th == "serveur" and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed, pages, page


class LeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, viewer_id: int, page: int = 0):
        super().__init__(timeout=300)
        self.guild, self.viewer_id, self.page = guild, viewer_id, page

    async def update(self, interaction: discord.Interaction):
        embed, pages, self.page = build_leaderboard(self.guild, get_conf(self.guild.id), self.page, self.viewer_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.update(interaction)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.update(interaction)


INV_STYLE_HELP = (
    "🎨 **Style du classement** — aperçu ci-dessous.\n"
    "Variables : `{rank}` `{user}` `{total}` `{regular}` `{left}` `{fake}` `{bonus}` `{server}` `{page}` `{pages}`"
)


async def refresh_inv_style(interaction: discord.Interaction):
    embed, _, _ = build_leaderboard(interaction.guild, get_conf(interaction.guild_id), 0, interaction.user.id)
    await interaction.response.edit_message(content=INV_STYLE_HELP, embed=embed, view=InvStyleView())


class InvStyleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("⛔ Permission requise : Gérer le serveur.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Texte", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def b_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = get_conf(interaction.guild_id)["invites"]["style"]
        await interaction.response.send_modal(FieldsModal("Texte du classement", s, [
            ("title", "Titre", False, 100, True),
            ("description", "Description", True, 1000, True),
            ("color", "Couleur (hex)", False, 7, True),
            ("footer", "Pied de page", False, 100, False),
        ], refresh_inv_style))

    @discord.ui.button(label="Lignes & images", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def b_lines(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = get_conf(interaction.guild_id)["invites"]["style"]
        await interaction.response.send_modal(FieldsModal("Lignes & images", s, [
            ("line", "Format d'une ligne", True, 300, True),
            ("image", "Grande image (URL)", False, 400, False),
            ("thumbnail", "Petite image : serveur / aucune / URL", False, 400, False),
        ], refresh_inv_style))

    @discord.ui.button(label="Envoyer le classement ici", emoji="📤", style=discord.ButtonStyle.success, row=1)
    async def b_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, _, _ = build_leaderboard(interaction.guild, get_conf(interaction.guild_id), 0)
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("✅ Classement envoyé.", ephemeral=True)

    @discord.ui.button(label="Réinitialiser", emoji="♻️", style=discord.ButtonStyle.danger, row=1)
    async def b_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        get_conf(interaction.guild_id)["invites"]["style"] = dict(DEFAULT_INV_STYLE)
        save()
        await refresh_inv_style(interaction)


# ============================================================
#  BIENVENUE (façon Koya)
# ============================================================
def member_vars(member: discord.Member, info: Optional[dict] = None) -> dict:
    g = member.guild
    conf = get_conf(g.id)
    info = info or {}
    inviter_id = info.get("inviter")
    inviter = g.get_member(inviter_id) if inviter_id else None
    if inviter_id:
        inviter_txt = f"<@{inviter_id}>"
    elif info.get("status") == "vanity":
        inviter_txt = "lien personnalisé"
    else:
        inviter_txt = "Inconnu"
    return {
        "user": member.mention,
        "user.name": member.display_name,
        "server": g.name,
        "count": g.member_count,
        "created": discord.utils.format_dt(member.created_at, "R"),
        "inviter": inviter_txt,
        "inviter.name": inviter.display_name if inviter else "Inconnu",
        "inviter.total": invite_total(conf, inviter_id),
    }


def welcome_embed(guild: discord.Guild, member: discord.Member, w: dict, info: Optional[dict], kind: str) -> discord.Embed:
    v = member_vars(member, info)
    if kind == "welcome":
        title, desc, color = w["title"], w["description"], w["color"]
    elif kind == "dm":
        title, desc, color = w["dm_title"], w["dm_description"], w["color"]
    else:
        title, desc, color = w["leave_title"], w["leave_description"], w["leave_color"]
    embed = discord.Embed(title=fmt(title, v)[:256], description=fmt(desc, v)[:4000], color=parse_color(color))

    if kind == "welcome":
        th = (w["thumbnail"] or "avatar").strip()
        if valid_url(th):
            embed.set_thumbnail(url=th)
        elif th.lower() == "serveur" and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        elif th.lower() == "avatar":
            embed.set_thumbnail(url=member.display_avatar.url)
        if valid_url(w["image"]):
            embed.set_image(url=w["image"])
        if w["footer"]:
            embed.set_footer(text=fmt(w["footer"], v)[:2000])
        embed.timestamp = discord.utils.utcnow()
    elif kind == "dm":
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=guild.name)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
    return embed


async def send_welcome(member: discord.Member, conf: dict, info: dict):
    w = conf["welcome"]
    guild = member.guild
    if w["enabled"] and w["channel"]:
        ch = guild.get_channel(w["channel"])
        if ch:
            try:
                await ch.send(
                    content=fmt(w["content"], member_vars(member, info))[:2000] or None,
                    embed=welcome_embed(guild, member, w, info, "welcome"),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                pass
    if w["dm_enabled"]:
        await try_dm(member, welcome_embed(guild, member, w, info, "dm"))


def welcome_status_embed(guild: discord.Guild, w: dict) -> discord.Embed:
    onoff = lambda b: "🟢 Activé" if b else "🔴 Désactivé"
    wel_ch = f"<#{w['channel']}>" if w["channel"] else "Salon non défini"
    leave_ch = f"<#{w['leave_channel']}>" if w["leave_channel"] else "Salon non défini"
    embed = discord.Embed(title="👋 Configuration de la bienvenue", color=0x5865F2,
                          description="Choisis les salons / le rôle, puis personnalise les messages avec les boutons.")
    embed.add_field(name="📥 Bienvenue", value=f"{onoff(w['enabled'])}\n{wel_ch}")
    embed.add_field(name="📤 Départ", value=f"{onoff(w['leave_enabled'])}\n{leave_ch}")
    embed.add_field(name="✉️ Message privé", value=onoff(w["dm_enabled"]))
    embed.add_field(name="🏷️ Rôle automatique", value=f"<@&{w['autorole']}>" if w["autorole"] else "Aucun", inline=False)
    embed.set_footer(text="Variables : {user} {user.name} {server} {count} {created} {inviter} {inviter.name} {inviter.total}")
    return embed


def welcome_panel(interaction: discord.Interaction):
    conf = get_conf(interaction.guild_id)
    w = conf["welcome"]
    info = {"inviter": interaction.user.id, "status": "regular"}
    embeds = [
        welcome_status_embed(interaction.guild, w),
        welcome_embed(interaction.guild, interaction.user, w, info, "welcome"),
        welcome_embed(interaction.guild, interaction.user, w, info, "leave"),
    ]
    return embeds, WelcomeView(w)


async def refresh_welcome(interaction: discord.Interaction):
    embeds, view = welcome_panel(interaction)
    await interaction.response.edit_message(embeds=embeds, view=view)


class WelcomeChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, key: str, placeholder: str, row: int):
        super().__init__(channel_types=[discord.ChannelType.text], placeholder=placeholder, row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        get_conf(interaction.guild_id)["welcome"][self.key] = self.values[0].id
        save()
        await refresh_welcome(interaction)


class WelcomeRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="🏷️ Rôle automatique à l'arrivée", row=2)

    async def callback(self, interaction: discord.Interaction):
        get_conf(interaction.guild_id)["welcome"]["autorole"] = self.values[0].id
        save()
        await refresh_welcome(interaction)


class WelcomeView(discord.ui.View):
    def __init__(self, w: dict):
        super().__init__(timeout=900)
        self.add_item(WelcomeChannelSelect("channel", "📥 Salon de bienvenue", 0))
        self.add_item(WelcomeChannelSelect("leave_channel", "📤 Salon des départs", 1))
        self.add_item(WelcomeRoleSelect())
        for btn, key, name in ((self.t_welcome, "enabled", "Bienvenue"), (self.t_dm, "dm_enabled", "MP"),
                               (self.t_leave, "leave_enabled", "Départ")):
            btn.label = f"{name} : {'ON' if w[key] else 'OFF'}"
            btn.style = discord.ButtonStyle.success if w[key] else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("⛔ Permission requise : Gérer le serveur.", ephemeral=True)
            return False
        return True

    def store(self, interaction: discord.Interaction) -> dict:
        return get_conf(interaction.guild_id)["welcome"]

    @discord.ui.button(label="Message", emoji="📝", style=discord.ButtonStyle.primary, row=3)
    async def b_msg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FieldsModal("Message de bienvenue", self.store(interaction), [
            ("title", "Titre", False, 100, True),
            ("description", "Description", True, 2000, True),
            ("color", "Couleur (hex)", False, 7, True),
            ("footer", "Pied de page", False, 100, False),
            ("content", "Texte hors embed (ex: {user})", False, 200, False),
        ], refresh_welcome))

    @discord.ui.button(label="Images", emoji="🖼️", style=discord.ButtonStyle.primary, row=3)
    async def b_img(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FieldsModal("Images de bienvenue", self.store(interaction), [
            ("image", "Grande image (URL, vide = aucune)", False, 400, False),
            ("thumbnail", "Petite : avatar / serveur / aucune / URL", False, 400, True),
        ], refresh_welcome))

    @discord.ui.button(label="Message privé", emoji="✉️", style=discord.ButtonStyle.primary, row=3)
    async def b_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FieldsModal("Message privé", self.store(interaction), [
            ("dm_title", "Titre", False, 100, True),
            ("dm_description", "Description", True, 2000, True),
        ], refresh_welcome))

    @discord.ui.button(label="Départ", emoji="👋", style=discord.ButtonStyle.primary, row=3)
    async def b_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FieldsModal("Message de départ", self.store(interaction), [
            ("leave_title", "Titre", False, 100, True),
            ("leave_description", "Description", True, 2000, True),
            ("leave_color", "Couleur (hex)", False, 7, True),
        ], refresh_welcome))

    @discord.ui.button(label="Bienvenue", row=4)
    async def t_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        w = self.store(interaction)
        w["enabled"] = not w["enabled"]
        save()
        await refresh_welcome(interaction)

    @discord.ui.button(label="MP", row=4)
    async def t_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        w = self.store(interaction)
        w["dm_enabled"] = not w["dm_enabled"]
        save()
        await refresh_welcome(interaction)

    @discord.ui.button(label="Départ", row=4)
    async def t_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        w = self.store(interaction)
        w["leave_enabled"] = not w["leave_enabled"]
        save()
        await refresh_welcome(interaction)

    @discord.ui.button(label="Tester ici", emoji="🧪", style=discord.ButtonStyle.success, row=4)
    async def b_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        w = self.store(interaction)
        info = {"inviter": interaction.user.id, "status": "regular"}
        await interaction.channel.send(
            content=fmt(w["content"], member_vars(interaction.user, info))[:2000] or None,
            embed=welcome_embed(interaction.guild, interaction.user, w, info, "welcome"),
        )
        await interaction.response.send_message("✅ Message de test envoyé dans ce salon.", ephemeral=True)

    @discord.ui.button(label="Reset", emoji="♻️", style=discord.ButtonStyle.danger, row=4)
    async def b_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        conf = get_conf(interaction.guild_id)
        conf["welcome"] = dict(DEFAULT_WELCOME)
        save()
        await refresh_welcome(interaction)


# ============================================================
#  BOT
# ============================================================
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True           # à activer dans le portail développeur
        intents.message_content = True   # à activer dans le portail développeur
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        for group in (ticket_group, invites_group, welcome_group, role_group):
            self.tree.add_command(group)
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
    for g in bot.guilds:
        await snapshot_invites(g)


@bot.event
async def on_guild_join(guild: discord.Guild):
    await snapshot_invites(guild)


@bot.event
async def on_invite_create(invite: discord.Invite):
    if invite.guild:
        INVITE_CACHE.setdefault(invite.guild.id, {})[invite.code] = (invite.uses or 0, invite.inviter.id if invite.inviter else None)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    if invite.guild:
        cached = INVITE_CACHE.get(invite.guild.id, {}).get(invite.code)
        INVITE_DELETED[invite.guild.id].append((invite.code, cached[1] if cached else None, time.time()))


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ Tu n'as pas la permission d'utiliser cette commande."
    else:
        print(error)
        msg = "❌ Une erreur est survenue."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.NotFound:
        pass  # interaction expirée, rien à faire


# ============================================================
#  COMMANDES : AIDE & INFOS
# ============================================================
@bot.tree.command(name="aide", description="Centre d'aide interactif du bot")
async def aide(interaction: discord.Interaction):
    await interaction.response.send_message(embed=help_embed("home"), view=HelpView(interaction.user.id), ephemeral=True)


@bot.tree.command(name="reglage", description="Ouvre le panneau de réglages du bot")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def reglage(interaction: discord.Interaction):
    if await deny_admin(interaction):
        return
    embed, view = build_panel(interaction.guild, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="ping", description="Affiche la latence du bot")
async def ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    icon = "🟢" if ms < 150 else "🟡" if ms < 300 else "🔴"
    embed = discord.Embed(title="🏓 Pong !", description=f"{icon} Latence : **{ms} ms**", color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="botinfo", description="Informations sur le bot")
async def botinfo(interaction: discord.Interaction):
    embed = discord.Embed(title=f"🤖 {bot.user.name}", color=0x5865F2,
                          description="Bot de modération, tickets, invitations et bienvenue.")
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="🏓 Latence", value=f"{round(bot.latency * 1000)} ms")
    embed.add_field(name="⏱️ En ligne depuis", value=human_duration(time.time() - BOT_START))
    embed.add_field(name="🌐 Serveurs", value=str(len(bot.guilds)))
    embed.add_field(name="👥 Utilisateurs", value=str(sum(g.member_count or 0 for g in bot.guilds)))
    embed.add_field(name="🐍 Python", value=platform.python_version())
    embed.add_field(name="📚 discord.py", value=discord.__version__)
    embed.add_field(name="📖 Commandes", value=f"{sum(len(v[3]) for v in HELP.values())} — voir **/aide**", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="Affiche l'avatar d'un membre")
@app_commands.describe(membre="Le membre")
async def avatar(interaction: discord.Interaction, membre: Optional[discord.User] = None):
    user = membre or interaction.user
    a = user.display_avatar
    links = " · ".join(f"[{f.upper()}]({a.with_format(f).url})" for f in ("png", "jpg", "webp"))
    embed = discord.Embed(title=f"🖼️ Avatar de {user.display_name}", description=f"Télécharger : {links}", color=0x5865F2)
    embed.set_image(url=a.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="banner", description="Affiche la bannière d'un membre")
@app_commands.describe(membre="Le membre")
async def banner(interaction: discord.Interaction, membre: Optional[discord.User] = None):
    user = membre or interaction.user
    try:
        full = await bot.fetch_user(user.id)
    except discord.HTTPException:
        full = None
    if not full or not full.banner:
        return await interaction.response.send_message(f"**{user.display_name}** n'a pas de bannière.", ephemeral=True)
    embed = discord.Embed(title=f"🎨 Bannière de {user.display_name}", color=full.accent_color or 0x5865F2)
    embed.set_image(url=full.banner.url)
    await interaction.response.send_message(embed=embed)


BADGES = {
    "staff": "👨‍💼 Staff Discord", "partner": "🤝 Partenaire", "hypesquad": "🎉 HypeSquad Events",
    "bug_hunter": "🐛 Bug Hunter", "hypesquad_bravery": "🛡️ Bravery", "hypesquad_brilliance": "🧠 Brilliance",
    "hypesquad_balance": "⚖️ Balance", "early_supporter": "💎 Soutien précoce",
    "verified_bot_developer": "🛠️ Dév. de bot vérifié", "active_developer": "🧑‍💻 Développeur actif",
    "discord_certified_moderator": "🛡️ Modérateur certifié",
}
KEY_PERMS = {
    "manage_guild": "Gérer le serveur", "manage_roles": "Gérer les rôles", "manage_channels": "Gérer les salons",
    "kick_members": "Expulser", "ban_members": "Bannir", "moderate_members": "Mettre en sourdine",
    "manage_messages": "Gérer les messages", "mention_everyone": "Mentionner @everyone",
}


@bot.tree.command(name="userinfo", description="Profil complet d'un membre")
@app_commands.guild_only()
@app_commands.describe(membre="Le membre")
async def userinfo(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    m = membre or interaction.user
    g = interaction.guild
    conf = get_conf(g.id)
    try:
        full = await bot.fetch_user(m.id)
    except discord.HTTPException:
        full = None

    embed = discord.Embed(title=m.display_name, description=f"{m.mention} · `{m.id}`",
                          color=m.color if m.color.value else discord.Color(0x5865F2))
    embed.set_thumbnail(url=m.display_avatar.url)
    if full and full.banner:
        embed.set_image(url=full.banner.url)

    ident = f"**Nom :** {m.name}"
    if m.global_name:
        ident += f"\n**Nom global :** {m.global_name}"
    ident += f"\n**Type :** {'🤖 Bot' if m.bot else '👤 Humain'}"
    embed.add_field(name="🏷️ Identité", value=ident)
    embed.add_field(name="📅 Compte créé", value=f"{discord.utils.format_dt(m.created_at, 'F')}\n({discord.utils.format_dt(m.created_at, 'R')})")
    if m.joined_at:
        ordered = sorted((x for x in g.members if x.joined_at), key=lambda x: x.joined_at)
        pos = ordered.index(m) + 1 if m in ordered else "?"
        embed.add_field(name="📥 Arrivé sur le serveur",
                        value=f"{discord.utils.format_dt(m.joined_at, 'F')}\n({discord.utils.format_dt(m.joined_at, 'R')})\n**Membre n°{pos}**")

    badges = [label for attr, label in BADGES.items() if getattr(m.public_flags, attr, False)]
    embed.add_field(name="🎖️ Badges", value="\n".join(badges) or "Aucun")
    embed.add_field(name="👑 Rôle principal", value=m.top_role.mention if m != g.default_role else "Aucun")
    extra = []
    if m.premium_since:
        extra.append(f"🚀 Booste depuis {discord.utils.format_dt(m.premium_since, 'R')}")
    if m.timed_out_until and m.timed_out_until > discord.utils.utcnow():
        extra.append(f"🔇 Sourdine jusqu'à {discord.utils.format_dt(m.timed_out_until, 'R')}")
    embed.add_field(name="✨ Statut", value="\n".join(extra) or "Normal")

    warns_count = len(conf["warns"].get(str(m.id), []))
    rec = conf["invites"]["members"].get(str(m.id))
    inv_txt = f"**{invite_total(conf, m.id)}** invitations\n"
    inv_txt += f"Invité par : <@{rec['inviter']}>" if rec and rec.get("inviter") else "Invité par : inconnu"
    embed.add_field(name="📨 Invitations", value=inv_txt)
    embed.add_field(name="⚠️ Avertissements", value=str(warns_count))

    perms = m.guild_permissions
    if perms.administrator:
        perm_txt = "👑 **Administrateur**"
    else:
        perm_txt = ", ".join(label for attr, label in KEY_PERMS.items() if getattr(perms, attr)) or "Aucune permission clé"
    embed.add_field(name="🔑 Permissions clés", value=perm_txt, inline=False)

    roles = [r.mention for r in reversed(m.roles) if r != g.default_role]
    roles_txt = " ".join(roles) or "Aucun"
    if len(roles_txt) > 1000:
        roles_txt = roles_txt[:1000].rsplit(" ", 1)[0] + " …"
    embed.add_field(name=f"🎭 Rôles ({len(roles)})", value=roles_txt, inline=False)
    await interaction.response.send_message(embed=embed)


VERIF = {"none": "Aucune", "low": "Faible (e-mail vérifié)", "medium": "Moyenne (5 min sur Discord)",
         "high": "Élevée (10 min sur le serveur)", "highest": "Maximale (téléphone vérifié)"}


@bot.tree.command(name="serverinfo", description="Fiche complète du serveur")
@app_commands.guild_only()
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    bots = sum(1 for m in g.members if m.bot)
    embed = discord.Embed(title=f"🏰 {g.name}", description=g.description or None, color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    if g.banner:
        embed.set_image(url=g.banner.url)
    embed.add_field(name="👑 Propriétaire", value=f"<@{g.owner_id}>")
    embed.add_field(name="🆔 ID", value=f"`{g.id}`")
    embed.add_field(name="📅 Créé", value=f"{discord.utils.format_dt(g.created_at, 'D')}\n({discord.utils.format_dt(g.created_at, 'R')})")
    embed.add_field(name="👥 Membres", value=f"**{g.member_count}** au total\n👤 {g.member_count - bots} humains\n🤖 {bots} bots")
    embed.add_field(name="💬 Salons",
                    value=(f"**{len(g.channels)}** au total\n💬 {len(g.text_channels)} textuels\n🔊 {len(g.voice_channels)} vocaux\n"
                           f"📁 {len(g.categories)} catégories\n📣 {len(g.forums)} forums · 🎤 {len(g.stage_channels)} scènes"))
    embed.add_field(name="🎭 Contenu", value=f"🎭 {len(g.roles)} rôles\n😀 {len(g.emojis)} émojis\n🏷️ {len(g.stickers)} stickers")
    embed.add_field(name="🚀 Boosts", value=f"Niveau **{g.premium_tier}**\n{g.premium_subscription_count} boosts")
    embed.add_field(name="🔒 Vérification", value=VERIF.get(g.verification_level.name, g.verification_level.name))
    embed.add_field(name="🌍 Langue", value=str(g.preferred_locale))
    if g.vanity_url_code:
        embed.add_field(name="🔗 Lien personnalisé", value=f"discord.gg/{g.vanity_url_code}")
    if g.features:
        feats = ", ".join(f.replace("_", " ").title() for f in list(g.features)[:8])
        embed.add_field(name="✨ Fonctionnalités", value=feats, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="servericon", description="Affiche l'icône et la bannière du serveur")
@app_commands.guild_only()
async def servericon(interaction: discord.Interaction):
    g = interaction.guild
    if not g.icon and not g.banner:
        return await interaction.response.send_message("Ce serveur n'a ni icône ni bannière.", ephemeral=True)
    embeds = []
    if g.icon:
        embeds.append(discord.Embed(title="🖼️ Icône", color=0x5865F2).set_image(url=g.icon.url))
    if g.banner:
        embeds.append(discord.Embed(title="🎨 Bannière", color=0x5865F2).set_image(url=g.banner.url))
    await interaction.response.send_message(embeds=embeds)


@bot.tree.command(name="membercount", description="Nombre de membres du serveur")
@app_commands.guild_only()
async def membercount(interaction: discord.Interaction):
    g = interaction.guild
    bots = sum(1 for m in g.members if m.bot)
    embed = discord.Embed(title=f"👥 {g.member_count} membres", color=0x5865F2)
    embed.add_field(name="👤 Humains", value=str(g.member_count - bots))
    embed.add_field(name="🤖 Bots", value=str(bots))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roleinfo", description="Détails d'un rôle")
@app_commands.guild_only()
@app_commands.describe(role="Le rôle")
async def roleinfo(interaction: discord.Interaction, role: discord.Role):
    perms = ", ".join(label for attr, label in KEY_PERMS.items() if getattr(role.permissions, attr))
    if role.permissions.administrator:
        perms = "👑 Administrateur"
    embed = discord.Embed(title=f"🎭 {role.name}", color=role.color if role.color.value else discord.Color(0x5865F2))
    embed.add_field(name="🆔 ID", value=f"`{role.id}`")
    embed.add_field(name="🎨 Couleur", value=str(role.color))
    embed.add_field(name="👥 Membres", value=str(len(role.members)))
    embed.add_field(name="📍 Position", value=f"{role.position} / {len(interaction.guild.roles) - 1}")
    embed.add_field(name="📅 Créé", value=discord.utils.format_dt(role.created_at, "R"))
    embed.add_field(name="⚙️ Options",
                    value=f"Mentionnable : {'✅' if role.mentionable else '❌'}\nAffiché à part : {'✅' if role.hoist else '❌'}\n"
                          f"Géré par une intégration : {'✅' if role.managed else '❌'}")
    embed.add_field(name="🔑 Permissions clés", value=perms or "Aucune", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="channelinfo", description="Détails d'un salon")
@app_commands.guild_only()
@app_commands.describe(salon="Le salon (par défaut : celui-ci)")
async def channelinfo(interaction: discord.Interaction, salon: Optional[discord.abc.GuildChannel] = None):
    ch = salon or interaction.channel
    embed = discord.Embed(title=f"📺 #{ch.name}", color=0x5865F2)
    embed.add_field(name="🆔 ID", value=f"`{ch.id}`")
    embed.add_field(name="📂 Type", value=ch.type.name.replace("_", " ").title())
    embed.add_field(name="📅 Créé", value=discord.utils.format_dt(ch.created_at, "R"))
    if getattr(ch, "category", None):
        embed.add_field(name="📁 Catégorie", value=ch.category.name)
    embed.add_field(name="📍 Position", value=str(ch.position))
    if hasattr(ch, "slowmode_delay"):
        embed.add_field(name="🐌 Mode lent", value=f"{ch.slowmode_delay} s" if ch.slowmode_delay else "Désactivé")
    if hasattr(ch, "nsfw"):
        embed.add_field(name="🔞 NSFW", value="Oui" if ch.nsfw else "Non")
    if hasattr(ch, "user_limit"):
        embed.add_field(name="👥 Limite vocale", value=str(ch.user_limit) if ch.user_limit else "Illimitée")
    if getattr(ch, "topic", None):
        embed.add_field(name="📝 Sujet", value=ch.topic[:1000], inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roles", description="Liste des rôles du serveur avec leur nombre de membres")
@app_commands.guild_only()
async def roles_list(interaction: discord.Interaction):
    roles = [r for r in reversed(interaction.guild.roles) if r != interaction.guild.default_role]
    lines = [f"{r.mention} — `{len(r.members)}`" for r in roles]
    text = "\n".join(lines) or "Aucun rôle."
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n…"
    embed = discord.Embed(title=f"🎭 Rôles ({len(roles)})", description=text, color=0x5865F2)
    await interaction.response.send_message(embed=embed)


# ============================================================
#  COMMANDES : UTILITAIRES
# ============================================================
class EmbedModal(discord.ui.Modal, title="Créer un embed"):
    f_title = discord.ui.TextInput(label="Titre", max_length=256, required=False)
    f_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=3000)
    f_color = discord.ui.TextInput(label="Couleur (hex)", default="#5865F2", max_length=7, required=False)
    f_image = discord.ui.TextInput(label="Image (URL)", required=False, max_length=400)
    f_footer = discord.ui.TextInput(label="Pied de page", required=False, max_length=200)

    def __init__(self, channel: discord.abc.Messageable):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title=self.f_title.value or None, description=self.f_desc.value,
                              color=parse_color(self.f_color.value))
        if valid_url(self.f_image.value.strip()):
            embed.set_image(url=self.f_image.value.strip())
        if self.f_footer.value:
            embed.set_footer(text=self.f_footer.value)
        try:
            await self.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ Impossible d'envoyer dans ce salon (permissions ou image invalide).", ephemeral=True)
        await interaction.response.send_message("✅ Embed envoyé.", ephemeral=True)


@bot.tree.command(name="embed", description="Crée un embed personnalisé")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(salon="Salon d'envoi (par défaut : celui-ci)")
async def embed_cmd(interaction: discord.Interaction, salon: Optional[discord.TextChannel] = None):
    await interaction.response.send_modal(EmbedModal(salon or interaction.channel))


NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


@bot.tree.command(name="sondage", description="Crée un sondage avec réactions")
@app_commands.guild_only()
async def sondage(interaction: discord.Interaction, question: str, option1: str, option2: str,
                  option3: Optional[str] = None, option4: Optional[str] = None, option5: Optional[str] = None):
    options = [o for o in (option1, option2, option3, option4, option5) if o]
    body = "\n\n".join(f"{NUM_EMOJI[i]} {o}" for i, o in enumerate(options))
    embed = discord.Embed(title=f"📊 {question}", description=body, color=0x5865F2)
    embed.set_footer(text=f"Sondage de {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for i in range(len(options)):
        try:
            await msg.add_reaction(NUM_EMOJI[i])
        except discord.HTTPException:
            break


# ============================================================
#  COMMANDES : MODÉRATION
# ============================================================
MOD_META = {
    "kick": ("👢", "Expulsion", 0xED4245), "ban": ("🔨", "Bannissement", 0xED4245),
    "timeout": ("🔇", "Sourdine", 0xFEE75C), "untimeout": ("🔊", "Fin de sourdine", 0x57F287),
    "warn": ("⚠️", "Avertissement", 0xFEE75C), "unban": ("♻️", "Débannissement", 0x57F287),
}


async def check_target(interaction: discord.Interaction, target: discord.Member) -> bool:
    if target.id == interaction.user.id:
        await interaction.response.send_message("Tu ne peux pas te cibler toi-même.", ephemeral=True)
        return False
    if target.id == interaction.guild.owner_id:
        await interaction.response.send_message("⛔ Tu ne peux pas cibler le propriétaire du serveur.", ephemeral=True)
        return False
    if interaction.guild.owner_id != interaction.user.id and target.top_role >= interaction.user.top_role:
        await interaction.response.send_message("⛔ Ce membre a un rôle supérieur ou égal au tien.", ephemeral=True)
        return False
    return True


async def mod_action(interaction: discord.Interaction, key: str, target, reason: str, action, extra: Optional[str] = None):
    emoji, label, color = MOD_META[key]
    conf = get_conf(interaction.guild_id)

    embed = discord.Embed(title=f"{emoji} {label}", color=color, timestamp=discord.utils.utcnow())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Membre", value=f"{target.mention}\n`{target.id}`")
    embed.add_field(name="Modérateur", value=interaction.user.mention)
    embed.add_field(name="Raison", value=reason[:1000], inline=False)
    if extra:
        embed.add_field(name="Détails", value=extra, inline=False)
    embed.set_footer(text=interaction.guild.name)

    dm = discord.Embed(title=f"{emoji} {label} — {interaction.guild.name}", color=color,
                       description=f"**Raison :** {reason}" + (f"\n{extra}" if extra else ""))
    if key in ("kick", "ban"):
        await try_dm(target, dm)          # avant, sinon plus de serveur en commun
    try:
        await action()
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Je n'ai pas les permissions (vérifie la hiérarchie de mes rôles).", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Erreur Discord : {e}", ephemeral=True)
    if key not in ("kick", "ban"):
        await try_dm(target, dm)

    await log_embed(interaction.guild, conf, embed)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="kick", description="Expulse un membre")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison"):
    if await check_target(interaction, membre):
        await mod_action(interaction, "kick", membre, raison, lambda: membre.kick(reason=raison))


@bot.tree.command(name="ban", description="Bannit un membre")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(supprimer_messages="Jours de messages à supprimer (0 à 7)")
async def ban(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison",
              supprimer_messages: app_commands.Range[int, 0, 7] = 0):
    if await check_target(interaction, membre):
        await mod_action(interaction, "ban", membre, raison,
                         lambda: membre.ban(reason=raison, delete_message_seconds=supprimer_messages * 86400),
                         extra=f"🗑️ Messages supprimés : {supprimer_messages} j" if supprimer_messages else None)


@bot.tree.command(name="unban", description="Débannit un utilisateur (par son ID)")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(utilisateur_id="L'ID de l'utilisateur banni")
async def unban(interaction: discord.Interaction, utilisateur_id: str, raison: str = "Aucune raison"):
    try:
        uid = int(utilisateur_id.strip())
    except ValueError:
        return await interaction.response.send_message("❌ Donne un ID valide (uniquement des chiffres).", ephemeral=True)
    try:
        entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
    except discord.NotFound:
        return await interaction.response.send_message("❌ Cet utilisateur n'est pas banni.", ephemeral=True)
    user = entry.user
    await mod_action(interaction, "unban", user, raison, lambda: interaction.guild.unban(user, reason=raison))


@bot.tree.command(name="timeout", description="Met un membre en sourdine")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(minutes="Durée en minutes (max 40320)")
async def timeout(interaction: discord.Interaction, membre: discord.Member,
                  minutes: app_commands.Range[int, 1, 40320], raison: str = "Aucune raison"):
    if await check_target(interaction, membre):
        await mod_action(interaction, "timeout", membre, raison,
                         lambda: membre.timeout(timedelta(minutes=minutes), reason=raison),
                         extra=f"⏱️ Durée : **{human_duration(minutes * 60)}**")


@bot.tree.command(name="untimeout", description="Retire la sourdine d'un membre")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, membre: discord.Member):
    await mod_action(interaction, "untimeout", membre, "—", lambda: membre.timeout(None))


@bot.tree.command(name="warn", description="Avertit un membre (3 avertissements = sourdine 1 h)")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison"):
    if not await check_target(interaction, membre):
        return
    conf = get_conf(interaction.guild_id)
    lst = conf["warns"].setdefault(str(membre.id), [])
    lst.append({"reason": raison, "mod": interaction.user.id, "ts": int(time.time())})
    save()
    n = len(lst)
    extra = f"📊 Avertissement n°**{n}** (3 = sourdine auto de 1 h)"

    async def act():
        if n >= 3:
            try:
                await membre.timeout(timedelta(hours=1), reason="3 avertissements")
            except discord.Forbidden:
                pass

    if n >= 3:
        extra += "\n🔇 Sourdine automatique de 1 h appliquée."
    await mod_action(interaction, "warn", membre, raison, act, extra=extra)


@bot.tree.command(name="warns", description="Affiche les avertissements d'un membre")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
async def warns(interaction: discord.Interaction, membre: discord.Member):
    lst = get_conf(interaction.guild_id)["warns"].get(str(membre.id), [])
    embed = discord.Embed(title=f"⚠️ Avertissements de {membre.display_name}", color=0xFEE75C)
    embed.set_thumbnail(url=membre.display_avatar.url)
    if not lst:
        embed.description = "✅ Aucun avertissement."
    else:
        embed.description = "\n\n".join(
            f"**#{i}** • <t:{w['ts']}:R> • par <@{w['mod']}>\n└ {w['reason'][:200]}"
            for i, w in enumerate(lst[-10:], start=max(1, len(lst) - 9))
        )
        embed.set_footer(text=f"Total : {len(lst)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarns", description="Supprime un ou tous les avertissements d'un membre")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(numero="Numéro de l'avertissement (vide = tous)")
async def clearwarns(interaction: discord.Interaction, membre: discord.Member, numero: Optional[int] = None):
    warns_db = get_conf(interaction.guild_id)["warns"]
    lst = warns_db.get(str(membre.id), [])
    if not lst:
        return await interaction.response.send_message("Ce membre n'a aucun avertissement.", ephemeral=True)
    if numero is None:
        warns_db.pop(str(membre.id), None)
        txt = f"✅ Tous les avertissements de {membre.mention} ont été supprimés."
    elif 1 <= numero <= len(lst):
        lst.pop(numero - 1)
        txt = f"✅ Avertissement **#{numero}** de {membre.mention} supprimé."
    else:
        return await interaction.response.send_message(f"❌ Numéro invalide (1 à {len(lst)}).", ephemeral=True)
    save()
    await interaction.response.send_message(embed=discord.Embed(description=txt, color=0x57F287))


@bot.tree.command(name="clear", description="Supprime des messages")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(nombre="Nombre de messages (1 à 100)", membre="Ne supprimer que les messages de ce membre")
async def clear(interaction: discord.Interaction, nombre: app_commands.Range[int, 1, 100], membre: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    count = 0

    def check(m: discord.Message) -> bool:
        nonlocal count
        if membre and m.author.id != membre.id:
            return False
        if count >= nombre:
            return False
        count += 1
        return True

    deleted = await interaction.channel.purge(limit=500 if membre else nombre, check=check)
    await interaction.followup.send(embed=discord.Embed(
        description=f"🧹 **{len(deleted)}** message(s) supprimé(s)" + (f" de {membre.mention}." if membre else "."),
        color=0x57F287), ephemeral=True)


@bot.tree.command(name="lock", description="Verrouille un salon (personne ne peut écrire)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, salon: Optional[discord.TextChannel] = None, raison: str = "Aucune raison"):
    ch = salon or interaction.channel
    ow = ch.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    try:
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow, reason=raison)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Je n'ai pas la permission de modifier ce salon.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(
        title="🔒 Salon verrouillé", description=f"{ch.mention} est verrouillé.\n**Raison :** {raison}", color=0xED4245))


@bot.tree.command(name="unlock", description="Déverrouille un salon")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, salon: Optional[discord.TextChannel] = None):
    ch = salon or interaction.channel
    ow = ch.overwrites_for(interaction.guild.default_role)
    ow.send_messages = None
    try:
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Je n'ai pas la permission de modifier ce salon.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(
        title="🔓 Salon déverrouillé", description=f"{ch.mention} est de nouveau ouvert.", color=0x57F287))


@bot.tree.command(name="slowmode", description="Règle le mode lent d'un salon (0 = désactivé)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, secondes: app_commands.Range[int, 0, 21600],
                   salon: Optional[discord.TextChannel] = None):
    ch = salon or interaction.channel
    try:
        await ch.edit(slowmode_delay=secondes)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Je n'ai pas la permission de modifier ce salon.", ephemeral=True)
    txt = f"🐌 Mode lent de {ch.mention} : **{secondes} s**" if secondes else f"✅ Mode lent désactivé dans {ch.mention}"
    await interaction.response.send_message(embed=discord.Embed(description=txt, color=0x5865F2))


@bot.tree.command(name="nick", description="Change le pseudo d'un membre (vide = réinitialiser)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_nicknames=True)
@app_commands.checks.has_permissions(manage_nicknames=True)
async def nick(interaction: discord.Interaction, membre: discord.Member, pseudo: Optional[app_commands.Range[str, 1, 32]] = None):
    if membre.id != interaction.user.id and not await check_target(interaction, membre):
        return
    try:
        await membre.edit(nick=pseudo)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Je ne peux pas modifier ce pseudo (hiérarchie des rôles).", ephemeral=True)
    txt = f"✏️ Pseudo de {membre.mention} changé en **{pseudo}**" if pseudo else f"✏️ Pseudo de {membre.mention} réinitialisé."
    await interaction.response.send_message(embed=discord.Embed(description=txt, color=0x5865F2))


# ---- Rôles ----
role_group = app_commands.Group(name="role", description="Gérer les rôles des membres", guild_only=True,
                                default_permissions=discord.Permissions(manage_roles=True))


def role_error(interaction: discord.Interaction, role: discord.Role) -> Optional[str]:
    g = interaction.guild
    if role.is_default() or role.managed:
        return "Ce rôle ne peut pas être attribué manuellement."
    if role >= g.me.top_role:
        return "Ce rôle est supérieur ou égal au mien (monte mon rôle dans la liste)."
    if g.owner_id != interaction.user.id and role >= interaction.user.top_role:
        return "Ce rôle est supérieur ou égal au tien."
    return None


@role_group.command(name="ajouter", description="Donne un rôle à un membre")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_add(interaction: discord.Interaction, membre: discord.Member, role: discord.Role):
    err = role_error(interaction, role)
    if err:
        return await interaction.response.send_message(f"⛔ {err}", ephemeral=True)
    try:
        await membre.add_roles(role, reason=f"Par {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"✅ {role.mention} ajouté à {membre.mention}.", color=0x57F287))


@role_group.command(name="retirer", description="Retire un rôle à un membre")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_remove(interaction: discord.Interaction, membre: discord.Member, role: discord.Role):
    err = role_error(interaction, role)
    if err:
        return await interaction.response.send_message(f"⛔ {err}", ephemeral=True)
    try:
        await membre.remove_roles(role, reason=f"Par {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"✅ {role.mention} retiré à {membre.mention}.", color=0x57F287))


# ============================================================
#  COMMANDES : TICKETS
# ============================================================
ticket_group = app_commands.Group(name="ticket", description="Gestion des tickets", guild_only=True)


@ticket_group.command(name="style", description="Personnalise l'apparence des tickets (admin)")
async def ticket_style(interaction: discord.Interaction):
    if await deny_admin(interaction):
        return
    await interaction.response.send_message(content=STYLE_HELP, embeds=style_preview(interaction),
                                            view=StyleView(), ephemeral=True)


@ticket_group.command(name="panel", description="Envoie le panneau de création de tickets (admin)")
async def ticket_panel(interaction: discord.Interaction):
    if await deny_admin(interaction):
        return
    await post_panel(interaction.channel, interaction.guild)
    await interaction.response.send_message("✅ Panneau envoyé.", ephemeral=True)


@ticket_group.command(name="ajouter", description="Ajoute un membre au ticket")
async def ticket_add(interaction: discord.Interaction, membre: discord.Member):
    conf = get_conf(interaction.guild_id)
    if not (interaction.channel.topic or "").startswith("ticket:"):
        return await interaction.response.send_message("Ce salon n'est pas un ticket.", ephemeral=True)
    if not is_staff(interaction.user, conf):
        return await interaction.response.send_message("⛔ Réservé au staff.", ephemeral=True)
    await interaction.channel.set_permissions(membre, view_channel=True, send_messages=True, read_message_history=True)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"✅ {membre.mention} a été ajouté au ticket.", color=0x57F287))


@ticket_group.command(name="fermer", description="Ferme le ticket actuel")
async def ticket_close(interaction: discord.Interaction):
    await close_ticket(interaction)


# ============================================================
#  COMMANDES : INVITATIONS
# ============================================================
invites_group = app_commands.Group(name="invites", description="Système d'invitations", guild_only=True)


@invites_group.command(name="stats", description="Statistiques d'invitations d'un membre")
async def inv_stats(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    m = membre or interaction.user
    conf = get_conf(interaction.guild_id)
    u = conf["invites"]["users"].get(str(m.id), {})
    total = inv_total(u)
    pos = next((i for i, (uid, _, _) in enumerate(ranked(conf), start=1) if uid == m.id), None)

    embed = discord.Embed(title=f"📨 Invitations de {m.display_name}", color=0x5865F2,
                          description=f"# {total} invitation{'s' if abs(total) > 1 else ''}")
    embed.set_thumbnail(url=m.display_avatar.url)
    embed.add_field(name="✅ Valides", value=str(u.get("regular", 0)))
    embed.add_field(name="🚪 Parties (−1)", value=str(u.get("left", 0)))
    embed.add_field(name="⚠️ Suspectes", value=str(u.get("fake", 0)))
    embed.add_field(name="🎁 Bonus", value=str(u.get("bonus", 0)))
    embed.add_field(name="🏆 Classement", value=f"#{pos}" if pos else "Non classé")
    rec = conf["invites"]["members"].get(str(m.id))
    embed.add_field(name="📥 Invité par", value=f"<@{rec['inviter']}>" if rec and rec.get("inviter") else "Inconnu")

    mine = [(int(mid), r) for mid, r in conf["invites"]["members"].items() if r.get("inviter") == m.id]
    mine.sort(key=lambda x: -x[1].get("ts", 0))
    if mine:
        lines = [f"{status_icon(r)} <@{mid}> • <t:{r.get('ts', 0)}:R>" for mid, r in mine[:8]]
        embed.add_field(name=f"👥 Derniers invités ({len(mine)} au total)", value="\n".join(lines), inline=False)
    embed.set_footer(text="✅ actif · 🚪 parti · ⚠️ suspect (non compté) · 🔁 retour")
    await interaction.response.send_message(embed=embed)


@invites_group.command(name="top", description="Classement des invitations")
async def inv_top(interaction: discord.Interaction):
    embed, _, page = build_leaderboard(interaction.guild, get_conf(interaction.guild_id), 0, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=LeaderboardView(interaction.guild, interaction.user.id, page))


@invites_group.command(name="qui", description="Découvre qui a invité un membre")
async def inv_who(interaction: discord.Interaction, membre: discord.Member):
    conf = get_conf(interaction.guild_id)
    rec = conf["invites"]["members"].get(str(membre.id))
    embed = discord.Embed(title=f"🔎 Qui a invité {membre.display_name} ?", color=0x5865F2)
    embed.set_thumbnail(url=membre.display_avatar.url)
    if not rec:
        embed.description = "Aucune donnée (le membre est arrivé avant l'activation du suivi, ou le bot n'a pas la permission **Gérer le serveur**)."
    else:
        embed.add_field(name="📨 Inviteur", value=f"<@{rec['inviter']}>" if rec.get("inviter") else "Inconnu")
        embed.add_field(name="🔗 Code", value=f"`{rec['code']}`" if rec.get("code") else "—")
        embed.add_field(name="📊 Statut", value=STATUS_TEXT.get(rec["status"], rec["status"]))
        embed.add_field(name="📅 Arrivée", value=f"<t:{rec.get('ts', 0)}:F>")
        embed.add_field(name="🚪 Parti ?", value="Oui" if rec.get("left") else "Non")
        if rec.get("reasons"):
            embed.add_field(name="⚠️ Raisons du signalement", value="\n".join(f"• {r}" for r in rec["reasons"]), inline=False)
    await interaction.response.send_message(embed=embed)


@invites_group.command(name="style", description="Personnalise le message du classement (admin)")
async def inv_style(interaction: discord.Interaction):
    if await deny_admin(interaction):
        return
    embed, _, _ = build_leaderboard(interaction.guild, get_conf(interaction.guild_id), 0, interaction.user.id)
    await interaction.response.send_message(content=INV_STYLE_HELP, embed=embed, view=InvStyleView(), ephemeral=True)


@invites_group.command(name="config", description="Règle l'anti-fake des invitations (admin)")
@app_commands.describe(actif="Activer / désactiver le suivi", age_min="Âge minimum du compte en jours (0 = ignoré)",
                       pseudos_similaires="Détecter les pseudos ressemblant à l'inviteur",
                       similarite="Seuil de ressemblance des pseudos en % (50 à 100)")
async def inv_config(interaction: discord.Interaction, actif: Optional[bool] = None,
                     age_min: Optional[app_commands.Range[int, 0, 365]] = None,
                     pseudos_similaires: Optional[bool] = None,
                     similarite: Optional[app_commands.Range[int, 50, 100]] = None):
    if await deny_admin(interaction):
        return
    cfg = get_conf(interaction.guild_id)["invites"]["config"]
    if actif is not None:
        cfg["enabled"] = actif
    if age_min is not None:
        cfg["min_age_days"] = age_min
    if pseudos_similaires is not None:
        cfg["similar_check"] = pseudos_similaires
    if similarite is not None:
        cfg["similarity"] = similarite
    save()
    perm_ok = interaction.guild.me.guild_permissions.manage_guild
    embed = discord.Embed(title="🛡️ Anti-fake des invitations", color=0x5865F2,
                          description="Une invitation **suspecte** n'est jamais comptée dans le classement.")
    embed.add_field(name="Suivi", value="🟢 Activé" if cfg["enabled"] else "🔴 Désactivé")
    embed.add_field(name="Âge minimum du compte", value=f"{cfg['min_age_days']} jours" if cfg["min_age_days"] else "Ignoré")
    embed.add_field(name="Pseudos similaires",
                    value=f"🟢 Seuil {cfg['similarity']} %" if cfg["similar_check"] else "🔴 Désactivé")
    embed.add_field(name="Permission du bot « Gérer le serveur »",
                    value="✅ OK" if perm_ok else "❌ Manquante — le suivi ne fonctionnera pas", inline=False)
    embed.add_field(name="Règles appliquées",
                    value=("• Compte trop récent → suspect\n• Pseudo / avatar ressemblant à l'inviteur → suspect\n"
                           "• Auto-invitation → suspect\n• Membre qui part → **−1**\n"
                           "• Membre qui revient → pas recompté (l'invitation est rendue s'il revient par le même inviteur)"),
                    inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@invites_group.command(name="ajouter", description="Ajoute des invitations bonus (admin)")
async def inv_add(interaction: discord.Interaction, membre: discord.Member, nombre: app_commands.Range[int, 1, 100000]):
    if await deny_admin(interaction):
        return
    conf = get_conf(interaction.guild_id)
    inv_user(conf, membre.id)["bonus"] += nombre
    save()
    await interaction.response.send_message(embed=discord.Embed(
        description=f"🎁 **+{nombre}** invitations bonus pour {membre.mention} (total : **{invite_total(conf, membre.id)}**).",
        color=0x57F287))


@invites_group.command(name="retirer", description="Retire des invitations bonus (admin)")
async def inv_remove(interaction: discord.Interaction, membre: discord.Member, nombre: app_commands.Range[int, 1, 100000]):
    if await deny_admin(interaction):
        return
    conf = get_conf(interaction.guild_id)
    inv_user(conf, membre.id)["bonus"] -= nombre
    save()
    await interaction.response.send_message(embed=discord.Embed(
        description=f"➖ **−{nombre}** invitations pour {membre.mention} (total : **{invite_total(conf, membre.id)}**).",
        color=0xED4245))


@invites_group.command(name="reset", description="Réinitialise les invitations d'un membre ou de tout le serveur (admin)")
@app_commands.describe(membre="Vide = tout le serveur")
async def inv_reset(interaction: discord.Interaction, membre: Optional[discord.Member] = None):
    if await deny_admin(interaction):
        return
    guild_id = interaction.guild_id

    async def do_reset(i: discord.Interaction):
        conf = get_conf(guild_id)
        if membre:
            conf["invites"]["users"].pop(str(membre.id), None)
            conf["invites"]["members"] = {k: v for k, v in conf["invites"]["members"].items() if v.get("inviter") != membre.id}
            txt = f"✅ Invitations de {membre.mention} réinitialisées."
        else:
            conf["invites"]["users"] = {}
            conf["invites"]["members"] = {}
            txt = "✅ Toutes les invitations du serveur ont été réinitialisées."
        save()
        await i.response.edit_message(content=txt, view=None)

    target = membre.mention if membre else "**tout le serveur**"
    await interaction.response.send_message(f"⚠️ Réinitialiser les invitations de {target} ? Action irréversible.",
                                            view=ConfirmView(interaction.user.id, do_reset), ephemeral=True)


# ============================================================
#  COMMANDES : BIENVENUE
# ============================================================
welcome_group = app_commands.Group(name="bienvenue", description="Système de bienvenue et d'au revoir", guild_only=True)


@welcome_group.command(name="config", description="Panneau de configuration de la bienvenue (admin)")
async def welcome_config(interaction: discord.Interaction):
    if await deny_admin(interaction):
        return
    embeds, view = welcome_panel(interaction)
    await interaction.response.send_message(embeds=embeds, view=view, ephemeral=True)


@welcome_group.command(name="tester", description="Envoie un message de bienvenue test ici (admin)")
async def welcome_test(interaction: discord.Interaction):
    if await deny_admin(interaction):
        return
    w = get_conf(interaction.guild_id)["welcome"]
    info = {"inviter": interaction.user.id, "status": "regular"}
    await interaction.channel.send(
        content=fmt(w["content"], member_vars(interaction.user, info))[:2000] or None,
        embed=welcome_embed(interaction.guild, interaction.user, w, info, "welcome"),
    )
    await interaction.response.send_message("✅ Message de test envoyé.", ephemeral=True)


# ============================================================
#  ÉVÉNEMENTS : MESSAGES (protections)
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
#  ÉVÉNEMENTS : ARRIVÉES / DÉPARTS (anti-raid, invitations, bienvenue)
# ============================================================
async def check_raid(member: discord.Member, conf: dict) -> bool:
    """True si le membre a été expulsé par l'anti-raid."""
    lvl = conf["modules"]["antiraid"]["level"]
    if lvl == "off":
        return False
    joins, seconds = LEVELS[lvl]["raid"]
    now = time.time()
    arr = [t for t in JOIN_LOG[member.guild.id] if now - t < seconds]
    arr.append(now)
    JOIN_LOG[member.guild.id] = arr
    if len(arr) >= joins:
        try:
            await member.kick(reason="Anti-raid : arrivées massives")
        except (discord.Forbidden, discord.HTTPException):
            return False
        await send_log(member.guild, conf, "🚨 Anti-raid", f"{member} expulsé ({len(arr)} arrivées en {seconds}s).", 0xED4245)
        return True
    return False


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    conf = get_conf(member.guild.id)
    if await check_raid(member, conf):
        return

    info = await track_join(member, conf)

    w = conf["welcome"]
    if w["autorole"]:
        role = member.guild.get_role(w["autorole"])
        if role:
            try:
                await member.add_roles(role, reason="Rôle automatique de bienvenue")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Log détaillé de l'arrivée
    age = (discord.utils.utcnow() - member.created_at).days
    embed = discord.Embed(title="📥 Nouveau membre", color=0x57F287, timestamp=discord.utils.utcnow(),
                          description=f"{member.mention} (`{member.id}`)")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📅 Compte créé", value=f"{discord.utils.format_dt(member.created_at, 'R')} ({age} j)")
    embed.add_field(name="📨 Inviteur", value=f"<@{info['inviter']}>" if info.get("inviter") else "Inconnu")
    embed.add_field(name="📊 Invitation", value=STATUS_TEXT.get(info["status"], info["status"]))
    if info.get("reasons"):
        embed.color = 0xFEE75C
        embed.add_field(name="⚠️ Invitation suspecte", value="\n".join(f"• {r}" for r in info["reasons"]), inline=False)
    await log_embed(member.guild, conf, embed)

    await send_welcome(member, conf, info)


@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot:
        return
    conf = get_conf(member.guild.id)
    rec = track_leave(member, conf)

    w = conf["welcome"]
    if w["leave_enabled"] and w["leave_channel"]:
        ch = member.guild.get_channel(w["leave_channel"])
        if ch:
            try:
                await ch.send(embed=welcome_embed(member.guild, member, w, None, "leave"))
            except discord.HTTPException:
                pass

    embed = discord.Embed(title="📤 Départ", color=0xED4245, timestamp=discord.utils.utcnow(),
                          description=f"**{member}** (`{member.id}`) a quitté le serveur.")
    embed.set_thumbnail(url=member.display_avatar.url)
    if rec and rec.get("inviter") and rec.get("status") == "regular":
        embed.add_field(name="📨 Invitation retirée", value=f"−1 pour <@{rec['inviter']}> (total : {invite_total(conf, rec['inviter'])})")
    await log_embed(member.guild, conf, embed)


# ============================================================
#  DÉMARRAGE
# ============================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ Token manquant : définis DISCORD_TOKEN (variable d'environnement ou .env)")
    Thread(target=run_web, daemon=True).start()
    bot.run(DISCORD_TOKEN)
