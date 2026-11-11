import asyncio
import io
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eclipse-rp")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True

bot = commands.Bot(command_prefix="!", intents=intents)
invite_cache = {}

def make_embed(title, description=None, color=config.VIOLET):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="ECLIPSE RP • Système officiel")
    return embed

def is_staff(member: discord.Member):
    cfg = db.get_config(member.guild.id)
    role_id = cfg.get("staff_role_id")
    return member.guild_permissions.administrator or (
        role_id is not None and any(role.id == role_id for role in member.roles)
    )

async def send_log(guild, title, description, color=config.ORANGE):
    cfg = db.get_config(guild.id)
    channel_id = cfg.get("logs_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        try:
            await channel.send(embed=make_embed(title, description, color))
        except discord.HTTPException:
            pass

async def refresh_invites(guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses for invite in invites}
    except (discord.Forbidden, discord.HTTPException):
        invite_cache[guild.id] = {}

async def get_used_invite(member):
    guild = member.guild
    old = invite_cache.get(guild.id, {})
    try:
        current = await guild.invites()
        used = None
        for invite in current:
            if invite.uses > old.get(invite.code, 0):
                used = invite
                break
        invite_cache[guild.id] = {invite.code: invite.uses for invite in current}
        return used
    except (discord.Forbidden, discord.HTTPException):
        return None

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="SESSION ECLIPSE RP", style=discord.ButtonStyle.primary, custom_id="eclipse:ticket:session", emoji="🌆")
    async def session(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "SESSION ECLIPSE RP")

    @discord.ui.button(label="ASSISTANCE", style=discord.ButtonStyle.secondary, custom_id="eclipse:ticket:assistance", emoji="🎫")
    async def assistance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "ASSISTANCE")

class TicketActions(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.success, custom_id="eclipse:ticket:claim", emoji="🙋")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Réservé au staff.", ephemeral=True)
        ok, message = db.claim_ticket(interaction.channel.id, interaction.user.id)
        if not ok:
            return await interaction.response.send_message(message, ephemeral=True)
        await interaction.response.send_message(f"🙋 Ticket pris en charge par {interaction.user.mention}.")
        await send_log(interaction.guild, "Ticket pris en charge", f"{interaction.channel.mention} • {interaction.user.mention}")

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, custom_id="eclipse:ticket:close", emoji="🔒")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("Ticket introuvable.", ephemeral=True)
        if interaction.user.id != ticket["opener_id"] and not is_staff(interaction.user):
            return await interaction.response.send_message("Tu ne peux pas fermer ce ticket.", ephemeral=True)
        await interaction.response.send_message("🔒 Fermeture du ticket dans quelques secondes.")
        db.close_ticket(interaction.channel.id)
        await send_log(interaction.guild, "Ticket fermé", f"Salon : {interaction.channel.name}\nPar : {interaction.user.mention}")
        await asyncio.sleep(4)
        try:
            await interaction.channel.delete(reason=f"Ticket fermé par {interaction.user}")
        except discord.Forbidden:
            pass

async def create_ticket(interaction: discord.Interaction, category: str):
    guild = interaction.guild
    cfg = db.get_config(guild.id)
    category_id = cfg.get("tickets_category_id")
    ticket_category = guild.get_channel(category_id) if category_id else None
    staff_role = guild.get_role(cfg.get("staff_role_id")) if cfg.get("staff_role_id") else None

    # Évite qu'un membre ouvre plusieurs tickets simultanément.
    existing = [
        ch for ch in guild.text_channels
        if (ticket := db.get_ticket(ch.id)) and ticket["opener_id"] == interaction.user.id and ticket["closed_at"] is None
    ]
    if existing:
        return await interaction.response.send_message(
            f"Tu as déjà un ticket ouvert : {existing[0].mention}", ephemeral=True
        )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    safe_name = f"{'session' if category.startswith('SESSION') else 'assistance'}-{interaction.user.name}".lower()
    try:
        channel = await guild.create_text_channel(
            name=safe_name[:90], category=ticket_category, overwrites=overwrites,
            topic=f"Ticket {category} • Ouvert par {interaction.user} ({interaction.user.id})"
        )
    except discord.Forbidden:
        return await interaction.response.send_message(
            "Je n'ai pas les permissions nécessaires pour créer un salon. Vérifie mes permissions.", ephemeral=True
        )
    db.add_ticket(channel.id, guild.id, interaction.user.id, category)
    embed = make_embed(f"🎫 {category}", f"Bonjour {interaction.user.mention} !\nExplique ta demande ici.\n\nUn membre du staff pourra prendre le ticket en charge.")
    await channel.send(content=interaction.user.mention, embed=embed, view=TicketActions())
    await interaction.response.send_message(f"Ton ticket est prêt : {channel.mention}", ephemeral=True)
    await send_log(guild, "Nouveau ticket", f"{category} • {interaction.user.mention} • {channel.mention}")

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Je vérifie mon compte", style=discord.ButtonStyle.success, custom_id="eclipse:verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = db.get_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("verified_role_id")) if cfg.get("verified_role_id") else None
        if not role:
            return await interaction.response.send_message("Le rôle vérifié n'est pas configuré. Contacte le staff.", ephemeral=True)
        try:
            await interaction.user.add_roles(role, reason="Vérification via le bouton ECLIPSE RP")
            unverified = interaction.guild.get_role(cfg.get("unverified_role_id")) if cfg.get("unverified_role_id") else None
            if unverified and unverified in interaction.user.roles:
                await interaction.user.remove_roles(unverified, reason="Membre vérifié")
            await interaction.response.send_message("✅ Ton compte est vérifié. Bienvenue sur ECLIPSE RP !", ephemeral=True)
            await send_log(interaction.guild, "Membre vérifié", f"{interaction.user.mention}")
        except discord.Forbidden:
            await interaction.response.send_message("Je ne peux pas modifier tes rôles. Vérifie la hiérarchie des rôles du bot.", ephemeral=True)

class ApplicationModal(discord.ui.Modal, title="Candidature ECLIPSE RP"):
    age = discord.ui.TextInput(label="Âge", placeholder="Ton âge", max_length=3)
    experience = discord.ui.TextInput(label="Expérience RP", placeholder="Présente ton expérience", style=discord.TextStyle.paragraph, max_length=700)
    motivation = discord.ui.TextInput(label="Motivation", placeholder="Pourquoi rejoindre l'équipe ?", style=discord.TextStyle.paragraph, max_length=700)

    async def on_submit(self, interaction: discord.Interaction):
        answers = f"Âge : {self.age.value}\n\nExpérience : {self.experience.value}\n\nMotivation : {self.motivation.value}"
        app_id = db.create_application(interaction.guild.id, interaction.user.id, answers)
        cfg = db.get_config(interaction.guild.id)
        channel = interaction.guild.get_channel(cfg.get("applications_channel_id")) if cfg.get("applications_channel_id") else None
        embed = make_embed(f"Nouvelle candidature #{app_id}", f"**Candidat :** {interaction.user.mention}\n\n{answers}", config.ORANGE)
        if channel:
            await channel.send(embed=embed, view=ApplicationReviewView())
        await interaction.response.send_message("✅ Ta candidature a été envoyée au staff.", ephemeral=True)
        await send_log(interaction.guild, "Candidature reçue", f"Candidature #{app_id} • {interaction.user.mention}")

class ApplicationReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success, custom_id="eclipse:application:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Réservé au staff.", ephemeral=True)
        await interaction.response.send_message("✅ Candidature marquée comme acceptée.")
        await interaction.message.edit(view=None)
        await send_log(interaction.guild, "Candidature acceptée", f"Décision par {interaction.user.mention}\nMessage : {interaction.message.jump_url}")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, custom_id="eclipse:application:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Réservé au staff.", ephemeral=True)
        await interaction.response.send_message("❌ Candidature marquée comme refusée.")
        await interaction.message.edit(view=None)
        await send_log(interaction.guild, "Candidature refusée", f"Décision par {interaction.user.mention}\nMessage : {interaction.message.jump_url}")

class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Déposer une candidature", style=discord.ButtonStyle.primary, custom_id="eclipse:application", emoji="📝")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplicationModal())

async def setup_hook():
    db.init_db()
    bot.add_view(TicketPanel())
    bot.add_view(TicketActions())
    bot.add_view(VerifyView())
    bot.add_view(ApplicationView())
    bot.add_view(ApplicationReviewView())
    try:
        synced = await bot.tree.sync()
        log.info("%s commandes slash synchronisées", len(synced))
    except Exception:
        log.exception("Erreur de synchronisation des commandes")

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    log.info("Connecté en tant que %s (%s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        await refresh_invites(guild)

@bot.event
async def on_member_join(member):
    db.record_join(member.guild.id)
    used = await get_used_invite(member)
    inviter = used.inviter if used else None
    db.save_invite(member.guild.id, member.id, inviter.id if inviter else None, used.code if used else None)
    cfg = db.get_config(member.guild.id)
    channel = member.guild.get_channel(cfg.get("welcome_channel_id")) if cfg.get("welcome_channel_id") else None
    if channel:
        await channel.send(embed=make_embed("🌆 Bienvenue sur ECLIPSE RP", f"Bienvenue {member.mention} ! Nous sommes maintenant **{member.guild.member_count}** membres.", config.ORANGE))
    await send_log(member.guild, "Membre arrivé", f"{member.mention}\nInvité par : {inviter.mention if inviter else 'Inconnu'}")

@bot.event
async def on_member_remove(member):
    db.record_leave(member.guild.id)
    await send_log(member.guild, "Membre parti", f"{member} (`{member.id}`)")

@bot.event
async def on_message_delete(message):
    if message.guild and not message.author.bot:
        content = message.content[:1200] if message.content else "*Aucun texte (peut-être une pièce jointe).*"
        await send_log(message.guild, "Message supprimé", f"**Auteur :** {message.author.mention}\n**Salon :** {message.channel.mention}\n**Contenu :** {content}")

@bot.event
async def on_message_edit(before, after):
    if before.guild and not before.author.bot and before.content != after.content:
        await send_log(before.guild, "Message modifié", f"**Auteur :** {before.author.mention}\n**Salon :** {before.channel.mention}\n**Avant :** {before.content[:700]}\n**Après :** {after.content[:700]}")

@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        if added or removed:
            await send_log(after.guild, "Rôles modifiés", f"{after.mention}\nAjoutés : {', '.join(added) or 'Aucun'}\nRetirés : {', '.join(removed) or 'Aucun'}")

@bot.event
async def on_guild_channel_delete(channel):
    await send_log(channel.guild, "Salon supprimé", f"{channel.name} (`{channel.id}`)")

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    # Commandes personnalisées : le nom doit être écrit avec le préfixe !
    if message.content.startswith("!"):
        name = message.content[1:].split(" ", 1)[0].lower()
        response = db.get_custom_command(message.guild.id, name)
        if response:
            if is_staff(message.author):
                await message.channel.send(response)
            else:
                await message.channel.send("Cette commande est réservée au staff.", delete_after=5)
    await bot.process_commands(message)

# -------------------- CONFIGURATION --------------------

@bot.tree.command(name="configurer_salon", description="Définir un salon utilisé par le bot.")
@app_commands.describe(type_salon="Fonction du salon", salon="Salon Discord")
@app_commands.choices(type_salon=[
    app_commands.Choice(name="Logs", value="logs_channel_id"),
    app_commands.Choice(name="Panneau tickets", value="ticket_panel_channel_id"),
    app_commands.Choice(name="Candidatures reçues", value="applications_channel_id"),
    app_commands.Choice(name="Bienvenue", value="welcome_channel_id"),
    app_commands.Choice(name="Annonces", value="announcements_channel_id"),
    app_commands.Choice(name="Panneau vérification", value="verify_panel_channel_id")
])
@app_commands.checks.has_permissions(administrator=True)
async def configure_channel(interaction: discord.Interaction, type_salon: app_commands.Choice[str], salon: discord.TextChannel):
    db.set_config(interaction.guild.id, type_salon.value, salon.id)
    await interaction.response.send_message(f"✅ {type_salon.name} configuré : {salon.mention}", ephemeral=True)

@bot.tree.command(name="configurer_categorie_tickets", description="Définir la catégorie où seront créés les tickets.")
@app_commands.checks.has_permissions(administrator=True)
async def configure_ticket_category(interaction: discord.Interaction, categorie: discord.CategoryChannel):
    db.set_config(interaction.guild.id, "tickets_category_id", categorie.id)
    await interaction.response.send_message(f"✅ Catégorie des tickets configurée : **{categorie.name}**", ephemeral=True)

@bot.tree.command(name="configurer_role", description="Définir un rôle utilisé par le bot.")
@app_commands.describe(type_role="Fonction du rôle", role="Rôle Discord")
@app_commands.choices(type_role=[
    app_commands.Choice(name="Staff", value="staff_role_id"),
    app_commands.Choice(name="Membre vérifié", value="verified_role_id"),
    app_commands.Choice(name="Non vérifié", value="unverified_role_id")
])
@app_commands.checks.has_permissions(administrator=True)
async def configure_role(interaction: discord.Interaction, type_role: app_commands.Choice[str], role: discord.Role):
    db.set_config(interaction.guild.id, type_role.value, role.id)
    await interaction.response.send_message(f"✅ {type_role.name} configuré : {role.mention}", ephemeral=True)

@bot.tree.command(name="publier_tickets", description="Publier le panneau des tickets.")
@app_commands.checks.has_permissions(administrator=True)
async def publish_tickets(interaction: discord.Interaction):
    embed = make_embed("🎫 Contacter ECLIPSE RP", "Choisis le type de ticket correspondant à ta demande.\n\n🌆 **SESSION ECLIPSE RP** : demandes liées aux sessions RP.\n\n🛠️ **ASSISTANCE** : aide et problèmes sur le serveur.", config.ORANGE)
    await interaction.channel.send(embed=embed, view=TicketPanel())
    await interaction.response.send_message("✅ Panneau des tickets publié.", ephemeral=True)

@bot.tree.command(name="publier_candidatures", description="Publier le panneau de recrutement.")
@app_commands.checks.has_permissions(administrator=True)
async def publish_applications(interaction: discord.Interaction):
    await interaction.channel.send(embed=make_embed("📝 Recrutement ECLIPSE RP", "Tu souhaites rejoindre l'équipe ? Clique ci-dessous pour déposer ta candidature.", config.VIOLET), view=ApplicationView())
    await interaction.response.send_message("✅ Panneau de candidatures publié.", ephemeral=True)

@bot.tree.command(name="publier_verification", description="Publier le panneau de vérification.")
@app_commands.checks.has_permissions(administrator=True)
async def publish_verification(interaction: discord.Interaction):
    await interaction.channel.send(embed=make_embed("🔐 Vérification", "Clique sur le bouton ci-dessous pour accéder au serveur.", config.ORANGE), view=VerifyView())
    await interaction.response.send_message("✅ Panneau de vérification publié.", ephemeral=True)

# -------------------- MODÉRATION --------------------

@bot.tree.command(name="avertir", description="Avertir un membre.")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison précisée"):
    await interaction.response.send_message(f"⚠️ {membre.mention} a reçu un avertissement.\nRaison : {raison}")
    await send_log(interaction.guild, "Avertissement", f"{membre.mention} • Par {interaction.user.mention}\nRaison : {raison}")

@bot.tree.command(name="timeout", description="Exclure temporairement un membre des conversations.")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_member(interaction: discord.Interaction, membre: discord.Member, minutes: app_commands.Range[int, 1, 40320], raison: str = "Aucune raison précisée"):
    await membre.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=raison)
    await interaction.response.send_message(f"⏳ {membre.mention} a été mis en timeout pendant {minutes} minute(s).")
    await send_log(interaction.guild, "Timeout", f"{membre.mention} • {minutes} min • {raison}")

@bot.tree.command(name="expulser", description="Expulser un membre du serveur.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick_member(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison précisée"):
    await membre.kick(reason=raison)
    await interaction.response.send_message(f"👢 {membre} a été expulsé.")
    await send_log(interaction.guild, "Membre expulsé", f"{membre} • Par {interaction.user.mention}\n{raison}")

@bot.tree.command(name="bannir", description="Bannir un membre du serveur.")
@app_commands.checks.has_permissions(ban_members=True)
async def ban_member(interaction: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison précisée"):
    await membre.ban(reason=raison)
    await interaction.response.send_message(f"🔨 {membre} a été banni.")
    await send_log(interaction.guild, "Membre banni", f"{membre} • Par {interaction.user.mention}\n{raison}")

@bot.tree.command(name="purger", description="Supprimer plusieurs messages.")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, nombre: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    await interaction.followup.send(f"🧹 {len(deleted)} message(s) supprimé(s).", ephemeral=True)
    await send_log(interaction.guild, "Messages purgés", f"{len(deleted)} message(s) • Par {interaction.user.mention}")

# -------------------- STATS / INVITATIONS / ANNONCES --------------------

@bot.tree.command(name="statistiques", description="Afficher les statistiques du serveur.")
async def stats(interaction: discord.Interaction):
    data = db.get_stats(interaction.guild.id)
    embed = make_embed("📊 Statistiques ECLIPSE RP", f"**Membres actuels :** {interaction.guild.member_count}\n**Arrivées enregistrées :** {data['joins']}\n**Départs enregistrés :** {data['leaves']}\n**Tickets ouverts :** {data['tickets']}\n**Candidatures en attente :** {data['applications']}", config.VIOLET)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="top_invitations", description="Afficher le classement des 10 meilleurs inviteurs.")
async def top_invites(interaction: discord.Interaction):
    rows = db.invite_top(interaction.guild.id)
    if not rows:
        return await interaction.response.send_message("Aucune invitation attribuée pour le moment.", ephemeral=True)
    lines = []
    for index, row in enumerate(rows, 1):
        member = interaction.guild.get_member(row["inviter_id"])
        lines.append(f"**{index}.** {member.mention if member else 'Membre parti'} — **{row['total']}** invitation(s)")
    await interaction.response.send_message(embed=make_embed("🏆 TOP 10 INVITATIONS", "\n".join(lines), config.ORANGE))

@bot.tree.command(name="annonce", description="Publier une annonce dans un salon.")
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(interaction: discord.Interaction, salon: discord.TextChannel, titre: str, message: str):
    await salon.send(embed=make_embed(titre, message, config.ORANGE))
    await interaction.response.send_message(f"✅ Annonce publiée dans {salon.mention}.", ephemeral=True)
    await send_log(interaction.guild, "Annonce publiée", f"Par {interaction.user.mention} dans {salon.mention}")

@bot.tree.command(name="commande_staff", description="Créer une commande personnalisée réservée au staff.")
@app_commands.checks.has_permissions(administrator=True)
async def create_staff_command(interaction: discord.Interaction, nom: str, reponse: str):
    db.add_custom_command(interaction.guild.id, nom, reponse)
    await interaction.response.send_message(f"✅ Commande créée : `!{nom.lower()}` (staff uniquement).", ephemeral=True)

@bot.tree.command(name="aide", description="Afficher les commandes du bot.")
async def help_command(interaction: discord.Interaction):
    description = (
        "**Configuration (administrateurs)**\n"
        "`/configurer_salon` • `/configurer_categorie_tickets` • `/configurer_role` • `/publier_tickets` • `/publier_candidatures` • `/publier_verification`\n\n"
        "**Modération**\n"
        "`/avertir` • `/timeout` • `/expulser` • `/bannir` • `/purger`\n\n"
        "**Gestion**\n"
        "`/statistiques` • `/top_invitations` • `/annonce` • `/commande_staff`\n\n"
        "Les commandes personnalisées du staff se lancent avec `!nom`."
    )
    await interaction.response.send_message(embed=make_embed("📚 Aide ECLIPSE RP", description))

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Tu n'as pas les permissions Discord nécessaires pour cette commande."
    elif isinstance(error, app_commands.CheckFailure):
        message = "Tu n'as pas l'autorisation d'utiliser cette commande."
    else:
        log.exception("Erreur commande slash", exc_info=error)
        message = "Une erreur est survenue. Vérifie les permissions du bot ou contacte le développeur."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

if not config.DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant. Ajoute-le dans les variables d'environnement de Render.")

bot.run(config.DISCORD_TOKEN)
