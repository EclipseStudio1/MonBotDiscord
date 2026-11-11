import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Couleurs ECLIPSE RP
ORANGE = 0xF28C28
VIOLET = 0x7B3FE4
DARK = 0x23202B

# La configuration des salons/rôles se fait ensuite avec les commandes
# /configurer_salon et /configurer_role dans Discord.
